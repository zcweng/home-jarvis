#!/usr/bin/env python3
"""本地时间工具自检：实现 / schema / 意图守卫分流。

    .venv/bin/python scripts/selftest_get_time.py
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.app import chat_with_guard  # noqa: E402
from jarvis.config import Settings  # noqa: E402
from jarvis.intent import looks_like_time, looks_like_web_search  # noqa: E402
from jarvis.llm import LLMResult, ToolCall  # noqa: E402
from jarvis.tools import Tools  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  —— {detail}" if detail else ""))


def main() -> int:
    st = Settings.load()
    tools = Tools.__new__(Tools)
    tools.settings = st
    tools.devices = {}
    tools.ha = None  # type: ignore[assignment]
    tools.dry_run = True
    tools._announce = None
    tools.memory = None
    tools._tts = None

    out = tools.get_time()
    now = datetime.now()
    check("返回含年份", str(now.year) in out, out)
    check("返回含月日", f"{now.month}月{now.day}日" in out, out)
    check("返回含点分", "点" in out and "分" in out, out)
    check("返回含星期", "星期" in out, out)

    dispatched = tools.dispatch("get_time", {})
    check("dispatch 一致", dispatched == out, dispatched)

    names = [s["function"]["name"] for s in tools.schemas()]
    check("schema 含 get_time", "get_time" in names, str(names))

    check("意图：现在几点了", looks_like_time("现在几点了"))
    check("意图：今天星期几", looks_like_time("今天星期几"))
    check("意图：报时", looks_like_time("报时"))
    check("非本地：电影院几点开门", not looks_like_time("电影院几点开门"))
    check("开门问句可走搜索", looks_like_web_search("电影院几点开门"))
    check("几点了不走搜索", not looks_like_web_search("现在几点了"))

    forced: list[str | None] = []

    class _Stub:
        def chat(self, messages, tools=None, dispatch=None, force_tool=None, **kw):  # noqa: ANN001
            forced.append(force_tool)
            if force_tool == "get_time":
                call = ToolCall("t1", "get_time", {})
                text = dispatch("get_time", {}) if dispatch else "ok"
                return LLMResult(text=text, rounds=1, tool_calls=[call], tool_results=[("get_time", text)])
            return LLMResult(text="我猜现在三点。", rounds=1)

    res = chat_with_guard(_Stub(), [], [], tools.dispatch, "现在几点了")
    check("守卫强制 get_time", forced == [None, "get_time"], str(forced))
    check("守卫结果含时间", bool(re.search(r"\d+点", res.text or "")), (res.text or "")[:60])

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
