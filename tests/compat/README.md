# Client compatibility tests

Promise: the server keeps working with the **current recorder and the five
releases before it** (`compat.SUPPORTED_CLIENT_WINDOW = 5` in
`meeting_notes/server/compat.py`). These tests are how that promise is enforced.

## How it works

`clients/vX_Y_Z/` holds a **frozen copy of one released recorder's network
layer**: `wire.py`, `api.py`, `queue.py`, `streamer.py`, `update.py`,
`resample.py`, and (0.7.0 and later) `logs_send.py` (only `send_zip`), plus
`identity.py` and `version_gate.py` (0.7.4 and later, which send the client header). They are
verbatim copies from that release's git commit (commit id in each folder's
`__init__.py`); only the `meeting_notes.*` imports are rewritten to be
package-relative, and each file says so in its header.

`test_compat_contract.py` is parametrized over every folder. It starts the
CURRENT server on a real socket and drives it with each old client's real code:
health, update manifest parse and installer download, live websocket stream
(hello, audio, acks, bad token, bad session id), pipeline PUT, track uploads,
finalize (and its retry), job polling, transcript fetch, the offline queue
worker end to end (including wrong token and server down), the History window's
list/detail/retranscribe/delete-audio/delete calls, the connection check,
import-recording upload, client-log upload (0.7.0+), and the macOS manifest,
package and `mac.sh` installer (0.7.5+, the first macOS client).

`test_compat_policy.py` covers the server side: window arithmetic, the lenient
`X-Meeting-Notes-Client` header, HTTP 426, the `/v1/clients` registry.
`test_compat_release_checklist.py` fails a release that is not registered or has
no fixture.

**If a contract test fails, the server change broke a recorder that is in the
field. Fix the server. Never edit a frozen client to make a test pass.**

## The 426 rule

A recorder reports `X-Meeting-Notes-Client: <version>; <platform>`. Every
release up to 0.7.3 sends nothing, and a missing or unparseable header is a
*legacy* client: **never rejected**. A recorder that reports a version older
than the window gets `426` with `{"detail": ..., "min_client_version": ...}`
only when it tries to **start** a new upload (pipeline PUT or track upload for
an unknown session, `POST /v1/uploads`, or a live stream for an unknown
session). Finalize, job polling, history, logs and any session already on the
server are never refused, and the recording stays on the recorder's disk, so
nothing is lost: it uploads after the update.

## Adding a release (do this in the release change, before tagging)

1. Bump `meeting_notes.__version__` (as usual).
2. Add the new version to `RELEASES` in `meeting_notes/server/compat.py`.
3. Freeze the client. From the release commit `<sha>`, create
   `tests/compat/clients/vX_Y_Z/` with `__init__.py` (`__version__ = "X.Y.Z"`,
   `COMMIT = "<sha>"`) and copy, via `git show <sha>:<path>`:
   `meeting_notes/wire.py` and `meeting_notes/client/{api,queue,streamer,update,resample}.py`,
   rewriting `from meeting_notes import wire|__version__` to `from . import ...`,
   `from meeting_notes.client.api|resample import` to `from .api|.resample import`,
   and `from meeting_notes.wire import` to `from .wire import`. Add
   `identity.py` and `version_gate.py` (0.7.4+; same rewrites), and
   `logs_send.py` with `SendResult` and `send_zip` from `client/logs.py`
   (see an existing folder for the exact shape). Keep the header comment.
4. Run the compat tests. A failure means the current server no longer serves an
   old recorder (or, for a new release, that the new client needs a new
   scenario in `test_compat_contract.py`).

## How the window rolls

The window is the newest release plus the `SUPPORTED_CLIENT_WINDOW` before it.
Adding a release moves `min_client_version` up by one (published in `/health`
and `/install/client-manifest.json`); the fixture that falls out of the window
may then be deleted (or kept: the checklist only requires fixtures for
supported versions). Dropping support for a version is a deliberate act: it
means lowering `SUPPORTED_CLIENT_WINDOW` or deleting old entries from
`RELEASES`, in a commit that says so.
