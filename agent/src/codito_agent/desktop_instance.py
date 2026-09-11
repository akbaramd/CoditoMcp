from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket

_ACTIVATE_COMMAND = b"show\n"


def desktop_server_name(data_directory: Path) -> str:
    """Return a stable per-user endpoint without exposing the local data path."""

    canonical = os.path.normcase(os.path.abspath(data_directory))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"codito-desktop-{digest}"


def activate_existing_desktop(name: str, *, timeout_ms: int = 500) -> bool:
    """Ask an existing tray process to show its window.

    A successful connection is authoritative even when delivery completes
    immediately and Qt has no pending bytes left to wait for.
    """

    socket = QLocalSocket()
    socket.connectToServer(name)
    if not socket.waitForConnected(timeout_ms):
        socket.abort()
        return False
    if socket.write(_ACTIVATE_COMMAND) != len(_ACTIVATE_COMMAND):
        socket.abort()
        return False
    socket.flush()
    # The Windows QLocalSocket backend flushes this short named-pipe payload as
    # part of graceful disconnect; waitForBytesWritten can return False while
    # the data is still queued. Waiting for disconnect prevents object cleanup
    # from dropping the activation command.
    socket.disconnectFromServer()
    if socket.state() != QLocalSocket.LocalSocketState.UnconnectedState:
        socket.waitForDisconnected(timeout_ms)
    return True


class DesktopInstanceServer(QObject):  # type: ignore[misc, unused-ignore]
    """Single-instance activation endpoint owned by the primary tray process."""

    def __init__(
        self,
        name: str,
        activate: Callable[[], None],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.name = name
        self.activate = activate
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._accept_connections)
        self._connections: set[QLocalSocket] = set()
        self._buffers: dict[QLocalSocket, bytearray] = {}
        self._accept_timer = QTimer(self)
        self._accept_timer.setInterval(100)
        self._accept_timer.timeout.connect(self._accept_connections)

    def start(self) -> bool:
        if self.server.listen(self.name):
            self._accept_timer.start()
            return True
        # A crashed process can leave a stale local endpoint. This is safe only
        # after the caller has already failed to connect to a live instance.
        QLocalServer.removeServer(self.name)
        listening = self.server.listen(self.name)
        if listening:
            self._accept_timer.start()
        return bool(listening)

    def close(self) -> None:
        self._accept_timer.stop()
        for socket in tuple(self._connections):
            socket.abort()
        self._connections.clear()
        self._buffers.clear()
        self.server.close()
        QLocalServer.removeServer(self.name)

    def _accept_connections(self) -> None:
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            self._connections.add(socket)
            self._buffers[socket] = bytearray()
            socket.readyRead.connect(lambda current=socket: self._read(current))
            socket.disconnected.connect(lambda current=socket: self._finish(current))
            self._read(socket)

    def _read(self, socket: QLocalSocket) -> None:
        buffer = self._buffers.get(socket)
        if buffer is None:
            return
        buffer.extend(socket.readAll().data())
        if b"\n" not in buffer and len(buffer) <= 32:
            return
        self._process(bytes(buffer))
        buffer.clear()
        socket.disconnectFromServer()

    def _finish(self, socket: QLocalSocket) -> None:
        # On Windows a very short-lived client can disconnect before readyRead
        # is emitted even though the complete message is already buffered.
        buffer = self._buffers.get(socket, bytearray())
        buffer.extend(socket.readAll().data())
        self._process(bytes(buffer))
        self._discard(socket)

    def _process(self, payload: bytes) -> bool:
        if payload.split(b"\n", 1)[0] == _ACTIVATE_COMMAND.rstrip(b"\n"):
            self.activate()
            return True
        return False

    def _discard(self, socket: QLocalSocket) -> None:
        self._connections.discard(socket)
        self._buffers.pop(socket, None)
        socket.deleteLater()
