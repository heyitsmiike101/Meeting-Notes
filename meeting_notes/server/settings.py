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
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List
from urllib.parse import urlparse

from ..transcribe.faster_whisper_backend import MODEL_CHOICES

DEFAULT_BEAM_SIZE = 5
DEFAULT_AUDIO_RETENTION_DAYS = -1  # keep forever
DEFAULT_RETENTION_CHECK_INTERVAL_MINUTES = 60
AI_PROVIDER_CHOICES = ("disabled", "codex", "ollama")
DEFAULT_OLLAMA_BASE_URL = "http://ollama:11434"
DEFAULT_OLLAMA_MODEL = "llama3.2"

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
    # Meeting-note generation is opt-in. ``codex`` uses the authenticated
    # server-side bridge; ``ollama`` uses an OpenAI-compatible local endpoint.
    ai_provider: str = "codex"
    ollama_base_url: str = DEFAULT_OLLAMA_BASE_URL
    ollama_model: str = DEFAULT_OLLAMA_MODEL

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

    def to_dict(self) -> dict:
        d = asdict(self)
        d["model_choices"] = self.model_choices()
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
        ollama_base_url=ollama_base_url,
        ollama_model=str(raw.get("ollama_model") or defaults.ollama_model),
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
        raise ValidationError("ai_provider must be disabled, codex, or ollama")
    ollama_base_url = str(fields.get("ollama_base_url") or DEFAULT_OLLAMA_BASE_URL).strip().rstrip("/")
    parsed_ollama = urlparse(ollama_base_url)
    if ai_provider == "ollama":
        if parsed_ollama.scheme not in ("http", "https") or not parsed_ollama.netloc:
            raise ValidationError("ollama_base_url must be a valid http:// or https:// URL")
    ollama_model = str(fields.get("ollama_model") or DEFAULT_OLLAMA_MODEL).strip()
    if ai_provider == "ollama" and not ollama_model:
        raise ValidationError("ollama_model is required when ai_provider is ollama")

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
        ollama_base_url=ollama_base_url,
        ollama_model=ollama_model,
    )
