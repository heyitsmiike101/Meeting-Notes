"""Server-wide settings, persisted at ``<data_root>/settings.json``.

Environment variables (``MEETING_NOTES_MODEL`` etc.) remain the *bootstrap*
defaults for a fresh install -- they are what ``Settings()`` falls back to
when no file exists yet -- but once an operator saves settings through the
web UI or ``PUT /v1/settings``, the file wins from then on, even across a
container restart where the env var reasserts its original value. This
mirrors how ``auth.py`` treats ``MEETING_NOTES_TOKEN``: the env var is a
convenient way to configure a fresh deployment, not a permanent source of
truth that overrides an operator's later, explicit choice.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from importlib import resources
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

from ..transcribe.faster_whisper_backend import MODEL_CHOICES
from .notion_api import normalize_page_id

DEFAULT_BEAM_SIZE = 5
DEFAULT_AUDIO_RETENTION_DAYS = -1  # keep forever
DEFAULT_RETENTION_CHECK_INTERVAL_MINUTES = 60
AI_PROVIDER_CHOICES = ("disabled", "codex", "claude", "ollama")
APPEARANCE_CHOICES = ("system", "light", "dark")
DEFAULT_APPEARANCE = "system"
_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9._\-\[\]]{1,80}$")
DEFAULT_OLLAMA_BASE_URL = "http://ollama:11434"
DEFAULT_OLLAMA_MODEL = "llama3.2"
MAX_AI_WORKFLOW_CHARS = 100_000

# -- note templates ("note types") ------------------------------------------
# A template is {"id", "name", "prompt"}. ``standard`` is special: its prompt is
# the long-standing ``ai_workflow`` setting (kept readable/writable so older
# tooling and a rollback keep working), so it is never stored in
# ``note_templates``. The other built-ins live in ``note_templates`` (prompt
# editable, cannot be deleted) next to any user templates. Every name is
# editable, built-ins included: Standard's lives in ``standard_name``, the other
# built-ins keep theirs in ``note_templates``; ids never change.
STANDARD_TEMPLATE_ID = "standard"
MAX_TEMPLATE_NAME_CHARS = 60
MAX_USER_TEMPLATES = 30
_TEMPLATE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
# id -> (display name, packaged prompt file)
BUILTIN_TEMPLATES = {
    STANDARD_TEMPLATE_ID: ("Standard", "workflow.md"),
    "quick": ("Quick notes", "notes_quick.md"),
    "webinar": ("Detailed webinar", "notes_webinar.md"),
}

# One process-wide lock around the read-modify-write of settings.json.
# Concurrent saves are rare (this comes from a human filling out a form, or
# an occasional API call) but a torn write would corrupt every setting, not
# just the one being changed.
_lock = threading.Lock()


def _default_model() -> str:
    # Deliberately NOT falling back to MODEL_CHOICES[0]: an unset
    # MEETING_NOTES_MODEL means "transcription isn't configured yet" (see
    # app.py's create_app), and defaulting to a real model here would quietly
    # turn transcription on for an operator who never asked for it.
    return os.environ.get("MEETING_NOTES_MODEL") or ""


def _default_diarization_enabled() -> bool:
    return os.environ.get("MEETING_NOTES_DIARIZATION", "").strip().lower() in (
        "1", "true", "on", "yes"
    )


def _default_server_address() -> str:
    return os.environ.get("MEETING_NOTES_SERVER_ADDRESS", "").strip().rstrip("/")


def _default_ai_workflow() -> str:
    """The editable workflow seeded for a fresh installation.

    It lives beside the bridge schema so a packaged install and a source
    checkout get the same instructions.  Once saved, the operator's copy in
    settings.json becomes the source used for all subsequently claimed jobs.
    """
    return resources.files("meeting_notes").joinpath("bridge", "workflow.md").read_text(
        encoding="utf-8"
    )


def _packaged_prompt(filename: str) -> str:
    return (
        resources.files("meeting_notes").joinpath("bridge", filename).read_text(encoding="utf-8").strip()
    )


def _builtin_extra_templates() -> List[dict]:
    """The built-in templates other than Standard, with their packaged prompts."""
    return [
        {"id": tid, "name": name, "prompt": _packaged_prompt(filename)}
        for tid, (name, filename) in BUILTIN_TEMPLATES.items()
        if tid != STANDARD_TEMPLATE_ID
    ]


@dataclass
class Settings:
    model: str = field(default_factory=_default_model)
    beam_size: int = DEFAULT_BEAM_SIZE
    # -1 = keep forever, 0 = delete as soon as the transcript is done,
    # N = delete N days after the session's created date. See retention.py
    # for the rules this drives.
    audio_retention_days: int = DEFAULT_AUDIO_RETENTION_DAYS
    delete_audio_only_after_success: bool = True
    retention_check_interval_minutes: int = DEFAULT_RETENTION_CHECK_INTERVAL_MINUTES
    diarization_enabled: bool = field(default_factory=_default_diarization_enabled)
    diarization_model: str = "pyannote/speaker-diarization-community-1"
    diarization_min_speakers: int = 1
    diarization_max_speakers: int = 8
    # Public/LAN address embedded into the generated client installer. Blank
    # means infer it from the browser request that downloads the installer.
    server_address: str = field(default_factory=_default_server_address)
    # ``disabled`` turns meeting notes off. ``codex`` uses the authenticated
    # server-side bridge; ``claude`` uses the Claude Code CLI's subscription
    # login; ``ollama`` uses an OpenAI-compatible local endpoint.
    ai_provider: str = "codex"
    # A NEW meeting gets notes queued once its first transcript finishes when
    # its note type is listed in ``auto_notes_types`` (a per-type choice) and the
    # provider is not "disabled". Types left out still get notes from the
    # Generate button. (The old global ``auto_generate_notes`` setting is gone;
    # a stale key in settings.json or an API call is ignored.)
    auto_notes_types: list = field(default_factory=lambda: list(BUILTIN_TEMPLATES))
    # Blank means use the authenticated Codex account's default model.
    codex_model: str = ""
    # Blank means use the Claude subscription account's default model.
    claude_model: str = ""
    ollama_base_url: str = DEFAULT_OLLAMA_BASE_URL
    ollama_model: str = DEFAULT_OLLAMA_MODEL
    # Instructions sent with every meeting-notes job.  This does not grant
    # the AI permission to rename a session; title is only a notes-summary
    # field in the review contract.
    ai_workflow: str = field(default_factory=_default_ai_workflow)
    # Display name of the Standard note type (its id stays "standard").
    standard_name: str = BUILTIN_TEMPLATES[STANDARD_TEMPLATE_ID][0]
    # Templates other than Standard: the built-ins ("quick", "webinar") plus any
    # user-created ones. Standard's prompt is ``ai_workflow`` above.
    note_templates: List[dict] = field(default_factory=_builtin_extra_templates)
    # Used by automatic notes, the meetings-list Generate button, and any request
    # that does not name a template.
    default_template_id: str = STANDARD_TEMPLATE_ID
    # Web UI theme: "system" follows the browser's light/dark preference.
    appearance: str = DEFAULT_APPEARANCE
    # Notion export (see notion.py). The integration token is NOT a setting: it
    # lives in its own file and never appears here or in any API response.
    # ``notion_parents`` maps a note type id (Standard included) to the 32-hex
    # id of the Notion page its monthly pages are created under; a style with no
    # entry is not copied. ``notion_auto_types`` lists the note type ids whose
    # notes are copied as they complete (a per-type choice). ``notion_auto_copy``
    # is derived from it (true when any type auto-copies) and kept for API callers.
    notion_auto_copy: bool = False
    notion_parents: dict = field(default_factory=dict)
    notion_auto_types: list = field(default_factory=list)

    def auto_notes_for(self, template_id) -> bool:
        """True when meetings of this note type get notes without a click."""
        return str(template_id) in (self.auto_notes_types or [])

    def model_choices(self) -> List[str]:
        """The curated list, plus whatever model is actually configured.

        A model chosen before it was curated (or set only via env, e.g. a
        locally fine-tuned name) must still appear as the selected option in
        the settings form instead of silently vanishing from the list.
        """
        choices = list(MODEL_CHOICES)
        if self.model and self.model not in choices:
            choices.append(self.model)
        return choices

    def all_templates(self) -> List[dict]:
        """Every template, Standard first: ``{id, name, prompt, builtin}``."""
        out = [{
            "id": STANDARD_TEMPLATE_ID,
            "name": self.standard_name,
            "prompt": self.ai_workflow,
            "builtin": True,
        }]
        for t in self.note_templates:
            out.append({**t, "builtin": t["id"] in BUILTIN_TEMPLATES})
        return out

    def find_template(self, ref) -> Optional[dict]:
        """Look a template up by id, else by case-insensitive name."""
        if not isinstance(ref, str) or not ref.strip():
            return None
        ref = ref.strip()
        templates = self.all_templates()
        for t in templates:
            if t["id"] == ref:
                return t
        lowered = ref.lower()
        for t in templates:
            if t["name"].lower() == lowered:
                return t
        return None

    def default_template(self) -> dict:
        return self.find_template(self.default_template_id) or self.all_templates()[0]

    def review_template(self, review: dict) -> Optional[dict]:
        """``{id, name}`` of the note type a review used, or None for legacy records.

        The live name wins while the template exists (so a rename shows up);
        once it is deleted the name stored on the review is used.
        """
        tid = review.get("template_id")
        if not tid:
            return None
        live = self.find_template(tid)
        return {"id": tid, "name": (live or {}).get("name") or review.get("template_name") or tid}

    def to_dict(self) -> dict:
        d = asdict(self)
        d["model_choices"] = self.model_choices()
        # The read-only resolved view (Standard included, flags added) for
        # clients that just want to list styles. ``note_templates`` stays the
        # writable list; Standard is edited through ``ai_workflow``.
        d["templates"] = self.all_templates()
        return d


class ValidationError(ValueError):
    """A settings field failed validation -- surfaced directly on the
    settings form, or as a 400 from PUT /v1/settings."""


def _settings_path(data_root) -> Path:
    return Path(data_root) / "settings.json"


def load_settings(data_root) -> Settings:
    path = _settings_path(data_root)
    if not path.exists():
        return Settings()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # A corrupt file must not take the whole server down -- fall back to
        # defaults, same spirit as store.py's _read_ranges.
        return Settings()
    if not isinstance(raw, dict):
        return Settings()
    defaults = Settings()
    ai_provider = str(raw.get("ai_provider") or defaults.ai_provider).strip().lower()
    if ai_provider not in AI_PROVIDER_CHOICES:
        ai_provider = defaults.ai_provider
    ollama_base_url = str(raw.get("ollama_base_url") or defaults.ollama_base_url).strip().rstrip("/")
    parsed_ollama = urlparse(ollama_base_url)
    if parsed_ollama.scheme not in ("http", "https") or not parsed_ollama.netloc:
        ollama_base_url = defaults.ollama_base_url
    appearance = str(raw.get("appearance") or defaults.appearance).strip().lower()
    if appearance not in APPEARANCE_CHOICES:
        appearance = defaults.appearance
    ai_workflow = _workflow_or(raw.get("ai_workflow"), defaults.ai_workflow)
    standard_name = _name_or_default(raw.get("standard_name"), BUILTIN_TEMPLATES[STANDARD_TEMPLATE_ID][0])
    note_templates = _load_templates(raw.get("note_templates"), standard_name)
    default_template_id = str(raw.get("default_template_id") or "").strip()
    if default_template_id != STANDARD_TEMPLATE_ID and default_template_id not in {
        t["id"] for t in note_templates
    }:
        default_template_id = STANDARD_TEMPLATE_ID
    notion_parents = _clean_notion_parents(raw.get("notion_parents"), note_templates)
    if "notion_auto_types" in raw:
        try:
            notion_auto_types = _clean_auto_types(raw.get("notion_auto_types"), note_templates)
        except ValidationError:
            notion_auto_types = []
    else:
        # Before 0.7.9 auto-copy was one switch for every type: carry it over.
        notion_auto_types = _all_type_ids(note_templates) if _coerce_bool(raw.get("notion_auto_copy", False)) else []
    if "auto_notes_types" in raw:
        try:
            auto_notes_types = _clean_auto_types(raw.get("auto_notes_types"), note_templates, "auto_notes_types")
        except ValidationError:
            auto_notes_types = _all_type_ids(note_templates)
    else:
        # Before this setting every meeting got notes: keep every type on.
        auto_notes_types = _all_type_ids(note_templates)
    return Settings(
        model=str(raw.get("model") or defaults.model),
        beam_size=_int_or(raw.get("beam_size"), defaults.beam_size),
        audio_retention_days=_int_or(raw.get("audio_retention_days"), defaults.audio_retention_days),
        delete_audio_only_after_success=bool(
            raw.get("delete_audio_only_after_success", defaults.delete_audio_only_after_success)
        ),
        retention_check_interval_minutes=_int_or(
            raw.get("retention_check_interval_minutes"), defaults.retention_check_interval_minutes
        ),
        diarization_enabled=bool(raw.get("diarization_enabled", defaults.diarization_enabled)),
        diarization_model=str(raw.get("diarization_model") or defaults.diarization_model),
        diarization_min_speakers=_int_or(
            raw.get("diarization_min_speakers"), defaults.diarization_min_speakers
        ),
        diarization_max_speakers=_int_or(
            raw.get("diarization_max_speakers"), defaults.diarization_max_speakers
        ),
        server_address=str(raw.get("server_address") or defaults.server_address),
        ai_provider=ai_provider,
        codex_model=str(raw.get("codex_model") or defaults.codex_model).strip(),
        claude_model=str(raw.get("claude_model") or defaults.claude_model).strip(),
        ollama_base_url=ollama_base_url,
        ollama_model=str(raw.get("ollama_model") or defaults.ollama_model),
        ai_workflow=ai_workflow,
        standard_name=standard_name,
        note_templates=note_templates,
        default_template_id=default_template_id,
        appearance=appearance,
        notion_auto_copy=bool(notion_auto_types),
        notion_parents=notion_parents,
        notion_auto_types=notion_auto_types,
        auto_notes_types=auto_notes_types,
    )


def save_settings(data_root, settings: Settings) -> None:
    path = _settings_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with _lock:
        tmp.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")
        os.replace(tmp, path)


def _int_or(value, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _workflow_or(value, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    value = value.strip()
    if not value or len(value) > MAX_AI_WORKFLOW_CHARS:
        return fallback
    return value


def _name_or_default(value, default: str) -> str:
    name = value.strip() if isinstance(value, str) else ""
    return name if name and len(name) <= MAX_TEMPLATE_NAME_CHARS else default


def _load_templates(raw, standard_name: str = BUILTIN_TEMPLATES[STANDARD_TEMPLATE_ID][0]) -> List[dict]:
    """Tolerant load of the stored ``note_templates``.

    A settings.json written before templates existed has no such key: the
    built-ins are seeded. Malformed entries are skipped rather than failing the
    whole load, and a missing built-in is restored (they cannot be deleted).
    """
    by_id: dict = {}
    order: List[str] = []
    seen_names: set = {standard_name.lower()}
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            tid = str(item.get("id") or "").strip()
            name = str(item.get("name") or "").strip()
            prompt = item.get("prompt")
            if (
                tid == STANDARD_TEMPLATE_ID
                or tid in by_id
                or not _TEMPLATE_ID_RE.match(tid)
                or not isinstance(prompt, str)
                or not prompt.strip()
                or len(prompt.strip()) > MAX_AI_WORKFLOW_CHARS
            ):
                continue
            if tid in BUILTIN_TEMPLATES:
                # Built-ins are never dropped: a bad or clashing stored name falls back to the default.
                if not name or len(name) > MAX_TEMPLATE_NAME_CHARS or name.lower() in seen_names:
                    name = BUILTIN_TEMPLATES[tid][0]
                seen_names.add(name.lower())
            else:
                if not name or len(name) > MAX_TEMPLATE_NAME_CHARS or name.lower() in seen_names:
                    continue
                seen_names.add(name.lower())
            by_id[tid] = {"id": tid, "name": name, "prompt": prompt.strip()}
            order.append(tid)
    return _with_builtins(by_id, order)


def _with_builtins(by_id: dict, order: List[str]) -> List[dict]:
    """Built-ins first in canonical order (restored if absent), then users in order."""
    out = []
    for tid, (name, filename) in BUILTIN_TEMPLATES.items():
        if tid == STANDARD_TEMPLATE_ID:
            continue
        out.append(by_id.get(tid) or {"id": tid, "name": name, "prompt": _packaged_prompt(filename)})
    out += [by_id[tid] for tid in order if tid not in BUILTIN_TEMPLATES]
    return out


def _validate_templates(value, standard_name: str = BUILTIN_TEMPLATES[STANDARD_TEMPLATE_ID][0]) -> List[dict]:
    """Strictly validate submitted ``note_templates`` (raises ``ValidationError``)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            raise ValidationError("note_templates must be valid JSON")
    if not isinstance(value, list):
        raise ValidationError("note_templates must be a list")
    by_id: dict = {}
    order: List[str] = []
    names: set = {standard_name.lower()}
    users = 0
    for item in value:
        if not isinstance(item, dict):
            raise ValidationError("each note template must be an object")
        tid = str(item.get("id") or "").strip()
        if tid == STANDARD_TEMPLATE_ID:
            # Standard is edited through ai_workflow; an echoed entry is ignored.
            continue
        prompt = item.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            label = str(item.get("name") or tid or "template").strip()
            raise ValidationError(f"prompt is required for note template '{label}'")
        prompt = prompt.strip()
        if len(prompt) > MAX_AI_WORKFLOW_CHARS:
            raise ValidationError(f"prompt must be {MAX_AI_WORKFLOW_CHARS} characters or fewer")
        name = item.get("name")
        name = name.strip() if isinstance(name, str) else ""
        if tid in BUILTIN_TEMPLATES:
            if tid in by_id:
                raise ValidationError(f"duplicate note template id: {tid}")
            if not name:
                name = BUILTIN_TEMPLATES[tid][0]  # an older client that omits the name
            elif len(name) > MAX_TEMPLATE_NAME_CHARS:
                raise ValidationError(
                    f"note template name must be {MAX_TEMPLATE_NAME_CHARS} characters or fewer"
                )
            if name.lower() in names:
                raise ValidationError(f"note template name already in use: {name}")
            names.add(name.lower())
            by_id[tid] = {"id": tid, "name": name, "prompt": prompt}
            order.append(tid)
            continue
        if not name:
            raise ValidationError("note template name is required")
        if len(name) > MAX_TEMPLATE_NAME_CHARS:
            raise ValidationError(
                f"note template name must be {MAX_TEMPLATE_NAME_CHARS} characters or fewer"
            )
        if name.lower() in names:
            raise ValidationError(f"note template name already in use: {name}")
        names.add(name.lower())
        if not tid:
            tid = "t" + uuid.uuid4().hex[:12]
        elif not _TEMPLATE_ID_RE.match(tid):
            raise ValidationError(f"invalid note template id: {tid!r}")
        if tid in by_id:
            raise ValidationError(f"duplicate note template id: {tid}")
        users += 1
        if users > MAX_USER_TEMPLATES:
            raise ValidationError(f"at most {MAX_USER_TEMPLATES} custom note templates are allowed")
        by_id[tid] = {"id": tid, "name": name, "prompt": prompt}
        order.append(tid)
    for tid, (default, _file) in BUILTIN_TEMPLATES.items():
        if tid != STANDARD_TEMPLATE_ID and tid not in by_id:
            # An omitted built-in comes back under its default name, which must not clash.
            if default.lower() in names:
                raise ValidationError(f"note template name already in use: {default}")
            names.add(default.lower())
    return _with_builtins(by_id, order)


def _all_type_ids(templates: List[dict]) -> list:
    return [STANDARD_TEMPLATE_ID] + [t["id"] for t in templates]


def _clean_auto_types(raw, templates: List[dict], field_name: str = "notion_auto_types") -> list:
    """Note type ids that auto-copy to Notion (or auto-generate notes): JSON text or a list; unknown ids are dropped."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except json.JSONDecodeError:
            raise ValidationError(f"{field_name} must be a JSON list of note type ids")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValidationError(f"{field_name} must be a list of note type ids")
    ids = _all_type_ids(templates)
    wanted = {str(x) for x in raw}
    return [tid for tid in ids if tid in wanted]


def _validated_auto_types(fields: dict, templates: List[dict]) -> list:
    """Per-type auto-copy from a save. ``notion_auto_types`` wins; the older single
    ``notion_auto_copy`` switch still works for callers that only change that: on with
    no types turns every type on, off turns every type off."""
    raw_types = fields.get("notion_auto_types")
    switch = fields.get("notion_auto_copy")
    if raw_types is None:
        return _all_type_ids(templates) if _coerce_bool(switch) else []
    types = _clean_auto_types(raw_types, templates)
    if switch is not None:
        on = _coerce_bool(switch)
        if on and not types:
            return _all_type_ids(templates)
        if not on:
            return []
    return types


def _clean_notion_parents(raw, templates: List[dict]) -> dict:
    """Tolerant load: keep only well-formed ids for styles that still exist."""
    ids = {STANDARD_TEMPLATE_ID} | {t["id"] for t in templates}
    out: dict = {}
    if isinstance(raw, dict):
        for tid, value in raw.items():
            if tid in ids and isinstance(value, str):
                try:
                    out[tid] = normalize_page_id(value)
                except ValueError:
                    continue
    return out


def _validate_notion_parents(value, templates: List[dict], standard_name: str = BUILTIN_TEMPLATES[STANDARD_TEMPLATE_ID][0]) -> dict:
    """Strict: a bad page link raises ``ValidationError`` naming the style."""
    if isinstance(value, str):
        try:
            value = json.loads(value or "{}")
        except json.JSONDecodeError:
            raise ValidationError("notion_parents must be valid JSON")
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValidationError("notion_parents must be an object of style id to page link")
    names = {STANDARD_TEMPLATE_ID: standard_name}
    names.update({t["id"]: t["name"] for t in templates})
    out: dict = {}
    for tid, raw in value.items():
        if tid not in names:
            continue  # a style that was deleted in the same save
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            continue
        if not isinstance(raw, str):
            raise ValidationError(f"Notion page for '{names[tid]}' must be a link or id")
        try:
            out[tid] = normalize_page_id(raw)
        except ValueError as exc:
            raise ValidationError(f"Notion page for '{names[tid]}': {exc}")
    return out


def _require_int(value, field_name: str, *, minimum: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field_name} must be a whole number")
    if n < minimum:
        raise ValidationError(f"{field_name} must be {minimum} or greater")
    return n


def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "on", "yes")


def validate(fields: dict) -> Settings:
    """Validate a raw dict -- form-decoded strings or a JSON body -- into a
    ``Settings``. Raises ``ValidationError`` naming the first bad field, since
    that message is shown directly on the settings form (and returned as a
    400's detail from the JSON API).
    """
    model = str(fields.get("model") or "").strip()
    if not model:
        raise ValidationError("model is required")

    beam_size = _require_int(fields.get("beam_size"), "beam_size", minimum=1)
    audio_retention_days = _require_int(
        fields.get("audio_retention_days"), "audio_retention_days", minimum=-1
    )
    retention_check_interval_minutes = _require_int(
        fields.get("retention_check_interval_minutes"),
        "retention_check_interval_minutes",
        minimum=1,
    )
    delete_audio_only_after_success = _coerce_bool(fields.get("delete_audio_only_after_success"))
    diarization_enabled = _coerce_bool(fields.get("diarization_enabled"))
    diarization_model = str(fields.get("diarization_model") or "").strip()
    if diarization_enabled and not diarization_model:
        raise ValidationError("diarization_model is required when diarization is enabled")
    diarization_min_speakers = _require_int(
        fields.get("diarization_min_speakers", 1), "diarization_min_speakers", minimum=1
    )
    diarization_max_speakers = _require_int(
        fields.get("diarization_max_speakers", 8), "diarization_max_speakers", minimum=1
    )
    if diarization_max_speakers < diarization_min_speakers:
        raise ValidationError("diarization_max_speakers must be at least diarization_min_speakers")
    server_address = str(fields.get("server_address") or "").strip().rstrip("/")
    if server_address and not server_address.startswith(("http://", "https://")):
        raise ValidationError("server_address must start with http:// or https://")

    ai_provider = str(fields.get("ai_provider") or "codex").strip().lower()
    if ai_provider not in AI_PROVIDER_CHOICES:
        raise ValidationError("ai_provider must be disabled, codex, claude, or ollama")
    codex_model = str(fields.get("codex_model") or "").strip()
    claude_model = str(fields.get("claude_model") or "").strip()
    if claude_model and not _MODEL_NAME_RE.match(claude_model):
        raise ValidationError("claude_model must be a valid model name")
    ollama_base_url = str(fields.get("ollama_base_url") or DEFAULT_OLLAMA_BASE_URL).strip().rstrip("/")
    parsed_ollama = urlparse(ollama_base_url)
    if ai_provider == "ollama":
        if parsed_ollama.scheme not in ("http", "https") or not parsed_ollama.netloc:
            raise ValidationError("ollama_base_url must be a valid http:// or https:// URL")
    ollama_model = str(fields.get("ollama_model") or DEFAULT_OLLAMA_MODEL).strip()
    if ai_provider == "ollama" and not ollama_model:
        raise ValidationError("ollama_model is required when ai_provider is ollama")
    ai_workflow = fields.get("ai_workflow")
    if ai_workflow is None:
        ai_workflow = _default_ai_workflow()
    if not isinstance(ai_workflow, str) or not ai_workflow.strip():
        raise ValidationError("ai_workflow must not be empty")
    ai_workflow = ai_workflow.strip()
    if len(ai_workflow) > MAX_AI_WORKFLOW_CHARS:
        raise ValidationError(f"ai_workflow must be {MAX_AI_WORKFLOW_CHARS} characters or fewer")

    # Omitted note_templates (an older client) means "the built-ins, unchanged".
    standard_name = fields.get("standard_name")
    if standard_name is None:
        standard_name = BUILTIN_TEMPLATES[STANDARD_TEMPLATE_ID][0]
    standard_name = standard_name.strip() if isinstance(standard_name, str) else ""
    if not standard_name:
        raise ValidationError("note template name is required")
    if len(standard_name) > MAX_TEMPLATE_NAME_CHARS:
        raise ValidationError(f"note template name must be {MAX_TEMPLATE_NAME_CHARS} characters or fewer")
    raw_templates = fields.get("note_templates")
    note_templates = _validate_templates([] if raw_templates is None else raw_templates, standard_name)
    default_template_id = str(fields.get("default_template_id") or STANDARD_TEMPLATE_ID).strip()
    if default_template_id != STANDARD_TEMPLATE_ID and default_template_id not in {
        t["id"] for t in note_templates
    }:
        raise ValidationError("default_template_id must be the id of an existing note template")

    appearance = str(fields.get("appearance") or DEFAULT_APPEARANCE).strip().lower()
    if appearance not in APPEARANCE_CHOICES:
        raise ValidationError("appearance must be system, light, or dark")

    notion_parents = _validate_notion_parents(fields.get("notion_parents"), note_templates, standard_name)
    notion_auto_types = _validated_auto_types(fields, note_templates)
    raw_auto_notes = fields.get("auto_notes_types")
    if raw_auto_notes is None:
        auto_notes_types = _all_type_ids(note_templates)
    else:
        wanted = set(_clean_auto_types(raw_auto_notes, note_templates, "auto_notes_types"))
        # A note type created by this save (no id of its own yet) starts on.
        given = {str(t.get("id") or "").strip() for t in (raw_templates or []) if isinstance(t, dict)}
        wanted |= {t["id"] for t in note_templates if t["id"] not in given and t["id"] not in BUILTIN_TEMPLATES}
        auto_notes_types = [tid for tid in _all_type_ids(note_templates) if tid in wanted]
    return Settings(
        model=model,
        beam_size=beam_size,
        audio_retention_days=audio_retention_days,
        delete_audio_only_after_success=delete_audio_only_after_success,
        retention_check_interval_minutes=retention_check_interval_minutes,
        diarization_enabled=diarization_enabled,
        diarization_model=diarization_model or "pyannote/speaker-diarization-community-1",
        diarization_min_speakers=diarization_min_speakers,
        diarization_max_speakers=diarization_max_speakers,
        server_address=server_address,
        ai_provider=ai_provider,
        codex_model=codex_model,
        claude_model=claude_model,
        ollama_base_url=ollama_base_url,
        ollama_model=ollama_model,
        ai_workflow=ai_workflow,
        standard_name=standard_name,
        note_templates=note_templates,
        default_template_id=default_template_id,
        appearance=appearance,
        notion_auto_copy=bool(notion_auto_types),
        notion_parents=notion_parents,
        notion_auto_types=notion_auto_types,
        auto_notes_types=auto_notes_types,
    )
