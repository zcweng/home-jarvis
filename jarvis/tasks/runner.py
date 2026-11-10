"""任务执行器：并发闸门 + 依赖检查 + 不重试 + 完成即播报。

三条硬规则（都是需求里点名的）：

1. **并发度有上限，超出的排队**：叶子任务执行前必须拿到 `ConcurrencyGate` 的一个槽位；
   拿不到就原地等（状态保持「排队中」），有人腾出来才轮得到它。
   闸门放在「工具执行」这一层，而不是任务图那一层——嵌套子图共用同一把闸，
   父任务不占槽位，所以不会出现「父等子、子等父」的自等死锁。
2. **失败不重试**：`attempts` 只增一次。工具返回里带失败文案就记 `failed`，
   把原话念给用户听，依赖它的任务直接跳过并说明原因。
3. **结果按完成顺序播报**：谁先跑完谁先入播报队列；不需要播报的（needs_voice=false）
   只记账不出声。子任务默认不逐条播报，由父任务汇总一句。
"""

from __future__ import annotations

import sys
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from . import graph as task_graph
from .announce import AnnounceItem, Announcer
from .model import (
    CANCELLED,
    DONE,
    FAILED,
    QUEUED,
    RUNNING,
    SKIPPED,
    TaskRecord,
    TaskRun,
    looks_failed,
    new_run_id,
)
from .plan import Planner, topo_order

DispatchFn = Callable[[str, dict[str, Any]], str]
LogFn = Callable[..., None]


def _warn(msg: str) -> None:
    print(f"  ⚠️  任务执行：{msg}", file=sys.stderr)


class ConcurrencyGate:
    """容量固定的执行槽位；超出容量的任务排队等待。"""

    def __init__(self, capacity: int = 2) -> None:
        self.capacity = max(1, int(capacity))
        self._sem = threading.BoundedSemaphore(self.capacity)
        self._lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.queued_ever = 0

    @contextmanager
    def slot(self, should_stop: Callable[[], bool] | None = None) -> Iterator[bool]:
        """拿一个槽位；yield False 表示等待期间被叫停（取消），调用方应直接返回。"""
        if should_stop is not None and should_stop():
            yield False
            return
        got = self._sem.acquire(blocking=False)
        if not got:
            with self._lock:
                self.queued_ever += 1
            while not got:
                got = self._sem.acquire(timeout=0.2)
                if not got and should_stop is not None and should_stop():
                    yield False
                    return
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            yield True
        finally:
            with self._lock:
                self.active -= 1
            self._sem.release()

    def stats(self) -> dict[str, int]:
        return {"capacity": self.capacity, "active": self.active, "peak": self.peak}


class TaskRunner:
    """跑完一个 TaskRun（含嵌套子 run）。"""

    def __init__(
        self,
        *,
        dispatch: DispatchFn,
        gate: ConcurrencyGate,
        announcer: Announcer,
        planner: Planner | None = None,
        max_depth: int = 2,
        announce_failures: bool = True,
        max_tasks: int = 8,
        log: LogFn | None = None,
        on_finish: Callable[[TaskRun], None] | None = None,
    ) -> None:
        self.dispatch = dispatch
        self.gate = gate
        self.announcer = announcer
        self.planner = planner
        self.max_depth = max(0, int(max_depth))
        self.announce_failures = announce_failures
        self.max_tasks = max(1, int(max_tasks))
        self._log = log
        self.on_finish = on_finish

    # ---------------------------------------------------------------- 入口

    def execute(self, run: TaskRun) -> None:
        t0 = time.perf_counter()
        self._log_event(run, "run_start", text=run.text, depth=run.depth)
        try:
            specs = topo_order([rec.spec for rec in run.records()])
            if not specs:
                run.error = "没有可执行的任务"
            elif not task_graph.available():
                run.error = "没有安装 langgraph（uv sync --extra tasks）"
            else:
                task_graph.run_graph(
                    specs,
                    lambda spec, seen: self._node(run, spec, seen),
                    recursion_limit=20 + len(specs) * 10,
                )
        except Exception as e:  # noqa: BLE001 — 图炸了也要给用户一个交代
            run.error = f"任务调度失败：{e}"
            _warn(run.error)
        finally:
            self._finalize(run, time.perf_counter() - t0)
            if self.on_finish is not None:
                try:
                    self.on_finish(run)
                except Exception as e:  # noqa: BLE001
                    _warn(f"收尾回调失败：{e}")

    def _finalize(self, run: TaskRun, elapsed: float) -> None:
        """图异常中断时，把还挂着的任务标成失败，别让状态永远停在「进行中」。"""
        with run.lock:
            stragglers = [rec for rec in run.tasks.values() if not rec.terminal]
        for rec in stragglers:
            reason = run.error or "任务没跑起来"
            run.set_status(rec, CANCELLED if run.cancelled else FAILED, result=reason, note=reason)
        run.summary = summarize(run)
        run.finished.set()
        self._log_event(
            run,
            "run_end",
            elapsed=round(elapsed, 3),
            counts=run.counts(),
            error=run.error,
        )

    # ---------------------------------------------------------------- 单节点

    def _node(self, run: TaskRun, spec, seen: dict[str, str]) -> str:  # noqa: ANN001
        rec = run.tasks[spec.id]

        # 已取消 / 依赖没成 → 直接跳过，不调用任何工具
        if run.cancelled or rec.status == CANCELLED:
            return rec.result or "已取消"
        blocked = self._blocked_by_dependency(run, rec)
        if blocked:
            run.set_status(rec, SKIPPED, result=blocked, note=blocked)
            self._log_event(run, "skip", rec=rec, note=blocked)
            print(f"  ⏭  {rec.spec.title}：{blocked}")
            self._announce_task(run, rec)  # 依赖没成也要说一声，否则用户以为漏了
            return blocked

        try:
            if spec.is_group:
                out = self._run_group(run, rec, seen)
            else:
                out = self._run_leaf(run, rec)
        except Exception as e:  # noqa: BLE001 — 绝不让异常冒进图里
            out = f"执行出错：{e}"
            run.set_status(rec, FAILED, result=out, note=str(e))
            self._log_event(run, "error", rec=rec, error=str(e))
            _warn(f"{rec.spec.title} 崩了：{e}")

        if rec.status in (QUEUED, RUNNING):  # 兜底：异常路径没写状态
            run.set_status(rec, DONE if not looks_failed(out) else FAILED, result=out)
        if rec.status != CANCELLED:
            self._announce_task(run, rec)
        return out

    @staticmethod
    def _blocked_by_dependency(run: TaskRun, rec: TaskRecord) -> str:
        for dep_id in rec.spec.depends_on:
            dep = run.tasks.get(dep_id)
            if dep is None or dep.status == DONE:
                continue
            return f"前置「{dep.spec.title}」{dep.label()}"
        return ""

    def _run_leaf(self, run: TaskRun, rec: TaskRecord) -> str:
        spec = rec.spec
        with self.gate.slot(should_stop=lambda: run.cancelled or rec.status == CANCELLED) as ok:
            if not ok:
                run.set_status(rec, CANCELLED, result="已取消", note="排队时被取消")
                self._log_event(run, "cancel", rec=rec)
                return "已取消"
            run.set_status(rec, RUNNING)
            self._log_event(run, "start", rec=rec, tool=spec.tool, args=spec.args)
            print(f"  ▶  {spec.title}（{spec.tool} {spec.args}）· 并发 {self.gate.active}/{self.gate.capacity}")
            t0 = time.perf_counter()
            out = self.dispatch(spec.tool, dict(spec.args))  # 不重试：调一次就是一次
            ms = (time.perf_counter() - t0) * 1000
        if rec.status == CANCELLED:
            self._log_event(run, "cancel", rec=rec, note="执行中被取消")
            return out
        status = FAILED if looks_failed(out) else DONE
        run.set_status(rec, status, result=str(out), note="" if status == DONE else str(out)[:60])
        self._log_event(run, "done", rec=rec, status=status, ms=round(ms), result=str(out)[:200])
        print(f"  {'✅' if status == DONE else '❌'} {spec.title}（{ms:.0f}ms）→ {str(out)[:50]}")
        return str(out)

    def _run_group(self, run: TaskRun, rec: TaskRecord, seen: dict[str, str]) -> str:
        """大任务 → 子 agent 再拆一层，跑一个嵌套 run。"""
        spec = rec.spec
        if self.planner is None:
            out = "没法再拆小任务（规划器不可用）"
            run.set_status(rec, FAILED, result=out, note=out)
            return out
        if run.depth + 1 >= self.max_depth:
            # 嵌套层数到顶（默认最多两层）：如实说拆不动，不装成功
            out = "这件事太大，一层拆不完，我先按刚才知道的做"
            run.set_status(rec, FAILED, result=out, note="depth_limit")
            self._log_event(run, "depth_limit", rec=rec, depth=run.depth)
            return out

        run.set_status(rec, RUNNING)
        self._log_event(run, "group_start", rec=rec, goal=spec.goal, depth=run.depth)
        print(f"  🧩 {spec.title}：拆子任务 → {spec.goal}")
        context = "；".join(f"{run.tasks[t].spec.title}：{seen[t]}" for t in seen if t in run.tasks)[:600]
        plan = self.planner.plan_goal(
            spec.goal,
            context=context,
            allow_group=(run.depth + 2) < self.max_depth,
        )
        if not plan.ok:
            out = f"子任务拆不出来：{plan.error or '模型没给出任务'}"
            run.set_status(rec, FAILED, result=out, note=plan.error)
            return out

        nested = TaskRun(
            id=new_run_id(),
            text=spec.goal,
            tasks={s.id: TaskRecord(spec=s) for s in plan.tasks},
            depth=run.depth + 1,
            parent=(run.id, spec.id),
            announce_leaf=False,  # 子任务不逐条念，父任务汇总一句
        )
        self.execute(nested)
        text = group_text(nested)
        status = DONE if all(r.status == DONE for r in nested.records()) else FAILED
        run.set_status(rec, status, result=text, note="" if status == DONE else text[:60])
        self._log_event(run, "group_done", rec=rec, status=status, nested=nested.id, result=text[:200])
        return text

    # ---------------------------------------------------------------- 播报

    def _announce_task(self, run: TaskRun, rec: TaskRecord) -> None:
        if not run.announce_leaf:
            return
        if not rec.spec.needs_voice:
            self._log_event(run, "silent", rec=rec, status=rec.status)
            return
        if rec.status == FAILED and not self.announce_failures:
            return
        text = str(rec.result or "").strip()
        if not text:
            return
        if rec.status == FAILED:
            text = f"{rec.spec.title}没做成：{text}"
        elif rec.status == SKIPPED:
            text = f"{rec.spec.title}先不做了：{text}"
        elif rec.status == CANCELLED:
            return
        if rec.announced:
            return
        queued = self.announcer.push(
            AnnounceItem(
                run_id=run.id,
                task_id=rec.spec.id,
                text=text,
                seq=run.next_seq(),
                title=rec.spec.title,
                status=rec.status,
            )
        )
        rec.announced = bool(queued)  # 静音模式（TASK_ANNOUNCE=false）下保持 False，别自欺

    # ---------------------------------------------------------------- 日志

    def _log_event(self, run: TaskRun, event: str, *, rec: TaskRecord | None = None, **fields: Any) -> None:
        if self._log is None:
            return
        try:
            self._log(run, event, rec, fields)
        except Exception:  # noqa: BLE001 — 日志永远不能影响任务
            pass


def summarize(run: TaskRun) -> str:
    """给主会话的一句话总结（**不带细节**，避免上下文膨胀）。

    例：（任务完成 2/3）查天气✓ 开客厅灯✓ 关晾衣架✗
    """
    recs = run.records()
    if not recs:
        return ""
    total = len(recs)
    ok = sum(1 for r in recs if r.status == DONE)
    parts = []
    for r in recs:
        mark = "✓" if r.status == DONE else ("✗" if r.status in (FAILED, SKIPPED) else "－")
        parts.append(f"{r.spec.title}{mark}")
    head = f"（任务完成 {ok}/{total}）" if ok != total else f"（任务完成 {total} 件）"
    return head + " ".join(parts)


def group_text(run: TaskRun) -> str:
    """子任务汇总成一句可念的话。"""
    bits = []
    for rec in run.records():
        text = str(rec.result or "").strip()
        if not text:
            continue
        if rec.status == FAILED:
            text = f"{rec.spec.title}没做成"
        bits.append(text)
    out = "；".join(bits)
    return out[:180]