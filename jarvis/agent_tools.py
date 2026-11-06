"""把 Tools 领域逻辑包装成 Agents SDK FunctionTool。

schema 与 Tools.schemas() 对齐（枚举、描述、必填），避免模型乱填参。
含 control / query / get_time / weather 等；按 settings 开关过滤。
control / query 成功后写 session.note_action，供「再暗一点」指代。

Tool Search 开启时另挂 tool_search / call_discovered_tool；
延迟工具只在目录里，经 call_discovered_tool 执行。

注意：工具参数不合法（TypeError）时不能覆盖 session.last，
否则「最近一次操作」会被清成空设备，下一句指代就断了。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from agents import FunctionTool
from agents.tool_context import ToolContext

from .llm import ToolCall
from .session import Session
from .tool_catalog import ToolCatalog
from .tool_search import meta_tool_schemas, run_call_discovered, run_tool_search
from .tools import Tools

# dispatch 返回这些前缀说明动作没生效，不能当作「最近一次操作」记下来
_FAIL_PREFIXES = ("没找到", "指代不清", "工具参数不对", "工具执行异常")


@dataclass
class ToolBag:
    """运行时共享袋：工具实现 + 可选会话（指代记忆）。"""

    tools: Tools
    session: Session | None = None
    # 可选：与 app._dispatch 同语义的旁路（FakeLLM / 测试）
    after_tool: Callable[[str, dict[str, Any], str], None] | None = None
    # 本轮已执行的工具结果（供 runtime 组装 LLMResult）
    last_results: list[tuple[str, str]] = field(default_factory=list)
    # 本轮 hook 收到的工具调用（兜底时也要能报出来）
    collected_calls: list[ToolCall] = field(default_factory=list)
    catalog: ToolCatalog = field(default_factory=ToolCatalog)
    discovered: set[str] = field(default_factory=set)
    mcp_servers: list[Any] = field(default_factory=list)
    tool_search_limit: int = 5

    def reset_turn(self) -> None:
        self.last_results.clear()
        self.collected_calls.clear()
        self.discovered.clear()


def _looks_failed(out: str) -> bool:
    return out.startswith(_FAIL_PREFIXES)


def _note(bag: ToolBag, name: str, args: dict[str, Any], out: str) -> None:
    bag.last_results.append((name, out))
    if bag.after_tool is not None:
        try:
            bag.after_tool(name, args, out)
        except Exception:  # noqa: BLE001
            pass
    if bag.session is None or _looks_failed(out):
        return
    if name == "control":
        dev = bag.tools.resolve(str(args.get("device", "")))
        bag.session.note_action(
            device=dev.name if dev else str(args.get("device", "")),
            action=str(args.get("action", "")),
            value=args.get("value"),
            result=out,
        )
    elif name == "query":
        dev = bag.tools.resolve(str(args.get("device", "")))
        bag.session.note_action(
            device=dev.name if dev else str(args.get("device", "")),
            action="query",
            value=None,
            result=out,
        )


def _note_session_only(bag: ToolBag, name: str, args: dict[str, Any], out: str) -> None:
    """只更新指代记忆，不追加 last_results（元工具已记过一次）。"""
    if bag.session is None or _looks_failed(out):
        return
    if name == "control":
        dev = bag.tools.resolve(str(args.get("device", "")))
        bag.session.note_action(
            device=dev.name if dev else str(args.get("device", "")),
            action=str(args.get("action", "")),
            value=args.get("value"),
            result=out,
        )
    elif name == "query":
        dev = bag.tools.resolve(str(args.get("device", "")))
        bag.session.note_action(
            device=dev.name if dev else str(args.get("device", "")),
            action="query",
            value=None,
            result=out,
        )


def _handler(bag: ToolBag, name: str) -> Callable[[ToolContext[Any], str], Any]:
    async def on_invoke(_ctx: ToolContext[Any], raw: str) -> str:
        try:
            args = json.loads(raw) if raw else {}
            if not isinstance(args, dict):
                args = {}
        except json.JSONDecodeError:
            args = {}
        out = bag.tools.dispatch(name, args)
        _note(bag, name, args, out)
        return out

    return on_invoke


def _meta_handler(bag: ToolBag, name: str) -> Callable[[ToolContext[Any], str], Any]:
    async def on_invoke(_ctx: ToolContext[Any], raw: str) -> str:
        try:
            args = json.loads(raw) if raw else {}
            if not isinstance(args, dict):
                args = {}
        except json.JSONDecodeError:
            args = {}
        if name == "tool_search":
            query = str(args.get("query") or "")
            limit = args.get("limit")
            try:
                lim = int(limit) if limit is not None else bag.tool_search_limit
            except (TypeError, ValueError):
                lim = bag.tool_search_limit
            out = run_tool_search(bag.catalog, bag.discovered, query, limit=lim)
            _note(bag, name, args, out)
            return out
        if name == "call_discovered_tool":
            tool_name = str(args.get("name") or "")
            inner_args = args.get("arguments")
            if inner_args is None:
                inner_args = {}
            if not isinstance(inner_args, dict):
                try:
                    inner_args = json.loads(inner_args) if isinstance(inner_args, str) else {}
                except (json.JSONDecodeError, TypeError):
                    inner_args = {}
            out = await run_call_discovered(
                catalog=bag.catalog,
                discovered=bag.discovered,
                name=tool_name,
                arguments=inner_args,
                local_dispatch=bag.tools.dispatch,
                mcp_servers=bag.mcp_servers,
            )
            # 记一次元调用；若是 local control/query 再补指代
            bag.last_results.append((name, out))
            if bag.after_tool is not None:
                try:
                    bag.after_tool(name, args, out)
                except Exception:  # noqa: BLE001
                    pass
            if tool_name in ("control", "query"):
                _note_session_only(bag, tool_name, dict(inner_args), out)
            return out
        out = f"没有这个工具：{name}"
        _note(bag, name, args, out)
        return out

    return on_invoke


def _ft_from_spec(spec: dict[str, Any], on_invoke) -> FunctionTool | None:
    fn = spec.get("function") or {}
    name = str(fn.get("name") or "")
    if not name:
        return None
    params = dict(fn.get("parameters") or {"type": "object", "properties": {}})
    return FunctionTool(
        name=name,
        description=str(fn.get("description") or name),
        params_json_schema=params,
        on_invoke_tool=on_invoke,
        strict_json_schema=False,
    )


def build_function_tools(bag: ToolBag) -> list[FunctionTool]:
    """从 Tools.schemas() 生成 FunctionTool；tool_search 开启时附加元工具。"""
    out: list[FunctionTool] = []
    for spec in bag.tools.schemas():
        ft = _ft_from_spec(spec, _handler(bag, str((spec.get("function") or {}).get("name") or "")))
        if ft is not None:
            out.append(ft)
    if getattr(bag.tools.settings, "tool_search_enabled", False):
        for spec in meta_tool_schemas():
            name = str((spec.get("function") or {}).get("name") or "")
            ft = _ft_from_spec(spec, _meta_handler(bag, name))
            if ft is not None:
                out.append(ft)
    return out
