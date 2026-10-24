#!/usr/bin/env python3
"""打断检测自检（合成信号，不需要麦克风）。

核心是证明两件事：
1. 播报回声本身**不会**触发打断（否则每句话都会自断）
2. 叠加上去的真人语音**会**触发（并且延迟可接受）

    .venv/bin/python scripts/selftest_bargein.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.bargein import BargeInConfig, BargeInDetector  # noqa: E402

SR = 16000
BLOCK = 1024


def tone(amp: float, ms: float, freq: float = 220.0) -> np.ndarray:
    n = int(SR * ms / 1000)
    t = np.arange(n) / SR
    # 用噪声+正弦混合，比纯正弦更像"声音"
    return (amp * (0.7 * np.sin(2 * np.pi * freq * t) + 0.3 * np.random.randn(n) * 0.1)).astype(np.float32)


def blocks(pcm: np.ndarray) -> list[np.ndarray]:
    return [pcm[i : i + BLOCK] for i in range(0, pcm.size, BLOCK)]


def feed(det: BargeInDetector, pcm: np.ndarray) -> tuple[bool, int]:
    """返回 (是否触发, 触发发生在第几块)。"""
    for i, b in enumerate(blocks(pcm)):
        if det.feed(b):
            return True, i
    return False, -1


def main() -> int:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(f"{'✅' if ok else '❌'} {name}" + (f"  —— {detail}" if detail else ""))

    # ① 只有回声：不应触发
    det = BargeInDetector(BargeInConfig())
    hit, idx = feed(det, tone(0.02, 3000))
    check("纯回声不自断", not hit, f"基线 {det.last_baseline_db:.1f}dBFS 当前 {det.last_level_db:.1f}dBFS")

    # ② 回声 + 400ms 大音量人声：应触发
    det = BargeInDetector(BargeInConfig())
    pcm = np.concatenate([tone(0.02, 1200), tone(0.25, 400)])
    hit, idx = feed(det, pcm)
    lat_ms = (idx * BLOCK) / SR * 1000 - 1200 if hit else -1
    check("叠加人声能打断", hit, f"额外延迟 {lat_ms:.0f}ms" if hit else "未触发")
    check("打断延迟 <600ms", 0 <= lat_ms < 600, f"{lat_ms:.0f}ms")

    # ③ 短促噪声（150ms，像键盘声）：不应触发
    det = BargeInDetector(BargeInConfig())
    pcm = np.concatenate([tone(0.02, 1200), tone(0.25, 150), tone(0.02, 800)])
    hit, _ = feed(det, pcm)
    check("短噪声不误触发", not hit, "150ms 未达 300ms 门限")

    # ④ 回声整体变大（用户调大音量）：自适应基线应跟上，不误触
    det = BargeInDetector(BargeInConfig())
    pcm = np.concatenate([tone(0.02, 1500), tone(0.06, 2500)])
    hit, _ = feed(det, pcm)
    check("音量变大不误触发", not hit, f"基线已自适应到 {det.last_baseline_db:.1f}dBFS")

    ok = all(r[1] for r in results)
    print("-" * 52)
    print("全部通过" if ok else f"失败 {sum(1 for r in results if not r[1])} 项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
