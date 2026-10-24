#!/usr/bin/env python3
"""LLM 层自检：连通性 / function calling / 指代消解 / 边界。

只走文本，不碰音频 —— 用来把"模型不听话"和"语音链路坏了"分开。
会真的操作 HA 里的设备（demo 虚拟灯）。

    .venv/bin/python scripts/selftest_llm.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.config import Settings  # noqa: E402
from jarvis.ha import HAClient  # noqa: E402
from jarvis.llm import DeepSeek  # noqa: E402
from jarvis.session import Session  # noqa: E402
from jarvis.tools import Tools  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  —— {detail}" if detail else ""))


def main() -> int:
    st = Settings.load()
    print(f"模型 {st.llm_model} @ {st.llm_base_url}\n")

    ha = HAClient(st)
    tools = Tools(ha)

    # 动态选一台在线的灯，避免硬编码（demo 虚拟灯早已移除）
    online = [
        d for d in tools.devices.values()
        if d.type == "light" and (ha.state(d.entity_id) or {}).get("state") not in (None, "unavailable")
    ]
    if not online:
        print("❌ 别名表里没有在线的灯")
        return 1
    dev = online[0]
    target = dev.name
    print(f"测试目标：{target}（{dev.entity_id}）")
    session = Session.load(tools.catalog())
    llm = DeepSeek(st)

    def turn(text: str):
        msgs = session.messages(text)
        t0 = time.perf_counter()
        res = llm.chat(msgs, Tools.schemas(), dispatch=dispatch)
        session.add_user(text)
        session.add_assistant(res.text)
        print(f"   耗时 {res.latency_ms:.0f}ms · 轮次 {res.rounds} · 工具调用 "
              f"{[c.name for c in res.tool_calls] or '无'}")
        return res

    def dispatch(name: str, args: dict) -> str:
        out = tools.dispatch(name, args)
        if name in ("control", "query"):
            dev = tools.resolve(str(args.get("device", "")))
            session.note_action(dev.name if dev else str(args.get("device", "")),
                                str(args.get("action", "")), args.get("value"), out)
        return out

    # 1. 纯聊天（不应调用工具）
    print("① 纯聊天：你好，你能做什么")
    r1 = turn("你好，你能做什么？")
    check("纯聊天返回文本", bool(r1.text), (r1.text or r1.raw_error)[:60])
    check("纯聊天不误调工具", not r1.tool_calls, str([c.name for c in r1.tool_calls]))

    # 2. 明确指令 → 必须调 control
    print("\n② 明确指令：把客厅灯打开")
    r2 = turn(f"把{target}打开")
    called = [c for c in r2.tool_calls if c.name == "control"]
    ok2 = bool(called) and called[0].arguments.get("action") in ("on", "打开")
    check("识别出 control 工具", bool(called), str([c.arguments for c in called])[:80])
    state = (ha.state(dev.entity_id) or {}).get("state")
    check("HA 状态真的变了", state == "on", f"light={state}")
    check("回答有确认文本", bool(r2.text), (r2.text or "")[:50])

    # 3. 指代：再暗一点（依赖上一轮设备）
    print("\n③ 指代：再暗一点（看它是否用上一轮的客厅灯）")
    r3 = turn("再暗一点")
    calls3 = [c for c in r3.tool_calls if c.name == "control"]
    used = calls3[0].arguments.get("device") if calls3 else ""
    check("指代命中上一台设备", used == target or (used and target in used), f"device={used!r}")
    check("相对动作", bool(calls3) and calls3[0].arguments.get("action") in ("brightness_delta", "brightness"),
          str(calls3[0].arguments) if calls3 else "no call")

    # 4. 查询
    print("\n④ 查询：客厅灯现在什么状态")
    r4 = turn(f"{target}现在是开着的吗")
    check("走 query 工具", any(c.name == "query" for c in r4.tool_calls),
          str([c.name for c in r4.tool_calls]))

    # 5. 不存在的设备 → 不瞎猜
    print("\n⑤ 边界：把火星灯打开")
    r5 = turn("把火星灯打开")
    no_bogus = all("火星" in str(c.arguments.get("device", "")) or c.name != "control" for c in r5.tool_calls)
    text5 = (r5.text or "") + " ".join(o for _, o in r5.tool_results)
    check("不虚构设备", no_bogus, text5[:80])

    # 6. 闲聊不误触工具
    print("\n⑥ 闲聊：今天适合跑步吗")
    r6 = turn("今天适合出去跑步吗？")
    check("闲聊不误触工具", not r6.tool_calls, str([c.name for c in r6.tool_calls]))

    # 7. 守卫：模型编造"已执行"时必须被强制拉回工具调用
    print("\n⑦ 守卫：模拟模型不调工具、直接回复已经关了")
    from jarvis.app import chat_with_guard
    from jarvis.llm import LLMResult, ToolCall

    class _Stub:
        def __init__(self) -> None:
            self.force_args: list[str | None] = []

        def chat(self, messages, tools=None, dispatch=None, force_tool=None, **kw):  # noqa: ANN001
            self.force_args.append(force_tool)
            if force_tool == "control":
                out = dispatch("control", {"device": "客厅灯", "action": "off"})
                return LLMResult(text="客厅灯已经关了。", rounds=1,
                                 tool_calls=[ToolCall("c1", "control", {"device": "客厅灯", "action": "off"})],
                                 tool_results=[("control", out)])
            return LLMResult(text="客厅灯已经关了。", rounds=1)

    stub = _Stub()
    guarded = chat_with_guard(stub, [], [], lambda n, a: f"tool:{n}", "把客厅灯关掉")
    check("守卫触发重问", stub.force_args == [None, "control"], str(stub.force_args))
    check("守卫后确有工具调用", bool(guarded.tool_calls) and guarded.forced, str([c.name for c in guarded.tool_calls]))

    llm.close()
    ha.close()
    ok = all(r[1] for r in results)
    print("-" * 56)
    print("全部通过" if ok else f"失败 {sum(1 for r in results if not r[1])} 项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
