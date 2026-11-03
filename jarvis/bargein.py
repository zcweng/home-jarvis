"""打断（barge-in）检测。

难点不在"检测到有人说话"，而在**不把自己打断**：喇叭回声 / AEC 残留有起伏，
响音节会短暂高过中位数，被误判成插话。

做法：
- 用最近窗口的高分位（默认 85%）跟踪回声底
- 当前能量比基线高 margin_db 时进入「候选打断」，**期间不更新基线**
  （避免人声自己把底抬高后永远差不够）
- 候选需持续 min_speech_ms；短促尖峰会掉回去并清零
- 起播 warmup 内只采样不判定
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
    margin_db: float = 18.0  # 音箱回声起伏常见 10–15dB，12 偏易误触
    min_speech_ms: float = 500.0
    baseline_window_s: float = 1.5
    warmup_ms: float = 400.0
    baseline_percentile: float = 85.0
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

        if self._elapsed_ms < self.cfg.warmup_ms:
            self._elapsed_ms += dur_ms
            self._levels.append(level)
            return False

        if self._levels:
            baseline = float(np.percentile(self._levels, self.cfg.baseline_percentile))
        else:
            baseline = level
        self.last_baseline_db = dbfs(baseline)

        louder = dbfs(level) - dbfs(max(baseline, 1e-6)) >= self.cfg.margin_db
        if louder:
            # 候选语音：不把当前帧写进基线，防止人声抬底
            self._speech_ms += dur_ms
        else:
            self._levels.append(level)
            self._speech_ms = 0.0
        self._elapsed_ms += dur_ms

        if self._speech_ms >= self.cfg.min_speech_ms:
            self._speech_ms = 0.0
            return True
        return False
