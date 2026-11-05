"""Only one ContentShare per Windows user.

Two copies would read the same mailbox and could start the same copy twice, so a second launch
(for example the Start-menu shortcut while the tray copy runs) asks the first to show its
window and then exits. The installer uses the same channel to ask a running copy to quit before
it replaces files.

The lock is a file lock (stale locks left by a crash are detected and removed). The requests
travel over two named pipes that only the current user can open. A request is just a
*connection*: the first pipe means "show your window", the second "quit". No bytes are ever
read from a client, so there is nothing to parse and nothing a stranger could send.
"""

from __future__ import annotations

import getpass
import re
from pathlib import Path

from PySide6.QtCore import QLockFile, QObject, Signal, SignalInstance
from PySide6.QtNetwork import QLocalServer, QLocalSocket

QUIT_SUFFIX = "-quit"


def pipe_name() -> str:
    user = re.sub(r"[^A-Za-z0-9_-]", "_", getpass.getuser())
    return f"ContentShareAI-{user}"


class SingleInstance(QObject):
    show_requested = Signal()
    quit_requested = Signal()

    def __init__(
        self, folder: Path, name: str | None = None, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        folder.mkdir(parents=True, exist_ok=True)
        self._lock = QLockFile(str(folder / "contentshare.lock"))
        self._name = name or pipe_name()
        self._servers: list[QLocalServer] = []

    def acquire(self) -> bool:
        """True if this process is the only instance. Otherwise the running one is asked to
        show its window and False is returned."""
        if self._lock.tryLock(0):
            self._listen(self._name, self.show_requested)
            self._listen(self._name + QUIT_SUFFIX, self.quit_requested)
            return True
        self.ask_to_show()
        return False

    def release(self) -> None:
        for server in self._servers:
            server.close()
        self._servers.clear()
        self._lock.unlock()

    def ask_to_show(self) -> bool:
        """Ask the running instance to show its window. False if none is running."""
        return self._knock(self._name)

    def ask_to_quit(self) -> bool:
        """Ask the running instance to quit. False if none is running."""
        return self._knock(self._name + QUIT_SUFFIX)

    # ------------------------------------------------------------------ internals
    def _listen(self, name: str, signal: SignalInstance) -> None:
        QLocalServer.removeServer(name)  # a crash can leave the name behind
        server = QLocalServer(self)
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        server.newConnection.connect(lambda: self._accept(server, signal))
        if server.listen(name):
            self._servers.append(server)

    @staticmethod
    def _accept(server: QLocalServer, signal: SignalInstance) -> None:
        while server.hasPendingConnections():
            client = server.nextPendingConnection()
            if client is not None:
                client.close()
                client.deleteLater()
                signal.emit()

    @staticmethod
    def _knock(name: str) -> bool:
        socket = QLocalSocket()
        socket.connectToServer(name)
        connected = socket.waitForConnected(1500)
        socket.abort()
        return connected
