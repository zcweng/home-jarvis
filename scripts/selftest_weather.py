#!/usr/bin/env python3
"""天气工具自检：格式化 +（可选）真网气象局。

    .venv/bin/python scripts/selftest_weather.py
    .venv/bin/python scripts/selftest_weather.py --live
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.config import Settings  # noqa: E402
from jarvis.intent import looks_like_weather, looks_like_web_search  # noqa: E402
from jarvis.tools import Tools  # noqa: E402
from jarvis.weather import FAIL, format_report, lookup, resolve_station  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  —— {detail}" if detail else ""))


NOW = {
    "location": {"id": "56187", "name": "温江", "path": "中国, 四川, 温江"},
    "now": {
        "precipitation": 0.0,
        "temperature": 14.4,
        "humidity": 80.0,
        "windDirection": "东北风",
        "windScale": "微风",
    },
}
DAILY = {
    "location": NOW["location"],
    "daily": [
        {
            "date": "2026/11/03",
            "high": 18.0,
            "dayText": "阴",
            "low": 13.0,
            "nightText": "多云",
        },
        {
            "date": "2026/11/04",
            "high": 22.0,
            "dayText": "晴",
            "low": 14.0,
            "nightText": "多云",
        },
    ],
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()

    text = format_report(NOW, DAILY, days=2)
    check("含城市", "温江" in text, text)
    check("含气温", "14" in text, text)
    check("含今天明天", "今天" in text and "明天" in text, text)
    check("非失败句", text != FAIL)

    check("意图天气", looks_like_weather("成都今天天气怎么样"))
    check("意图天气：温江", looks_like_weather("温江下雨吗"))
    check("非天气命令", not looks_like_weather("把吸顶灯关掉"))
    check("天气仍可匹配搜索词", looks_like_web_search("成都今天天气怎么样"))

    st = replace(Settings.load(), weather_enabled=True)
    tools = Tools.__new__(Tools)
    tools.settings = st
    tools.devices = {}
    tools.ha = None  # type: ignore[assignment]
    tools.dry_run = True
    tools._announce = None
    names = [s["function"]["name"] for s in tools.schemas()]
    check("schema 含 weather", "weather" in names, str(names))
    tools.settings = replace(st, weather_enabled=False)
    check("关闭无 weather", "weather" not in [s["function"]["name"] for s in tools.schemas()])

    if args.live:
        hit = resolve_station("温江", timeout=5.0)
        check("解析温江", bool(hit and hit[0] == "56187"), str(hit))
        out = lookup("温江", timeout=5.0)
        check("真网有返回", bool(out) and out != FAIL and "温江" in out, out[:160])
        out2 = lookup("成都", timeout=5.0)
        check("真网成都", bool(out2) and out2 != FAIL and "成都" in out2, out2[:160])

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
