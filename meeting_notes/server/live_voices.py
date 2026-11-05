"""Names for the live preview: who is talking, from their voice, while a meeting records.

Each finished live utterance is embedded with the same pyannote model that makes the voice profiles, and
compared with the people the server already knows. Only the voice decides: a name said aloud, or written in
the meeting's title, never names anyone here. The result is for the live preview only. Nothing it finds is
stored: it does not touch the people, their voiceprints or the transcript, and the notes are not told. The final
pass re-diarizes the whole recording against the same profiles, which sees far more speech and is more reliable.

People are tried in the order the owner asked for: those already heard in this meeting first, then everyone else
by how many meetings they are in and how recently they were heard. A confident match ends the search at once.
Anything uncertain shows nothing, so the preview falls back to "Them".
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import namedtuple
from typing import Callable, Dict, List, Optional

import numpy as np

from .. import wire
from . import people as people_mod
from . import settings as settings_mod

logger = logging.getLogger("meeting_notes.server.live_voices")

MIN_SECONDS = 3.0  # shorter utterances give a vector too noisy to name anyone from
SHOW = 0.65        # at or above (and the best of everyone): the name is shown
SURE = 0.75        # at or above: stop looking, this is them. Also what someone other than you needs on the mic
MARGIN = 0.10      # below SURE, the best must beat the next person by this: a mix of two voices can fit both
REFRESH_CHECK_SECONDS = 30.0  # how often the people are checked for a change while a meeting records
RETRY_LOAD_SECONDS = 300.0    # a model that did not load is tried again after this (a token fixed, a network back)
MAX_SESSIONS = 64             # sessions remembered; a late utterance can arrive after its session was forgotten

# One known, named person. ``meetings``: meetings their voice profile is made from; ``last_heard``: when
# the newest of those voiceprints was made (0 = never).
Person = namedtuple("Person", "id name meetings last_heard")


class Roster:
    """A snapshot of who the live preview can name. Built on a background thread, never changed once built."""

    def __init__(self, revision: int, people: List[Person], self_id: Optional[str], centroids: dict,
                 exemplars: dict):
        self.revision = revision
        self.people = people          # named people, not you
        self.self_id = self_id        # the "You" person (even unnamed), kept apart
        self.centroids = centroids    # {person_id: unit vector}
        self.exemplars = exemplars    # {person_id: [unit vector, ...]}
        self.names = {p.id: p.name for p in people}


def order_people(people: List[Person], heard_here: List[str]) -> List[str]:
    """Person ids in the order to check them.

    ``heard_here``: ids already heard in this meeting, most recently heard first. They come first. Everyone else
    follows by the sum of two ranks: most meetings first, and heard most recently first. Ties go by person id."""
    by_meetings = {p.id: i for i, p in enumerate(sorted(people, key=lambda p: (-p.meetings, p.id)))}
    by_recent = {p.id: i for i, p in enumerate(sorted(people, key=lambda p: (-p.last_heard, p.id)))}
    known = {p.id for p in people}
    first = [pid for pid in heard_here if pid in known]
    rest = sorted((p.id for p in people if p.id not in set(first)), key=lambda i: (by_meetings[i] + by_recent[i], i))
    return first + rest


def match(track: str, ordered: List[str], self_id: Optional[str], score: Callable[[str], float]) -> Optional[str]:
    """The person id to show for one utterance, or None (show nothing: "You", "Them", or not sure).

    ``ordered``: the people to try, in order (``order_people``); ``score(person_id)``: how well the voice fits them.
    The search stops at the first score of ``SURE`` or more. When nobody gets that far every person has been scored
    and the best is shown if it reaches ``SHOW`` and beats the next person by ``MARGIN``. The system track never names
    you, and a voice closest to yours names nobody. On the mic You is the default: a fit of ``SHOW`` to your own voice
    means You (None), and anyone else needs ``SURE``."""
    others = [pid for pid in ordered if pid != self_id]
    if track == "mic":
        if self_id is not None and score(self_id) >= SHOW:
            return None
        for pid in others:
            if score(pid) >= SURE:
                return pid
        return None
    best, best_score, second = None, 0.0, 0.0
    for pid in others:
        got = score(pid)
        if got >= SURE:
            return pid
        if got > best_score:
            best, best_score, second = pid, got, best_score
        elif got > second:
            second = got
    if best is None or best_score < SHOW:
        return None
    if self_id is not None:
        second = max(second, score(self_id))
    return best if best_score - second >= MARGIN else None


def _embedder_for(pipeline, torch) -> Callable[[np.ndarray], Optional[list]]:
    embedding = pipeline._embedding

    def embed(audio: np.ndarray) -> Optional[list]:
        with torch.no_grad():
            out = embedding(torch.from_numpy(audio)[None, None])
        return people_mod._unit(np.asarray(out, dtype=np.float64).reshape(-1))

    return embed


class LiveVoices:
    """Names live utterances from voice, for ``LivePreview``. ``identify`` never raises and never waits for a model.

    ``embedder_loader`` builds the embed function (tests give a fake); ``settings_loader`` returns the current
    settings (the model name, and the on/off switches, are read from it when first wanted).
    """

    def __init__(self, store, settings_loader: Optional[Callable[[], object]] = None,
                 embedder_loader: Optional[Callable[[], Callable]] = None):
        self.store = store
        self._settings = settings_loader or (lambda: settings_mod.load_settings(store.root))
        self._embedder_loader = embedder_loader or self._default_loader
        self._embed: Optional[Callable] = None
        self._embed_state = "idle"  # idle -> loading -> ready | failed
        self._embed_ready = threading.Event()  # set when loading ended, whichever way
        self._embed_failed = 0.0  # monotonic time of the last failed load
        # Taken without waiting: if another session is embedding right now this utterance simply goes unnamed.
        self._embed_lock = threading.Lock()
        self._roster: Optional[Roster] = None
        self._roster_busy = False
        self._roster_checked = 0.0
        self._roster_idle = threading.Event()  # set while no roster build is running
        self._roster_idle.set()
        self._lock = threading.Lock()  # guards the small state above and ``_heard``
        self._heard: Dict[str, List[str]] = {}  # session id -> person ids, most recently heard first

    # -- model ---------------------------------------------------------------------------------------------

    def _default_loader(self):
        return _load_embedder(self._settings())

    def _want_embedder(self) -> Optional[Callable]:
        with self._lock:
            again = self._embed_state == "failed" and time.monotonic() - self._embed_failed >= RETRY_LOAD_SECONDS
            if self._embed_state == "idle" or again:
                self._embed_state = "loading"
                self._embed_ready.clear()
                threading.Thread(target=self._load_embedder_thread, name="live-voices-model", daemon=True).start()
            return self._embed if self._embed_state == "ready" else None

    def _load_embedder_thread(self) -> None:
        embed, state = None, "failed"
        try:
            embed, state = self._embedder_loader(), "ready"
        except Exception as exc:  # noqa: BLE001 - no pyannote, or the model will not load: stay off
            logger.warning("live voice names are off: the speaker model did not load (%s)", exc)
        with self._lock:
            self._embed, self._embed_state = embed, state
            self._embed_failed = time.monotonic()
        self._embed_ready.set()

    # -- roster --------------------------------------------------------------------------------------------

    def _kick_roster(self, force: bool = False) -> None:
        """Start a background check of the people (a rebuild only when their revision moved, or ``force``)."""
        now = time.monotonic()
        with self._lock:
            if self._roster_busy or not (force or self._roster is None or now - self._roster_checked >= REFRESH_CHECK_SECONDS):
                return
            self._roster_busy, self._roster_checked = True, now
            self._roster_idle.clear()
        threading.Thread(target=self._refresh_roster, args=(force,), name="live-voices-roster", daemon=True).start()

    def _refresh_roster(self, force: bool) -> None:
        try:
            current = self._roster
            built = self._build_roster(None if force or current is None else current.revision)
            if built is not None:
                with self._lock:
                    self._roster = built
        except Exception:  # noqa: BLE001 - the last good roster stays
            logger.warning("live voice names: could not read the people", exc_info=True)
        finally:
            with self._lock:
                self._roster_busy = False
            self._roster_idle.set()

    def _build_roster(self, unless_revision: Optional[int]) -> Optional[Roster]:
        """Read the people and their voiceprints (many small files: only ever called off the utterance path).
        None when ``unless_revision`` is still the people's revision, so the roster in hand stands."""
        store = self.store
        with people_mod._lock:
            data = people_mod._load(store.root)
            revision = int(data.get("revision") or 0)
            if unless_revision is not None and revision == unless_revision:
                return None
            visible = people_mod._visible(store)
            models = people_mod._models(visible, data)
            last_heard: Dict[str, float] = {}
            for vp in visible.values():
                made = float(vp.get("created") or 0.0)
                for a in vp.get("speakers") or []:
                    pid = a.get("person_id")
                    if pid and made > last_heard.get(pid, 0.0):
                        last_heard[pid] = made
        self_person = next((p for p in data["people"] if p.get("is_self")), None)
        self_id = self_person["id"] if self_person else None
        named = [Person(p["id"], str(p["name"]), models[p["id"]]["meetings"], last_heard.get(p["id"], 0.0))
                 for p in data["people"] if p.get("name") and not p.get("is_self") and p["id"] in models]
        keep = {p.id for p in named} | ({self_id} if self_id else set())
        return Roster(
            revision, named, self_id,
            {pid: m["centroid"] for pid, m in models.items() if pid in keep},
            {pid: m["exemplars"] for pid, m in models.items() if pid in keep and m["exemplars"]})

    # -- one utterance -------------------------------------------------------------------------------------

    def identify(self, session_id: str, track: str, pcm: bytes) -> str:
        """The name to show for this utterance, or "" (use You / Them). Never raises, never waits."""
        try:
            return self._identify(session_id, track, pcm)
        except Exception:  # noqa: BLE001 - names are a nicety; the preview must go out without them
            logger.debug("live voice name failed for %s/%s", session_id, track, exc_info=True)
            return ""

    def _identify(self, session_id: str, track: str, pcm: bytes) -> str:
        if len(pcm) // wire.BYTES_PER_FRAME < MIN_SECONDS * wire.STREAM_SAMPLE_RATE:
            return ""
        current = self._settings()
        if not (people_mod.enabled(current) and getattr(current, "live_voice_names", False)):
            return ""
        embed = self._want_embedder()
        with self._lock:
            new_session = session_id not in self._heard
            if new_session:
                self._heard[session_id] = []
                while len(self._heard) > MAX_SESSIONS:
                    del self._heard[next(iter(self._heard))]  # the oldest
            roster = self._roster
        self._kick_roster(force=new_session)
        if embed is None or roster is None:
            return ""
        if not self._embed_lock.acquire(blocking=False):
            return ""  # another session is embedding: skip this utterance rather than queue behind it
        try:
            audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
            vector = embed(audio)
        finally:
            self._embed_lock.release()
        if not vector:
            return ""
        with self._lock:
            heard = list(self._heard.get(session_id, []))
        ordered = order_people(roster.people, heard)

        def score(pid: str) -> float:
            return people_mod._score(vector, pid, roster.centroids, roster.exemplars)

        found = match(track, ordered, roster.self_id, score)
        if found is None:
            return ""
        with self._lock:
            seen = self._heard.get(session_id)
            if seen is not None:
                if found in seen:
                    seen.remove(found)
                seen.insert(0, found)
        return roster.names.get(found, "")

    def forget_session(self, session_id: str) -> None:
        with self._lock:
            self._heard.pop(session_id, None)


def _load_embedder(current) -> Callable:
    """pyannote's speaker embedder, from its own pipeline (never the job diarizer, which jobs use at the same time)."""
    import torch
    from pyannote.audio import Pipeline

    token = os.environ.get("HUGGINGFACE_TOKEN") or os.environ.get("HF_TOKEN")
    pipeline = Pipeline.from_pretrained(current.diarization_model, token=token or None)
    return _embedder_for(pipeline, torch)
