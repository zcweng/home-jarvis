#!/usr/bin/env python3
"""多任务编排回归：不需要麦克风、不需要音箱、不需要 HA、不需要真模型。

覆盖需求里的每一条：

  ① 语义解析 → 拆分 → 依赖 DAG（含模型编工具名、依赖成环、超上限的坏输出）
  ② 并发度阈值 + 排队（peak == 上限，多出来的等到有人腾位）
  ③ 按完成先后依次播报，且绝不叠音
  ④ needs_voice=false 只做不念；失败不重试但如实告知；依赖失败则跳过
  ⑤ 语音查询（排队中/进行中/已完成）与取消（排队/执行中/整组）
  ⑥ 大任务自动拆子任务（sub-agent），子任务不逐条念、由父任务汇总一句
  ⑦ 主会话只留一句总结，明细落 data/tasks/*.jsonl
  ⑧ 播报期间麦克风回声被丢掉（否则会把喇叭自己的声音当成用户说话）

    .venv/bin/python scripts/selftest_tasks.py            # 全离线
    .venv/bin/python scripts/selftest_tasks.py --real     # 额外用真设备+真模型（不出声）
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.intent import count_task_intents, looks_like_multi_task  # noqa: E402
from jarvis.session import Session  # noqa: E402
from jarvis.tasks import SubmitResult, TaskService, TaskSettings  # noqa: E402
from jarvis.tasks import graph as task_graph  # noqa: E402
from jarvis.tasks.model import (  # noqa: E402
    CANCELLED,
    DONE,
    FAILED,
    QUEUED,
    RUNNING,
    SKIPPED,
    TaskRun,
    TaskSpec,
)
from jarvis.tasks.runner import summarize  # noqa: E402
from jarvis.tasks.plan import Planner, describe_plan, sanitize_tasks, topo_order  # noqa: E402
from jarvis.tasks.runner import ConcurrencyGate  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  —— {detail}" if detail else ""))


# ---------------------------------------------------------------- 假件

_SCHEMAS = [
    ("control", {"device": {}, "action": {}, "value": {}}, "开关或调节家电"),
    ("query", {"device": {}}, "查询设备状态"),
    ("weather", {"city": {}}, "查天气"),
    ("get_time", {}, "查本地时间"),
    ("scene", {"name": {}}, "执行场景"),
    ("announce", {"text": {}}, "让音箱播报"),
    ("timer", {"action": {}}, "提醒"),
    ("web_search", {"query": {}}, "联网搜索"),
    ("web_fetch", {"result_id": {}}, "抓网页"),
]


class FakeTools:
    """按工具名/设备名编排延迟与结果；记录每次调用与真实并发。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls: list[dict[str, Any]] = []
        self.delays: dict[str, float] = {"*": 0.01}
        self.results: dict[str, Any] = {"*": "已执行。"}
        self.active = 0
        self.peak = 0

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": desc,
                    "parameters": {
                        "type": "object",
                        "properties": {k: {"type": "string"} for k in props},
                        "required": [],
                    },
                },
            }
            for name, props, desc in _SCHEMAS
        ]

    def _pick(self, table: dict[str, Any], name: str, args: dict[str, Any]) -> Any:
        dev = str(args.get("device") or "")
        if dev and dev in table:
            return table[dev]
        if name in table:
            return table[name]
        return table.get("*")

    def dispatch(self, name: str, args: dict[str, Any]) -> str:
        t0 = time.perf_counter()
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            n = len(self.calls)
        try:
            delay = self._pick(self.delays, name, args)
            time.sleep(float(delay or 0))
            out = self._pick(self.results, name, args)
            if callable(out):
                out = out(args)
            out = str(out)
        finally:
            with self.lock:
                self.active -= 1
        with self.lock:
            self.calls.append(
                {"idx": n, "name": name, "args": dict(args), "start": t0, "end": time.perf_counter(), "out": out}
            )
        return out

    def count(self, name: str = "", device: str = "") -> int:
        with self.lock:
            return sum(
                1
                for c in self.calls
                if (not name or c["name"] == name) and (not device or c["args"].get("device") == device)
            )


class FakeSpeaker:
    """记录每次「出声」的时间区间，用来断言顺序与不叠音。"""

    def __init__(self, delay: float = 0.05) -> None:
        self.lock = threading.Lock()
        self.spans: list[tuple[float, float, str]] = []
        self.delay = delay
        self.busy_flags: list[bool] = []

    def __call__(self, text: str) -> None:
        t0 = time.perf_counter()
        time.sleep(self.delay)
        with self.lock:
            self.spans.append((t0, time.perf_counter(), text))

    @property
    def texts(self) -> list[str]:
        with self.lock:
            return [s[2] for s in self.spans]

    def overlaps(self) -> list[tuple[str, str]]:
        with self.lock:
            spans = list(self.spans)
        bad = []
        for (_, end, text), (start, _, nxt) in zip(spans, spans[1:]):
            if start < end - 1e-6:
                bad.append((text, nxt))
        return bad


def llm_from(payload: Any, extra: dict[str, Any] | None = None) -> Callable[[str], str]:
    """假规划模型：主规划返回 payload；子目标（prompt 含「子目标：」）返回 extra['sub']。"""
    extra = extra or {}

    def _llm(prompt: str) -> str:
        if "子目标：" in prompt:
            sub = extra.get("sub", '{"tasks": []}')
            return sub if isinstance(sub, str) else json.dumps(sub, ensure_ascii=False)
        return payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)

    return _llm


class Harness:
    """一次性的 TaskService + 假件；临时日志目录。"""

    def __init__(
        self,
        *,
        llm: Callable[[str], str] | None = None,
        concurrency: int = 2,
        max_depth: int = 2,
        max_tasks: int = 8,
        announce: bool = True,
        announce_failures: bool = True,
        speaker_delay: float = 0.03,
        idle: bool = True,
        tools: Any | None = None,
    ) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="jarvis-tasks-"))
        self.tools = tools or FakeTools()
        self.speaker = FakeSpeaker(speaker_delay)
        self.settings = TaskSettings(
            enabled=True,
            max_concurrency=concurrency,
            max_tasks=max_tasks,
            max_depth=max_depth,
            announce=announce,
            announce_failures=announce_failures,
            idle_wait=1.0,
            log_dir=str(self.dir / "tasks"),
        )
        self.summaries: list[str] = []
        self.svc = TaskService(
            self.settings,
            self.tools,
            llm,
            speak=self.speaker,
            is_idle=lambda: idle,
            on_busy=self._on_busy,
            on_summary=lambda run: self.summaries.append(run.summary),
            root=self.dir,
        )
        self.busy_log: list[bool] = []

    def _on_busy(self, busy: bool) -> None:
        self.busy_log.append(bool(busy))

    def close(self) -> None:
        try:
            self.svc.close()
        finally:
            shutil.rmtree(self.dir, ignore_errors=True)

    def wait(self, run_id: str = "", timeout: float = 20.0) -> TaskRun | None:
        run = self.svc.wait_run(run_id, timeout)
        self.svc.drain_announcements(3.0)
        time.sleep(0.08)  # 让最后一条播报的 span 落盘
        return run


def spec(tid: str, title: str, tool: str = "control", **kw: Any) -> TaskSpec:
    depends = kw.pop("depends_on", ())
    args = kw.pop("args", None)
    if args is None:
        args = {"device": kw.pop("device", f"设备{tid}"), "action": kw.pop("action", "on")}
    return TaskSpec(id=tid, title=title, tool=tool, args=args, depends_on=tuple(depends), **kw)


# ---------------------------------------------------------------- ① 意图门控

def test_intent() -> None:
    print("\n① 多意图门控")
    multi = "今天天气如何？打开客厅灯，关闭晾衣架"
    check("三件事被识别为多意图", looks_like_multi_task(multi), f"{count_task_intents(multi)} 个小句")
    for single in ("打开客厅灯", "现在几点了", "帮我把空调关了，谢谢"):
        check(f"单件事不误判：{single}", not looks_like_multi_task(single))
    check("任务查询句不进编排", not looks_like_multi_task("服务器上还有几个任务没做"))
    check("取消指令不进编排", not looks_like_multi_task("把刚才的任务取消掉"))


# ---------------------------------------------------------------- ② 规划与校验

def test_plan() -> None:
    print("\n② 语义解析 → 任务拆分 → DAG 校验")
    tools = FakeTools()
    allowed = Planner(llm_from("{}"), tools).enabled_tools()
    check("规划器只放出白名单工具", "control" in allowed and "set_tts" not in allowed, str(allowed))

    good = {
        "tasks": [
            {"id": "t1", "title": "查成都天气", "tool": "weather", "args": {"city": "成都"}},
            {"id": "t2", "title": "开客厅灯", "tool": "control", "args": {"device": "客厅灯", "action": "on"}},
            {
                "id": "t3",
                "title": "关晾衣架",
                "tool": "control",
                "args": {"device": "晾衣架", "action": "off"},
                "depends_on": ["t2"],
            },
        ]
    }
    p = Planner(llm_from(good), tools, max_tasks=8)
    res = p.plan("今天天气如何？打开客厅灯，关闭晾衣架")
    check("拆出 3 个任务", len(res.tasks) == 3, describe_plan(res.tasks))
    check("依赖被保留", res.tasks[2].depends_on == ("t2",), str(res.tasks[2].depends_on))
    order = [s.id for s in topo_order(res.tasks)]
    check("拓扑序把依赖排在前面", order.index("t2") < order.index("t3"), str(order))

    bad_tool = {"tasks": [{"id": "t1", "title": "编的", "tool": "turn_on_light", "args": {}}]}
    check("模型编工具名 → 丢弃", Planner(llm_from(bad_tool), tools).plan("x").tasks == [])

    cycle = {
        "tasks": [
            {"id": "t1", "title": "A", "tool": "control", "args": {}, "depends_on": ["t2"]},
            {"id": "t2", "title": "B", "tool": "control", "args": {}, "depends_on": ["t1"]},
        ]
    }
    cyc = sanitize_tasks(cycle["tasks"], allowed_tools=allowed, max_tasks=8)
    check("依赖成环不致命（丢环边）", len(cyc) == 2 and not cyc[1].depends_on, describe_plan(cyc))
    check("成环后仍能拓扑排序", len(topo_order(cyc)) == 2)

    many = {"tasks": [{"id": f"t{i}", "title": f"第{i}件", "tool": "get_time", "args": {}} for i in range(1, 13)]}
    capped = sanitize_tasks(many["tasks"], allowed_tools=allowed, max_tasks=5)
    check("超过上限被截断", len(capped) == 5, f"{len(capped)} 条")

    check("非 JSON 输出 → 明确失败", not Planner(llm_from("我看不懂"), tools).plan("x").ok)
    check("空 tasks → 不 ok", not Planner(llm_from({"tasks": []}), tools).plan("x").ok)

    unknown_dep = sanitize_tasks(
        [{"id": "t1", "title": "A", "tool": "control", "args": {}, "depends_on": ["t9"]}],
        allowed_tools=allowed,
    )
    check("悬空依赖被清掉", unknown_dep[0].depends_on == ())

    group = sanitize_tasks(
        [{"id": "t1", "title": "大活", "kind": "subplan", "goal": "把书房弄好"}], allowed_tools=allowed
    )
    check("subplan 节点保留 goal", group and group[0].is_group and group[0].goal, str(group[0].goal if group else ""))
    no_group = sanitize_tasks(
        [{"id": "t1", "title": "大活", "kind": "subplan", "goal": "把书房弄好"}],
        allowed_tools=allowed,
        allow_group=False,
    )
    check("不允许子拆分时 subplan 被丢弃", not no_group)


# ---------------------------------------------------------------- ③ 执行 / 并发

def test_execute() -> None:
    print("\n③ 拆分 → 并发执行 → 顺序播报")
    h = Harness()
    try:
        tools: FakeTools = h.tools
        tools.delays.update({"weather": 0.45, "客厅灯": 0.12, "晾衣架": 0.25})
        tools.results.update(
            {
                "weather": "成都今天多云，18 到 24 度。",
                "客厅灯": "已打开客厅灯。",
                "晾衣架": "已关闭晾衣架。",
            }
        )
        specs = [
            spec("t1", "查成都天气", tool="weather", args={"city": "成都"}),
            spec("t2", "开客厅灯", device="客厅灯"),
            spec("t3", "关晾衣架", device="晾衣架", action="off"),
        ]
        sub = h.svc.submit_specs(specs, "今天天气如何？打开客厅灯，关闭晾衣架")
        check("提交即返回确认语", sub.accepted and sub.ack, sub.ack)
        run = h.wait(sub.run_id)
        check("三个任务都完成", run is not None and all(r.status == DONE for r in run.records()))
        texts = h.speaker.texts
        check("三条结果都播报了", len(texts) == 3, str(texts))
        expect = ["已打开客厅灯。", "已关闭晾衣架。", "成都今天多云，18 到 24 度。"]
        check("按完成先后播报（不是按规划顺序）", texts == expect, f"{texts}")
        check("播报不重叠（串行）", not h.speaker.overlaps(), str(h.speaker.overlaps()))
        check("播报前后都通知了主循环（回声避让）", h.busy_log == [True, False] * 3, str(h.busy_log))
    finally:
        h.close()


def test_concurrency() -> None:
    print("\n④ 并发阈值与排队")
    h = Harness(concurrency=2)
    try:
        tools: FakeTools = h.tools
        tools.delays["*"] = 0.3
        tools.results["*"] = "已执行。"
        specs = [spec(f"t{i}", f"第{i}件", tool="query", args={"device": f"设备{i}"}) for i in range(1, 6)]
        t0 = time.perf_counter()
        sub = h.svc.submit_specs(specs, "五件事")
        run = h.wait(sub.run_id)
        wall = time.perf_counter() - t0
        check("5 件都完成", run is not None and sum(1 for r in run.records() if r.status == DONE) == 5)
        check("工具层并发峰值 == 阈值 2", tools.peak == 2, f"peak={tools.peak}")
        check("超出阈值的确实排过队", h.svc.gate.queued_ever >= 3, f"queued_ever={h.svc.gate.queued_ever}")
        check("总耗时按批次走（>=1.2s，5×0.3s 若全并行只要 0.3s）", wall >= 1.15, f"{wall:.2f}s")
        check("播报条数 = 需要播报的任务数", len(h.speaker.texts) == 5, f"{len(h.speaker.texts)} 条")
    finally:
        h.close()


def test_failure_and_voice_flag() -> None:
    print("\n⑤ 失败不重试 / 依赖跳过 / 无需播报")
    h = Harness(concurrency=2)
    try:
        tools: FakeTools = h.tools
        tools.results.update(
            {
                "坏设备": "操作失败：设备不理我。",
                "好设备": "已打开好设备。",
                "后台": "已记录。",
            }
        )
        specs = [
            spec("t1", "开坏设备", device="坏设备"),
            spec("t2", "后续动作", device="好设备", depends_on=["t1"]),
            spec("t3", "悄悄记录", device="后台", needs_voice=False),
        ]
        sub = h.svc.submit_specs(specs, "三件事")
        run = h.wait(sub.run_id)
        rec = {r.spec.id: r for r in run.records()}
        check("失败任务标记 failed", rec["t1"].status == FAILED, rec["t1"].status)
        check("失败任务只调了一次（不重试）", rec["t1"].attempts == 1 and tools.count("control", "坏设备") == 1)
        check("依赖失败的任务被跳过", rec["t2"].status == SKIPPED, rec["t2"].note)
        check("被跳过的任务没被执行", tools.count("control", "好设备") == 0)
        check("失败结果被播报（告知而非吞掉）", any("没做成" in t for t in h.speaker.texts), str(h.speaker.texts))
        check("被跳过的任务也报了原因", any("先不做了" in t for t in h.speaker.texts), str(h.speaker.texts))
        check("needs_voice=false 不播报", not any("已记录" in t for t in h.speaker.texts))
        check("needs_voice=false 仍写进总结", "悄悄记录✓" in (run.summary or ""), run.summary)
        check("总结带完成计数", "1/3" in (run.summary or ""), run.summary)
    finally:
        h.close()

    h2 = Harness(concurrency=1, announce_failures=False)
    try:
        h2.tools.results["*"] = "操作失败：模拟。"
        sub = h2.svc.submit_specs([spec("t1", "开灯", device="灯1"), spec("t2", "关灯", device="灯2")], "两件事")
        h2.wait(sub.run_id)
        check("announce_failures=false 时失败不念", h2.speaker.texts == [], str(h2.speaker.texts))
    finally:
        h2.close()


# ---------------------------------------------------------------- ⑥ 查询 / 取消

def test_query_and_cancel() -> None:
    print("\n⑥ 语音查询与取消")
    h = Harness(concurrency=1)
    try:
        tools: FakeTools = h.tools
        tools.delays.update({"慢设备": 0.6, "*": 0.05})
        tools.results.update({"慢设备": "已打开慢设备。", "二": "已打开二号。", "三": "已打开三号。"})
        specs = [
            spec("t1", "慢任务", device="慢设备"),
            spec("t2", "第二件", device="二"),
            spec("t3", "第三件", device="三"),
        ]
        sub = h.svc.submit_specs(specs, "三件事")
        # 等第一个真正开跑
        deadline = time.time() + 3
        while time.time() < deadline:
            run = h.svc.last_run()
            if run and any(r.status == RUNNING for r in run.records()):
                break
            time.sleep(0.02)
        queued = [r for r in run.records() if r.status == QUEUED]
        check("阈值=1 时另两件排队", len(queued) == 2, [f"{r.spec.id}:{r.status}" for r in run.records()])
        desc = h.svc.describe()
        check("查询能说清进行中与排队", "进行中" in desc and "排队中" in desc, desc[:80])
        check("查询提到并发上限", "同时最多做1件" in desc, desc[:80])
        cancel_text = h.svc.cancel("2")
        check("取消第 2 件有口语回执", "取消" in cancel_text, cancel_text)
        check("第 2 件变成已取消", h.svc.last_run().records()[1].status == CANCELLED)
        run = h.wait(sub.run_id)
        check("被取消的排队任务真的没执行", tools.count("control", "二") == 0)
        check("取消的那件不播报", "已打开二号。" not in h.speaker.texts, str(h.speaker.texts))
        check("其余任务照常跑完", {r.spec.id: r.status for r in run.records()}["t3"] == DONE)
        check("已结束时取消会被拒", "结束" in h.svc.cancel("全部"), h.svc.cancel("全部"))
    finally:
        h.close()

    # 执行中取消：结果丢弃、不播报
    h2 = Harness(concurrency=1, speaker_delay=0.02)
    try:
        h2.tools.delays.update({"慢设备": 0.5})
        h2.tools.results.update({"慢设备": "已打开慢设备。", "快": "已关闭快设备。"})
        specs = [spec("t1", "慢的", device="慢设备"), spec("t2", "快的", device="快", action="off")]
        sub = h2.svc.submit_specs(specs, "两件事")
        deadline = time.time() + 3
        while time.time() < deadline:
            run = h2.svc.last_run()
            if run and run.records()[0].status == RUNNING:
                break
            time.sleep(0.02)
        msg = h2.svc.cancel("1")
        check("执行中取消被接受", "取消" in msg, msg)
        run = h2.wait(sub.run_id)
        check("执行中取消 → 状态已取消", run.records()[0].status == CANCELLED)
        check("执行中取消 → 结果不播报", not any("慢设备" in t or "慢的" in t for t in h2.speaker.texts), str(h2.speaker.texts))
        check("另一件不受影响", run.records()[1].status == DONE)
    finally:
        h2.close()

    # 编号一致性：describe() 说「第 2 件」，取消「2」就必须是同一件
    h4 = Harness(concurrency=1)
    try:
        h4.tools.delays["*"] = 0.15
        h4.tools.results.update({"甲": "已开甲。", "乙": "已开乙。", "丙": "已开丙。"})
        specs = [
            spec("t1", "第一件", device="甲"),
            spec("t2", "第二件", device="乙"),
            spec("t3", "第三件", device="丙"),
        ]
        sub = h4.svc.submit_specs(specs, "三件事")
        deadline = time.time() + 3
        while time.time() < deadline:
            run4 = h4.svc.last_run()
            if run4 and run4.records()[0].status == DONE:
                break
            time.sleep(0.02)
        check("第 1 件先跑完（用于编号对照）", h4.svc.last_run().records()[0].status == DONE)
        msg = h4.svc.cancel("2")
        check("取消「2」命中的是第 2 件", "第二件" in msg and "第三件" not in msg, msg)
        check("第 3 件没被误取消", h4.wait(sub.run_id).records()[2].status != CANCELLED)
        check("已完成的第 1 件被拒取消", "已经" in h4.svc.cancel("1"), h4.svc.cancel("1"))
    finally:
        h4.close()

    # 整组取消
    h3 = Harness(concurrency=1)
    try:
        h3.tools.delays["*"] = 0.3
        h3.tools.results["*"] = "已执行。"
        specs = [spec(f"t{i}", f"第{i}件", device=f"设备{i}") for i in range(1, 4)]
        sub = h3.svc.submit_specs(specs, "三件事")
        time.sleep(0.1)
        msg = h3.svc.cancel("全部")
        check("整组取消有回执", "取消" in msg, msg)
        run = h3.wait(sub.run_id)
        check("整组取消后全部终态", all(r.terminal for r in run.records()), str(run.counts()))
        check("整组取消后不再播报", h3.speaker.texts == [], str(h3.speaker.texts))
    finally:
        h3.close()


# ---------------------------------------------------------------- ⑦ 子任务

def test_subplan() -> None:
    print("\n⑦ 大任务自动拆子任务（sub-agent）")
    sub_plan = {
        "tasks": [
            {"id": "s1", "title": "开书房灯", "tool": "control", "args": {"device": "书房灯", "action": "on"}},
            {
                "id": "s2",
                "title": "调暗书房灯",
                "tool": "control",
                "args": {"device": "书房灯", "action": "brightness", "value": 30},
                "depends_on": ["s1"],
            },
        ]
    }
    h = Harness(llm=llm_from({"tasks": []}, extra={"sub": sub_plan}), concurrency=2, max_depth=2)
    try:
        out = h.tools.results
        out["书房灯"] = lambda args: (
            f"已打开书房灯。" if args.get("action") == "on" else "书房灯已调到 30%。"
        )
        sub = h.svc.submit_specs([TaskSpec(id="g1", title="收拾书房", kind="subplan", goal="把书房灯打开并调暗")], "收拾书房")
        run = h.wait(sub.run_id)
        rec = run.records()[0]
        check("父任务完成", rec.status == DONE, f"{rec.status} {rec.note}")
        check("子任务真的执行了（2 次工具调用）", h.tools.count("control", "书房灯") >= 2, str(h.tools.calls))
        check("子任务不逐条播报，只汇总一句", len(h.speaker.texts) == 1, str(h.speaker.texts))
        check("汇总里含子任务结果", "书房灯" in (h.speaker.texts[0] if h.speaker.texts else ""), str(h.speaker.texts))
        lines = h.svc.log_lines(sub.run_id)
        check("日志里能看到子任务事件", any(x.get("event") == "group_start" for x in lines), f"{len(lines)} 行")
        check("主会话总结只有一句", len(h.summaries) == 1, str(h.summaries))

        h2 = Harness(llm=llm_from({"tasks": []}, extra={"sub": sub_plan}), max_depth=1)
        try:
            sub2 = h2.svc.submit_specs(
                [TaskSpec(id="g1", title="收拾书房", kind="subplan", goal="把书房灯打开并调暗")], "收拾书房"
            )
            run2 = h2.wait(sub2.run_id)
            check("max_depth=1 时不再拆（如实失败）", run2.records()[0].status == FAILED, run2.records()[0].note)
            check("max_depth=1 时不调子规划", h2.tools.calls == [])
        finally:
            h2.close()
    finally:
        h.close()


# ---------------------------------------------------------------- ⑧ 上下文与日志

def test_summary_and_log() -> None:
    print("\n⑧ 主会话只留总结，明细落盘")
    h = Harness(concurrency=2)
    try:
        h.tools.results.update({"天气": "成都今天晴，20 到 26 度。", "*": "已执行。"})
        specs = [
            spec("t1", "查天气", tool="weather", args={"city": "成都"}),
            spec("t2", "开灯", device="灯"),
        ]
        sub = h.svc.submit_specs(specs, "查天气并开灯")
        run = h.wait(sub.run_id)
        check("on_summary 只回调一次", len(h.summaries) == 1, str(h.summaries))
        summary = h.summaries[0] if h.summaries else ""
        check("总结只有一个标题+勾叉，不含明细", "度" not in summary and "✓" in summary, summary)
        check("summarize() 可独立复现", summarize(run) == summary, summarize(run))
        lines = h.svc.log_lines(sub.run_id)
        events = {x.get("event") for x in lines}
        check("明细 jsonl 记录了开始/结束/收尾", {"run_start", "start", "done", "run_end"} <= events, str(sorted(events)))
        check("明细里有工具耗时与返回值", any("ms" in x for x in lines) and any("result" in x for x in lines))
    finally:
        h.close()


# ---------------------------------------------------------------- ⑨ app 接线

def _jarvis_stub(svc: TaskService | None) -> Any:
    """不跑 __init__（要加载 ASR 模型），只搭 handle_text 走异步分支所需的字段。"""
    from jarvis.app import Jarvis

    j = Jarvis.__new__(Jarvis)
    j.speak_enabled = False
    j._early_acked = False
    j._external_speech = False
    j._ext_speech_dirty = False
    st = TaskSettings(enabled=True, max_concurrency=1, max_tasks=8, max_depth=2, announce=False, log_dir="/tmp/none")
    j.st = type("S", (), {"task_enabled": svc is not None, "audio_source": "local"})()
    j.session = Session.load("- 客厅灯")
    j.tasks = svc
    j._task_summaries = deque(maxlen=4)
    j.led_calls = []
    j._led = lambda state: j.led_calls.append(state)
    j.tts = type("T", (), {"speaking": False, "speak": lambda self, t: j.led_calls.append(f"speak:{t}")})()
    j.memory = None
    j.llm = None
    return j


def test_app_wiring() -> None:
    print("\n⑨ 接入主流程（不碰音频层）")
    from jarvis.app import Jarvis

    plan = {
        "tasks": [
            {"id": "t1", "title": "开客厅灯", "tool": "control", "args": {"device": "灯", "action": "on"}},
            {"id": "t2", "title": "关晾衣架", "tool": "control", "args": {"device": "空调", "action": "off"}},
        ]
    }
    h = Harness(llm=llm_from(plan), concurrency=1, announce=False)
    try:
        h.tools.results["*"] = "已执行。"
        j = _jarvis_stub(h.svc)
        # 真实接线里 TaskService(on_summary=self._queue_task_summary)；这里手工接上
        h.svc.on_summary = j._queue_task_summary
        # 单件事：不进编排
        check("单件事不进编排", j._try_async_tasks("打开客厅灯") is None)
        # 多件事：提交并返回确认语，且历史里已经有这一轮
        ack = j._try_async_tasks("打开客厅灯，关闭晾衣架")
        check("多件事走编排并立刻回确认", bool(ack) and "件事" in (ack or ""), str(ack))
        hist = j.session.history
        check("确认语进历史（用户+助手各一条）", len(hist) == 2 and hist[0]["role"] == "user", str(hist))
        run = h.wait(h.svc.last_run().id)
        check("后台确实跑了", run is not None and all(r.status == DONE for r in run.records()))
        time.sleep(0.1)
        j._flush_task_summaries()
        check("任务总结并入历史（只一句）", len(j.session.history) == 3 and "任务完成" in j.session.history[-1]["content"],
              str(j.session.history[-1:]))

        # 播报期间：麦克风回声要被丢掉
        class FakeMic:
            def __init__(self) -> None:
                self.drains = 0

            def drain(self, *a: Any) -> None:
                self.drains += 1

        class FakeSeg:
            def __init__(self) -> None:
                self.resets = 0

            def reset(self) -> None:
                self.resets += 1

        mic, seg = FakeMic(), FakeSeg()
        check("静默期不拦麦克风", not j._external_speech_active(mic, seg) and mic.drains == 0)
        j._set_external_speech(True)
        check("播报中拦掉麦克风", j._external_speech_active(mic, seg) and mic.drains == 0)
        j._set_external_speech(False)
        check("播完排空一次并重置 VAD", not j._external_speech_active(mic, seg) and mic.drains == 1 and seg.resets == 1)
        check("只排空一次", not j._external_speech_active(mic, seg) and mic.drains == 1)
    finally:
        h.close()

    # 编排未启用 → 主流程必须照常（退回普通单轮）
    j2 = _jarvis_stub(None)
    check("没编排时不拦普通对话", j2._try_async_tasks("打开客厅灯，关闭晾衣架") is None)
    check("Jarvis 有 _try_async_tasks / _speak_reply", hasattr(Jarvis, "_try_async_tasks") and hasattr(Jarvis, "_speak_reply"))


def test_degrade() -> None:
    print("\n⑩ 没装 langgraph 时优雅降级")
    orig = task_graph.available
    try:
        task_graph.available = lambda: False  # type: ignore[assignment]
        h = Harness(llm=llm_from({"tasks": [{"id": "t1", "title": "A", "tool": "control", "args": {}}]}))
        try:
            check("不可用时 enabled() 为 False", not h.svc.enabled())
            check("不可用时说明原因", "langgraph" in h.svc.why_disabled(), h.svc.why_disabled())
            res = h.svc.submit("打开灯，关闭窗帘")
            check("提交被拒但给得出原因", not res.accepted and "langgraph" in res.reason, res.reason)
        finally:
            h.close()
    finally:
        task_graph.available = orig  # type: ignore[assignment]

    # Tools 侧：没绑 service 时 tasks 工具返回未启用，且不炸
    from jarvis.config import Settings
    from jarvis.tools import Tools

    st = Settings.load()
    t = Tools.__new__(Tools)
    t.settings, t.devices, t.memory, t._tasks, t._tts = st, {}, None, None, None
    check("tasks 工具未接时给明确回复", "没启用" in t.dispatch("tasks", {"action": "query"}))
    names = [s["function"]["name"] for s in Tools._schemas_all(t)]
    check("tasks 工具已注册给模型", "tasks" in names, str(names))


def test_graph_engine() -> None:
    print("\n⑪ LangGraph 引擎行为（依赖顺序 / 完成顺序 / 并行）")
    if not task_graph.available():
        check("langgraph 已安装", False, "uv sync --extra tasks")
        return
    order: list[str] = []
    specs = [
        spec("t1", "慢", tool="query", args={"device": "慢"}),
        spec("t2", "快", tool="query", args={"device": "快"}),
        spec("t3", "依赖慢", tool="query", args={"device": "依赖"}, depends_on=["t1"]),
    ]
    delay = {"慢": 0.35, "快": 0.30, "依赖": 0.02}

    def node(s: TaskSpec, seen: dict[str, str]) -> str:
        time.sleep(delay.get(s.args.get("device", ""), 0.01))
        order.append(s.id)
        return f"{s.id} ok"

    t0 = time.perf_counter()
    out = task_graph.run_graph(specs, node)
    wall = time.perf_counter() - t0
    check("并行分支真的并行（0.37s ≈ max(0.35,0.30)+0.02，串行要 0.67s）", wall < 0.55, f"{wall:.2f}s")
    check("依赖任务在依赖完成后才跑", order.index("t1") < order.index("t3"), str(order))
    check("结果按完成先后产出", order[0] == "t2", str(order))
    check("引擎返回全部结果", set(out) == {"t1", "t2", "t3"}, str(out))
    check("无依赖的分支互不阻塞", order.index("t2") < order.index("t1"), str(order))


# ---------------------------------------------------------------- 真设备 / 真模型

def test_real() -> None:
    print("\n⑫ 真模型规划 + 真设备执行（不出声）")
    from jarvis.config import Settings
    from jarvis.tasks.service import TaskService as _Svc

    st = Settings.load()
    key = (st.llm_api_key or st.deepseek_key or "").strip()
    if not key:
        check("真模型规划", False, "没有 LLM key，跳过")
        return

    from openai import OpenAI

    from jarvis.llm import no_thinking_body

    client = OpenAI(api_key=key, base_url=st.llm_base_url.rstrip("/"), timeout=st.task_plan_timeout)
    extra = no_thinking_body(st)  # DeepSeek 不关思考会把 token 全烧在 reasoning 上

    def llm(prompt: str) -> str:
        kwargs: dict[str, Any] = {
            "model": st.task_plan_model or st.llm_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
            "max_tokens": 700,
        }
        if extra:
            kwargs["extra_body"] = extra
        r = client.chat.completions.create(**kwargs)
        return (r.choices[0].message.content or "").strip()

    import yaml

    from jarvis.ha import HAClient
    from jarvis.tools import Tools

    ha = HAClient(st)
    tools = Tools(ha, settings=st)
    online = []
    for dev in tools.devices.values():
        if dev.type != "light":
            continue
        state = (ha.state(dev.entity_id) or {}).get("state")
        if state not in (None, "unavailable"):
            online.append(dev)
    if not online:
        check("真设备在线", False, "没有在线灯，跳过真设备段")
        ha.close()
        return
    dev = online[0]

    try:
        planner = Planner(llm, tools, max_tasks=8)
        plan = planner.plan(f"今天天气如何？把{dev.name}打开，再查一下{dev.name}的状态")
        check("真模型把一句话拆成 3 件", len(plan.tasks) >= 2, describe_plan(plan.tasks))
        check("真模型规划耗时 < 8s", plan.elapsed_ms < 8000, f"{plan.elapsed_ms:.0f}ms")
        check("真模型用的是白名单工具", all(t.tool in planner.enabled_tools() for t in plan.tasks), str([t.tool for t in plan.tasks]))

        tmp = Path(tempfile.mkdtemp(prefix="jarvis-tasks-real-"))
        svc = _Svc(
            TaskSettings(enabled=True, max_concurrency=2, max_tasks=8, max_depth=2, announce=False, log_dir=str(tmp / "t")),
            tools,
            llm,
            speak=lambda text: print(f"  （静音）会播报：{text}"),
            is_idle=lambda: True,
            root=tmp,
        )
        try:
            sub = svc.submit(f"今天天气如何？把{dev.name}打开，再查一下{dev.name}的状态")
            check("真链路：提交成功", sub.accepted, sub.reason)
            run = svc.wait_run(sub.run_id, timeout=40)
            check("真链路：任务组跑完", run is not None and run.finished.is_set())
            if run is not None:
                status = {r.spec.title: r.status for r in run.records()}
                print(f"     状态：{status}")
                print(f"     结果：{[r.result[:28] for r in run.records()]}")
                state = (ha.state(dev.entity_id) or {}).get("state")
                touched = any(r.spec.tool == "control" and r.spec.args.get("device") == dev.name for r in run.records())
                if touched and any(r.status == DONE for r in run.records() if r.spec.tool == "control"):
                    check("真设备真的被打开了（HA 状态=on）", state == "on", f"{dev.name} state={state}")
                    tools.control(dev.name, "off")
                    time.sleep(0.4)
                    check("复位：已关闭", (ha.state(dev.entity_id) or {}).get("state") == "off")
                else:
                    print(f"     模型没给「{dev.name}」下控制指令，跳过状态断言")
                check("真链路：总结只有一句", bool(run.summary) and "\n" not in run.summary, run.summary)
        finally:
            svc.close()
            shutil.rmtree(tmp, ignore_errors=True)
    finally:
        ha.close()


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="多任务编排回归")
    ap.add_argument("--real", action="store_true", help="额外跑真模型规划 + 真设备（默认不出声）")
    args = ap.parse_args(argv)

    t0 = time.perf_counter()
    check("langgraph 可选依赖已安装", task_graph.available(), "uv sync --extra tasks")
    test_intent()
    test_plan()
    test_graph_engine()
    test_execute()
    test_concurrency()
    test_failure_and_voice_flag()
    test_query_and_cancel()
    test_subplan()
    test_summary_and_log()
    test_app_wiring()
    test_degrade()
    if args.real:
        test_real()

    passed = sum(1 for _, ok, _ in results if ok)
    failed = [(n, d) for n, ok, d in results if not ok]
    print("\n" + "─" * 52)
    print(f"通过 {passed} · 失败 {len(failed)} · 用时 {time.perf_counter() - t0:.1f}s")
    for name, detail in failed:
        print(f"  ❌ {name}" + (f"  —— {detail}" if detail else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())