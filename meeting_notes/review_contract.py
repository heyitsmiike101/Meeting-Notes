"""Dependency-free contract shared by the web server and Codex bridge."""

from __future__ import annotations

import json
from importlib import resources
from typing import Any, Dict


class ReviewValidationError(ValueError):
    """Raised when generated meeting notes do not match the public contract."""


def _resource_text(name: str) -> str:
    return resources.files("meeting_notes").joinpath("bridge", name).read_text(encoding="utf-8")


def output_schema() -> Dict[str, Any]:
    return json.loads(_resource_text("meeting_notes.schema.json"))


def workflow_text() -> str:
    return _resource_text("workflow.md")


def _string(value: Any, name: str, *, empty: bool = True) -> None:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ReviewValidationError(f"notes.{name} must be a string")


def validate_notes(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ReviewValidationError("notes must be a JSON object")
    required = {
        "title", "summary", "meeting_notes", "participants", "key_points",
        "decisions", "action_items", "open_questions", "risks", "next_steps",
    }
    unknown = sorted(set(value) - required)
    missing = sorted(required - set(value))
    if unknown:
        raise ReviewValidationError(f"notes has unknown fields: {', '.join(unknown)}")
    if missing:
        raise ReviewValidationError(f"notes is missing: {', '.join(missing)}")
    _string(value["title"], "title", empty=False)
    _string(value["summary"], "summary", empty=False)
    _string(value["meeting_notes"], "meeting_notes")
    for key in (
        "participants", "key_points", "decisions", "open_questions", "risks", "next_steps"
    ):
        if not isinstance(value[key], list) or not all(isinstance(item, str) for item in value[key]):
            raise ReviewValidationError(f"notes.{key} must be a list of strings")
    if not isinstance(value["action_items"], list):
        raise ReviewValidationError("notes.action_items must be a list")
    for item in value["action_items"]:
        if not isinstance(item, dict) or set(item) != {"task", "owner", "due"}:
            raise ReviewValidationError("each action item must contain only task, owner, and due")
        _string(item["task"], "action_items.task", empty=False)
        for key in ("owner", "due"):
            if item[key] is not None and not isinstance(item[key], str):
                raise ReviewValidationError(f"action item {key} must be a string or null")
    # Copy through JSON to prevent callers from persisting custom container
    # objects even if they happen to pass the type checks above.
    return json.loads(json.dumps(value, ensure_ascii=False))
