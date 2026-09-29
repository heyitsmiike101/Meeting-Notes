"""One error type for the whole agent surface (REST and MCP alike).

Every failure carries an HTTP-ish ``status``, a stable machine ``code`` and a
human ``detail``. REST turns it into ``{"detail": ..., "code": ...}`` with that
status; the MCP tools turn the same pair into a tool error whose text is that
JSON body, so an agent branches on ``code`` whichever transport it used.
"""

from __future__ import annotations

import json


class AgentError(Exception):
    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail

    def body(self) -> dict:
        return {"detail": self.detail, "code": self.code}

    def json(self) -> str:
        return json.dumps(self.body())


def bad_request(detail: str, code: str = "bad_request") -> AgentError:
    return AgentError(400, code, detail)


def not_found(detail: str, code: str = "not_found") -> AgentError:
    return AgentError(404, code, detail)
