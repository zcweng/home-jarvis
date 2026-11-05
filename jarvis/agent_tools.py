"""把 Tools 领域逻辑包装成 Agents SDK FunctionTool。

schema 与 Tools.schemas() 对齐（枚举、描述、必填），避免模型乱填参。
含 control / query / get_time / weather 等；按 settings 开关过滤。
control / query 成功后写 session.note_action，供「再暗一点」指代。

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


def build_function_tools(bag: ToolBag) -> list[FunctionTool]:
    """从 Tools.schemas() 生成 FunctionTool 列表（按 settings 开关过滤）。"""
    out: list[FunctionTool] = []
    for spec in bag.tools.schemas():
        fn = spec.get("function") or {}
        name = str(fn.get("name") or "")
        if not name:
            continue
        params = dict(fn.get("parameters") or {"type": "object", "properties": {}})
        out.append(
            FunctionTool(
                name=name,
                description=str(fn.get("description") or name),
                params_json_schema=params,
                on_invoke_tool=_handler(bag, name),
                # 关闭 strict：保留 value 等可选字段（strict 会把 properties 全塞进 required）
                strict_json_schema=False,
            )
        )
    return out
