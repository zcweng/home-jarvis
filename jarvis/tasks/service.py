"""任务编排的总入口：提交、查询、取消、收尾。

对外只有四个动作，全部可以被语音工具直接转发：
    submit(一句话)   → 拆任务、起后台线程、回一句确认
    describe()       → 「现在做到哪了」（排队中/进行中/已完成逐条列）
    cancel(目标)     → 「取消第 2 件」/「全部取消」
    close()          → 退出时收线

设计取舍：
- **提交是同步拆解、异步执行**：规划（一次 LLM）在主对话里等，因为要立刻回一句确认；
  真正干活在后台线程，用户可以接着说下一句。
- **主会话只留一句总结**：明细写 `data/tasks/<run>.jsonl`，历史里只有
  「（任务完成 2/3）查天气✓ 关灯✗」，避免长任务把上下文撑爆。
- **取消是协作式的**：排队中的任务直接不跑；已在跑的 HTTP 调用没法硬掐，
  标成取消后丢弃结果、不播报。
"""

from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..config import Settings
from ..tools import Tools
from . import graph as task_graph
from .announce import Announcer
from .model import FAILED, QUEUED, TaskRecord, TaskRun, new_run_id
from .plan import LLMFn, Planner, describe_plan, topo_order
from .runner import ConcurrencyGate, TaskRunner, summarize

_CN_NUM = {1: "一", 2: "两", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九", 10: "十"}


def _warn(msg: str) -> None:
    print(f"  ⚠️  任务编排：{msg}", file=sys.stderr)


def cn_num(n: int) -> str:
    return _CN_NUM.get(n, str(n))


@dataclass
class TaskSettings:
    enabled: bool = True
    max_concurrency: int = 2  # 同时在跑的工具任务上限，超出的排队
    max_tasks: int = 8  # 一句话最多拆几件
    max_depth: int = 2  # 子 agent 嵌套层数（1=不拆子任务）
    announce: bool = True  # False=只记账不出声（夜里/自检用）
    announce_failures: bool = True
    idle_wait: float = 12.0  # 等主对话说完再播任务结果
    log_dir: str = "data/tasks"

    @classmethod
    def from_settings(cls, st: Settings) -> TaskSettings:
        return cls(
            enabled=bool(st.task_enabled),
            max_concurrency=max(1, int(st.task_max_concurrency)),
            max_tasks=max(1, int(st.task_max_tasks)),
            max_depth=max(1, int(st.task_max_depth)),
            announce=bool(st.task_announce),
            announce_failures=bool(st.task_announce_failures),
            idle_wait=float(st.task_idle_wait),
            log_dir=str(st.task_log_dir),
        )


@dataclass
class SubmitResult:
    accepted: bool = False
    run_id: str = ""
    ack: str = ""
    reason: str = ""
    count: int = 0
    plan_ms: float = 0.0
    specs: list[Any] = field(default_factory=list)


class TaskService:
    def __init__(
        self,
        settings: TaskSettings,
        tools: Tools,
        llm: LLMFn | None,
        *,
        speak: Callable[[str], None],
        is_idle: Callable[[], bool] | None = None,
        on_busy: Callable[[bool], None] | None = None,
        on_summary: Callable[[TaskRun], None] | None = None,
        root: Path | None = None,
    ) -> None:
        self.settings = settings
        self.tools = tools
        self.on_summary = on_summary
        self._root = root or Path(__file__).resolve().parent.parent.parent
        self._lock = threading.Lock()
        self._runs: list[TaskRun] = []
        self._seq = 0
        self.gate = ConcurrencyGate(settings.max_concurrency)
        self.announcer = Announcer(
            speak,
            is_idle=is_idle,
            on_busy=on_busy,
            idle_wait=settings.idle_wait,
            enabled=settings.announce,
        )
        self.planner = Planner(llm, tools, max_tasks=settings.max_tasks) if llm is not None else None
        self.runner = TaskRunner(
            dispatch=self._dispatch,
            gate=self.gate,
            announcer=self.announcer,
            planner=self.planner,
            max_depth=settings.max_depth,
            announce_failures=settings.announce_failures,
            max_tasks=settings.max_tasks,
            log=self._log_event,
            on_finish=self._on_run_finish,
        )
        self._log_dir: Path | None = None
        if settings.enabled:
            try:
                self._log_dir = Path(settings.log_dir)
                if not self._log_dir.is_absolute():
                    self._log_dir = self._root / self._log_dir
                self._log_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:  # noqa: BLE001 — 只读盘也别影响主链路
                _warn(f"任务日志目录不可写，明细不落盘：{e}")
                self._log_dir = None

    # ---------------------------------------------------------------- 能力

    def enabled(self) -> bool:
        """能不能跑/查/取消任务。规划模型缺了不影响查询与取消。"""
        if not self.settings.enabled:
            return False
        if not task_graph.available():
            _warn("多任务编排需要 langgraph：uv sync --extra tasks")
            return False
        return True

    def can_plan(self) -> bool:
        """能不能把一句话拆成任务（缺模型时主流程要退回普通单轮）。"""
        return self.enabled() and self.planner is not None

    def why_disabled(self) -> str:
        if not self.settings.enabled:
            return "多任务编排未开启（TASK_ENABLED）"
        if not task_graph.available():
            return "多任务编排需要 langgraph（uv sync --extra tasks）"
        if self.planner is None:
            return "多任务编排缺规划模型（假的 LLM 或没配 key）"
        return ""

    # ---------------------------------------------------------------- 提交

    def submit(self, text: str, context: str = "") -> SubmitResult:
        """拆任务并起后台执行；不接受时调用方退回普通单轮。"""
        text = (text or "").strip()
        if not self.can_plan():
            return SubmitResult(reason=self.why_disabled())
        if not text:
            return SubmitResult(reason="空指令")

        planner = self.planner
        assert planner is not None
        plan = planner.plan(text, context=context)
        if not plan.ok:
            return SubmitResult(reason=plan.error or "没拆出任务", plan_ms=plan.elapsed_ms)
        if len(plan.tasks) < 2:
            # 只有一件，没必要进后台：省一次播报、也省掉"排队"这套仪式
            return SubmitResult(reason="只拆出 1 件任务", plan_ms=plan.elapsed_ms, count=len(plan.tasks))

        return self.submit_specs(plan.tasks, text, plan_ms=plan.elapsed_ms)

    def submit_specs(self, specs_in: list[Any], text: str, *, plan_ms: float = 0.0) -> SubmitResult:
        """已经拿到 TaskSpec 时直接开跑（规划器已在外面做完；自检也走这条路）。"""
        specs = topo_order(list(specs_in))
        if not specs:
            return SubmitResult(reason="没有可执行的任务", plan_ms=plan_ms)
        run = TaskRun(
            id=new_run_id(),
            text=text,
            tasks={s.id: TaskRecord(spec=s) for s in specs},
            depth=0,
        )
        with self._lock:
            self._runs.append(run)
            self._runs = self._runs[-20:]  # 只留最近 20 次，避免长跑内存长
            self._seq += 1
            idx = self._seq

        print(
            f"  🧠 任务拆分 #{idx}（{plan_ms:.0f}ms，{len(specs)} 件）：{describe_plan(specs)}",
            flush=True,
        )
        ack = self._ack(specs)
        t = threading.Thread(
            target=self.runner.execute,
            args=(run,),
            name=f"jarvis-tasks-{run.id}",
            daemon=True,
        )
        t.start()
        return SubmitResult(
            accepted=True,
            run_id=run.id,
            ack=ack,
            count=len(specs),
            plan_ms=plan_ms,
            specs=specs,
        )

    @staticmethod
    def _ack(specs: list[Any]) -> str:
        n = len(specs)
        return f"好，{cn_num(n)}件事，我分开做。"

    # ---------------------------------------------------------------- 查询

    def _current_run(self) -> TaskRun | None:
        with self._lock:
            runs = list(self._runs)
        for run in reversed(runs):
            if run.depth == 0 and not run.finished.is_set():
                return run
        return runs[-1] if runs else None

    def describe(self) -> str:
        """「现在做到哪了」——逐条列状态，排队中的也要说清。"""
        if not self.enabled():
            return f"{self.why_disabled()}。"
        run = self._current_run()
        if run is None:
            return "现在没有后台任务。"

        recs = run.records()
        done = run.finished.is_set()
        parts: list[str] = []
        for i, rec in enumerate(recs, 1):
            label = rec.label()
            if rec.status == FAILED and rec.note:
                label += f"（{rec.note[:18]}）"
            parts.append(f"第{i}件{rec.spec.title}{label}")
        head = "刚才那件已经做完了：" if done else "现在手上的任务："
        tail = ""
        if not done:
            queued = sum(1 for r in recs if r.status == QUEUED)
            if queued:
                tail = f"同时最多做{self.gate.capacity}件，另外{cn_num(queued)}件在排队。"
        return head + "；".join(parts) + "。" + tail

    def status_text(self) -> str:
        """给日志/自检用的紧凑状态。"""
        run = self._current_run()
        if run is None:
            return "→ 无任务"
        pairs = " ".join(f"{r.spec.id}:{r.status}" for r in run.records())
        return f"{'完成' if run.finished.is_set() else '进行'} {pairs} 并发={self.gate.active}/{self.gate.capacity}"

    # ---------------------------------------------------------------- 取消

    def cancel(self, target: str = "") -> str:
        if not self.enabled():
            return f"{self.why_disabled()}。"
        run = self._current_run()
        if run is None:
            return "现在没有可以取消的任务。"
        if run.finished.is_set():
            return "刚才的任务都已经结束了，没有可以取消的。"

        t = (target or "").strip()
        if t in ("", "全部", "所有", "都", "全", "all", "ALL"):
            n = run.mark_cancelled_all()
            if n:
                self._log_event(run, "cancel_all", None, {"by": "voice", "count": n})
                print(f"  ⛔ 取消整个任务组 #{run.id}（{n} 件）")
                return f"好，剩下的{cn_num(n)}件都取消了。"
            return "手上的都已经做完了，没什么可取消的。"

        # 用完整列表选：describe() 报的「第 2 件」和这里必须是同一件
        rec = self._pick(recs=run.records(), target=t)
        if rec is None:
            return f"没找到叫「{t}」的任务。"
        if rec.terminal:
            return f"「{rec.spec.title}」已经{rec.label()}了，取消不了。"
        if not run.cancel_task(rec):
            return f"「{rec.spec.title}」已经{rec.label()}了，取消不了。"
        self._log_event(run, "cancel", rec, {"by": "voice", "target": t})
        print(f"  ⛔ 取消任务 {rec.spec.id} {rec.spec.title}")
        return f"好，{rec.spec.title}取消了。"

    @staticmethod
    def _pick(*, recs: list[TaskRecord], target: str) -> TaskRecord | None:
        """按「第2个 / 2 / 名字片段」找一条任务（编号与 describe() 一致）。"""
        t = target.strip()
        if not t:
            return None
        digits = "".join(ch for ch in t if ch.isdigit())
        if digits:
            try:
                idx = int(digits)
            except ValueError:
                idx = 0
            if 1 <= idx <= len(recs):
                return recs[idx - 1]
        for rec in recs:
            if t in rec.spec.title or rec.spec.title in t:
                return rec
        return None

    # ---------------------------------------------------------------- 收尾

    def _on_run_finish(self, run: TaskRun) -> None:
        if run.depth != 0:
            return
        summary = run.summary or summarize(run)
        print(f"  🏁 {summary}", flush=True)
        if self.on_summary is not None:
            try:
                self.on_summary(run)
            except Exception as e:  # noqa: BLE001
                _warn(f"任务总结回调失败：{e}")

    # ---------------------------------------------------------------- 执行/日志

    def _dispatch(self, name: str, args: dict[str, Any]) -> str:
        """工具调用：一次就是一次，异常吞成可朗读的失败文案。"""
        try:
            return str(self.tools.dispatch(name, dict(args)))
        except Exception as e:  # noqa: BLE001
            return f"工具执行异常：{e}"

    def _log_event(self, run: TaskRun, event: str, rec: TaskRecord | None = None, fields: dict | None = None) -> None:
        if self._log_dir is None:
            return
        line: dict[str, Any] = {
            "ts": round(time.time(), 3),
            "run": run.id,
            "depth": run.depth,
            "event": event,
        }
        if rec is not None:
            line["task"] = rec.spec.id
            line["title"] = rec.spec.title
            line["status"] = rec.status
        if fields:
            line.update(fields)
        try:
            path = self._log_dir / f"{run.id}.jsonl"
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
        except Exception:  # noqa: BLE001
            pass

    def log_lines(self, run_id: str) -> list[dict[str, Any]]:
        """读回某次任务的明细（自检/排障用）。"""
        if self._log_dir is None:
            return []
        path = self._log_dir / f"{run_id}.jsonl"
        if not path.exists():
            return []
        out = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
        return out

    def close(self) -> None:
        try:
            self.announcer.close()
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------------- 自检辅助

    def last_run(self) -> TaskRun | None:
        with self._lock:
            return self._runs[-1] if self._runs else None

    def wait_run(self, run_id: str = "", timeout: float = 30.0) -> TaskRun | None:
        """等某次任务（默认最近一次）跑完；超时返回 None。"""
        run = self.last_run() if not run_id else next((r for r in self._runs if r.id == run_id), None)
        if run is None:
            return None
        if run.finished.wait(timeout):
            return run
        return None

    def drain_announcements(self, timeout: float = 5.0) -> None:
        """等播报队列清空（自检用，避免断言和播报线程赛跑）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.announcer.pending == 0:
                time.sleep(0.05)
                if self.announcer.pending == 0:
                    return
            time.sleep(0.02)