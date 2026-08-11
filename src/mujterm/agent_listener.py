from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from .paths import ensure_private_dir, runtime_dir


class AgentSocketListener:
    def __init__(self, callback: Callable[[dict[str, Any]], None]) -> None:
        self.callback = callback
        self.path = ensure_private_dir(runtime_dir()) / "agent.sock"
        self._socket: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()

    def start(self) -> None:
        if self._thread:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            listener.bind(str(self.path))
            os.chmod(self.path, 0o600)
        except OSError:
            listener.close()
            raise
        listener.settimeout(0.5)
        self._socket = listener
        self._thread = threading.Thread(target=self._run, name="agent-events", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._socket:
            self._socket.close()
        if self._thread:
            self._thread.join(timeout=1)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                assert self._socket is not None
                data = self._socket.recv(8192)
                value = json.loads(data.decode("utf-8"))
                if isinstance(value, dict):
                    self.callback(value)
            except socket.timeout:
                continue
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                if self._stopping.is_set():
                    return
