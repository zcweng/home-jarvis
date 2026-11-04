#!/usr/bin/env python3
"""网络音频端点离线回归：不需要真音箱、不需要音频设备。

覆盖「设备重启后 Mac 侧失聪」这条最隐蔽的故障：

  ① 半开连接：设备重启不发 FIN，socket 仍 ESTABLISHED 但永远没数据
     → 超过 stale_after 必须主动重连，不能永久收不到声音
  ② 正常收流：数据持续到达时不能被误判为 stale 而反复重连
  ③ 断线重连：对端 close（正常 FIN）后能自动连回来
  ④ 短包对齐：S16 半字节残留不能污染下一帧（按整样本切）

    .venv/bin/python scripts/selftest_endpoint.py
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from jarvis.endpoint import FRAME_BYTES, UplinkReader  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  —— {detail}" if detail else ""))


class FakeDevice:
    """最小上行端点：只监听、只发送，模拟 s12audio 的 8765。"""

    def __init__(self) -> None:
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        self.conns: list[socket.socket] = []
        self.accepted = 0
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.thread.start()

    def _accept_loop(self) -> None:
        self.srv.settimeout(0.2)
        while not self._stop.is_set():
            try:
                c, _ = self.srv.accept()
            except (socket.timeout, OSError):
                continue
            self.accepted += 1
            with threading.Lock():
                self.conns.append(c)

    def wait_conn(self, timeout: float = 3.0) -> socket.socket | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.conns:
                return self.conns[-1]
            time.sleep(0.02)
        return None

    def stream(self, seconds: float, amp: int = 3000, conn: socket.socket | None = None) -> None:
        """按 10ms 一帧连续发正弦，模拟真实上行。"""
        conn = conn or self.wait_conn()
        if conn is None:
            return
        n = int(seconds * 100)
        t = 0
        for _ in range(n):
            i = np.arange(160) + t
            blk = (amp * np.sin(2 * np.pi * 440 * i / 16000)).astype("<i2")
            try:
                conn.sendall(blk.tobytes())
            except OSError:
                return
            t += 160
            time.sleep(0.01)

    def drop_all(self) -> None:
        """模拟设备重启：先 RST，让对端立刻看到；这是「能收到 FIN/RST」的对照。"""
        for c in self.conns:
            try:
                c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
                c.close()
            except OSError:
                pass
        self.conns.clear()

    def mute_silently(self) -> None:
        """模拟半开：不 close、不发数据，socket 在 Mac 侧仍是 ESTABLISHED。"""
        # 什么都不做——这正是危险之处

    def close(self) -> None:
        self._stop.set()
        for c in self.conns:
            try:
                c.close()
            except OSError:
                pass
        try:
            self.srv.close()
        except OSError:
            pass


def test_healthy_stream() -> None:
    dev = FakeDevice()
    r = UplinkReader("127.0.0.1", dev.port)
    r.stale_after = 1.0
    r.read_capture(timeout=0.2)  # 先建连，再起发流，避免「还没 accept 就发」的竞态
    conn = dev.wait_conn()
    got = 0.0
    if conn is not None:
        threading.Thread(target=dev.stream, args=(1.2,), kwargs={"conn": conn}, daemon=True).start()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            cap = r.read_capture(timeout=0.1)
            if cap is not None:
                got += cap.mono.size / 16000.0
    check("持续收流不误判 stale", got > 0.8 and r.reconnects <= 2, f"收 {got:.2f}s，重连 {r.reconnects} 次")
    r.close()
    dev.close()


def test_half_open_reconnect() -> None:
    """核心用例：设备重启不发 FIN，必须靠 stale 超时自救。"""
    dev = FakeDevice()
    r = UplinkReader("127.0.0.1", dev.port)
    r.stale_after = 0.6
    threading.Thread(target=dev.stream, args=(0.5,), daemon=True).start()
    t0 = time.monotonic()
    while time.monotonic() - t0 < 1.2:
        r.read_capture(timeout=0.1)
    first_accepted = dev.accepted
    check("先建立连接", first_accepted >= 1, f"accepted={first_accepted}")

    # 设备侧静默（半开），Mac 侧应该超时后主动重连
    dev.mute_silently()
    t0 = time.monotonic()
    while time.monotonic() - t0 < 2.5:
        r.read_capture(timeout=0.1)
    check("半开连接会自动重连", dev.accepted > first_accepted, f"accepted {first_accepted} → {dev.accepted}")

    # 重连后能重新收到声音
    threading.Thread(target=dev.stream, args=(0.6,), daemon=True).start()
    got = 0.0
    t0 = time.monotonic()
    while time.monotonic() - t0 < 1.0:
        cap = r.read_capture(timeout=0.1)
        if cap is not None:
            got += cap.mono.size / 16000.0
    check("重连后恢复收流", got > 0.3, f"收 {got:.2f}s")
    r.close()
    dev.close()


def test_pause_during_tts_not_stale() -> None:
    """模拟无 barge-in：播报/LLM 期间上层不调 read，回来后不能误判重连。"""
    dev = FakeDevice()
    r = UplinkReader("127.0.0.1", dev.port)
    r.stale_after = 0.6
    r.read_capture(timeout=0.2)
    conn = dev.wait_conn()
    assert conn is not None
    threading.Thread(target=dev.stream, args=(0.4,), kwargs={"conn": conn}, daemon=True).start()
    t0 = time.monotonic()
    while time.monotonic() - t0 < 0.8:
        r.read_capture(timeout=0.1)
    before = r.reconnects
    accepted_before = dev.accepted

    # 上层去「播报」1.5s，期间设备继续推流（推不动就丢），Mac 不读
    time.sleep(1.5)
    threading.Thread(target=dev.stream, args=(0.5,), kwargs={"conn": conn}, daemon=True).start()
    got = 0.0
    t0 = time.monotonic()
    while time.monotonic() - t0 < 1.2:
        cap = r.read_capture(timeout=0.1)
        if cap is not None:
            got += cap.mono.size / 16000.0
    check(
        "播报暂停后不误判重连",
        r.reconnects == before and dev.accepted == accepted_before and got > 0.2,
        f"重连 {before}→{r.reconnects}，accepted {accepted_before}→{dev.accepted}，收 {got:.2f}s",
    )
    r.close()
    dev.close()


def test_fin_reconnect() -> None:
    dev = FakeDevice()
    r = UplinkReader("127.0.0.1", dev.port)
    r.stale_after = 5.0  # 故意调大，验证 FIN 路径不依赖 stale
    r.read_capture(timeout=0.2)
    conn1 = dev.wait_conn()
    if conn1 is not None:
        threading.Thread(target=dev.stream, args=(0.4,), kwargs={"conn": conn1}, daemon=True).start()
        t0 = time.monotonic()
        while time.monotonic() - t0 < 0.9:
            r.read_capture(timeout=0.1)
    before = dev.accepted
    dev.drop_all()
    r.read_capture(timeout=0.3)  # 让 reader 看到 FIN 并重连
    conn2 = dev.wait_conn()
    got = 0.0
    if conn2 is not None:
        threading.Thread(target=dev.stream, args=(0.5,), kwargs={"conn": conn2}, daemon=True).start()
        t0 = time.monotonic()
        while time.monotonic() - t0 < 1.5:
            cap = r.read_capture(timeout=0.1)
            if cap is not None:
                got += cap.mono.size / 16000.0
    check("FIN 断线能重连", dev.accepted > before and got > 0.2, f"accepted {before} → {dev.accepted}，收 {got:.2f}s")
    r.close()
    dev.close()


def test_frame_alignment() -> None:
    """上行是裸 S16 流，必须按整样本切；半字节残留不能污染下一帧。"""
    dev = FakeDevice()
    r = UplinkReader("127.0.0.1", dev.port)
    r.stale_after = 5.0
    r.read_capture(timeout=0.2)  # 触发连接
    # 手工发 3 个样本（6 字节）+ 1 个半样本（1 字节）
    conn = dev.conns[-1]
    conn.sendall(np.array([100, -100, 200], dtype="<i2").tobytes() + b"\x7f")
    time.sleep(0.15)
    cap = r.read_capture(timeout=0.3, min_samples=3)
    ok = cap is not None and cap.mono.size == 3 and int(cap.mono[1] * 32768) == -100
    check("S16 按整样本对齐", ok, f"收到 {0 if cap is None else cap.mono.size} 样本")
    r.close()
    dev.close()


def test_constants() -> None:
    check("帧长常量一致", FRAME_BYTES == 320, f"FRAME_BYTES={FRAME_BYTES}")


def main() -> int:
    print("=" * 60)
    print(" 网络音频端点回归")
    print("=" * 60)
    test_constants()
    test_healthy_stream()
    test_half_open_reconnect()
    test_pause_during_tts_not_stale()
    test_fin_reconnect()
    test_frame_alignment()

    print()
    bad = [r for r in results if not r[1]]
    print("─" * 60)
    print(f"通过 {len(results) - len(bad)} · 失败 {len(bad)}")
    if bad:
        for name, _, detail in bad:
            print(f"  ❌ {name}  {detail}")
        return 1
    print("全部通过 🎉")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
