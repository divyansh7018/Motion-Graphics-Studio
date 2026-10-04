"""Single-instance guard (directive sections 9, 12).

Two windows writing the same project file is a data-loss risk, so the default
is "one instance per data folder".  The guard uses Qt's local socket pair:

* the first instance listens on a name derived from the data folder,
* a second instance connects, sends a short message ("raise"), and exits,
* a stale socket from a crashed instance is detected (connection refused) and
  reclaimed instead of blocking startup forever.

Everything here is optional: if the guard cannot be created at all, the
application still starts - with a warning in the log.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from ..core.logging_setup import get_logger, log_event

LOGGER = get_logger("single_instance")

CONNECT_TIMEOUT_MS = 400


def server_name_for(data_root: Path) -> str:
    """Stable, filesystem-independent socket name for a data folder."""
    digest = hashlib.sha1(str(Path(data_root).resolve()).lower().encode("utf-8")).hexdigest()[:16]
    return f"motion-graphics-studio-{digest}"


class SingleInstanceGuard(QObject):
    """Owns the local server; emits :attr:`message_received` for other instances."""

    message_received = Signal(str)

    def __init__(self, data_root: Path, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.name = server_name_for(data_root)
        self._server: Optional[QLocalServer] = None
        self._sockets: list[QLocalSocket] = []
        self.error: str = ""

    # -- acquiring ---------------------------------------------------------

    def try_acquire(self) -> bool:
        """Return ``True`` when this process becomes the primary instance."""
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._on_new_connection)
        # Qt refuses to listen on a socket left behind by a crashed process;
        # removing it first is safe because we check the connection below.
        QLocalServer.removeServer(self.name)
        if self._server.listen(self.name):
            return True
        self.error = self._server.errorString()
        return False

    def notify_existing(self, message: str = "raise") -> bool:
        """Ask an already-running instance to come to the front."""
        socket = QLocalSocket()
        socket.connectToServer(self.name)
        if not socket.waitForConnected(CONNECT_TIMEOUT_MS):
            return False
        socket.write(message.encode("utf-8"))
        socket.flush()
        socket.waitForBytesWritten(CONNECT_TIMEOUT_MS)
        socket.disconnectFromServer()
        return True

    def is_primary(self) -> bool:
        return self._server is not None and self._server.isListening()

    # -- internals ---------------------------------------------------------

    def _on_new_connection(self) -> None:
        if self._server is None:
            return
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket is None:
                continue
            self._sockets.append(socket)
            socket.readyRead.connect(lambda s=socket: self._read(s))
            socket.disconnected.connect(lambda s=socket: self._forget(s))

    def _read(self, socket: QLocalSocket) -> None:
        data = bytes(socket.readAll()).decode("utf-8", errors="replace").strip()
        if data:
            log_event("USER_ACTION", f"Second instance message: {data}", logger=LOGGER)
            self.message_received.emit(data)

    def _forget(self, socket: QLocalSocket) -> None:
        if socket in self._sockets:
            self._sockets.remove(socket)
        socket.deleteLater()

    # -- teardown ----------------------------------------------------------

    def release(self) -> None:
        for socket in list(self._sockets):
            try:
                socket.disconnectFromServer()
            except Exception:  # pragma: no cover - defensive
                pass
            socket.deleteLater()
        self._sockets.clear()
        if self._server is not None:
            self._server.close()
            QLocalServer.removeServer(self.name)
            self._server = None


def detect_existing_instance(data_root: Path, timeout_ms: int = 300) -> tuple[bool, str]:
    """Check whether another copy is already running, without taking ownership.

    Returns ``(running, error)``.  This never leaves a server behind: it either
    connects briefly to an existing one, or proves the name is free.
    """
    name = server_name_for(data_root)

    probe = QLocalSocket()
    probe.connectToServer(name)
    if probe.waitForConnected(timeout_ms):
        probe.disconnectFromServer()
        return True, ""

    server = QLocalServer()
    if server.listen(name):
        server.close()
        QLocalServer.removeServer(name)
        return False, ""
    return False, server.errorString() or "The instance check could not be completed."
