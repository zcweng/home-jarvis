#!/usr/bin/env python3
"""Tool Search / defer_loading 客户端等价实现自检（无需真 LLM / HA / MCP）。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.agent_tools import ToolBag, build_function_tools  # noqa: E402
from jarvis.config import Settings  # noqa: E402
from jarvis.tool_catalog import ToolCatalog  # noqa: E402
from jarvis.tool_search import run_call_discovered, run_tool_search  # noqa: E402
from jarvis.tools import Device, Tools  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  —— {detail}" if detail else ""))


class FakeHA:
    def state(self, entity_id: str) -> dict[str, Any]:
        return {"state": "off", "attributes": {}}

    def call_service(self, domain: str, service: str, data: dict[str, Any] | None = None) -> None:
        pass

    def states(self) -> list[dict[str, Any]]:
        return []


def _settings(**over: Any) -> Settings:
    st = Settings.load()
    return Settings(**{**st.__dict__, **over})


def test_catalog_keyword() -> None:
    print("\n① 目录关键词检索")
    cat = ToolCatalog()
    cat.add_local_schemas(
        [
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "联网搜索新闻价格",
                    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "memory",
                    "description": "记住或忘掉偏好",
                    "parameters": {"type": "object", "properties": {"action": {"type": "string"}}},
                },
            },
        ]
    )
    hits = cat.search("查一下青城山门票价格", limit=3)
    check("价格问句命中 web_search", any(h.name == "web_search" for h in hits), str([h.name for h in hits]))
    hits2 = cat.search("帮我记住不吃香菜", limit=3)
    check("记住问句命中 memory", any(h.name == "memory" for h in hits2), str([h.name for h in hits2]))
    blob = ToolCatalog.format_discovery(hits)
    check("discovery 含完整 schema", "parameters" in blob and "web_search" in blob, blob[:80])


def test_discovered_gate() -> None:
    print("\n② 未 discovered 拒绝调用")
    st = _settings(
        tool_search_enabled=True,
        web_search_enabled=True,
        memory_enabled=True,
        weather_enabled=False,
    )
    tools = Tools(FakeHA(), settings=st, announce=lambda _t: None)
    tools.devices["客厅灯"] = Device(entity_id="light.a", name="客厅灯", room="客厅", aliases=[], type="light")
    bag = ToolBag(tools=tools)
    bag.catalog.add_local_schemas(tools.deferred_local_schemas())

    async def _run() -> str:
        return await run_call_discovered(
            catalog=bag.catalog,
            discovered=set(),
            name="web_search",
            arguments={"query": "hi"},
            local_dispatch=tools.dispatch,
            mcp_servers=[],
        )

    out = asyncio.run(_run())
    check("未 search 不能 call", "先 tool_search" in out, out)

    disc: set[str] = set()
    run_tool_search(bag.catalog, disc, "联网搜索新闻", limit=5)
    check("search 写入 discovered", "web_search" in disc, str(disc))

    async def _run2() -> str:
        return await run_call_discovered(
            catalog=bag.catalog,
            discovered=disc,
            name="web_search",
            arguments={"query": "测试"},
            local_dispatch=lambda n, a: f"ok:{n}:{a.get('query')}",
            mcp_servers=[],
        )

    out2 = asyncio.run(_run2())
    check("discovered 后可 call", out2.startswith("ok:web_search"), out2)


def test_eager_vs_defer() -> None:
    print("\n③ eager / defer 分流与开关回退")
    st_on = _settings(
        tool_search_enabled=True,
        web_search_enabled=True,
        memory_enabled=True,
        weather_enabled=True,
        tool_search_defer_local="web_search,web_fetch,memory",
    )
    tools_on = Tools(FakeHA(), settings=st_on, announce=lambda _t: None)
    eager = {(s.get("function") or {}).get("name") for s in tools_on.schemas()}
    defer = {(s.get("function") or {}).get("name") for s in tools_on.deferred_local_schemas()}
    check("weather 仍 eager", "weather" in eager, str(eager))
    check("control 仍 eager", "control" in eager, str(eager))
    check("web_search 已 defer", "web_search" in defer and "web_search" not in eager, str(defer))
    check("memory 已 defer", "memory" in defer and "memory" not in eager, str(defer))

    bag = ToolBag(tools=tools_on)
    bag.catalog.add_local_schemas(tools_on.deferred_local_schemas())
    names = {t.name for t in build_function_tools(bag)}
    check("元工具挂上", "tool_search" in names and "call_discovered_tool" in names, str(sorted(names)))
    check("eager 不含 web_search", "web_search" not in names, str(sorted(names)))

    st_off = _settings(
        tool_search_enabled=False,
        web_search_enabled=True,
        memory_enabled=True,
        weather_enabled=True,
    )
    tools_off = Tools(FakeHA(), settings=st_off, announce=lambda _t: None)
    eager_off = {(s.get("function") or {}).get("name") for s in tools_off.schemas()}
    check("关闭 tool_search 时 web_search 回前缀", "web_search" in eager_off, str(eager_off))
    check("关闭时无 defer 列表", not tools_off.deferred_local_schemas(), str(tools_off.deferred_local_schemas()))
    bag_off = ToolBag(tools=tools_off)
    names_off = {t.name for t in build_function_tools(bag_off)}
    check("关闭时无元工具", "tool_search" not in names_off, str(sorted(names_off)))


def main() -> int:
    test_catalog_keyword()
    test_discovered_gate()
    test_eager_vs_defer()
    print("-" * 60)
    bad = [n for n, ok, _ in results if not ok]
    if bad:
        print(f"失败 {len(bad)} 项：{'、'.join(bad)}")
        return 1
    print(f"全部通过（{len(results)} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())