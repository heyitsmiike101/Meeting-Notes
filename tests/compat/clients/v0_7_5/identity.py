# COMPAT FIXTURE - do not edit. Verbatim copy of meeting_notes/client/identity.py from the 0.7.5 client
# (release/0.7.5), with only the meeting_notes.* imports rewritten to be package-relative.
"""Who the client says it is on every request to the server.

The server reads ``X-Meeting-Notes-Client: <version>; <platform>`` to know which
client versions are still in the field and, if it wants, to refuse ones that are
too old (see ``version_gate``). It is sent on every HTTP request (uploads, auth
check, updater) and on the live-stream websocket handshake.
"""

from __future__ import annotations

import platform
import re
from typing import Dict

from . import __version__

HEADER = "X-Meeting-Notes-Client"


def client_header_value() -> str:
    try:
        plat = f"{platform.system()} {platform.release()}".strip() or "unknown"
    except Exception:  # noqa: BLE001 - never let identity break a request
        plat = "unknown"
    # HTTP header values must be plain ASCII without control characters.
    plat = re.sub(r"[^\x20-\x7e]", "", plat).strip() or "unknown"
    return f"{__version__}; {plat}"


def client_headers() -> Dict[str, str]:
    return {HEADER: client_header_value()}
