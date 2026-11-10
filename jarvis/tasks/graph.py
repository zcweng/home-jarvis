"""LangGraph 适配层：把 TaskSpec 列表编译成一张 DAG 并执行。

为什么用 LangGraph（而不是自己写个线程池拓扑排序）：
- 依赖图、并行分叉、汇合都是它现成的；
- **节点同步函数会被放进线程池并发跑**（实测确认），工具层是同步 httpx，
  不需要为编排把 HA/天气全改成 async；
- `stream_mode="updates"` 按**完成先后**吐出节点结果（实测：0.2s 的先出、0.9s 的后出），
  正好对应「结果按返回顺序依次播报」。

踩过的坑（勿重踩）：
1. LangGraph 是**可选依赖**（`uv sync --extra tasks`）；没装就让编排整体降级，
   主语音链路一行都不受影响。所以所有 langgraph import 都在函数里做。
2. 节点函数抛异常会**整张图中断**，其余分支的收尾全丢。所以 runner 里的节点
   必须自己 try/except 到顶，绝不让异常冒到图里。
3. 别用图的 `max_concurrency` 当并发闸门：它按「图内同时在跑的节点数」算，嵌套子图
   各算各的，父节点占着槽位等子图时容易把自己等死。并发闸门放在**叶子工具执行**上
   （runner.ConcurrencyGate），并行与嵌套共用同一把闸。
"""

from __future__ import annotations

import sys
from typing import Any, Annotated, Callable, TypedDict

from .model import TaskSpec


def available() -> bool:
    """LangGraph 是否可用（可选依赖）。"""
    try:
        import langgraph.graph  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def _merge(a: dict[str, str] | None, b: dict[str, str] | None) -> dict[str, str]:
    out = dict(a or {})
    out.update(b or {})
    return out


class TaskState(TypedDict):
    # 并行分支各自返回 {任务 id: 输出}，用 reducer 合并（默认行为是覆盖）
    results: Annotated[dict[str, str], _merge]


def build_graph(
    specs: list[TaskSpec],
    node_fn: Callable[[TaskSpec, dict[str, str]], str],
) -> Any:
    """把任务列表编译成图。node_fn(spec, 已完成结果) → 该任务的输出文本。"""
    from langgraph.graph import END, START, StateGraph

    known = {s.id for s in specs}
    graph = StateGraph(TaskState)

    def make_node(spec: TaskSpec) -> Callable[[TaskState], dict[str, Any]]:
        def _node(state: TaskState) -> dict[str, Any]:
            seen = dict(state.get("results") or {})
            return {"results": {spec.id: node_fn(spec, seen)}}

        return _node

    for spec in specs:
        graph.add_node(spec.id, make_node(spec))

    for spec in specs:
        deps = [d for d in spec.depends_on if d in known]
        if not deps:
            graph.add_edge(START, spec.id)
        for dep in deps:
            graph.add_edge(dep, spec.id)

    # 没有后继的任务连到 END，否则图跑完没有出口
    has_successor = {d for s in specs for d in s.depends_on if d in known}
    for spec in specs:
        if spec.id not in has_successor:
            graph.add_edge(spec.id, END)

    return graph.compile()


def run_graph(
    specs: list[TaskSpec],
    node_fn: Callable[[TaskSpec, dict[str, str]], str],
    *,
    recursion_limit: int = 60,
) -> dict[str, str]:
    """同步执行整张图，返回 {task_id: 输出}。"""
    if not available():
        raise RuntimeError("没有安装 langgraph（uv sync --extra tasks）")
    app = build_graph(specs, node_fn)
    out = app.invoke({"results": {}}, config={"recursion_limit": max(25, recursion_limit)})
    results = dict((out or {}).get("results") or {})
    if not results:
        print("  ⚠️  任务图跑完但没有产出", file=sys.stderr)
    return results