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
MAX_CLIPS = 3
CLIP_MAX_SECONDS = 12.0
CLIP_MIN_SECONDS = 1.0
MIC_MIN_SPEAKERS, MIC_MAX_SPEAKERS = 1, 4
MAX_NAME_CHARS = 120
MAX_EMAIL_CHARS = 254
MAX_VECTOR = 1024

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
    return {
        "revision": int(raw.get("revision") or 0),
        "next_number": int(nxt) if isinstance(nxt, int) and nxt >= 1 else 1,
        "people": people,
    }


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


def _centroids(store, data: dict, exclude_session: Optional[str] = None) -> dict:
    pooled: dict = {}
    for sid, vp in _visible(store).items():
        if sid == exclude_session:
            continue
        for a in vp.get("speakers") or []:
            if a.get("person_id") and a.get("link") in ("auto", "confirmed") and a.get("embedding"):
                pooled.setdefault(a["person_id"], []).append((a["embedding"], a.get("speech_sec") or 0.0))
    known = {p["id"] for p in data["people"]}
    return {pid: c for pid, items in pooled.items() if pid in known and (c := _centroid(items)) is not None}


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
        centroids = _centroids(store, data, exclude_session=session_id)
        assigned: dict = {}  # candidate index -> (person_id, link, score, suggested_person_id)
        used: set = set()    # people already taken in this meeting

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
        # Everyone else with enough speech: best score first, one person per candidate and the other way round.
        eligible = [i for i, c in enumerate(candidates) if i not in assigned and c["speech_sec"] >= MIN_SPEECH]
        scored = sorted(((cosine(candidates[i]["embedding"], vec), i, pid)
                         for i in eligible for pid, vec in centroids.items()), reverse=True)
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
                pid, link, score, suggested = _new_person(data)["id"], "auto", None, None
                if i in suggestions:
                    suggested, score = suggestions[i]
            else:
                pid, link, score, suggested = None, "auto", None, None
            speakers_out.append({
                "track": c["track"], "raw": c["raw"], "label": c["label"],
                "embedding": [round(x, 6) for x in c["embedding"]], "speech_sec": c["speech_sec"],
                "person_id": pid, "link": "suggested" if suggested else link,
                "score": None if score is None else round(score, 4), "suggested_person_id": suggested, "clips": []})
        # Clips come from the audio the server has right now; the voiceprint replaces the old one and its clips.
        shutil.rmtree(_clips_dir(root, session_id), ignore_errors=True)
        for out, c in zip(speakers_out, candidates):
            out["clips"] = _cut_clips(wav_paths.get(c["track"]), c["turns"], _clips_dir(root, session_id), _slug(c["label"]))
        _put_vp(root, {"session_id": session_id, "job_id": job_id, "created": time.time(), "speakers": speakers_out})
        _gc(root, data)
        _save(root, data)


# -- read-time overlay ----------------------------------------------------------------------------------------


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


def named_labels(root, session_id: str, settings=None) -> dict:
    """``{label: name}`` for speakers linked to a person with a name (the self person counts once named)."""
    return {label: v["display"] for label, v in overlay(root, session_id, settings).items() if v["named"]}


def apply_names(root, session_id: str, segments, settings=None):
    """Segments (dicts with ``label``) with the labels of named people replaced by their names. The input is not
    modified; with nothing to apply it is returned as is."""
    mapping = named_labels(root, session_id, settings)
    if not mapping or not segments:
        return segments
    return [{**s, "label": mapping.get(s.get("label"), s.get("label"))} if isinstance(s, dict) else s for s in segments]


_MD_SPEAKER = re.compile(r"^(\*\*\[[^\]]+\] )(.+?)(:\*\* )", re.MULTILINE)


def apply_names_markdown(root, session_id: str, markdown: str, settings=None) -> str:
    """A rendered transcript (``**[mm:ss] Label:** text``) with the labels of named people replaced by their names."""
    mapping = named_labels(root, session_id, settings)
    if not mapping or not markdown:
        return markdown
    return _MD_SPEAKER.sub(lambda m: f"{m.group(1)}{mapping.get(m.group(2), m.group(2))}{m.group(3)}", markdown)


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
    return {
        "id": person["id"], "number": person.get("number"), "name": person.get("name") or "",
        "display": display_name(person), "email": person.get("email") or "", "company": person.get("company") or "",
        "is_self": bool(person.get("is_self")), "meetings": len({r["session_id"] for r in rows}),
        "talk_seconds": round(sum(r.get("speech_sec") or 0.0 for r in rows), 1),
        "last_heard": max(created) if created else None,
        "suggestion": {"person_id": other["id"], "display": display_name(other)} if other else None,
    }


def list_people(store, sort: str = "talking", q: Optional[str] = None) -> list:
    with _lock:
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
                "suggested_display": display_name(other) if other else None})
        samples = []
        for r in sorted(mine, key=lambda r: -(r.get("speech_sec") or 0.0)):
            name = next((n for n in r.get("clips") or []
                         if (_clips_dir(store.root, r["session_id"]) / n).is_file()), None)
            if name and len(samples) < MAX_CLIPS:
                samples.append({"session_id": r["session_id"], "name": name,
                                "url": f"/v1/people/{person_id}/clips/{r['session_id']}/{name}"})
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
    """Set name / email / company (any subset). ValueError on a bad email; PersonNotFound."""
    root = _root(root)
    with _lock:
        data = _load(root)
        person = _by_id(data, person_id)
        if person is None:
            raise PersonNotFound(person_id)
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
        return dict(person)


def merge_people(root, source_id: str, into_id: str) -> dict:
    """Move all of ``source``'s appearances to ``into`` (as confirmed), fill ``into``'s blank fields, delete source."""
    root = _root(root)
    if source_id == into_id:
        raise ValueError("cannot merge a person into themselves")
    with _lock:
        data = _load(root)
        source, target = _by_id(data, source_id), _by_id(data, into_id)
        if source is None or target is None:
            raise PersonNotFound(source_id if source is None else into_id)
        if source.get("is_self"):
            raise ValueError("the profile for you cannot be merged into another one")
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
        for key in ("name", "email", "company"):
            if not target.get(key) and source.get(key):
                target[key] = source[key]
        target["updated"] = time.time()
        data["people"] = [p for p in data["people"] if p["id"] != source_id]
        _save(root, data)
        return dict(target)


def appearance_action(root, session_id: str, label: str, action: str, person_id: Optional[str] = None) -> dict:
    """``confirm`` (accept the suggestion), ``reject`` ("not them": drop the suggestion), ``assign`` (to
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
        if action == "reject":
            a["suggested_person_id"] = None
            if a.get("link") == "suggested":
                a["link"] = "auto"
        elif action == "confirm":
            if not a.get("suggested_person_id") or _by_id(data, a["suggested_person_id"]) is None:
                raise ValueError("there is no suggestion to confirm")
            a.update(person_id=a["suggested_person_id"], link="confirmed", suggested_person_id=None)
        elif action == "assign":
            if _by_id(data, person_id) is None:
                raise PersonNotFound(str(person_id))
            a.update(person_id=person_id, link="confirmed", suggested_person_id=None)
        else:
            a.update(person_id=_new_person(data)["id"], link="confirmed", suggested_person_id=None)
        _put_vp(root, vp)
        _gc(root, data)
        _save(root, data)
        return dict(a)


def delete_person(root, person_id: str) -> None:
    """Delete the profile; its appearances stay but show their plain labels again."""
    root = _root(root)
    with _lock:
        data = _load(root)
        if _by_id(data, person_id) is None:
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
