"""多任务编排的数据模型。

一句话的需求 → 若干 TaskSpec（DAG 节点）→ 运行期用 TaskRecord 跟踪状态，
一次提交对应一个 TaskRun。子任务（sub-agent）再拆一层时，就是嵌套的 TaskRun。

状态只有六个，且**单向流动**：queued → running → done/failed/skipped/cancelled。
没有重试、没有回滚：失败就如实告知（用户明确要求「失败的任务不需要重试」）。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"
CANCELLED = "cancelled"

TERMINAL = frozenset({DONE, FAILED, SKIPPED, CANCELLED})

STATUS_LABEL = {
    QUEUED: "排队中",
    RUNNING: "进行中",
    DONE: "已完成",
    FAILED: "失败",
    SKIPPED: "已跳过",
    CANCELLED: "已取消",
}

# 工具契约是「返回值就是要念出来的那句中文」，没有结构化 error 字段，
# 所以成功/失败只能靠文案判断。宁可多判成失败：判错的代价是多跳过一件依赖任务，
# 反过来的代价是对着用户演「已打开」。
FAIL_MARKS = (
    "没找到",
    "指代不清",
    "参数不对",
    "工具执行异常",
    "执行异常",
    "操作失败",
    "播报失败",
    "失败",
    "不在线",
    "离线",
    "未启用",
    "暂时查不到",
    "没有可用",
    "不支持的动作",
    "请告诉我要查哪个城市",
    "请提供",
    "还没接",
)


def looks_failed(text: str) -> bool:
    """工具返回是否代表「这一步没成」。"""
    t = (text or "").strip()
    if not t:
        return True
    return any(mark in t for mark in FAIL_MARKS)


def new_run_id() -> str:
    return time.strftime("r%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]


@dataclass
class TaskSpec:
    """规划器产出的一条任务。"""

    id: str
    title: str
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    kind: str = "tool"  # tool=调一个工具；subplan=太大，交给子 agent 再拆一层
    goal: str = ""  # kind=subplan 时的自然语言目标
    needs_voice: bool = True  # False=执行但不播报（纯准备工作）
    depends_on: tuple[str, ...] = ()

    @property
    def is_group(self) -> bool:
        return self.kind == "subplan"

    def brief(self) -> str:
        return self.title or self.tool or self.id


@dataclass
class TaskRecord:
    """运行期一条任务的状态。"""

    spec: TaskSpec
    status: str = QUEUED
    result: str = ""  # 工具原始返回（可朗读）
    note: str = ""  # 内部备注：失败的简短原因、跳过原因
    started_at: float = 0.0  # time.perf_counter()
    finished_at: float = 0.0
    announced: bool = False
    attempts: int = 0

    @property
    def ok(self) -> bool:
        return self.status == DONE

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    @property
    def elapsed(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.finished_at or time.perf_counter()
        return max(0.0, end - self.started_at)

    def label(self) -> str:
        return STATUS_LABEL.get(self.status, self.status)


@dataclass
class TaskRun:
    """一次提交（一句话）的全部任务。"""

    id: str
    text: str
    tasks: dict[str, TaskRecord]
    depth: int = 0
    parent: tuple[str, str] | None = None  # (父 run id, 父任务 id)
    announce_leaf: bool = True  # 子任务默认不逐条播报，由父任务汇总
    created_at: float = field(default_factory=time.time)
    summary: str = ""
    error: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    cancelled: bool = False  # 整轮取消
    finished: threading.Event = field(default_factory=threading.Event, repr=False)
    seq: int = 0  # 播报顺序号（只在锁内自增）

    # ---------------------------------------------------------------- 读取

    def records(self) -> list[TaskRecord]:
        """按规划顺序返回（dict 保序，Python 3.7+ 可靠）。"""
        return list(self.tasks.values())

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for rec in self.records():
            out[rec.status] = out.get(rec.status, 0) + 1
        return out

    @property
    def active(self) -> list[TaskRecord]:
        return [rec for rec in self.records() if not rec.terminal]

    def running(self) -> list[TaskRecord]:
        return [rec for rec in self.records() if rec.status == RUNNING]

    def queued(self) -> list[TaskRecord]:
        return [rec for rec in self.records() if rec.status == QUEUED]

    @property
    def done(self) -> bool:
        return all(rec.terminal for rec in self.records())

    # ---------------------------------------------------------------- 变更

    def set_status(self, rec: TaskRecord, status: str, *, result: str = "", note: str = "") -> None:
        with self.lock:
            if rec.terminal and status not in TERMINAL:
                return  # 已到终态，不再回滚
            rec.status = status
            if result:
                rec.result = result
            if note:
                rec.note = note
            if status == RUNNING:
                rec.started_at = time.perf_counter()
                rec.attempts += 1
            if status in TERMINAL:
                rec.finished_at = time.perf_counter()

    def next_seq(self) -> int:
        with self.lock:
            self.seq += 1
            return self.seq

    def mark_cancelled_all(self) -> int:
        """取消整轮：未开始/在跑的都标记取消，返回受影响条数。"""
        n = 0
        with self.lock:
            self.cancelled = True
            for rec in self.tasks.values():
                if rec.status in (QUEUED, RUNNING):
                    rec.status = CANCELLED
                    rec.note = "已取消"
                    rec.finished_at = rec.finished_at or time.perf_counter()
                    n += 1
        return n

    def cancel_task(self, rec: TaskRecord) -> bool:
        with self.lock:
            if rec.status == QUEUED:
                rec.status = CANCELLED
                rec.note = "已取消"
                rec.finished_at = time.perf_counter()
                return True
            if rec.status == RUNNING:
                # 正在跑的工具调用没法硬掐（HA 的 HTTP 请求不可中断），
                # 标取消后结果丢弃不播报。
                rec.status = CANCELLED
                rec.note = "已取消（执行中，结果不再播报）"
                return True
            return False