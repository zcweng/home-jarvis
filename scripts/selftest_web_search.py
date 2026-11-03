#!/usr/bin/env python3
"""网页搜索自检：mock HTML 解析 +（可选）真网百度一次。

    .venv/bin/python scripts/selftest_web_search.py
    .venv/bin/python scripts/selftest_web_search.py --live
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.config import Settings  # noqa: E402
from jarvis.intent import looks_like_web_search  # noqa: E402
from jarvis.tools import Tools  # noqa: E402
from jarvis.web_search import FAIL, format_hits, parse_hits, search  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  —— {detail}" if detail else ""))


FIXTURE = """
<html><body>
<h3><a href="/link">成都天气预报一周查询</a></h3>
<div class="c-abstract">今天多云，最高 22 度，最低 14 度，空气良。</div>
<h3><a href="/link">生活服务</a></h3>
<div>应被跳过</div>
<h3><a href="/link">青城山一日游攻略</a></h3>
<span class="cu-line-clamp-1">前山看道观，后山徒步溪流，适合慢慢走一天。</span>
<h3><a href="/link">百度一下</a></h3>
</body></html>
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="真请求百度一次")
    args = ap.parse_args()

    hits = parse_hits(FIXTURE, count=5)
    check("解析条数", len(hits) == 2, str(hits))
    check("标题1", hits and "成都天气" in hits[0][0], hits[0][0] if hits else "")
    check("摘要1", hits and "22" in hits[0][1], hits[0][1] if hits else "")
    check("跳过生活服务", all("生活服务" not in t for t, _ in hits))

    text = format_hits(hits)
    check("格式含书名号", "「" in text and "」" in text, text[:80])
    check("非失败句", text != FAIL)

    long_hits = [("很长的标题" * 10, "摘要" * 40)] * 5
    clipped = format_hits(long_hits, max_chars=80)
    check("超长截断", len(clipped) <= 80 and clipped.endswith("…"), f"len={len(clipped)}")

    check("意图：天气", looks_like_web_search("成都今天天气怎么样"))
    check("意图：攻略", looks_like_web_search("青城山有什么好玩的"))
    check("意图：非命令误判", not looks_like_web_search("把吸顶灯关掉"))
    check("意图：闲聊", not looks_like_web_search("你好"))

    st = replace(Settings.load(), web_search_enabled=True)
    # Tools 需要 HA；用 dry 路径只测 schema，避免无 token 崩
    try:
        tools = Tools.__new__(Tools)
        tools.settings = st
        tools.devices = {}
        tools.ha = None  # type: ignore[assignment]
        tools.dry_run = True
        tools._announce = None
        names = [s["function"]["name"] for s in tools.schemas()]
        check("schema 含 web_search", "web_search" in names, str(names))
        st_off = replace(st, web_search_enabled=False)
        tools.settings = st_off
        names_off = [s["function"]["name"] for s in tools.schemas()]
        check("关闭时无 web_search", "web_search" not in names_off)
    except Exception as e:  # noqa: BLE001
        check("schema 注册", False, str(e))

    if args.live:
        out = search("成都天气", count=3, timeout=6.0)
        check("真网有返回", bool(out) and len(out) > 5, out[:120])
        if out == FAIL:
            print("⚠️  真网返回失败句（可能反爬），mock 路径仍算通过")
        else:
            check("真网含标题标记", "「" in out, out[:80])

        # 编排层：拒答后 force web_search → dispatch 真搜一次
        from jarvis.app import chat_with_guard
        from jarvis.llm_fake import FakeLLM

        calls: list[str] = []

        def dispatch(name: str, args: dict) -> str:
            calls.append(name)
            if name == "web_search":
                return search(str(args.get("query", "")), count=3, timeout=6.0)
            return "ok"

        # 第一次不调工具（像模型拒答），guard 应 force 重问
        class SilentThenForce(FakeLLM):
            def chat(self, messages, tools=None, dispatch=None, max_rounds=3,
                     temperature=0.3, force_tool=None, on_tool_calls=None):
                if force_tool:
                    return super().chat(
                        messages, tools, dispatch, max_rounds, temperature,
                        force_tool=force_tool, on_tool_calls=on_tool_calls,
                    )
                from jarvis.llm import LLMResult
                self.calls += 1
                return LLMResult(text="我没接天气查询。", rounds=1)

        llm = SilentThenForce()
        res = chat_with_guard(
            llm,
            [{"role": "user", "content": "成都今天天气怎么样"}],
            [{"type": "function", "function": {"name": "web_search", "parameters": {}}}],
            dispatch,
            "成都今天天气怎么样",
            web_search_enabled=True,
        )
        check("guard 强制 web_search", getattr(res, "forced", False) and "web_search" in calls,
              f"forced={getattr(res, 'forced', None)} calls={calls} text={res.text[:60]!r}")
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
