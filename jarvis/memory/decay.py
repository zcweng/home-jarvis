"""衰减与热度纯函数。"""

from __future__ import annotations

import math
from typing import Iterable

from .types import (
    ARCHIVE_HEAT_THRESHOLD,
    ARCHIVE_MIN_AGE_DAYS,
    DEFAULT_HALF_LIFE_DAYS,
    HEAT_BOOST_ON_RECALL,
    MemoryRecord,
)

SECONDS_PER_DAY = 86400.0


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def time_decay(
    now: float,
    last_activity_at: float,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    *,
    layer: str = "short",
) -> float:
    """永久记忆不衰减；短期记忆按半衰期指数衰减。"""
    if layer == "perm" or half_life_days <= 0:
        return 1.0
    age_days = max(0.0, (now - last_activity_at) / SECONDS_PER_DAY)
    return 2.0 ** (-age_days / half_life_days)


def recency_of_recalls(
    now: float,
    recall_times: Iterable[float],
    *,
    half_life_days: float = 7.0,
) -> float:
    """近 N 次召回时间的指数加权，范围 [0, 1]。"""
    times = sorted(t for t in recall_times if t > 0)
    if not times:
        return 0.0
    total = 0.0
    weight = 0.0
    for i, t in enumerate(reversed(times[-8:])):
        w = 0.5**i
        age_days = max(0.0, (now - t) / SECONDS_PER_DAY)
        total += w * (2.0 ** (-age_days / half_life_days))
        weight += w
    return clamp(total / weight if weight else 0.0)


def compute_heat(
    importance: float,
    decay: float,
    recall_recency: float = 0.0,
    *,
    boost: float = 0.0,
) -> float:
    return clamp(importance * decay + 0.25 * recall_recency + boost)


def apply_recall_boost(heat: float, boost: float = HEAT_BOOST_ON_RECALL) -> float:
    return clamp(heat + boost)


def effective_heat(rec: MemoryRecord, now: float) -> float:
    """按当前时刻重算短期热度；永久记忆返回 importance。"""
    if rec.layer == "perm":
        return clamp(rec.importance)
    decay = time_decay(now, rec.last_activity_at, rec.half_life_days, layer=rec.layer)
    # 没有完整召回历史时，用 last_recall_at 近似
    recall_times = [rec.last_recall_at] if rec.last_recall_at else []
    rr = recency_of_recalls(now, recall_times)
    return compute_heat(rec.importance, decay, rr)


def should_archive(rec: MemoryRecord, now: float) -> bool:
    if rec.layer != "short" or rec.is_archived:
        return False
    age_days = (now - rec.created_at) / SECONDS_PER_DAY
    if age_days < ARCHIVE_MIN_AGE_DAYS:
        return False
    return effective_heat(rec, now) < ARCHIVE_HEAT_THRESHOLD


def estimate_tokens(text: str) -> int:
    """CJK 约 1 token/字；ASCII 词约 0.25 token/字符。留一点余量。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return max(1, cjk + math.ceil(other / 4))
