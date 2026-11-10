"""语义解析 → 任务拆分 → 依赖 DAG 构建。

一次 LLM 调用（JSON 输出）把「今天天气如何？打开客厅灯，关闭晾衣架」拆成 3 个任务，
再在本地做一轮**严格校验**：工具必须在白名单里、参数必须是对象、id 必须唯一、
依赖不能悬空、不能成环、数量不能超上限。

为什么要校验而不是直接信模型：
- 模型会编工具名（`turn_on_light`），编出来执行时才发现，用户已经等了一次播报；
- 模型会写出互相依赖成环的两条（A 依赖 B、B 依赖 A），图会直接死锁；
- 模型会拆出 15 件"事"，一次念 15 句，用户会以为设备疯了。

校验失败一律**丢掉坏的那一条**，保留能跑的：宁可少做一件，也不要整句失败。
拆完只剩 1 件时由编排层退回普通单轮对话（不是错误，是没这个必要）。
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..jsonx import parse_json_object
from ..tools import Tools
from .model import TaskSpec

LLMFn = Callable[[str], str]  # prompt → raw text

# 允许进后台任务的工具：能读能写、幂等或半幂等、不算敏感。
# 故意不含 set_tts（换声音是前台交互）、memory（后台偷偷写记忆太危险）、tasks（元工具）。
PLANNER_TOOLS = (
    "control",
    "query",
    "weather",
    "get_time",
    "scene",
    "announce",
    "timer",
    "web_search",
    "web_fetch",
)

_ID_SAFE = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"

_PLAN_PROMPT = """你是家庭语音管家的任务规划器。把用户的一句话拆成尽量并行的小任务 DAG。
只输出 JSON，不要解释，不要 Markdown。schema：
{{"tasks": [{{"id": "t1", "title": "查成都天气", "tool": "weather", "args": {{"city": "成都"}},
            "kind": "tool", "needs_voice": true, "depends_on": []}}]}}
规则：
1. 一句话里有几件互不相干的事，就拆几个任务；能并行时 depends_on 留空。
2. 只有必须分先后时才写依赖（例："先把客厅灯打开再调到三成亮" → 调亮度那条 depends_on 开灯那条）。
3. tool 只能从下面的工具清单里选，args 必须符合它的参数 schema，不要编参数名。
4. 不需要念给用户听的任务（纯准备、纯记录）设 needs_voice=false。
5. 单条任务明显还太大（要先查再做多步、或把好几件事绑在一起）时，用 kind="subplan" 且 goal 写清目标，tool 留空。
6. 最多 {max_tasks} 个任务；id 用 t1、t2…；title 不超过 8 个汉字、口语化、能念出来。
7. 实在拆不出（只有一件事、或只是闲聊）就返回空的 tasks。

可用工具：
{tools}

用户原话：{text}
{context}"""

_SUB_PROMPT = """你是家庭语音管家的子任务规划器。下面这个目标太大，需要拆成几条更小的任务。
只输出 JSON，schema 与父规划器一致：
{{"tasks": [{{"id": "t1", "title": "…", "tool": "control", "args": {{}},
            "kind": "tool", "needs_voice": true, "depends_on": []}}]}}
规则：能并行就并行；只有必须分先后才写 depends_on；tool 只能从清单里选；最多 {max_tasks} 条。
{group_hint}

可用工具：
{tools}

子目标：{goal}
{context}"""


def _warn(msg: str) -> None:
    print(f"  ⚠️  任务规划：{msg}", file=sys.stderr)


@dataclass
class PlanResult:
    tasks: list[TaskSpec] = field(default_factory=list)
    error: str = ""
    raw: str = ""
    elapsed_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return bool(self.tasks)


def _reachable(edges: dict[str, tuple[str, ...]], start: str, target: str) -> bool:
    """从 start 沿依赖走，能否走到 target（用来判环）。"""
    seen: set[str] = set()
    stack = [start]
    while stack:
        cur = stack.pop()
        if cur == target:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(edges.get(cur, ()))
    return False


def _as_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes", "y", "是"):
            return True
        if low in ("false", "0", "no", "n", "否"):
            return False
    if isinstance(value, int | float):
        return bool(value)
    return default


def _clean_title(raw: Any, fallback: str) -> str:
    title = " ".join(str(raw or "").split())
    if not title:
        return fallback
    return title[:16]


def sanitize_tasks(
    raw_tasks: Any,
    *,
    allowed_tools: Iterable[str],
    max_tasks: int = 8,
    allow_group: bool = True,
) -> list[TaskSpec]:
    """模型给的 tasks → 可执行 TaskSpec 列表（坏条目直接丢）。"""
    if not isinstance(raw_tasks, list):
        return []
    allowed = set(allowed_tools)
    specs: list[TaskSpec] = []
    pairs: list[tuple[TaskSpec, dict[str, Any]]] = []  # 保住 spec ↔ 原始条目 的对应
    seen_ids: set[str] = set()

    for i, item in enumerate(raw_tasks):
        if not isinstance(item, dict):
            continue
        if len(specs) >= max(1, max_tasks):
            _warn(f"任务数超过上限 {max_tasks}，多余的已丢弃")
            break

        tid = str(item.get("id") or "").strip()
        if not tid or any(ch not in _ID_SAFE for ch in tid) or tid in seen_ids:
            tid = f"t{i + 1}"
            while tid in seen_ids:
                tid += "_"
        seen_ids.add(tid)

        kind = str(item.get("kind") or "tool").strip().lower()
        goal = str(item.get("goal") or "").strip()
        tool = str(item.get("tool") or "").strip()
        args = item.get("args")
        if not isinstance(args, dict):
            args = {}

        if kind == "subplan":
            if not allow_group:
                _warn(f"「{tool or tid}」嵌套层数到顶，已丢弃子拆分节点")
                continue
            if not goal:
                _warn(f"子任务节点 {tid} 没写 goal，已丢弃")
                continue
            tool = ""
        else:
            kind = "tool"
            if tool not in allowed:
                _warn(f"模型编了不可用的工具「{tool}」，已丢掉 {tid}")
                continue
            if not tool:
                continue

        title = _clean_title(item.get("title"), tool or (goal[:8] if goal else f"第{i + 1}件"))
        spec = TaskSpec(
            id=tid,
            title=title,
            tool=tool,
            args=dict(args),
            kind=kind,
            goal=goal,
            needs_voice=_as_bool(item.get("needs_voice"), True),
            depends_on=(),
        )
        specs.append(spec)
        pairs.append((spec, item))

    if not specs:
        return []

    known = {s.id for s in specs}
    edges: dict[str, tuple[str, ...]] = {}
    for spec, item in pairs:
        deps_raw = item.get("depends_on") or []
        if isinstance(deps_raw, str):
            deps_raw = [deps_raw]
        deps: list[str] = []
        for dep in deps_raw if isinstance(deps_raw, list) else []:
            dep_id = str(dep).strip()
            if dep_id == spec.id or dep_id not in known:
                continue
            # 加这条边会不会成环：dep 已经（直接或间接）依赖 spec
            if _reachable(edges, dep_id, spec.id):
                _warn(f"依赖成环，已丢掉 {spec.id} → {dep_id}")
                continue
            if dep_id in deps:
                continue
            deps.append(dep_id)
            edges[spec.id] = tuple(deps)
        spec.depends_on = tuple(deps)
        edges.setdefault(spec.id, tuple(deps))
    return specs


def topo_order(specs: list[TaskSpec]) -> list[TaskSpec]:
    """按依赖排序（同层保持规划顺序）；有环时兜底返回原顺序。"""
    by_id = {s.id: s for s in specs}
    out: list[TaskSpec] = []
    temp: set[str] = set()
    done: set[str] = set()

    def visit(spec: TaskSpec) -> None:
        if spec.id in done:
            return
        if spec.id in temp:
            return
        temp.add(spec.id)
        for dep in spec.depends_on:
            if dep in by_id:
                visit(by_id[dep])
        temp.discard(spec.id)
        done.add(spec.id)
        out.append(spec)

    for spec in specs:
        visit(spec)
    return out


class Planner:
    """把一句话/一个子目标拆成 TaskSpec 列表。"""

    def __init__(
        self,
        llm: LLMFn,
        tools: Tools,
        *,
        max_tasks: int = 8,
        default_city: str = "成都",
    ) -> None:
        self._llm = llm
        self.tools = tools
        self.max_tasks = max(1, int(max_tasks))
        self.default_city = default_city
        self._catalog_cache: str = ""

    # ---------------------------------------------------------------- 工具清单

    def enabled_tools(self) -> list[str]:
        names = []
        for spec in self.tools.schemas():
            name = str((spec.get("function") or {}).get("name") or "")
            if name in PLANNER_TOOLS:
                names.append(name)
        return names

    def tool_catalog(self) -> str:
        if self._catalog_cache:
            return self._catalog_cache
        lines: list[str] = []
        for spec in self.tools.schemas():
            fn = spec.get("function") or {}
            name = str(fn.get("name") or "")
            if name not in PLANNER_TOOLS:
                continue
            params = (fn.get("parameters") or {}).get("properties") or {}
            args_hint = "、".join(str(k) for k in params) or "无参数"
            desc = str(fn.get("description") or "").split("。")[0][:60]
            lines.append(f"- {name}（参数：{args_hint}）：{desc}")
        self._catalog_cache = "\n".join(lines) or "- 无"
        return self._catalog_cache

    # ---------------------------------------------------------------- 调用

    def plan(self, text: str, context: str = "") -> PlanResult:
        prompt = _PLAN_PROMPT.format(
            max_tasks=self.max_tasks,
            tools=self.tool_catalog(),
            text=(text or "").strip(),
            context=(f"补充上下文：{context.strip()}" if context.strip() else ""),
        )
        return self._run(prompt, allow_group=True)

    def plan_goal(self, goal: str, context: str = "", *, allow_group: bool = True) -> PlanResult:
        prompt = _SUB_PROMPT.format(
            max_tasks=self.max_tasks,
            tools=self.tool_catalog(),
            goal=(goal or "").strip(),
            context=(f"已知结果：{context.strip()}" if context.strip() else ""),
            group_hint=(
                "本轮允许继续用 kind=\"subplan\" 再拆一层。"
                if allow_group
                else "本轮**不允许**再用 subplan（嵌套层数到顶），只能拆成 tool 任务。"
            ),
        )
        return self._run(prompt, allow_group=allow_group)

    def _run(self, prompt: str, *, allow_group: bool) -> PlanResult:
        t0 = time.perf_counter()
        try:
            raw = self._llm(prompt) or ""
        except Exception as e:  # noqa: BLE001
            return PlanResult(error=f"规划调用失败：{e}", elapsed_ms=(time.perf_counter() - t0) * 1000)
        ms = (time.perf_counter() - t0) * 1000
        data = parse_json_object(raw)
        if data is None:
            return PlanResult(error="模型没给出合法 JSON", raw=str(raw)[:300], elapsed_ms=ms)
        tasks = sanitize_tasks(
            data.get("tasks"),
            allowed_tools=self.enabled_tools(),
            max_tasks=self.max_tasks,
            allow_group=allow_group,
        )
        return PlanResult(tasks=tasks, raw=str(raw)[:2000], elapsed_ms=ms)


def describe_plan(specs: list[TaskSpec]) -> str:
    """调试用：t1 查天气 → t2 开灯（依赖 t1）"""
    parts = []
    for s in topo_order(specs):
        tail = f"（依赖 {'、'.join(s.depends_on)}）" if s.depends_on else ""
        parts.append(f"{s.id} {s.title}{tail}")
    return " → ".join(parts)


def plan_to_json(specs: list[TaskSpec]) -> str:
    return json.dumps(
        [
            {
                "id": s.id,
                "title": s.title,
                "tool": s.tool,
                "args": s.args,
                "kind": s.kind,
                "goal": s.goal,
                "needs_voice": s.needs_voice,
                "depends_on": list(s.depends_on),
            }
            for s in specs
        ],
        ensure_ascii=False,
    )