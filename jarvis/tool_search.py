"""客户端 Tool Search：tool_search + call_discovered_tool（Chat Completions 等价 defer_loading）。"""

from __future__ import annotations

import json
from typing import Any

from .tool_catalog import ToolCatalog


def meta_tool_schemas() -> list[dict[str, Any]]:
    """始终暴露给模型的两个元工具 schema（OpenAI function 形态）。"""
    return [
        {
            "type": "function",
            "function": {
                "name": "tool_search",
                "description": (
                    "按自然语言意图检索延迟加载的工具（联网搜索、记忆管理、外部 MCP 等）。"
                    "家电控制/查状态/天气/时间/换 TTS 请直接调对应常驻工具，不要搜。"
                    "返回匹配工具的完整 schema；再用 call_discovered_tool 调用。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "意图描述，例如「查青城山门票」「记住我不吃香菜」",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "最多返回几条，默认 5",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "call_discovered_tool",
                "description": (
                    "调用 tool_search 刚发现的工具。"
                    "name 必须是 discovery 结果里的 name；arguments 按该工具 parameters 填写。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "discovered 工具名",
                        },
                        "arguments": {
                            "type": "object",
                            "description": "传给该工具的参数对象；无参时可省略或传 {}",
                        },
                    },
                    "required": ["name"],
                },
            },
        },
    ]


def run_tool_search(
    catalog: ToolCatalog,
    discovered: set[str],
    query: str,
    *,
    limit: int = 5,
) -> str:
    hits = catalog.search(query, limit=limit)
    for e in hits:
        discovered.add(e.name)
    return ToolCatalog.format_discovery(hits)


def _mcp_result_text(result: Any) -> str:
    if result is None:
        return ""
    if getattr(result, "is_error", False):
        # 仍尽量抽出可读内容
        pass
    parts: list[str] = []
    content = getattr(result, "content", None) or []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            parts.append(str(text))
            continue
        # 兜底：结构化块
        if hasattr(block, "model_dump"):
            parts.append(json.dumps(block.model_dump(exclude_none=True), ensure_ascii=False))
        else:
            parts.append(str(block))
    structured = getattr(result, "structured_content", None)
    if structured and not parts:
        try:
            parts.append(json.dumps(structured, ensure_ascii=False))
        except TypeError:
            parts.append(str(structured))
    text = "\n".join(parts).strip()
    if getattr(result, "is_error", False) and text:
        return f"工具失败：{text}"
    if getattr(result, "is_error", False):
        return "工具失败（无详情）"
    return text or "（空结果）"


async def run_call_discovered(
    *,
    catalog: ToolCatalog,
    discovered: set[str],
    name: str,
    arguments: dict[str, Any] | None,
    local_dispatch,
    mcp_servers: list[Any],
) -> str:
    tool_name = (name or "").strip()
    if not tool_name:
        return "缺少工具名。"
    if tool_name not in discovered:
        # 允许精确命中目录且本轮已搜过同名别名？严格：必须 discovered
        return f"请先 tool_search 发现「{tool_name}」再调用。"
    entry = catalog.get(tool_name)
    if entry is None:
        return f"目录里没有工具：{tool_name}"
    args = arguments if isinstance(arguments, dict) else {}

    if entry.source == "local":
        # catalog key 即本地工具名
        return str(local_dispatch(entry.name, args))

    real_name = (entry.mcp_tool_name or entry.name).strip()
    server = None
    for s in mcp_servers:
        if str(getattr(s, "name", "")) == entry.server:
            server = s
            break
    if server is None and len(mcp_servers) == 1:
        server = mcp_servers[0]
    if server is None:
        return f"找不到 MCP 服务「{entry.server}」。"

    try:
        result = await server.call_tool(real_name, args)
    except Exception as e:  # noqa: BLE001
        return f"MCP 调用失败：{e}"
    return _mcp_result_text(result)
