"""Current-user named-pipe IPC with bounded framing and durable request IDs."""
from __future__ import annotations

import getpass
import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from PySide6.QtCore import QObject, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket

MAX_MESSAGE = 2 * 1024 * 1024


@contextmanager
def database(path):
    db = sqlite3.connect(path, timeout=5)
    try:
        with db:
            yield db
    finally:
        db.close()


def server_name(directory):
    key = getpass.getuser() + "|" + str(Path(directory).resolve()).casefold()
    return "MagicConverter-" + hashlib.sha256(key.encode()).hexdigest()[:24]


class IpcServer(QObject):
    def __init__(self, directory, handler, parent=None):
        super().__init__(parent)
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db_path = self.directory / "ipc-receipts.sqlite3"
        self.instance = uuid.uuid4().hex
        self.handler = handler
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.server.newConnection.connect(self.accept)
        self.clients = {}
        with database(self.db_path) as db:
            if db.execute("PRAGMA user_version").fetchone()[0] > 1:
                raise ValueError("최신 버전 IPC 기록은 보존합니다.")
            db.execute("CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, digest TEXT NOT NULL, instance TEXT NOT NULL, response TEXT, created REAL NOT NULL)")
            db.execute("PRAGMA user_version=1")

    def start(self):
        # The caller owns the profile's QLockFile before removing a stale pipe.
        name = server_name(self.directory)
        QLocalServer.removeServer(name)
        if not self.server.listen(name):
            raise OSError(self.server.errorString())

    def accept(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            if len(self.clients) >= 16:
                socket.abort()
                socket.deleteLater()
                continue
            self.clients[socket] = bytearray()
            socket.readyRead.connect(lambda s=socket: self.read(s))
            socket.disconnected.connect(lambda s=socket: self.drop(s))
            QTimer.singleShot(10000, socket, lambda s=socket: s.abort() if s in self.clients else None)
            if socket.bytesAvailable():
                self.read(socket)

    def drop(self, socket):
        self.clients.pop(socket, None)
        socket.deleteLater()

    def read(self, socket):
        if socket not in self.clients:
            return
        raw = self.clients[socket]
        raw.extend(bytes(socket.readAll()))
        if len(raw) > MAX_MESSAGE:
            socket.abort()
            return
        if b"\n" not in raw:
            return
        try:
            line, trailing = raw.split(b"\n", 1)
            if trailing.strip():
                raise ValueError("IPC 연결에는 요청 하나만 보낼 수 있습니다.")
            request = json.loads(line.decode("utf-8"))
            response = self.dispatch(request)
        except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as exc:
            response = {"ok": False, "error": str(exc)}
        socket.write(json.dumps(response, ensure_ascii=False).encode() + b"\n")
        socket.flush()
        socket.disconnectFromServer()

    def dispatch(self, request):
        if not isinstance(request, dict) or request.get("version") != 1:
            raise ValueError("IPC 프로토콜 버전이 올바르지 않습니다.")
        request_id = request.get("id", "")
        if not isinstance(request_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", request_id):
            raise ValueError("IPC 요청 ID가 올바르지 않습니다.")
        payload = json.dumps(request, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with database(self.db_path) as db:
            previous = db.execute("SELECT digest,instance,response FROM receipts WHERE id=?", (request_id,)).fetchone()
            if previous:
                if previous[0] != digest:
                    raise ValueError("동일한 IPC 요청 ID에 다른 내용이 전달되었습니다.")
                response = json.loads(previous[2]) if previous[2] else {"ok": False, "error": "이전 요청이 중단됐습니다. 상태를 확인한 후 새 ID로 재시도하세요."}
                return {**response, "replayed": True, "previous_instance": previous[1] != self.instance}
            db.execute("INSERT INTO receipts VALUES (?,?,?,?,?)", (request_id, digest, self.instance, None, time.time()))
        try:
            response = {"ok": True, "instance": self.instance, **self.handler(request)}
        except Exception as exc:
            response = {"ok": False, "instance": self.instance, "error": str(exc)}
        with database(self.db_path) as db:
            db.execute("UPDATE receipts SET response=? WHERE id=?", (json.dumps(response, ensure_ascii=False), request_id))
            db.execute("DELETE FROM receipts WHERE id IN (SELECT id FROM receipts ORDER BY created DESC LIMIT -1 OFFSET 10000)")
        return response

    def close(self):
        self.server.close()
        for socket in list(self.clients):
            socket.abort()


def call(directory, request, *, timeout=10):
    request = {"version": 1, "id": uuid.uuid4().hex, **request}
    raw = json.dumps(request, ensure_ascii=False).encode() + b"\n"
    if len(raw) > MAX_MESSAGE:
        raise ValueError("IPC 요청이 너무 큽니다.")
    deadline = time.monotonic() + timeout
    socket = QLocalSocket()
    # Retry a listener that is still starting, as Filee's SingleInstance does.
    while True:
        socket.connectToServer(server_name(directory))
        if socket.waitForConnected(min(500, max(1, int((deadline - time.monotonic()) * 1000)))):
            break
        socket.abort()
        if time.monotonic() >= deadline:
            raise OSError("실행 중인 magic_converter에 연결하지 못했습니다.")
        time.sleep(.05)
    socket.write(raw)
    socket.flush()
    response = bytearray()
    while b"\n" not in response:
        left = deadline - time.monotonic()
        if left <= 0 or not socket.bytesAvailable() and not socket.waitForReadyRead(max(1, int(left * 1000))):
            raise TimeoutError("IPC 응답 시간이 초과되었습니다. 같은 요청 ID로 재시도할 수 있습니다.")
        response.extend(bytes(socket.readAll()))
        if len(response) > MAX_MESSAGE:
            raise ValueError("IPC 응답이 너무 큽니다.")
    socket.disconnectFromServer()
    return json.loads(response.split(b"\n", 1)[0].decode("utf-8"))
