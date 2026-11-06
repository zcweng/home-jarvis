#!/usr/bin/env python3
"""网页搜索自检：格式化/schema +（可选）真连 SearXNG。

    .venv/bin/python scripts/selftest_web_search.py
    .venv/bin/python scripts/selftest_web_search.py --live
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.config import Settings  # noqa: E402
from jarvis.intent import looks_like_web_search  # noqa: E402
from jarvis.tools import Tools  # noqa: E402
from jarvis.web_search import (  # noqa: E402
    FAIL,
    FAIL_FETCH,
    format_fetch_payload,
    format_search_payload,
    search,
)

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  —— {detail}" if detail else ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="真请求本机 SearXNG 一次")
    args = ap.parse_args()

    payload = format_search_payload(
        {
            "query": "青城山门票",
            "results": [
                {
                    "result_id": "res_demo1",
                    "title": "青城山门票价格",
                    "url": "https://example.com/qingcheng",
                    "snippet": "成人票约 80 元。",
                    "full_text_command_text": "web_fetch(result_id='res_demo1')",
                },
                {
                    "result_id": "res_demo2",
                    "title": "青城山一日游",
                    "url": "https://example.com/guide",
                    "snippet": "前山后山怎么走。",
                    "full_text_command_text": "web_fetch(result_id='res_demo2')",
                },
            ],
        }
    )
    check("非失败句", payload != FAIL, payload[:80])
    try:
        data = json.loads(payload)
        check("JSON 可解析", True)
        check("含 result_id", data["results"][0]["result_id"] == "res_demo1")
        check("含 hint", "web_fetch" in data.get("hint", ""))
    except json.JSONDecodeError as e:
        check("JSON 可解析", False, str(e))

    check("空结果失败句", format_search_payload({"query": "x", "results": []}) == FAIL)

    fetch_ok = format_fetch_payload(
        {
            "success": True,
            "result_id": "res_demo1",
            "url": "https://example.com/qingcheng",
            "title": "门票",
            "content": "成人票 80 元，学生半价。",
            "start": 0,
            "end": 20,
            "total_chars": 20,
            "has_more": False,
            "next_fetch_command_text": None,
        }
    )
    check("fetch 成功含正文", "80" in fetch_ok and "res_demo1" in fetch_ok, fetch_ok[:80])
    check(
        "fetch 失败句",
        format_fetch_payload({"success": False, "error": ""}) == FAIL_FETCH,
    )

    check("意图：天气", looks_like_web_search("成都今天天气怎么样"))
    check("意图：攻略", looks_like_web_search("青城山有什么好玩的"))
    check("意图：非命令误判", not looks_like_web_search("把吸顶灯关掉"))
    check("意图：闲聊", not looks_like_web_search("你好"))

    st = replace(Settings.load(), web_search_enabled=True, tool_search_enabled=False)
    try:
        tools = Tools.__new__(Tools)
        tools.settings = st
        tools.devices = {}
        tools.ha = None  # type: ignore[assignment]
        tools.dry_run = True
        tools._announce = None
        names = [s["function"]["name"] for s in tools.schemas()]
        check("关闭 tool_search 时 schema 含 web_search", "web_search" in names, str(names))
        check("关闭 tool_search 时 schema 含 web_fetch", "web_fetch" in names, str(names))

        from jarvis.agent_tools import ToolBag, build_function_tools

        bag = ToolBag(tools=tools, session=None)
        ft_names = [t.name for t in build_function_tools(bag)]
        check("关闭 tool_search 时 FunctionTool 含 web_search", "web_search" in ft_names, str(ft_names))
        check("关闭 tool_search 时 FunctionTool 含 web_fetch", "web_fetch" in ft_names, str(ft_names))

        st_defer = replace(st, tool_search_enabled=True)
        tools.settings = st_defer
        eager = [s["function"]["name"] for s in tools.schemas()]
        deferred = [s["function"]["name"] for s in tools.deferred_local_schemas()]
        check("开启 tool_search 时 web_search 不在前缀", "web_search" not in eager, str(eager))
        check("开启 tool_search 时 web_search 在目录", "web_search" in deferred, str(deferred))
        bag2 = ToolBag(tools=tools, session=None)
        ft2 = [t.name for t in build_function_tools(bag2)]
        check("开启时有元工具", "tool_search" in ft2 and "call_discovered_tool" in ft2, str(ft2))

        st_off = replace(st, web_search_enabled=False)
        tools.settings = st_off
        names_off = [s["function"]["name"] for s in tools.schemas()]
        check("关闭 web 时无 web_search", "web_search" not in names_off)
        check("关闭 web 时无 web_fetch", "web_fetch" not in names_off)
    except Exception as e:  # noqa: BLE001
        check("schema 注册", False, str(e))

    if args.live:
        out = search("成都天气", count=3, timeout=8.0)
        check("真网有返回", bool(out) and len(out) > 5, out[:160])
        if out == FAIL or "依赖未安装" in out:
            print("⚠️  真网失败（SearXNG 未起或未装 --extra web），mock 路径仍算通过")
        else:
            try:
                live = json.loads(out)
                check("真网 JSON", "results" in live, out[:80])
                check("真网有 result_id", bool(live.get("results")), out[:80])
            except json.JSONDecodeError:
                check("真网 JSON", False, out[:80])

        from jarvis.app import chat_with_guard
        from jarvis.llm_fake import FakeLLM

        calls: list[str] = []

        def dispatch(name: str, args: dict) -> str:
            calls.append(name)
            if name == "web_search":
                return search(str(args.get("query", "")), count=3, timeout=8.0)
            return "ok"

        class SilentThenForce(FakeLLM):
            def chat(
                self,
                messages,
                tools=None,
                dispatch=None,
                max_rounds=3,
                temperature=0.3,
                force_tool=None,
                on_tool_calls=None,
            ):
                if force_tool:
                    return super().chat(
                        messages,
                        tools,
                        dispatch,
                        max_rounds,
                        temperature,
                        force_tool=force_tool,
                        on_tool_calls=on_tool_calls,
                    )
                from jarvis.llm import LLMResult

                self.calls += 1
                return LLMResult(text="我没接天气查询。", rounds=1)

        llm = SilentThenForce()
        res = chat_with_guard(
            llm,
            [{"role": "user", "content": "青城山有什么好玩的"}],
            [{"type": "function", "function": {"name": "web_search", "parameters": {}}}],
            dispatch,
            "青城山有什么好玩的",
            web_search_enabled=True,
        )
        check(
            "guard 强制 web_search",
            getattr(res, "forced", False) and "web_search" in calls,
            f"forced={getattr(res, 'forced', None)} calls={calls} text={res.text[:60]!r}",
        )
        check("guard 口述非空", bool(res.text) and len(res.text) > 5, res.text[:100])

    print("-" * 52)
    bad = [n for n, ok, _ in results if not ok]
    if bad:
        print(f"失败 {len(bad)} 项")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
