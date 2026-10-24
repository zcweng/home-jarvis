"""打断（barge-in）检测。

难点不在"检测到有人说话"，而在**不把自己打断**：没有 AEC 时，喇叭的声音会被
麦克风收回来，朴素 VAD 会在每一句播报里"听到"自己，然后疯狂自断。

P0 用的办法是**自适应回声基线**：
- 播报期间持续记录麦克风能量，取最近 1.2 秒的**中位数**当作"回声底"
- 当前能量比基线高出 margin_db（默认 9dB）并持续 300ms，才算真打断
- 用中位数而不是均值：喇叭音量本身有起伏，均值会被自己带跑

真正的解法是硬件 AEC（ESP32-S3 的 ESP-SR AFE），所以这条路在 P0 里
标注为"可用但脆"，默认关闭。
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import numpy as np


def rms(pcm: np.ndarray) -> float:
    if pcm.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(pcm))))


def dbfs(x: float) -> float:
    return -120.0 if x <= 1e-9 else 20.0 * math.log10(x)


@dataclass
class BargeInConfig:
    margin_db: float = 12.0       # 要比回声基线高多少 dB 才算人说话（耳机口腔近讲一般 +20dB 以上）
    min_speech_ms: float = 350.0  # 持续多久才算（过滤键盘声/爆音）
    baseline_window_s: float = 1.2
    warmup_ms: float = 200.0      # 起播瞬间不判，等缓冲填满
    sample_rate: int = 16000


class BargeInDetector:
    """喂 16k 的麦克风音频块，判断"是不是被打断了"。"""

    def __init__(self, cfg: BargeInConfig | None = None) -> None:
        self.cfg = cfg or BargeInConfig()
        self.reset()

    def reset(self) -> None:
        maxlen = max(4, int(self.cfg.baseline_window_s * self.cfg.sample_rate / 1024) + 2)
        self._levels: deque[float] = deque(maxlen=maxlen)
        self._elapsed_ms = 0.0
        self._speech_ms = 0.0
        self.last_baseline_db = -120.0
        self.last_level_db = -120.0

    def feed(self, chunk: np.ndarray) -> bool:
        """返回 True 表示判定为真打断（调用方应立刻停播并转入收音）。"""
        if chunk.size == 0:
            return False
        dur_ms = chunk.size / self.cfg.sample_rate * 1000
        level = rms(chunk)
        self.last_level_db = dbfs(level)

        # 前 200ms 只采样不判定：避免把起播的爆音当成打断
        if self._elapsed_ms < self.cfg.warmup_ms:
            self._elapsed_ms += dur_ms
            self._levels.append(level)
            return False

        baseline = float(np.median(self._levels)) if self._levels else level
        self.last_baseline_db = dbfs(baseline)
        self._levels.append(level)

        louder = dbfs(level) - dbfs(max(baseline, 1e-6)) >= self.cfg.margin_db
        if louder:
            self._speech_ms += dur_ms
        else:
            self._speech_ms = 0.0
        self._elapsed_ms += dur_ms

        if self._speech_ms >= self.cfg.min_speech_ms:
            self._speech_ms = 0.0
            return True
        return False
