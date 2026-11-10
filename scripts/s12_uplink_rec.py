#!/usr/bin/env python3
"""录一段**产品路径**的上行音频（设备端 AEC + 选路 + 降噪之后），存成 wav。

用途：把「设备真正送给 ASR 的东西」和「6 路裸麦克风」放在同一套指标下比较。
很多「耳朵不灵」的锅不在麦克风，而在中间这层处理（选路抖动、降噪过压、AEC 误伤）。

    .venv/bin/python scripts/s12_uplink_rec.py --seconds 12 --play /tmp/speech_test.wav
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.endpoint import UplinkReader  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=12.0)
    ap.add_argument("--play", default="")
    ap.add_argument("--out", default="/tmp/up_prod.wav")
    ap.add_argument("--host", default="192.168.1.3")
    ap.add_argument("--mac-mic", action="store_true")
    args = ap.parse_args()

    reader = UplinkReader(args.host, 8765)
    chunks: list[np.ndarray] = []
    stop = threading.Event()

    def pump() -> None:
        # 丢掉开头的残包，只从第一个完整帧开始
        reader.connect()
        while not stop.is_set():
            cap = reader.read_capture(timeout=0.3)
            if cap is not None and cap.mono.size:
                chunks.append(np.asarray(cap.mono, np.float32).copy())

    th = threading.Thread(target=pump, daemon=True)
    th.start()
    time.sleep(0.3)

    mic = []
    if args.mac_mic:
        mic.append(
            subprocess.Popen(
                ["ffmpeg", "-y", "-loglevel", "error", "-f", "avfoundation", "-i", ":0",
                 "-t", str(args.seconds + 1.0), "-ar", "16000", "-ac", "1",
                 str(Path(args.out).with_suffix(".mac.wav"))]
            )
        )

    t0 = time.time()
    if args.play:
        time.sleep(0.5)
        player = subprocess.Popen(["afplay", args.play])
    else:
        player = None
    while time.time() - t0 < args.seconds:
        time.sleep(0.1)
    if player:
        player.terminate()
    stop.set()
    th.join(timeout=2.0)
    for p in mic:
        p.wait(timeout=20)

    x = np.concatenate(chunks) if chunks else np.zeros(0, np.float32)
    print(f"收到 {len(x) / 16000:.2f}s 上行音频，{len(chunks)} 块")
    with wave.open(args.out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
    print(f"→ {args.out}")
    if len(x):
        print(f"  rms={20 * np.log10(np.sqrt(np.mean(x**2)) + 1e-12):.1f} dBFS  "
              f"peak={20 * np.log10(np.max(np.abs(x)) + 1e-12):.1f} dBFS")
        # 10ms 帧能量的分位，快速判断有没有「选路抖动」造成的锯齿
        n = len(x) // 160
        e = np.sqrt(np.mean(x[: n * 160].reshape(n, 160) ** 2, axis=1))
        e_db = 20 * np.log10(e + 1e-12)
        d = np.abs(np.diff(e_db))
        print(f"  帧能量跳变 |ΔE| 中位={np.median(d):.2f} dB  p95={np.percentile(d, 95):.2f} dB"
              f"  （选路抖动会把这个数推高）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())