"""记忆共享类型与常量。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Layer = Literal["perm", "short"]
MemoryType = Literal[
    "profile",
    "preference",
    "rule",
    "task",
    "episode",
    "context",
    "digest",
]
PERM_TYPES = frozenset({"profile", "preference", "rule", "task"})
SHORT_TYPES = frozenset({"episode", "context", "digest"})

TYPE_PRIOR: dict[str, float] = {
    "profile": 1.0,
    "rule": 0.95,
    "task": 0.9,
    "preference": 0.85,
    "digest": 0.55,
    "episode": 0.5,
    "context": 0.45,
}

# 拼装顺序：永久档案 → 规则 → 提醒 → 偏好 → 高热度短期 → 近期摘要
PACK_ORDER: dict[str, int] = {
    "profile": 0,
    "rule": 1,
    "task": 2,
    "preference": 3,
    "episode": 10,
    "context": 11,
    "digest": 12,
}

DEFAULT_HALF_LIFE_DAYS = 180.0
HEAT_BOOST_ON_RECALL = 0.15
ARCHIVE_HEAT_THRESHOLD = 0.08
ARCHIVE_MIN_AGE_DAYS = 30.0
TOMBSTONE_TTL_DAYS = 30.0
SIM_MERGE = 0.95
SIM_CANDIDATE = 0.86
SIM_CLUSTER = 0.9
SIM_DEDUP = 0.92
RECALL_HEAT_BOOST = HEAT_BOOST_ON_RECALL


@dataclass
class MemoryRecord:
    id: str
    layer: Layer
    type: MemoryType
    content: str
    summary: str = ""
    slot: str = ""
    tags: list[str] = field(default_factory=list)
    importance: float = 0.5
    heat: float = 0.5
    recall_count: int = 0
    created_at: float = 0.0
    last_activity_at: float = 0.0
    last_recall_at: float = 0.0
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS
    is_archived: bool = False
    superseded_by: str = ""
    session_id: str = ""
    source: str = "observe"
    ttl_at: float | None = None
    # 召回时填充
    sim: float = 0.0
    score: float = 0.0

    def text(self) -> str:
        return (self.summary or self.content or "").strip()


@dataclass
class ClassifyResult:
    should_store: bool = False
    layer: Layer = "short"
    type: MemoryType = "episode"
    slot: str = ""
    content: str = ""
    summary: str = ""
    importance: float = 0.5
    tags: list[str] = field(default_factory=list)
    conflict_hint: str = ""
    profile_key: str = ""
    schedule: dict[str, Any] | None = None
    erase: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class WriteResult:
    action: str  # insert | refresh | supersede | merge | skip | blocked
    memory_id: str = ""
    message: str = ""
    archived_ids: list[str] = field(default_factory=list)


@dataclass
class EraseResult:
    ok: bool
    message: str
    erased_ids: list[str] = field(default_factory=list)
    need_confirm: bool = False
    candidates: list[MemoryRecord] = field(default_factory=list)
