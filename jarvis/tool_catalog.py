"""延迟加载工具目录：服务端可搜，但不进模型 tools 前缀（对齐 Anthropic defer_loading）。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

Source = Literal["local", "mcp"]

# 本地 defer 工具的检索别名（关键词命中用）
LOCAL_ALIASES: dict[str, tuple[str, ...]] = {
    "web_search": ("搜索", "联网", "查一下", "新闻", "价格", "营业时间", "攻略", "search"),
    "web_fetch": ("抓取", "全文", "网页内容", "fetch", "result_id"),
    "memory": ("记住", "忘掉", "记忆", "偏好", "remind", "forget"),
}

_TOKEN = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN.findall(text or "") if t}


@dataclass
class ToolEntry:
    name: str
    description: str
    parameters: dict[str, Any]
    source: Source
    defer: bool = True
    aliases: tuple[str, ...] = ()
    server: str = ""  # MCP server name；local 为空
    mcp_tool_name: str = ""  # MCP 原始工具名（call 时用）
    embed: list[float] | None = field(default=None, repr=False)

    def searchable_text(self) -> str:
        parts = [self.name, self.description, *self.aliases]
        if self.server:
            parts.append(self.server)
        return " ".join(parts)


@dataclass
class ToolCatalog:
    entries: dict[str, ToolEntry] = field(default_factory=dict)

    def clear(self) -> None:
        self.entries.clear()

    def upsert(self, entry: ToolEntry) -> None:
        self.entries[entry.name] = entry

    def remove_source(self, source: Source, *, server: str | None = None) -> None:
        drop = [
            n
            for n, e in self.entries.items()
            if e.source == source and (server is None or e.server == server)
        ]
        for n in drop:
            del self.entries[n]

    def get(self, name: str) -> ToolEntry | None:
        return self.entries.get(name)

    def deferred(self) -> list[ToolEntry]:
        return [e for e in self.entries.values() if e.defer]

    def add_local_schemas(self, schemas: list[dict[str, Any]], *, defer: bool = True) -> None:
        for spec in schemas:
            fn = spec.get("function") or {}
            name = str(fn.get("name") or "")
            if not name:
                continue
            params = dict(fn.get("parameters") or {"type": "object", "properties": {}})
            aliases = LOCAL_ALIASES.get(name, ())
            self.upsert(
                ToolEntry(
                    name=name,
                    description=str(fn.get("description") or name),
                    parameters=params,
                    source="local",
                    defer=defer,
                    aliases=aliases,
                )
            )

    def add_mcp_tools(self, server_name: str, tools: list[Any]) -> None:
        self.remove_source("mcp", server=server_name)
        for t in tools:
            name = str(getattr(t, "name", "") or "")
            if not name:
                continue
            desc = str(getattr(t, "description", None) or name)
            schema = getattr(t, "inputSchema", None) or getattr(t, "input_schema", None) or {}
            if hasattr(schema, "model_dump"):
                schema = schema.model_dump(exclude_none=True)
            if not isinstance(schema, dict):
                schema = {"type": "object", "properties": {}}
            # 同名冲突：MCP 用 server__name 规避
            key = name
            if key in self.entries and (
                self.entries[key].source != "mcp" or self.entries[key].server != server_name
            ):
                key = f"{server_name}__{name}"
            aliases: tuple[str, ...] = (server_name,)
            if key != name:
                aliases = (name, server_name)
            self.upsert(
                ToolEntry(
                    name=key,
                    description=desc,
                    parameters=dict(schema),
                    source="mcp",
                    defer=True,
                    aliases=aliases,
                    server=server_name,
                    mcp_tool_name=name,
                )
            )

    def search(self, query: str, *, limit: int = 5) -> list[ToolEntry]:
        q = (query or "").strip()
        if not q:
            return []
        q_low = q.lower()
        q_toks = _tokens(q)
        scored: list[tuple[float, ToolEntry]] = []
        for e in self.deferred():
            score = 0.0
            blob = e.searchable_text().lower()
            if q_low in blob or e.name.lower() in q_low:
                score += 10.0
            for a in e.aliases:
                if a and (a.lower() in q_low or q_low in a.lower()):
                    score += 6.0
            e_toks = _tokens(e.searchable_text())
            if q_toks and e_toks:
                overlap = len(q_toks & e_toks)
                score += overlap * 2.0
            # 名称前缀/子串
            if e.name.lower() in q_low or q_low in e.name.lower():
                score += 4.0
            if score > 0:
                scored.append((score, e))
        scored.sort(key=lambda x: (-x[0], x[1].name))
        return [e for _, e in scored[: max(1, limit)]]

    @staticmethod
    def format_discovery(entries: list[ToolEntry]) -> str:
        if not entries:
            return "未找到匹配工具。换个说法再搜，或说明具体能力。"
        payload = []
        for e in entries:
            payload.append(
                {
                    "name": e.name,
                    "description": e.description,
                    "parameters": e.parameters,
                    "source": e.source,
                    "call_via": "call_discovered_tool",
                }
            )
        return json.dumps({"discovered": payload}, ensure_ascii=False)
