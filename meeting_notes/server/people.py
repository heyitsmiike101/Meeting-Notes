"""Voice profiles ("People"): recognise the same person across meetings by voice.

Design: ``docs/voice-profiles-design.md``. Everything lives under ``<data_root>/people/``:

* ``people.json``            -- the people (name, email, company, ...). No voice data.
* ``voiceprints/<sid>.json`` -- per meeting, one *appearance* per diarized speaker: the transcript label, a voice
  embedding, how much they spoke and which person it is linked to. Centroids are derived from these, never stored.
* ``clips/<sid>/<slug>-<n>.wav`` -- a few short voice samples per appearance, cut when the meeting is recorded so
  they outlive audio retention.

The feature is optional (``voice_profiles_enabled`` and ``diarization_enabled``, see ``enabled``). Stored
transcripts are never rewritten: names are applied at read time by ``overlay``. All reads and writes go through one
module-level ``RLock`` and every file is written atomically. Matching never runs pyannote under the lock.
"""

from __future__ import annotations

import json
import logging
import math
import re
import secrets
import shutil
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..transcribe.diarize import Diarization, speaker_labels
from . import names as names_mod
from . import settings as settings_mod
from .store import _atomic_write_json, _read_json, is_safe_id

logger = logging.getLogger("meeting_notes.server.people")

# Cosine similarity thresholds (tuned later on real meetings).
AUTO = 0.70      # at or above: the same person, linked automatically
SUGGEST = 0.55   # at or above (below AUTO): a new unnamed person, plus a "sounds like X?" suggestion
MIN_SPEECH = 10.0  # seconds a speaker needs to get a person at all (shorter ones are recorded but not linked)
ECHO = 0.70      # a mic cluster this close to a system-track cluster of the same meeting is speaker echo
CONFIRMED_WEIGHT = 2.0   # a link the user confirmed counts this many times as much as one made by voice
MAX_WEIGHT_SEC = 600.0   # one meeting counts for at most this many seconds of speech in a voice profile
MAX_EXEMPLARS = 40       # confirmed voices kept per person for matching (the ones with the most speech)
MAX_CLIPS = 3
CLIP_MAX_SECONDS = 12.0
CLIP_MIN_SECONDS = 1.0
MIC_MIN_SPEAKERS, MIC_MAX_SPEAKERS = 1, 4
MAX_NAME_CHARS = 120
MAX_EMAIL_CHARS = 254
MAX_VECTOR = 1024
SECONDS_PER_DAY = 86400.0

_lock = threading.RLock()
_voiceprints: dict = {}  # str(root) -> {session_id: voiceprint}, loaded lazily, kept in step with the files
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CLIP_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*-\d+\.wav$")


class PersonNotFound(LookupError):
    """No such person (or no such appearance)."""


def enabled(settings) -> bool:
    """Voice profiles are effective only while diarization is on too."""
    return bool(getattr(settings, "voice_profiles_enabled", False) and getattr(settings, "diarization_enabled", False))


def is_enabled(root) -> bool:
    try:
        return enabled(settings_mod.load_settings(Path(root)))
    except Exception:  # noqa: BLE001 - an unreadable settings file means "off"
        return False


@dataclass
class TrackSpeakers:
    """One diarized track as it was labelled in the transcript."""

    diarization: Diarization
    labels: dict = field(default_factory=dict)  # raw speaker id -> transcript label ("Them 2", "In room 1", ...)
    self_raw: Optional[str] = None              # mic track: the raw id that is the user's own voice
    self_label: str = "You"                     # ... and the label that voice has in the transcript


@dataclass
class MicMapping:
    labels: dict = field(default_factory=dict)  # raw -> "In room N"
    self_raw: Optional[str] = None
    folded: list = field(default_factory=list)  # raw ids folded into the user's own voice (short, or echo)


# -- paths and storage --------------------------------------------------------------------------------------


def _root(root) -> Path:
    return Path(root)


def people_dir(root) -> Path:
    return _root(root) / "people"


def _people_path(root) -> Path:
    return people_dir(root) / "people.json"


def _vp_dir(root) -> Path:
    return people_dir(root) / "voiceprints"


def _clips_dir(root, session_id: str) -> Path:
    return people_dir(root) / "clips" / session_id


def _load(root) -> dict:
    try:
        raw = _read_json(_people_path(root))
    except (OSError, ValueError):
        raw = None
    if not isinstance(raw, dict):
        raw = {}
    people = [p for p in raw.get("people") or [] if isinstance(p, dict) and p.get("id")]
    nxt = raw.get("next_number")
    data = {
        "revision": int(raw.get("revision") or 0),
        "next_number": int(nxt) if isinstance(nxt, int) and nxt >= 1 else 1,
        "people": people,
    }
    if isinstance(raw.get("last_cleanup"), dict):
        data["last_cleanup"] = raw["last_cleanup"]  # what the last cleanup did (see ``cleanup``)
    return data


def _save(root, data: dict) -> None:
    data["revision"] = int(data.get("revision") or 0) + 1
    _atomic_write_json(_people_path(root), data)


def _vps(root) -> dict:
    key = str(_root(root))
    cached = _voiceprints.get(key)
    if cached is None:
        cached = {}
        directory = _vp_dir(root)
        if directory.is_dir():
            for path in directory.glob("*.json"):
                try:
                    vp = _read_json(path)
                except (OSError, ValueError):
                    continue
                if isinstance(vp, dict) and isinstance(vp.get("speakers"), list) and vp.get("session_id"):
                    cached[str(vp["session_id"])] = vp
        _voiceprints[key] = cached
    return cached


def _put_vp(root, vp: dict) -> None:
    _atomic_write_json(_vp_dir(root) / f"{vp['session_id']}.json", vp)
    _vps(root)[vp["session_id"]] = vp


def _drop_vp(root, session_id: str) -> None:
    try:
        (_vp_dir(root) / f"{session_id}.json").unlink()
    except OSError:
        pass
    _vps(root).pop(session_id, None)
    shutil.rmtree(_clips_dir(root, session_id), ignore_errors=True)


def _by_id(data: dict, person_id) -> Optional[dict]:
    return next((p for p in data["people"] if p.get("id") == person_id), None)


def _new_person(data: dict, *, is_self: bool = False) -> dict:
    now = time.time()
    if is_self:
        number = 0  # shown as "You"; does not use up a "Person N"
    else:
        number = data["next_number"]
        data["next_number"] = number + 1
    person = {"id": "p_" + secrets.token_hex(5), "number": number, "name": "", "email": "", "company": "",
              "is_self": is_self, "created": now, "updated": now}
    data["people"].append(person)
    return person


def _self_person(data: dict, create: bool = True) -> Optional[dict]:
    found = next((p for p in data["people"] if p.get("is_self")), None)
    return found if found is not None or not create else _new_person(data, is_self=True)


def display_name(person: dict) -> str:
    if person.get("name"):
        return str(person["name"])
    return "You" if person.get("is_self") else f"Person {person.get('number')}"


def _gc(root, data: dict) -> bool:
    """Drop unnamed people no stored appearance points to. True when something was removed."""
    used = {a.get("person_id") for vp in _vps(root).values() for a in vp.get("speakers") or []}
    keep = [p for p in data["people"] if p["id"] in used or p.get("name") or p.get("email") or p.get("company")]
    if len(keep) == len(data["people"]):
        return False
    data["people"] = keep
    ids = {p["id"] for p in keep}
    for vp in _vps(root).values():
        changed = False
        for a in vp.get("speakers") or []:
            if a.get("suggested_person_id") and a["suggested_person_id"] not in ids:
                a["suggested_person_id"] = None
                changed = True
        if changed:
            _put_vp(root, vp)
    return True


# -- vectors --------------------------------------------------------------------------------------------------


def _unit(vector) -> Optional[list]:
    """The vector as an L2-normalised list of floats, or None when it is unusable."""
    try:
        values = [float(x) for x in vector]
    except (TypeError, ValueError):
        return None
    if not 2 <= len(values) <= MAX_VECTOR or not all(math.isfinite(x) for x in values):
        return None
    norm = math.sqrt(sum(x * x for x in values))
    return [x / norm for x in values] if norm > 1e-9 else None


def cosine(a, b) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def _centroid(items) -> Optional[list]:
    """Speech-weighted mean of ``(vector, weight)`` pairs, renormalised."""
    total: Optional[list] = None
    for vector, weight in items:
        if total is None:
            total = [0.0] * len(vector)
        if len(vector) != len(total):
            continue
        w = max(float(weight), 0.01)
        for i, x in enumerate(vector):
            total[i] += x * w
    return _unit(total) if total else None


def _visible(store) -> dict:
    """Voiceprints of meetings that exist and are not in the trash."""
    return {sid: vp for sid, vp in _vps(store.root).items() if store.session_exists(sid)}


def _visible_root(root) -> dict:
    """``_visible`` from the data root alone (a trashed meeting's folder is moved out of ``sessions/``)."""
    sessions = _root(root) / "sessions"
    return {sid: vp for sid, vp in _vps(root).items() if (sessions / sid).exists()}


def _models(visible: dict, data: dict, exclude_session: Optional[str] = None) -> dict:
    """What is known of each person's voice: ``{person_id: {centroid, exemplars, meetings, confirmed}}``.

    The centroid is the weighted mean of every ``auto``/``confirmed`` appearance: a confirmed one counts
    ``CONFIRMED_WEIGHT`` times, and one meeting counts for at most ``MAX_WEIGHT_SEC`` seconds of speech. When the
    person has confirmed appearances, ``auto`` ones that sound unlike them (cosine to the confirmed-only centroid
    below ``SUGGEST``) are left out, so one wrong automatic link cannot pull the profile away. ``exemplars`` are the
    confirmed appearances' own vectors (most speech first), so a voice heard on different setups still matches.
    ``meetings`` / ``confirmed`` count the meetings the profile is made from."""
    pooled: dict = {}
    for sid, vp in visible.items():
        if sid == exclude_session:
            continue
        for a in vp.get("speakers") or []:
            if a.get("person_id") and a.get("link") in ("auto", "confirmed") and a.get("embedding"):
                pooled.setdefault(a["person_id"], []).append(
                    (sid, a["embedding"], min(float(a.get("speech_sec") or 0.0), MAX_WEIGHT_SEC), a["link"] == "confirmed"))
    known = {p["id"] for p in data["people"]}
    out: dict = {}
    for pid, items in pooled.items():
        if pid not in known:
            continue
        confirmed = [i for i in items if i[3]]
        if confirmed:
            anchor = _centroid((e, w) for _, e, w, _ in confirmed)
            items = confirmed + [i for i in items if not i[3] and anchor is not None and cosine(i[1], anchor) >= SUGGEST]
        centroid = _centroid((e, w * (CONFIRMED_WEIGHT if c else 1.0)) for _, e, w, c in items)
        if centroid is None:
            continue
        best = sorted(confirmed, key=lambda i: -i[2])[:MAX_EXEMPLARS]
        out[pid] = {"centroid": centroid, "exemplars": [_unit(i[1]) or i[1] for i in best],
                    "meetings": len({i[0] for i in items}), "confirmed": len({i[0] for i in confirmed})}
    return out


def _centroids(store, data: dict, exclude_session: Optional[str] = None) -> dict:
    return {pid: m["centroid"] for pid, m in _models(_visible(store), data, exclude_session).items()}


def _exemplars(store, data: dict, exclude_session: Optional[str] = None) -> dict:
    return {pid: m["exemplars"] for pid, m in _models(_visible(store), data, exclude_session).items() if m["exemplars"]}


def _score(vector, pid: str, centroids: dict, exemplars: Optional[dict]) -> float:
    """How well a voice fits a person: the better of the centroid and the closest single confirmed voice."""
    best = cosine(vector, centroids.get(pid))
    for ex in (exemplars or {}).get(pid) or ():
        best = max(best, cosine(vector, ex))
    return best


# -- mic mapping ----------------------------------------------------------------------------------------------


def _speech(turns) -> dict:
    seconds: dict = {}
    for t in turns:
        seconds[t.speaker] = seconds.get(t.speaker, 0.0) + max(0.0, t.end - t.start)
    return seconds


def mic_mapping(turns, embeddings: dict, system_embeddings: dict, self_centroid: Optional[list]) -> MicMapping:
    """Decide which mic-track clusters are the user, which are speaker echo or too short to matter, and which are
    other people in the room. Pure. Without embeddings nothing can be told apart: the whole track stays ``You``."""
    speech = _speech(turns)
    clusters = [raw for raw in speech if raw in embeddings]
    if not clusters:
        return MicMapping()
    self_raw = None
    if self_centroid:
        best = max(clusters, key=lambda raw: cosine(embeddings[raw], self_centroid))
        if cosine(embeddings[best], self_centroid) >= SUGGEST:
            self_raw = best
    if self_raw is None:
        self_raw = max(clusters, key=lambda raw: speech[raw])
    folded, rooms = [], []
    for raw in clusters:
        if raw == self_raw:
            continue
        echo = any(cosine(embeddings[raw], other) >= ECHO for other in (system_embeddings or {}).values())
        if speech[raw] < MIN_SPEECH or echo:
            folded.append(raw)
        else:
            rooms.append(raw)
    numbered = speaker_labels([t for t in turns if t.speaker in rooms], "In room")
    return MicMapping(labels=numbered, self_raw=self_raw, folded=folded)


def map_mic(store, session_id: str, diarization: Diarization, system_embeddings: Optional[dict]) -> MicMapping:
    """``mic_mapping`` with the self centroid read from the stored voiceprints (this meeting excluded)."""
    with _lock:
        data = _load(store.root)
        me = _self_person(data, create=False)
        centroid = _centroids(store, data, exclude_session=session_id).get(me["id"]) if me else None
    return mic_mapping(diarization.turns, diarization.embeddings, system_embeddings or {}, centroid)


# -- recording a meeting --------------------------------------------------------------------------------------


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "speaker"


def _cut_clips(wav_path, turns, directory: Path, slug: str) -> list:
    """Up to ``MAX_CLIPS`` WAV clips (longest turns first, each at most ``CLIP_MAX_SECONDS``). Never raises."""
    names: list = []
    try:
        if wav_path is None or not Path(wav_path).exists():
            return names
        longest = sorted((t for t in turns if t.end - t.start >= CLIP_MIN_SECONDS),
                         key=lambda t: t.end - t.start, reverse=True)[:MAX_CLIPS]
        with wave.open(str(wav_path), "rb") as src:
            rate, width, channels, total = src.getframerate(), src.getsampwidth(), src.getnchannels(), src.getnframes()
            if width != 2 or rate <= 0:
                return names
            for n, turn in enumerate(longest, start=1):
                first = max(0, int(turn.start * rate))
                count = min(int(min(turn.end - turn.start, CLIP_MAX_SECONDS) * rate), total - first)
                if count <= 0:
                    continue
                src.setpos(first)
                frames = src.readframes(count)
                directory.mkdir(parents=True, exist_ok=True)
                name = f"{slug}-{n}.wav"
                with wave.open(str(directory / name), "wb") as out:
                    out.setnchannels(channels)
                    out.setsampwidth(width)
                    out.setframerate(rate)
                    out.writeframes(frames)
                names.append(name)
    except Exception:  # noqa: BLE001 - samples are a nicety; they must never fail a job
        logger.warning("could not cut voice clips from %s", wav_path, exc_info=True)
    return names


def _link_candidates(data: dict, candidates: list, centroids: dict, assigned: dict, reuse: Optional[dict] = None,
                     exemplars: Optional[dict] = None) -> list:
    """Link the speakers nobody has claimed yet to people, and return the appearances (``clips`` still empty).

    ``assigned`` maps candidate index -> ``(person_id, link, score, suggested_person_id)`` for the ones already
    settled (the user's own voice, links carried over or confirmed by hand). Everyone else with enough speech gets
    the best matching person by cosine score, best first, one person per speaker and the other way round: at or
    above ``AUTO`` it is linked, between ``SUGGEST`` and ``AUTO`` it becomes a new unnamed person plus a "sounds
    like X?" suggestion, otherwise a new unnamed person. ``reuse`` (candidate index -> person id) names an
    existing unnamed person to take instead of creating one. Shared by ``record_meeting`` and ``reprofile``."""
    used = {a[0] for a in assigned.values()}
    eligible = [i for i, c in enumerate(candidates) if i not in assigned and c["speech_sec"] >= MIN_SPEECH]
    scored = sorted(((_score(candidates[i]["embedding"], pid, centroids, exemplars), i, pid)
                     for i in eligible for pid in centroids if pid not in (candidates[i].get("rejected") or ())), reverse=True)
    suggestions: dict = {}
    for score, i, pid in scored:
        if score < SUGGEST:
            break
        if i in assigned or i in suggestions or pid in used:
            continue
        used.add(pid)
        if score >= AUTO:
            assigned[i] = (pid, "auto", score, None)
        else:
            suggestions[i] = (pid, score)
    speakers_out: list = []
    for i, c in enumerate(candidates):
        if i in assigned:
            pid, link, score, suggested = assigned[i]
        elif c["speech_sec"] >= MIN_SPEECH:
            pid, link, score, suggested = (reuse or {}).get(i) or _new_person(data)["id"], "auto", None, None
            if i in suggestions:
                suggested, score = suggestions[i]
        else:
            pid, link, score, suggested = None, "auto", None, None
        row = {
            "track": c["track"], "raw": c["raw"], "label": c["label"],
            "embedding": [round(x, 6) for x in c["embedding"]], "speech_sec": c["speech_sec"],
            "person_id": pid, "link": "suggested" if suggested else link,
            "score": None if score is None else round(score, 4), "suggested_person_id": suggested, "clips": []}
        if c.get("rejected"):
            row["rejected_person_ids"] = list(c["rejected"])
        if c.get("rejected_names"):
            row["rejected_intro_names"] = list(c["rejected_names"])
        owner = _by_id(data, pid) if pid else None
        if c.get("intro") and owner is not None and not (owner.get("name") or owner.get("is_self")):
            row["intro_suggestion"] = dict(c["intro"])
        speakers_out.append(row)
    return speakers_out


def record_meeting(store, session_id: str, job_id: str, speakers: Optional[dict], wav_paths: Optional[dict] = None) -> None:
    """Link each speaker of this meeting to a person and store the voiceprint (replacing any earlier one).

    ``speakers``: ``{track: TrackSpeakers}``. With none (diarization skipped or failed) the meeting's old
    voiceprint is deleted so no stale labels linger. Does nothing while the feature is off or the meeting is
    trashed."""
    if not is_enabled(store.root):
        return
    wav_paths = wav_paths or {}
    if store.is_trashed(session_id) or not store.session_exists(session_id):
        return
    candidates: list = []
    seen_labels: set = set()
    for track in sorted(speakers or {}):
        ts = speakers[track]
        speech = _speech(ts.diarization.turns)
        for raw, vector in ts.diarization.embeddings.items():
            label = ts.self_label if raw == ts.self_raw else ts.labels.get(raw)
            unit = _unit(vector)
            if not label or unit is None or label in seen_labels:
                continue
            seen_labels.add(label)
            candidates.append({"track": track, "raw": raw, "label": label, "embedding": unit,
                               "speech_sec": round(speech.get(raw, 0.0), 2), "is_self": raw == ts.self_raw,
                               "turns": [t for t in ts.diarization.turns if t.speaker == raw]})
    root = store.root
    with _lock:
        if not candidates:
            if session_id in _vps(root):
                _drop_vp(root, session_id)
                data = _load(root)
                if _gc(root, data):
                    _save(root, data)
            return
        data = _load(root)
        old = _vps(root).get(session_id) or {}
        models = _models(_visible(store), data, session_id)
        centroids = {pid: m["centroid"] for pid, m in models.items()}
        exemplars = {pid: m["exemplars"] for pid, m in models.items() if m["exemplars"]}
        assigned: dict = {}  # candidate index -> (person_id, link, score, suggested_person_id)
        used: set = set()    # people already taken in this meeting (by the self voice and the carried-over links)

        # The user's own voice on the mic is always the self person.
        for i, c in enumerate(candidates):
            if c["is_self"]:
                me = _self_person(data)
                score = cosine(c["embedding"], centroids[me["id"]]) if me["id"] in centroids else None
                assigned[i] = (me["id"], "confirmed", score, None)
                used.add(me["id"])
        # What was confirmed by hand on the previous transcript of this meeting carries over.
        olds = [a for a in old.get("speakers") or []
                if a.get("link") == "confirmed" and a.get("embedding") and _by_id(data, a.get("person_id"))]
        pairs = sorted(((cosine(c["embedding"], a["embedding"]), i, j)
                        for i, c in enumerate(candidates) if i not in assigned
                        for j, a in enumerate(olds)), reverse=True)
        taken_old: set = set()
        for score, i, j in pairs:
            if score < AUTO:
                break
            if i in assigned or j in taken_old:
                continue
            pid = olds[j]["person_id"]
            if pid in used:
                continue
            assigned[i] = (pid, "confirmed", score, None)
            taken_old.add(j)
            used.add(pid)
        for c in candidates:  # "not them" answers (and undone introduction names) carry over to the closest earlier voice
            near = [a for a in old.get("speakers") or []
                    if (a.get("rejected_person_ids") or a.get("rejected_intro_names")) and a.get("embedding")
                    and cosine(c["embedding"], a["embedding"]) >= AUTO]
            if near:
                c["rejected"] = sorted({r for a in near for r in a.get("rejected_person_ids") or []})
                c["rejected_names"] = sorted({r for a in near for r in a.get("rejected_intro_names") or []})
        speakers_out = _link_candidates(data, candidates, centroids, assigned, exemplars=exemplars)
        # Clips come from the audio the server has right now; the voiceprint replaces the old one and its clips.
        shutil.rmtree(_clips_dir(root, session_id), ignore_errors=True)
        for out, c in zip(speakers_out, candidates):
            out["clips"] = _cut_clips(wav_paths.get(c["track"]), c["turns"], _clips_dir(root, session_id), _slug(c["label"]))
        _put_vp(root, {"session_id": session_id, "job_id": job_id, "created": time.time(), "speakers": speakers_out})
        _wake_archived(data, speakers_out)  # a new meeting with an archived person brings them back
        _gc(root, data)
        _save(root, data)


class NoVoiceprint(LookupError):
    """The meeting has no stored voiceprint to match again."""


def reprofile(store, session_id: str) -> dict:
    """Match one meeting's speakers against the people again, from the embeddings already stored for it.

    No audio and no diarization: the current people (their centroids built from every *other* meeting) are the
    only thing that changed. Links marked ``confirmed`` (and the user's own voice) stay; ``auto`` and
    ``suggested`` ones are worked out again, so a person named or merged since the meeting was recorded can now be
    recognised in it. A speaker with enough speech and no match gets a person as in ``record_meeting`` (an unnamed
    one only this meeting knows is kept rather than replaced by a new "Person N"). Stored clips are kept. Quick
    and synchronous. Raises ``PersonNotFound`` for a missing/trashed meeting and ``NoVoiceprint`` when there is
    nothing stored for it. Returns ``{"session_id", "speakers", "changed"}``."""
    root = store.root
    if store.is_trashed(session_id) or not store.session_exists(session_id):
        raise PersonNotFound(session_id)
    with _lock:
        vp = _vps(root).get(session_id)
        rows = [r for r in (vp or {}).get("speakers") or [] if isinstance(r, dict)]
        if not rows:
            raise NoVoiceprint(session_id)
        data = _load(root)
        models = _models(_visible(store), data, session_id)
        centroids = {pid: m["centroid"] for pid, m in models.items()}
        exemplars = {pid: m["exemplars"] for pid, m in models.items() if m["exemplars"]}
        elsewhere = {a.get("person_id") for sid, other in _vps(root).items() if sid != session_id
                     for a in other.get("speakers") or []}
        candidates: list = []
        slots: list = []     # per stored row: its candidate index, or None when its vector is unusable (kept as is)
        assigned: dict = {}
        reuse: dict = {}
        for row in rows:
            unit = _unit(row.get("embedding"))
            if unit is None:
                slots.append(None)
                continue
            i = len(candidates)
            slots.append(i)
            candidates.append({"track": row.get("track"), "raw": row.get("raw"), "label": row.get("label"),
                               "embedding": unit, "speech_sec": float(row.get("speech_sec") or 0.0), "row": row,
                               "rejected": list(row.get("rejected_person_ids") or []),
                               "rejected_names": list(row.get("rejected_intro_names") or []),
                               "intro": row.get("intro_suggestion")})
            person = _by_id(data, row.get("person_id"))
            if person is None:
                continue
            if row.get("link") == "confirmed" or person.get("is_self"):
                assigned[i] = (person["id"], "confirmed", row.get("score"), None)
            elif (row.get("link") in ("auto", "suggested") and person["id"] not in elsewhere
                  and (not person.get("name") or _named_here(person, session_id))):
                reuse[i] = person["id"]
        linked = _link_candidates(data, candidates, centroids, assigned, reuse, exemplars)
        final: list = []
        for row, slot in zip(rows, slots):
            if slot is None:
                final.append(row)
            else:
                linked[slot]["clips"] = list(row.get("clips") or [])
                final.append(linked[slot])
        key = lambda a: (a.get("person_id"), a.get("link"), a.get("suggested_person_id"))  # noqa: E731
        changed = sum(1 for before, after in zip(rows, final) if key(before) != key(after))
        _put_vp(root, {**vp, "speakers": final, "reprofiled": time.time()})
        archive_days = int(getattr(settings_mod.load_settings(root), "people_archive_days", 0) or 0)
        if archive_days and _meeting_time(store, session_id, vp) >= time.time() - archive_days * SECONDS_PER_DAY:
            _wake_archived(data, final)  # only a meeting inside the window counts as hearing them again
        _gc(root, data)
        _save(root, data)
        return {"session_id": session_id, "speakers": len(final), "changed": changed}


def _unprofiled(row: dict, data: dict) -> bool:
    """A stored speaker the nightly recheck should look at again: long enough to link, not confirmed by hand, and
    either a "sounds like X?" suggestion or linked to a person nobody has named (or to nobody)."""
    if not isinstance(row, dict) or row.get("link") == "confirmed" or _unit(row.get("embedding")) is None:
        return False
    if float(row.get("speech_sec") or 0.0) < MIN_SPEECH:
        return False
    if row.get("link") == "suggested":
        return True
    person = _by_id(data, row.get("person_id"))
    return person is None or not (person.get("name") or person.get("is_self"))


def recheck_candidates(store, since: float) -> list:
    """Meetings created at or after ``since`` whose voiceprint still has an unprofiled speaker (see
    ``_unprofiled``), newest first. Trashed and missing meetings are left out."""
    root = store.root
    with _lock:
        data = _load(root)
        found = []
        for sid, vp in _visible(store).items():
            if not any(_unprofiled(r, data) for r in vp.get("speakers") or []):
                continue
            row = store.session_index_row(sid) if is_safe_id(sid) else None
            created = float((row or {}).get("created") or 0.0)
            if row is not None and created >= since:
                found.append((created, sid))
    return [sid for _created, sid in sorted(found, reverse=True)]


# -- read-time overlay ----------------------------------------------------------------------------------------


def has_voiceprint(root, session_id: str) -> bool:
    """Whether a voiceprint is stored for the meeting (so ``reprofile`` has something to match)."""
    with _lock:
        return bool(_appearances(_root(root), session_id))


def _appearances(root, session_id: str) -> list:
    return [a for a in (_vps(root).get(session_id) or {}).get("speakers") or []]


def overlay(root, session_id: str, settings=None) -> dict:
    """``{label: {person_id, display, named, link, suggested_person_id, suggested_display}}`` for the speakers of one
    meeting that are linked to a person. Empty while the feature is off."""
    root = _root(root)
    if settings is not None and not enabled(settings) or settings is None and not is_enabled(root):
        return {}
    with _lock:
        rows = [a for a in _appearances(root, session_id) if a.get("person_id")]
        if not rows:
            return {}
        data = _load(root)
        out: dict = {}
        for a in rows:
            person = _by_id(data, a["person_id"])
            if person is None:
                continue
            other = _by_id(data, a.get("suggested_person_id")) if a.get("suggested_person_id") else None
            out[a["label"]] = {
                "person_id": person["id"], "display": display_name(person), "named": bool(person.get("name")),
                "is_self": bool(person.get("is_self")), "company": str(person.get("company") or ""),
                "link": a.get("link"), "suggested_person_id": other["id"] if other else None,
                "suggested_display": display_name(other) if other else None}
        return out


def suggestions(root, session_id: str, settings=None) -> list:
    """Pending "Sounds like X?" questions for one meeting: ``[{label, person_id, suggested_person_id,
    suggested_display}]``."""
    return [{"label": label, "person_id": v["person_id"], "display": v["display"],
             "suggested_person_id": v["suggested_person_id"], "suggested_display": v["suggested_display"]}
            for label, v in overlay(root, session_id, settings).items()
            if v["link"] == "suggested" and v["suggested_person_id"]]


def heard(root, session_id: str, segments=None, settings=None) -> list:
    """The people recognised by voice in one meeting, for the Participants rail of its notes: ``[{person_id, display,
    named, company, is_self, labels, talk_seconds}]``, one row per person (a person heard under two labels is one
    row), named people first, then by talking time in ``segments``. Empty while the feature is off or nobody in
    the meeting is linked to a person."""
    over = overlay(root, session_id, settings)
    if not over:
        return []
    talk: dict = {}
    for seg in segments or []:
        if not isinstance(seg, dict) or seg.get("in_gap"):
            continue
        try:
            length = float(seg.get("end") or 0.0) - float(seg.get("start") or 0.0)
        except (TypeError, ValueError):
            continue
        if length > 0:
            talk[seg.get("label")] = talk.get(seg.get("label"), 0.0) + length
    rows: dict = {}
    for label, v in over.items():
        row = rows.setdefault(v["person_id"], {
            "person_id": v["person_id"], "display": v["display"], "named": v["named"], "company": v["company"],
            "is_self": v["is_self"], "labels": [], "talk_seconds": 0.0})
        row["labels"].append(label)
        row["talk_seconds"] += talk.get(label, 0.0)
    out = list(rows.values())
    for row in out:
        row["labels"].sort()
        row["talk_seconds"] = round(row["talk_seconds"], 1)
    out.sort(key=lambda r: (not r["named"], -r["talk_seconds"], r["display"].casefold()))
    return out


def _name_tokens(name: str) -> list:
    return [t for t in re.split(r"[\s\-]+", str(name or "").casefold()) if t]


def fold_names_into_voices(root, session_id: str, names, settings=None) -> list:
    """Names written for a meeting (the AI's participants) that clearly belong to one voice heard in it become that
    person's: "Grant" when the only voice in the meeting named "Grant …" is Grant Batt. A voice-less person called
    "Grant" is combined into Grant Batt (the voice keeps the name it was given; "Grant" becomes a spelling of it,
    so the next notes link straight to them); a bare name just becomes a spelling. Nothing happens for a name People
    already resolves as a spelling, a name that fits two voices or none, a name that has a voice of its own, the
    recorder's profile, or while the feature is off. Returns the names folded."""
    root = _root(root)
    voices = [r for r in heard(root, session_id, None, settings) if r["named"] and r["display"].casefold() != "you"]
    if not voices:
        return []
    folded, merged_into = [], set()
    with _lock:
        data = _load(root)
        voiced = {a["person_id"] for vp in _vps(root).values() for a in vp.get("speakers") or [] if a.get("person_id")}
        for raw in names or []:
            name = names_mod.clean_name(raw if isinstance(raw, str) else (raw.get("name") if isinstance(raw, dict) else ""))
            if not name:
                continue
            hit = lookup(root, [name]).get(name)
            if hit and hit["via"] == "alias":
                continue  # a spelling somebody already claimed
            want = _name_tokens(name)
            fits = [v for v in voices if set(want) <= set(_name_tokens(v["display"])) and want != _name_tokens(v["display"])]
            if len(fits) != 1:
                continue
            target = _by_id(data, fits[0]["person_id"])
            if target is None or not target.get("name"):
                continue
            own = _by_id(data, hit["person_id"]) if hit else None
            if own is not None:
                if own["id"] in voiced or own.get("is_self") or own["id"] == target["id"]:
                    continue
                _merge_locked(root, data, own["id"], target["id"])
                merged_into.add(target["id"])
            else:
                names_mod.add_correction(root, name, target["name"])
            folded.append(name)
        if merged_into:
            _save(root, data)
    for pid in merged_into:
        refresh_suggestions(root, pid)
    return folded


def named_labels(root, session_id: str, settings=None, *, keep_self: bool = False) -> dict:
    """``{label: name}`` for speakers linked to a person with a name (the self person counts once named).

    ``keep_self`` leaves the recorder's own label out, so a transcript handed to the notes AI keeps "You": its
    workflow builds on "You" being the recorder (action-item owners, "we" vs the other side)."""
    return {label: v["display"] for label, v in overlay(root, session_id, settings).items()
            if v["named"] and not (keep_self and v["is_self"])}


def apply_names(root, session_id: str, segments, settings=None, *, keep_self: bool = False):
    """Segments (dicts with ``label``) with the labels of named people replaced by their names. The input is not
    modified; with nothing to apply it is returned as is."""
    mapping = named_labels(root, session_id, settings, keep_self=keep_self)
    if not mapping or not segments:
        return segments
    return [{**s, "label": mapping.get(s.get("label"), s.get("label"))} if isinstance(s, dict) else s for s in segments]


_MD_SPEAKER = re.compile(r"^(\*\*\[[^\]]+\] )(.+?)(:\*\* )", re.MULTILINE)


def apply_names_markdown(root, session_id: str, markdown: str, settings=None, *, keep_self: bool = False) -> str:
    """A rendered transcript (``**[mm:ss] Label:** text``) with the labels of named people replaced by their names."""
    mapping = named_labels(root, session_id, settings, keep_self=keep_self)
    if not mapping or not markdown:
        return markdown
    return _MD_SPEAKER.sub(lambda m: f"{m.group(1)}{mapping.get(m.group(2), m.group(2))}{m.group(3)}", markdown)


def prompt_section(root, session_id: str, settings=None) -> str:
    """Text appended to a review's prompt (after the known names) when speakers of the meeting were recognised by
    voice: which transcript labels are names, with each person's company, and who the recorder is. '' while voice
    profiles are off or nobody heard in the meeting is a named person, so the workflow's plain "You" / "Them N"
    reading applies unchanged."""
    over = overlay(root, session_id, settings)
    named = {label: v for label, v in over.items() if v["named"]}
    if not named:
        return ""
    others = [v for v in named.values() if not v["is_self"]]
    me = next((v for v in named.values() if v["is_self"]), None)
    out = ["", "", "## Identified speakers", ""]
    if others:
        seen: set = set()
        shown = []
        for v in others:
            key = v["display"].casefold()
            if key in seen:
                continue
            seen.add(key)
            shown.append(f"{v['display']} ({v['company']})" if v["company"] else v["display"])
        out.append(
            "These speakers were recognised by voice from earlier meetings, so their labels in the transcript are "
            "their names, not guesses: " + "; ".join(shown) + ". Treat each as that person speaking, and list them in "
            "`participants` with the organization given here when the transcript gives none. Any other label "
            "(\"Them\", \"Them 2\", \"Speaker 1\", \"In room 1\") is still an unidentified voice."
        )
    if me is not None and me["display"].casefold() != "you":
        if others:
            out.append("")
        out.append(
            f"\"You\" (the recorder) is {me['display']}. The transcript keeps the label \"You\"; still write "
            "\"You\" as the owner of the recorder's own action items."
        )
    return "\n".join(out) + "\n"


# -- list / profile / quotes ----------------------------------------------------------------------------------


def _session_info(store, session_id: str) -> dict:
    row = store.session_index_row(session_id) or {}
    return {"session_id": session_id, "session_name": row.get("name") or session_id, "created": row.get("created")}


def _rows_for(store, data: dict) -> dict:
    """person_id -> visible appearances (each with its session id)."""
    rows: dict = {}
    for sid, vp in _visible(store).items():
        for a in vp.get("speakers") or []:
            if a.get("person_id"):
                rows.setdefault(a["person_id"], []).append({**a, "session_id": sid})
    return rows


def _summary(store, person: dict, rows: list, data: dict) -> dict:
    created = [(_session_info(store, r["session_id"]).get("created") or 0.0) for r in rows]
    hint = next((r for r in rows if r.get("link") == "suggested" and _by_id(data, r.get("suggested_person_id"))), None)
    other = _by_id(data, hint["suggested_person_id"]) if hint else None
    intro = next((r["intro_suggestion"] for r in rows if isinstance(r.get("intro_suggestion"), dict)), None)
    return {
        "id": person["id"], "number": person.get("number"), "name": person.get("name") or "",
        "display": display_name(person), "email": person.get("email") or "", "company": person.get("company") or "",
        "is_self": bool(person.get("is_self")), "meetings": len({r["session_id"] for r in rows}),
        "talk_seconds": round(sum(r.get("speech_sec") or 0.0 for r in rows), 1),
        "last_heard": max(created) if created else None,
        "archived": person.get("archived") if isinstance(person.get("archived"), dict) else None,
        "suggestion": {"person_id": other["id"], "display": display_name(other)} if other else None,
        "intro_name": intro.get("name") if intro and not (person.get("name") or person.get("is_self")) else None,
    }


def list_people(store, sort: str = "talking", q: Optional[str] = None) -> list:
    with _lock:
        sync_known_names(store.root)  # every known name is a person (older names.json entries become voice-less people)
        data = _load(store.root)
        rows = _rows_for(store, data)
        out = []
        for person in data["people"]:
            mine = rows.get(person["id"], [])
            if not mine and not (person.get("name") or person.get("email") or person.get("company")):
                continue
            out.append(_summary(store, person, mine, data))
    if q:
        needle = q.strip().casefold()
        out = [p for p in out if needle in " ".join((p["display"], p["email"], p["company"])).casefold()]
    if sort == "recent":
        out.sort(key=lambda p: (p["last_heard"] is None, -(p["last_heard"] or 0.0), p["number"] or 0))
    elif sort == "alpha":
        out.sort(key=lambda p: (0, p["name"].casefold(), 0) if p["name"] else (1, "", p["number"] or 0))
    else:
        out.sort(key=lambda p: (-p["talk_seconds"], p["number"] or 0))
    return out


def get_person(store, person_id: str, quotes_limit: int = 20) -> dict:
    """Profile: summary + appearances + up to 3 voice samples (different meetings) + latest quotes."""
    with _lock:
        data = _load(store.root)
        person = _by_id(data, person_id)
        if person is None:
            raise PersonNotFound(person_id)
        mine = _rows_for(store, data).get(person_id, [])
        profile = _summary(store, person, mine, data)
        meetings = []
        for r in sorted(mine, key=lambda r: -(_session_info(store, r["session_id"]).get("created") or 0.0)):
            other = _by_id(data, r.get("suggested_person_id")) if r.get("suggested_person_id") else None
            meetings.append({
                **_session_info(store, r["session_id"]), "label": r["label"], "track": r.get("track"),
                "speech_sec": r.get("speech_sec"), "link": r.get("link"), "score": r.get("score"),
                "suggested_person_id": other["id"] if other else None,
                "suggested_display": display_name(other) if other else None,
                "intro_suggestion": r.get("intro_suggestion")})
        samples = []
        for r in sorted(mine, key=lambda r: -(r.get("speech_sec") or 0.0)):
            name = next((n for n in r.get("clips") or []
                         if (_clips_dir(store.root, r["session_id"]) / n).is_file()), None)
            if name and len(samples) < MAX_CLIPS:
                samples.append({"session_id": r["session_id"], "name": name,
                                "url": f"/v1/people/{person_id}/clips/{r['session_id']}/{name}"})
        model = _models(_visible(store), data).get(person_id) or {}
        profile["aliases"] = aliases(store.root, person)
        profile["voice_profile"] = {"meetings": model.get("meetings", 0), "confirmed": model.get("confirmed", 0)}
        profile["named_from"], profile["intro_suggestion"] = _intro_info(store, person, mine)
    profile.update(appearances=meetings, samples=samples, quotes=quotes(store, person_id, limit=quotes_limit))
    return profile


def quotes(store, person_id: str, *, limit: int = 20, q: Optional[str] = None, meeting_id: Optional[str] = None) -> list:
    """What a person said, newest meeting first: ``[{session_id, session_name, created, start, end, label, text}]``.
    Taken from the stored transcript segments whose label is the person's label in that meeting."""
    with _lock:
        rows = [r for r in _rows_for(store, _load(store.root)).get(person_id, [])
                if meeting_id is None or r["session_id"] == meeting_id]
    needle = (q or "").strip().casefold()
    infos = {r["session_id"]: _session_info(store, r["session_id"]) for r in rows}
    out: list = []
    for r in sorted(rows, key=lambda r: -(infos[r["session_id"]].get("created") or 0.0)):
        done = store.latest_done_job(r["session_id"])
        transcript = store.read_transcript(done["job_id"]) if done else None
        try:
            payload = json.loads((transcript or {}).get("json") or "{}")
        except ValueError:
            continue
        found = [s for s in (payload.get("segments") if isinstance(payload, dict) else None) or []
                 if isinstance(s, dict) and s.get("label") == r["label"] and s.get("text") and not s.get("in_gap")
                 and (not r.get("track") or s.get("track") in (None, r["track"]))
                 and (not needle or needle in str(s["text"]).casefold())]
        for s in sorted(found, key=lambda s: -float(s.get("start") or 0.0)):
            out.append({**infos[r["session_id"]], "start": s.get("start"), "end": s.get("end"),
                        "label": r["label"], "text": s["text"]})
            if len(out) >= limit:
                return out
    return out


def clip_path(store, person_id: str, session_id: str, name: str) -> Optional[Path]:
    """The stored sample, only when that person has it in a visible meeting."""
    if not is_safe_id(session_id) or not _CLIP_NAME.match(name or ""):
        return None
    with _lock:
        vp = _visible(store).get(session_id)
        if vp is None or not any(a.get("person_id") == person_id and name in (a.get("clips") or [])
                                 for a in vp.get("speakers") or []):
            return None
    path = _clips_dir(store.root, session_id) / name
    return path if path.is_file() else None


# -- edits ----------------------------------------------------------------------------------------------------


def _clean_field(value, limit: int) -> str:
    return names_mod.clean_name(value)[:limit]


def update_person(root, person_id: str, fields: dict) -> dict:
    """Set name / email / company (any subset). ValueError on a bad email; PersonNotFound.

    A name is also a known name for the notes prompt and transcription (``names.json``), and "also heard as"
    spellings that pointed at the old name follow the new one. A new name can make other meetings' unnamed voices
    sound like this person (``refresh_suggestions``)."""
    root = _root(root)
    with _lock:
        data = _load(root)
        person = _by_id(data, person_id)
        if person is None:
            raise PersonNotFound(person_id)
        old_name = person.get("name") or ""
        if "name" in fields:
            person["name"] = _clean_field(fields["name"], MAX_NAME_CHARS)
        if "company" in fields:
            person["company"] = _clean_field(fields["company"], MAX_NAME_CHARS)
        if "email" in fields:
            email = _clean_field(fields["email"], MAX_EMAIL_CHARS + 1)
            if email and (len(email) > MAX_EMAIL_CHARS or not _EMAIL.match(email)):
                raise ValueError("email does not look like an email address")
            person["email"] = email
        person["updated"] = time.time()
        _save(root, data)
        if person["name"] != old_name:
            names_mod.rename_person(root, old_name, person["name"])
            if person["name"]:
                refresh_suggestions(root, person_id)
        return dict(person)


def add_person(root, name: str, company: str = "") -> dict:
    """A person by name with no voice yet (it is a known name straight away). ValueError when the name is empty
    or already in People."""
    root = _root(root)
    name, company = _clean_field(name, MAX_NAME_CHARS), _clean_field(company, MAX_NAME_CHARS)
    if not name:
        raise ValueError("name is required")
    with _lock:
        data = _load(root)
        if any((p.get("name") or "").casefold() == name.casefold() for p in data["people"]):
            raise ValueError(f"{name} is already in People")
        person = _new_person(data)
        person.update(name=name, company=company)
        _save(root, data)
        names_mod.add_name(root, name)
        return dict(person)


def sync_known_names(root) -> int:
    """Make every name in the names glossary a person (no voice yet). Idempotent; returns how many were added."""
    root = _root(root)
    with _lock:
        known = names_mod.load(root)["known_names"]
        data = _load(root)
        have = {(p.get("name") or "").casefold() for p in data["people"]}
        added = 0
        for name in known:
            if name.casefold() not in have:
                _new_person(data)["name"] = name
                have.add(name.casefold())
                added += 1
        if added:
            _save(root, data)
        return added


def aliases(root, person: dict) -> list:
    """"Also heard as" spellings: the corrections whose right spelling is this person's name."""
    name = (person.get("name") or "").casefold()
    if not name:
        return []
    return [c["wrong"] for c in names_mod.load(root)["name_corrections"] if c["right"].casefold() == name]


def add_alias(root, person_id: str, wrong: str) -> dict:
    root = _root(root)
    with _lock:
        person = _by_id(_load(root), person_id)
        if person is None:
            raise PersonNotFound(person_id)
        wrong = names_mod.clean_name(wrong)[:MAX_NAME_CHARS]
        if not person.get("name"):
            raise ValueError("give this person a name first")
        if not wrong:
            raise ValueError("wrong is required")
        if wrong.casefold() == person["name"].casefold():
            raise ValueError("that is already their name")
        names_mod.add_correction(root, wrong, person["name"])
        return {"person_id": person_id, "aliases": aliases(root, person)}


def remove_alias(root, person_id: str, wrong: str) -> dict:
    root = _root(root)
    with _lock:
        person = _by_id(_load(root), person_id)
        if person is None:
            raise PersonNotFound(person_id)
        names_mod.remove_correction(root, names_mod.clean_name(wrong), person.get("name") or "")
        return {"person_id": person_id, "aliases": aliases(root, person)}


def lookup(root, wanted) -> dict:
    """``{name: {person_id, display, company, is_self, via}}`` for the spellings that match a person's name or an
    "also heard as" spelling (case-insensitive); names that match nobody are left out."""
    root = _root(root)
    with _lock:
        data = _load(root)
        by_name: dict = {}
        for p in data["people"]:
            if p.get("name"):
                by_name.setdefault(p["name"].casefold(), (p, "name"))
        for c in names_mod.load(root)["name_corrections"]:
            owner = by_name.get(c["right"].casefold())
            if owner and owner[1] == "name":
                by_name.setdefault(c["wrong"].casefold(), (owner[0], "alias"))
        out: dict = {}
        for raw in wanted:
            hit = by_name.get(names_mod.clean_name(raw).casefold())
            if hit:
                out[raw] = {"person_id": hit[0]["id"], "display": display_name(hit[0]),
                            "company": str(hit[0].get("company") or ""), "is_self": bool(hit[0].get("is_self")),
                            "via": hit[1]}
        return out


def list_companies(root) -> list:
    """Distinct companies across people (case-insensitive, first spelling kept), sorted."""
    root = _root(root)
    with _lock:
        seen: dict = {}
        for p in _load(root)["people"]:
            c = (p.get("company") or "").strip()
            if c:
                seen.setdefault(c.casefold(), c)
    return sorted(seen.values(), key=str.casefold)


def _merge_locked(root, data: dict, source_id: str, into_id: str) -> dict:
    source, target = _by_id(data, source_id), _by_id(data, into_id)
    if source is None or target is None:
        raise PersonNotFound(source_id if source is None else into_id)
    if source.get("is_self"):
        raise ValueError("the profile for you cannot be combined into another one")
    for vp in _vps(root).values():
        changed = False
        for a in vp.get("speakers") or []:
            if a.get("person_id") == source_id:
                a.update(person_id=into_id, link="confirmed", suggested_person_id=None)
                changed = True
            elif a.get("suggested_person_id") == source_id:
                a["suggested_person_id"] = None if a.get("person_id") == into_id else into_id
                changed = True
        if changed:
            _put_vp(root, vp)
    source_name = source.get("name") or ""
    for key in ("name", "email", "company"):
        if not target.get(key) and source.get(key):
            target[key] = source[key]
    if source.get("named_from"):
        target["named_from"] = list(target.get("named_from") or []) + list(source["named_from"])
    target["updated"] = time.time()
    data["people"] = [p for p in data["people"] if p["id"] != source_id]
    if source_name and target.get("name") and source_name.casefold() != target["name"].casefold():
        # their name is now another way to say the kept name, and so are the spellings that pointed at them
        older = [c["wrong"] for c in names_mod.load(root)["name_corrections"]
                 if c["right"].casefold() == source_name.casefold()]
        names_mod.add_correction(root, source_name, target["name"])
        for wrong in older:
            names_mod.add_correction(root, wrong, target["name"])
    return target


def merge_people(root, source_id: str, into_id: str) -> dict:
    """Move all of ``source``'s appearances to ``into`` (as confirmed), fill ``into``'s blank fields, delete source.
    The source's name (and its "also heard as" spellings) become spellings of ``into``."""
    root = _root(root)
    if source_id == into_id:
        raise ValueError("cannot combine a person with themselves")
    with _lock:
        data = _load(root)
        target = _merge_locked(root, data, source_id, into_id)
        _save(root, data)
        if target.get("name"):
            names_mod.add_name(root, target["name"])
        refresh_suggestions(root, into_id)
        return dict(target)


def combine_people(root, ids, into_id: str) -> dict:
    """Combine several people into ``into_id`` (one of ``ids``) in one step. All checks come before any change:
    ValueError for fewer than two different people, ``into`` not among them, or the "You" profile among them while
    someone else is kept (the profile for you cannot go away; keep it); PersonNotFound for an unknown id."""
    root = _root(root)
    ids = list(dict.fromkeys(i for i in ids if isinstance(i, str)))
    if len(ids) < 2:
        raise ValueError("choose at least two different people to combine")
    if into_id not in ids:
        raise ValueError("the person to keep must be one of the chosen people")
    with _lock:
        data = _load(root)
        for i in ids:
            if _by_id(data, i) is None:
                raise PersonNotFound(i)
        me = _self_person(data, create=False)
        if me and me["id"] in ids and me["id"] != into_id:
            raise ValueError("the profile for you cannot be combined into another one; keep it instead")
        target = _by_id(data, into_id)
        for source_id in ids:
            if source_id != into_id:
                target = _merge_locked(root, data, source_id, into_id)
        _save(root, data)
        if target.get("name"):
            names_mod.add_name(root, target["name"])
        refresh_suggestions(root, into_id)
        return dict(target)


def refresh_suggestions(source, person_id: str) -> int:
    """After ``person_id`` learned something new, look through the other meetings again: a voice linked to an
    unnamed person (not you) with no question pending that now fits this named person gets a "sounds like X?"
    suggestion. Never relinks, never asks about a person the user already said "not them" to, and never asks
    about someone already in that meeting. ``source`` is a store or a data root. Returns the number of new
    suggestions. Inside the lock; trashed meetings are skipped."""
    root = Path(source.root if hasattr(source, "session_exists") else source)  # (a Path has a .root too)
    with _lock:
        data = _load(root)
        person = _by_id(data, person_id)
        if person is None or not (person.get("name") or person.get("is_self")):
            return 0
        visible = _visible(source) if hasattr(source, "session_exists") else _visible_root(root)
        model = _models(visible, data).get(person_id)
        if model is None:
            return 0
        centroids, exemplars = {person_id: model["centroid"]}, {person_id: model["exemplars"]}
        added = 0
        for vp in visible.values():
            rows = vp.get("speakers") or []
            if any(a.get("person_id") == person_id for a in rows):
                continue
            changed = False
            for a in rows:
                owner = _by_id(data, a.get("person_id")) if a.get("person_id") else None
                if (owner is None or owner.get("name") or owner.get("is_self") or a.get("link") != "auto"
                        or a.get("suggested_person_id") or not a.get("embedding")
                        or person_id in (a.get("rejected_person_ids") or [])):
                    continue
                vec = _unit(a["embedding"])
                score = _score(vec, person_id, centroids, exemplars) if vec else 0.0
                if score >= SUGGEST:
                    a.update(suggested_person_id=person_id, link="suggested", score=round(score, 4))
                    changed = True
                    added += 1
                    break  # one question per person per meeting
            if changed:
                _put_vp(root, vp)
        return added


def appearance_action(root, session_id: str, label: str, action: str, person_id: Optional[str] = None) -> dict:
    """``confirm`` (accept the suggestion), ``reject`` ("not them": drop the suggestion for good), ``assign`` (to
    ``person_id``) or ``unlink`` (to a new unnamed person). Returns the changed appearance."""
    root = _root(root)
    if action not in ("confirm", "reject", "assign", "unlink"):
        raise ValueError("action must be confirm, reject, assign or unlink")
    with _lock:
        vp = _vps(root).get(session_id)
        a = next((x for x in (vp or {}).get("speakers") or [] if x.get("label") == label), None)
        if a is None:
            raise PersonNotFound(f"{session_id}/{label}")
        data = _load(root)
        learned = None
        if action == "reject":
            if a.get("suggested_person_id"):
                rejected = a.setdefault("rejected_person_ids", [])
                if a["suggested_person_id"] not in rejected:
                    rejected.append(a["suggested_person_id"])
            a["suggested_person_id"] = None
            if a.get("link") == "suggested":
                a["link"] = "auto"
        elif action == "confirm":
            if not a.get("suggested_person_id") or _by_id(data, a["suggested_person_id"]) is None:
                raise ValueError("there is no suggestion to confirm")
            learned = a["suggested_person_id"]
            a.update(person_id=learned, link="confirmed", suggested_person_id=None)
        elif action == "assign":
            if _by_id(data, person_id) is None:
                raise PersonNotFound(str(person_id))
            learned = person_id
            a.update(person_id=person_id, link="confirmed", suggested_person_id=None)
        else:
            a.update(person_id=_new_person(data)["id"], link="confirmed", suggested_person_id=None)
        _put_vp(root, vp)
        if learned:
            _wake_archived(data, [a])  # you just told us it is them: no longer archived
        _gc(root, data)
        _save(root, data)
        if learned:
            refresh_suggestions(root, learned)
        return dict(a)


# -- names from introductions (the optional AI job, server/intro_names.py) -------------------------------------------


def _named_here(person: dict, session_id: str) -> bool:
    """The person got its name from an introduction in this meeting (so a re-match keeps them on this voice)."""
    return any(e.get("session_id") == session_id and e.get("mode") == "named" for e in person.get("named_from") or [])


def intro_candidates(root, session_id: str, settings=None) -> dict:
    """Who the introduction job may name in one meeting, from ``heard``: ``{"unnamed": [labels of voices linked to
    an unnamed person (not you)], "named": {label: name}, "self": [the recorder's labels]}``. All empty while the
    feature is off or nobody in the meeting is linked to a person."""
    out: dict = {"unnamed": [], "named": {}, "self": []}
    for row in heard(root, session_id, None, settings):
        if row["is_self"]:
            out["self"].extend(row["labels"])
        elif row["named"]:
            out["named"].update({label: row["display"] for label in row["labels"]})
        else:
            out["unnamed"].extend(row["labels"])
    out["unnamed"].sort()
    return out


def _intro_entry(session_id: str, label: str, name: str, quote: str, mode: str, company_set: bool = False) -> dict:
    """Provenance kept on the person an introduction named (or was merged into)."""
    return {"session_id": session_id, "label": label, "name": name, "quote": quote, "mode": mode,
            "company_set": company_set, "at": time.time()}


def _apply_intro_one(root, data: dict, session_id: str, a: dict, e: dict, *, manual: bool = False) -> tuple:
    """One validated introduction against one appearance (inside the lock, ``data`` loaded). Returns ``(outcome,
    person_id)``; outcomes: ``named``, ``merged``, ``suggested`` (a "sounds like X?" question), ``intro_suggestion``
    (a low-confidence name kept for the user), or ``skipped:<why>``. ``manual``: the user pressed "Use this name", so
    a confirmed link and an earlier "not them" do not stop it."""
    person = _by_id(data, a.get("person_id"))
    name, company = e["name"], e.get("company") or ""
    if person is None or person.get("is_self"):
        return "skipped:no person", None
    if person.get("name"):
        return "skipped:already named", person["id"]
    if not manual and name.casefold() in [n.casefold() for n in a.get("rejected_intro_names") or []]:
        return "skipped:undone", person["id"]
    if not manual and a.get("link") == "confirmed":
        return "skipped:confirmed", person["id"]
    hit = lookup(root, [name]).get(name)
    other = _by_id(data, hit["person_id"]) if hit else None
    if other is None and len(_name_tokens(name)) == 1:
        # "Dana" alone, said in the meeting, is the one person called "Dana …" when there is exactly one
        firsts = [p for p in data["people"] if p.get("name") and not p.get("is_self")
                  and _name_tokens(p["name"])[:1] == _name_tokens(name)]
        other = firsts[0] if len(firsts) == 1 else None
        ambiguous = len(firsts) > 1  # two people called "Dana …": only suggest, never a new third "Dana"
    else:
        ambiguous = False
    high = (e.get("confidence") == "high" and not ambiguous) or manual
    if other is None:
        if not high:
            a["intro_suggestion"] = {"name": name, "company": company, "quote": e["quote"]}
            return "intro_suggestion", person["id"]
        person["name"] = name
        gave_company = bool(company and not person.get("company"))
        if gave_company:
            person["company"] = company
        person["updated"] = time.time()
        person["named_from"] = list(person.get("named_from") or []) + [
            _intro_entry(session_id, a["label"], name, e["quote"], "named", gave_company)]
        a.pop("intro_suggestion", None)
        names_mod.rename_person(root, "", name)
        return "named", person["id"]
    if other["id"] == person["id"]:
        return "skipped:same person", person["id"]
    if other.get("is_self"):
        return "skipped:you", person["id"]  # someone said your name; another voice is never you
    voiced = other.get("is_self") or any(
        x.get("person_id") == other["id"] for vp in _vps(root).values() for x in vp.get("speakers") or [])
    if high and not voiced:
        entry = _intro_entry(session_id, a["label"], other["name"], e["quote"], "merged")
        target = _merge_locked(root, data, person["id"], other["id"])
        target["named_from"] = list(target.get("named_from") or []) + [entry]
        if company and not target.get("company"):
            target["company"] = company
        a.pop("intro_suggestion", None)
        return "merged", target["id"]
    if other["id"] in (a.get("rejected_person_ids") or []):
        return "skipped:not them", person["id"]
    a.update(suggested_person_id=other["id"], link="suggested")
    a.pop("intro_suggestion", None)
    return "suggested", other["id"]


def apply_intro_names(root, session_id: str, entries) -> list:
    """Apply the introduction job's validated names to one meeting (see ``_apply_intro_one``). ``entries``:
    ``[{label, name, company?, quote, confidence}]`` already checked against the transcript. Nothing changes for a
    person who has a name now, is you, was confirmed by hand, or whose name the user undid for this voice. Returns
    ``[{label, name, outcome, person_id}]``. Other meetings are asked again afterwards (``refresh_suggestions``)."""
    root = _root(root)
    results: list = []
    named: set = set()
    with _lock:
        vp = _vps(root).get(session_id)
        data = _load(root)
        for e in entries or []:
            a = next((x for x in (vp or {}).get("speakers") or [] if x.get("label") == e["label"]), None)
            if a is None:
                results.append({"label": e["label"], "name": e["name"], "outcome": "skipped:no voice", "person_id": None})
                continue
            outcome, pid = _apply_intro_one(root, data, session_id, a, e)
            results.append({"label": e["label"], "name": e["name"], "outcome": outcome, "person_id": pid})
            if outcome in ("named", "merged"):
                named.add(pid)
            if not outcome.startswith("skipped"):
                _put_vp(root, vp)
                _save(root, data)  # (lookup and a merge read the file for the next entry)
        if named:
            _gc(root, data)
            _save(root, data)
    for pid in named:
        refresh_suggestions(root, pid)
    return results


def _intro_row(root, session_id: str, label: str) -> tuple:
    vp = _vps(root).get(session_id)
    a = next((x for x in (vp or {}).get("speakers") or [] if x.get("label") == label), None)
    if a is None:
        raise PersonNotFound(f"{session_id}/{label}")
    return vp, a


def accept_intro_suggestion(root, session_id: str, label: str) -> dict:
    """"Use this name": apply the appearance's low-confidence ``intro_suggestion`` as if it were certain. ValueError
    when there is none or the person has a name already. Returns ``{outcome, person_id}``."""
    root = _root(root)
    with _lock:
        vp, a = _intro_row(root, session_id, label)
        pending = a.get("intro_suggestion")
        if not isinstance(pending, dict) or not pending.get("name"):
            raise ValueError("there is no suggested name to use")
        data = _load(root)
        person = _by_id(data, a.get("person_id"))
        if person is not None and (person.get("name") or person.get("is_self")):
            raise ValueError("this person already has a name")
        entry = {"label": label, "name": pending["name"], "company": pending.get("company") or "",
                 "quote": pending.get("quote") or "", "confidence": "high"}
        outcome, pid = _apply_intro_one(root, data, session_id, a, entry, manual=True)
        a.pop("intro_suggestion", None)
        _put_vp(root, vp)
        _gc(root, data)
        _save(root, data)
    if outcome in ("named", "merged"):
        refresh_suggestions(root, pid)
    return {"outcome": outcome, "person_id": pid}


def dismiss_intro_suggestion(root, session_id: str, label: str) -> dict:
    """"Not them": drop the suggested name and remember it for this voice, so a re-run never offers it again."""
    root = _root(root)
    with _lock:
        vp, a = _intro_row(root, session_id, label)
        pending = a.pop("intro_suggestion", None)
        if not isinstance(pending, dict) or not pending.get("name"):
            raise ValueError("there is no suggested name to dismiss")
        rejected = a.setdefault("rejected_intro_names", [])
        if pending["name"].casefold() not in [n.casefold() for n in rejected]:
            rejected.append(pending["name"])
        _put_vp(root, vp)
        return {"ok": True}


def undo_intro_name(root, session_id: str, label: str) -> dict:
    """Take back a name the introduction job gave. A plain naming clears the name (and the company it filled in)
    again; a merge moves this voice out into a new unnamed person and leaves the kept person as they were. Either
    way the name is remembered as rejected for this voice. ValueError when this voice was not named that way.
    Returns ``{mode, person_id}`` (the person the voice is linked to now)."""
    root = _root(root)
    with _lock:
        vp, a = _intro_row(root, session_id, label)
        data = _load(root)
        person = _by_id(data, a.get("person_id"))
        entry = next((e for e in (person or {}).get("named_from") or []
                      if e.get("session_id") == session_id and e.get("label") == label), None)
        if person is None or entry is None:
            raise ValueError("this voice was not named from an introduction")
        person["named_from"] = [e for e in person["named_from"] if e is not entry]
        if not person["named_from"]:
            del person["named_from"]
        rejected = a.setdefault("rejected_intro_names", [])
        if entry["name"].casefold() not in [n.casefold() for n in rejected]:
            rejected.append(entry["name"])
        if entry.get("mode") == "merged":
            fresh = _new_person(data)
            a.update(person_id=fresh["id"], link="confirmed", suggested_person_id=None)
            result_id = fresh["id"]
        else:
            old = person.get("name") or ""
            if old.casefold() == entry["name"].casefold():
                person["name"] = ""
                if entry.get("company_set"):
                    person["company"] = ""
                person["updated"] = time.time()
                names_mod.rename_person(root, old, "")
            result_id = person["id"]
        _put_vp(root, vp)
        _gc(root, data)
        _save(root, data)
        return {"mode": entry.get("mode"), "person_id": result_id}


def _intro_info(store, person: dict, rows: list) -> tuple:
    """For the person page: ``(named_from, intro_suggestion)``. The first lists the introductions that named this
    person and still hold (the voice is still theirs): ``[{session_id, session_name, label, name, quote, mode,
    at}]``. The second is the first low-confidence suggested name on one of their voices while they have no name."""
    mine = {(r["session_id"], r["label"]) for r in rows}
    named_from = [{**_session_info(store, e["session_id"]), "label": e["label"], "name": e.get("name"),
                   "quote": e.get("quote"), "mode": e.get("mode"), "at": e.get("at")}
                  for e in person.get("named_from") or [] if (e.get("session_id"), e.get("label")) in mine]
    pending = None
    if not (person.get("name") or person.get("is_self")):
        row = next((r for r in rows if isinstance(r.get("intro_suggestion"), dict)), None)
        if row is not None:
            pending = {**_session_info(store, row["session_id"]), "label": row["label"], **row["intro_suggestion"]}
    return named_from, pending


def delete_person(root, person_id: str) -> None:
    """Delete the profile; its appearances stay but show their plain labels again."""
    root = _root(root)
    with _lock:
        data = _load(root)
        person = _by_id(data, person_id)
        if person is None:
            raise PersonNotFound(person_id)
        for vp in _vps(root).values():
            changed = False
            for a in vp.get("speakers") or []:
                if a.get("person_id") == person_id:
                    a.update(person_id=None, link="auto", score=None, suggested_person_id=None)
                    changed = True
                elif a.get("suggested_person_id") == person_id:
                    a["suggested_person_id"] = None
                    changed = True
            if changed:
                _put_vp(root, vp)
        data["people"] = [p for p in data["people"] if p["id"] != person_id]
        _save(root, data)
        if person.get("name"):
            names_mod.remove_name(root, person["name"])  # else the names glossary would bring them back


# -- archive and forget (run with the nightly voice recheck) ---------------------------------------------------


def _wake_archived(data: dict, rows) -> None:
    """Clear ``archived`` on every person these appearances link to (they were just heard, or just confirmed)."""
    for a in rows:
        person = _by_id(data, a.get("person_id")) if a.get("person_id") else None
        if person is not None:
            person.pop("archived", None)


def _meeting_time(store, session_id: str, vp: Optional[dict] = None) -> float:
    """When the meeting started, as ``recheck_candidates`` dates it: the session index's ``created``, else the
    voiceprint's own ``created`` (0 when neither is known)."""
    row = store.session_index_row(session_id) if is_safe_id(session_id) else None
    return float((row or {}).get("created") or (vp or {}).get("created") or 0.0)


def _last_heard(store) -> dict:
    """``{person_id: start of the newest meeting with an appearance linked to them}``, trashed meetings left out."""
    heard: dict = {}
    for sid, vp in _visible(store).items():
        when = _meeting_time(store, sid, vp)
        for a in vp.get("speakers") or []:
            pid = a.get("person_id")
            if pid and when > heard.get(pid, 0.0):
                heard[pid] = when
    return heard


def set_archived(root, person_id: str, archived: bool, now: Optional[float] = None) -> dict:
    """Archive a person by hand (``{"at", "auto": False}``) or restore them. Archived people stay in the voice
    matching; they are only hidden from the People list. ValueError for the "You" profile; PersonNotFound."""
    root = _root(root)
    with _lock:
        data = _load(root)
        person = _by_id(data, person_id)
        if person is None:
            raise PersonNotFound(person_id)
        if archived and person.get("is_self"):
            raise ValueError("the profile for you cannot be archived")
        if archived:
            person["archived"] = {"at": time.time() if now is None else now, "auto": False}
        else:
            person.pop("archived", None)
        _save(root, data)
        return dict(person)


def _open_question(a: dict) -> bool:
    """The People page asks something about this voice: a suggested name or a "sounds like X?" suggestion."""
    return isinstance(a.get("intro_suggestion"), dict) or (
        a.get("link") == "suggested" and bool(a.get("suggested_person_id")))


def cleanup(store, settings, now: Optional[float] = None) -> dict:
    """Archive, restore and forget people by when they were last heard (``_last_heard``).

    * Named people (not you) not heard for ``people_archive_days`` (someone never heard: since they were added) get ``archived = {"at", "auto": True}``. They are
      hidden from the People list but still match voices. Anyone archived (by hand too) who has been heard in a
      meeting since is restored.
    * Unnamed voices (no name, email or company, not you, not archived by hand) not heard for ``people_forget_days``
      are forgotten: their appearances and clips are removed from the meetings' voiceprints (the meetings and
      transcripts are untouched, those voices show their transcript label again) and the person is deleted. A voice
      with a question pending, or one another voice is suggested to be, is kept.

    0 turns each part off. When something changed it is also kept as ``last_cleanup`` in people.json. Returns
    ``{"at", "archived": [{"id", "name"}], "restored": [...], "forgotten": count}``."""
    root = store.root
    now = time.time() if now is None else now
    archive_days = max(0, int(getattr(settings, "people_archive_days", 0) or 0))
    forget_days = max(0, int(getattr(settings, "people_forget_days", 0) or 0))
    out: dict = {"at": now, "archived": [], "restored": [], "forgotten": 0}
    with _lock:
        data = _load(root)
        heard = _last_heard(store)
        for person in data["people"]:
            if person.get("is_self") or not person.get("name"):
                continue
            mark = person.get("archived") if isinstance(person.get("archived"), dict) else None
            last = heard.get(person["id"])
            if mark and last and last > float(mark.get("at") or 0.0):
                person.pop("archived")
                out["restored"].append({"id": person["id"], "name": person["name"]})
            elif (not mark and archive_days
                  and (last or float(person.get("created") or 0.0)) < now - archive_days * SECONDS_PER_DAY):
                person["archived"] = {"at": now, "auto": True}
                out["archived"].append({"id": person["id"], "name": person["name"]})
        if forget_days:
            asked = {a.get("suggested_person_id") for vp in _vps(root).values() for a in vp.get("speakers") or []
                     if a.get("suggested_person_id")}
            doomed = set()
            for person in data["people"]:
                last = heard.get(person["id"])
                if (person.get("is_self") or person.get("name") or person.get("email") or person.get("company")
                        or person.get("archived") or last is None or last >= now - forget_days * SECONDS_PER_DAY
                        or person["id"] in asked):
                    continue
                if any(a.get("person_id") == person["id"] and _open_question(a)
                       for vp in _vps(root).values() for a in vp.get("speakers") or []):
                    continue
                doomed.add(person["id"])
            for vp in list(_vps(root).values()):
                gone = [a for a in vp.get("speakers") or [] if a.get("person_id") in doomed]
                if not gone:
                    continue
                for a in gone:
                    for name in a.get("clips") or []:
                        if _CLIP_NAME.match(str(name)):
                            try:
                                (_clips_dir(root, vp["session_id"]) / name).unlink()
                            except OSError:
                                pass
                left = [a for a in vp["speakers"] if a not in gone]
                if left:
                    _put_vp(root, {**vp, "speakers": left})
                else:
                    _drop_vp(root, vp["session_id"])
            data["people"] = [p for p in data["people"] if p["id"] not in doomed]
            out["forgotten"] = len(doomed)
        if out["archived"] or out["restored"] or out["forgotten"]:
            _gc(root, data)
            data["last_cleanup"] = out
            _save(root, data)
    return out


def last_cleanup(root) -> Optional[dict]:
    """What the last cleanup that changed anything did (``cleanup``'s result), or None."""
    with _lock:
        return _load(_root(root)).get("last_cleanup")


def delete_all(root) -> None:
    """Remove every voice profile, voiceprint and clip."""
    root = _root(root)
    with _lock:
        shutil.rmtree(people_dir(root), ignore_errors=True)
        _voiceprints.pop(str(root), None)


def forget_session(root, session_id: str) -> None:
    """A meeting is gone for good: drop its voiceprint and clips, then people left with nothing to show."""
    root = _root(root)
    try:
        with _lock:
            if not people_dir(root).exists():
                return
            _drop_vp(root, session_id)
            data = _load(root)
            if _gc(root, data):
                _save(root, data)
    except Exception:  # noqa: BLE001 - housekeeping must never break a delete
        logger.warning("could not forget voice profiles of %s", session_id, exc_info=True)
