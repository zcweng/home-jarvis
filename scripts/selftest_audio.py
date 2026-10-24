#!/usr/bin/env python3
"""音频层自检：设备选择 / 录音 / WAV 往返 / 播放可打断。

    .venv/bin/python scripts/selftest_audio.py

会真的出声（3 秒正弦波，0.4 秒后被掐断），请把音量调低。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.audio import (  # noqa: E402
    TARGET_RATE,
    Mic,
    Speaker,
    dbfs,
    pick_device,
    read_wav,
    write_wav,
)
from jarvis.config import Settings  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  —— {detail}" if detail else ""))


def main() -> int:
    st = Settings.load()

    # 1. 设备解析（找不到会回落并打印警告，不应抛异常）
    try:
        in_dev = pick_device(st.input_device, "input")
        out_dev = pick_device(st.output_device, "output")
        check("设备解析", True, f"in={in_dev[1]} / out={out_dev[1]}")
    except SystemExit as e:
        check("设备解析", False, str(e))
        return 1

    # 2. 录音：电平不应恒为 0（恒为 0 基本就是麦克风权限或 TRRS 接法问题）
    t0 = time.perf_counter()
    with Mic(st.input_device) as mic:
        pcm = mic.record(1.0)
    dt = time.perf_counter() - t0
    peak = float(np.abs(pcm).max()) if pcm.size else 0.0
    check("录音", pcm.size > 0 and peak > 1e-4, f"{pcm.size / TARGET_RATE:.2f}s, {dbfs(pcm):.1f} dBFS, 耗时 {dt:.2f}s")

    # 3. WAV 往返：量化误差应 < 1/32767*2
    tmp = ROOT / "debug" / "selftest.wav"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    write_wav(tmp, pcm, TARGET_RATE)
    back, rate = read_wav(tmp)
    same_len = back.size == pcm.size and rate == TARGET_RATE
    err = float(np.abs(back - pcm).max()) if same_len else 1.0
    check("WAV 往返", same_len and err < 1e-4, f"max 误差 {err:.2e}")

    # 4. 播放可打断：3 秒音调，0.4 秒后 stop()，应立刻返回
    t = np.arange(int(3.0 * TARGET_RATE), dtype=np.float32) / TARGET_RATE
    tone = (0.15 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    spk = Speaker(st.output_device)
    th = spk.play(tone, TARGET_RATE, blocking=False)
    time.sleep(0.4)
    t_stop = time.perf_counter()
    spk.stop()
    if th is not None:
        th.join(timeout=3.0)
    stop_latency = time.perf_counter() - t_stop
    check("播放可打断", th is not None and not th.is_alive() and stop_latency < 1.0,
          f"stop→线程退出 {stop_latency * 1000:.0f}ms")

    # 5. 打断后再播（状态没被 stop 卡死）
    spk.play((0.05 * np.sin(2 * np.pi * 660 * t[: TARGET_RATE // 4])).astype(np.float32), TARGET_RATE)
    check("打断后可再次播放", True)

    ok = all(r[1] for r in results)
    print("-" * 48)
    print("全部通过" if ok else f"失败 {sum(1 for r in results if not r[1])} 项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
