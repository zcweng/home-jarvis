"""S12 灯环客户端：SSH 调用设备端 /data/jarvis-led-set.sh。

状态约定：idle / listen / think / speak / error
"""

from __future__ import annotations

import subprocess
import sys
import threading
from typing import Literal

LedState = Literal["idle", "listen", "think", "speak", "error"]

REMOTE_SET = "/data/jarvis-led-set.sh"
SSH_OPTS = [
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=3",
    "-o",
    "HostKeyAlgorithms=+ssh-rsa",
    "-o",
    "PubkeyAcceptedKeyTypes=+ssh-rsa",
]


class S12Led:
    """异步下发；失败只打日志，不堵语音主循环。"""

    def __init__(
        self,
        host: str,
        port: int = 8767,  # 兼容旧配置，未使用
        *,
        enabled: bool = True,
        user: str = "root",
    ) -> None:
        self.host = (host or "").strip()
        self.port = int(port)
        self.user = user
        self.enabled = bool(enabled) and bool(self.host)
        self._lock = threading.Lock()
        self._state: LedState | None = None
        self._busy = threading.Lock()  # 串行 SSH，避免状态乱序

    def close(self) -> None:
        self.set("idle")

    def set(self, state: LedState) -> None:
        if not self.enabled:
            return
        with self._lock:
            if state == self._state:
                return
            self._state = state
        threading.Thread(target=self._send, args=(state,), daemon=True).start()

    def set_blocking(self, state: LedState, timeout: float = 5.0) -> bool:
        """同步下发（演示/自检用）。"""
        if not self.enabled:
            return False
        with self._lock:
            self._state = state
        return self._send(state, timeout=timeout)

    def _send(self, state: LedState, timeout: float = 4.0) -> bool:
        with self._busy:
            remote = f"export PATH=/usr/sbin:/usr/bin:/sbin:/bin; {REMOTE_SET} {state}"
            cmd = ["ssh", *SSH_OPTS, f"{self.user}@{self.host}", remote]
            try:
                r = subprocess.run(
                    cmd,
                    check=False,
                    timeout=timeout,
                    capture_output=True,
                    text=True,
                )
                if r.returncode != 0:
                    err = (r.stderr or r.stdout or "").strip()[:120]
                    print(f"⚠️  灯环 {state} rc={r.returncode} {err}", file=sys.stderr)
                    return False
                return True
            except Exception as e:  # noqa: BLE001
                print(f"⚠️  灯环 {state} 失败：{e}", file=sys.stderr)
                return False


class NullLed:
    def set(self, state: str) -> None:  # noqa: ARG002
        return

    def set_blocking(self, state: str, timeout: float = 5.0) -> bool:  # noqa: ARG002
        return False

    def close(self) -> None:
        return
