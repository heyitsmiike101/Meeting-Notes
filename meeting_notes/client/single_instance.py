"""One Meeting Notes per Windows user.

The first instance listens on a per-user local socket (a named pipe on Windows). A second launch connects, says
``activate`` (so the first one shows its window) and exits; with ``--tray`` it just exits.
"""

from __future__ import annotations

import getpass
import logging
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

log = logging.getLogger("meeting_notes.client.single_instance")

ACTIVATE = b"activate\n"
PING = b"ping\n"
CONNECT_TIMEOUT_MS = 800


def server_name(suffix: str = "") -> str:
    """The per-user socket name (``MEETING_NOTES_INSTANCE_SUFFIX`` / ``suffix`` keeps tests apart)."""
    import os

    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = "user"
    safe = "".join(c if c.isalnum() else "_" for c in user)
    extra = suffix or os.environ.get("MEETING_NOTES_INSTANCE_SUFFIX", "")
    return f"MeetingNotes-{safe}{('-' + extra) if extra else ''}"


class SingleInstance(QObject):
    """``acquire()`` is True for the first instance (it then emits ``activate_requested`` on a second launch)."""

    activate_requested = Signal()

    def __init__(self, name: Optional[str] = None, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.name = name or server_name()
        self._server: Optional[QLocalServer] = None

    def notify_running(self, message: bytes = PING) -> bool:
        """Tell an already-running instance (True when one is there). The write is flushed by hanging up, and
        we wait for the close to finish, so the message is not lost when this process exits right after."""
        sock = QLocalSocket()
        sock.connectToServer(self.name)
        if not sock.waitForConnected(CONNECT_TIMEOUT_MS):
            return False
        sock.write(message or PING)
        sock.flush()
        sock.disconnectFromServer()
        if sock.state() != QLocalSocket.UnconnectedState:
            sock.waitForDisconnected(CONNECT_TIMEOUT_MS)
        return True

    def acquire(self, message: bytes = ACTIVATE) -> bool:
        """True for the first instance. When one is already running it gets ``message`` (``ACTIVATE`` shows its
        window, ``PING`` does nothing; one connection only, since a second right after the first is unreliable
        on Windows named pipes) and this returns False. When nothing answers and listening still fails it returns True (start unguarded)."""
        if self._server is not None:
            return True
        if self.notify_running(message):
            return False  # somebody answered: this is the second instance
        server = self._listen()
        if server is None:
            # Two launches can race: the other one may have started listening since our probe. Ask again.
            if self.notify_running(message):
                return False
            QLocalServer.removeServer(self.name)  # nobody answers: a socket file left by a crash
            server = self._listen()
        if server is None:
            # Nobody answers and we can't listen either (a broken pipe namespace, a policy). A recorder that
            # won't open is worse than a rare second copy, so start anyway, just without the guard.
            log.error("single instance: could not listen on %s; starting without the single-instance guard", self.name)
            return True
        server.newConnection.connect(self._on_connection)
        self._server = server
        return True

    def _listen(self) -> Optional[QLocalServer]:
        server = QLocalServer(self)
        server.setSocketOptions(QLocalServer.UserAccessOption)
        if server.listen(self.name):
            return server
        log.info("single instance: could not listen on %s: %s", self.name, server.errorString())
        server.setParent(None)
        server.deleteLater()
        return None

    def release(self) -> None:
        server, self._server = self._server, None
        for conn in self.findChildren(_Connection):
            conn.shutdown()
        if server is not None:
            server.close()
            server.setParent(None)
            server.deleteLater()

    def _on_connection(self) -> None:
        while self._server is not None and self._server.hasPendingConnections():
            _Connection(self._server.nextPendingConnection(), self)


class _Connection(QObject):
    """One client of the first instance: reads what it sent, then goes away when it hangs up."""

    def __init__(self, sock: QLocalSocket, owner: SingleInstance):
        super().__init__(owner)
        self._owner = owner
        self._sock = sock
        sock.setParent(self)
        sock.readyRead.connect(self._read)
        sock.disconnected.connect(self._gone)
        self._read()

    def _read(self) -> None:
        data = bytes(self._sock.readAll())
        if b"activate" in data:
            self._owner.activate_requested.emit()

    def _gone(self) -> None:
        self._read()  # whatever arrived just before the hang-up
        self.deleteLater()

    def shutdown(self) -> None:
        for signal in (self._sock.readyRead, self._sock.disconnected):
            try:
                signal.disconnect()
            except (RuntimeError, TypeError):
                pass
        self._sock.abort()
        self.setParent(None)
        self.deleteLater()
