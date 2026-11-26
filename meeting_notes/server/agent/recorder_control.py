"""Recorder list + start/stop, shared by the REST routes and the MCP tools.

The hub lives on ``app.state.recorder_hub`` and is installed after agent access,
so callers pass it lazily. ``send_command`` is a coroutine; both REST handlers
and MCP tool handlers already run on the server's event loop, so they simply
``await`` it (no thread bridge is needed).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ... import remote
from .errors import AgentError, bad_request

COMMANDS = ("start", "stop")  # the only commands the control scope may send


def require_hub(hub):
    if hub is None:
        raise AgentError(503, "recorders_unavailable", "the recorder hub is not running")
    return hub


def list_recorders(hub) -> Dict[str, Any]:
    items: List[dict] = []
    for item in require_hub(hub).list_items():
        state = item.get("state") or {}
        meeting = state.get("meeting") or {}
        items.append({
            "id": item.get("instance_id"),
            "device": item.get("device"),
            "status": state.get("status") or "idle",
            "name": meeting.get("name") or None,
        })
    return {"items": items}


def check_command(recorder_id: str, command: Any, args: Any):
    """Validate; returns ``(command, clean_args)`` or raises a 400 AgentError."""
    if not remote.valid_instance_id(recorder_id):
        raise bad_request("invalid recorder id", "invalid_recorder_id")
    if command not in COMMANDS:
        raise bad_request("command must be 'start' or 'stop'", "command_not_allowed")
    try:
        return remote.clean_command(command, args)
    except ValueError as exc:
        raise bad_request(str(exc), "invalid_args")


async def send(hub, recorder_id: str, command: Any, args: Optional[Any] = None) -> Dict[str, Any]:
    from ..recorders import CommandTimeout, RecorderGone

    command, args = check_command(recorder_id, command, args)
    try:
        return await require_hub(hub).send_command(recorder_id, command, args)
    except KeyError:
        raise AgentError(404, "recorder_not_connected", "recorder is not connected")
    except BufferError:
        raise AgentError(429, "recorder_busy", "the recorder is still busy with earlier commands")
    except CommandTimeout:
        raise AgentError(504, "recorder_timeout", "the recorder did not answer in time")
    except RecorderGone:
        raise AgentError(409, "recorder_gone", "the recorder disconnected")
