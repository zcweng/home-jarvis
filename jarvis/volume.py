"""S12 播放音量：0–100% → 下行 F_VOLUME + 蓝色灯弧反馈。"""

from __future__ import annotations

import math
import socket
import sys
import threading
from typing import Callable


MIN_PERCENT = 5


def percent_to_gain_byte(pct: int) -> int:
    """线性响度：100%→0 dB（byte 60）；不低于 MIN_PERCENT。"""
    p = max(MIN_PERCENT, min(100, int(pct)))
    # g = p/100 → dB = 20*log10(g) → byte = 60 + dB
    db = 20.0 * math.log10(p / 100.0)
    return int(round(max(0.0, min(60.0, 60.0 + db))))


class VolumeController:
    def __init__(
        self,
        set_gain_byte: Callable[[int], bool],
        set_led_volume: Callable[[int], None],
        *,
        initial: int = 70,
        listen_port: int = 8769,
        min_percent: int = MIN_PERCENT,
        on_mute: Callable[[bool], None] | None = None,
    ) -> None:
        self._set_gain = set_gain_byte
        self._set_led = set_led_volume
        self._on_mute = on_mute
        self._min = max(1, int(min_percent))
        self._pct = max(self._min, min(100, initial))
        self._muted = False
        self._lock = threading.Lock()
        self._port = listen_port
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def muted(self) -> bool:
        with self._lock:
            return self._muted

    def set_muted(self, muted: bool) -> None:
        m = bool(muted)
        with self._lock:
            if m == self._muted:
                return
            self._muted = m
        print(f"  {'🔇' if m else '🎤'} 麦克风{'禁用' if m else '启用'}", flush=True)
        if self._on_mute is not None:
            try:
                self._on_mute(m)
            except Exception as e:  # noqa: BLE001
                print(f"⚠️  麦静音回调失败：{e}", file=sys.stderr)

    @property
    def percent(self) -> int:
        with self._lock:
            return self._pct

    def set_percent(self, pct: int, *, led: bool = True) -> int:
        p = max(self._min, min(100, int(pct)))
        with self._lock:
            if p == self._pct:
                return p
            self._pct = p
        ok = self._set_gain(percent_to_gain_byte(p))
        if not ok:
            print(f"⚠️  音量增益下发失败（{p}%）", file=sys.stderr)
        if led:
            try:
                self._set_led(p)
            except Exception as e:  # noqa: BLE001
                print(f"⚠️  音量灯弧失败：{e}", file=sys.stderr)
        print(f"  🔊 音量 {p}%", flush=True)
        return p

    def adjust(self, delta: int) -> int:
        return self.set_percent(self.percent + delta)

    def start_udp_listener(self) -> None:
        """收设备 volumed 的 V<pct> 包。"""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._udp_loop, name="vol-udp", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def _udp_loop(self) -> None:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", self._port))
            s.settimeout(0.5)
            self._sock = s
        except OSError as e:
            print(f"⚠️  音量 UDP 监听失败：{e}", file=sys.stderr)
            return
        print(f"音量/麦键：UDP :{self._port}（V<pct> / M0|M1）", file=sys.stderr)
        while not self._stop.is_set():
            try:
                data, _ = s.recvfrom(64)
            except socket.timeout:
                continue
            except OSError:
                break
            msg = data.decode("ascii", errors="ignore").strip()
            if msg.startswith("M") and len(msg) >= 2:
                try:
                    self.set_muted(int(msg[1:].strip()) != 0)
                except ValueError:
                    pass
                continue
            if not msg.startswith("V"):
                continue
            try:
                pct = int(msg[1:].strip())
            except ValueError:
                continue
            # 设备已画灯弧，这里只同步增益，避免 SSH 再刷一遍灯
            self.set_percent(pct, led=False)
