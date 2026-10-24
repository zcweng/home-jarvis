#!/usr/bin/env python3
"""唤醒词 + 首句先出声 自检（纯逻辑，不需要麦克风和模型）。

覆盖：
1. 精确唤醒、模糊唤醒（ASR 把"贾维斯"听成"亚维斯"）
2. 不含唤醒词的话必须被忽略
3. 唤醒词与指令同句 → 剥离唤醒词后只留指令
4. 连续对话窗口：唤醒后免唤醒；窗口过期后重新需要唤醒词
5. 首句先出声：工具决定后立刻播应答，早于模型最终回答返回

    .venv/bin/python scripts/selftest_wake.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.app import chat_with_guard  # noqa: E402
from jarvis.llm import LLMResult, ToolCall  # noqa: E402
from jarvis.wake import WakeGate  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  —— {detail}" if detail else ""))


def fresh(follow_up: float = 0.0) -> WakeGate:
    """每句独立判定用（窗口 0 = 说完这一句就忘）。"""
    return WakeGate(follow_up_seconds=follow_up)


def main() -> int:
    # ① 精确 / 模糊唤醒
    check("精确唤醒", fresh().feed("贾维斯打开卧室灯") == (True, "打开卧室灯"))
    check("模糊唤醒（亚维斯）", fresh().feed("亚维斯打开卧室灯") == (True, "打开卧室灯"),
          str(fresh().feed("亚维斯打开卧室灯")))
    check("模糊唤醒（佳维斯）", fresh().feed("佳维斯，把灯关了") == (True, "把灯关了"))

    # ② 不含唤醒词 → 忽略
    for text in ["今天天气怎么样", "打开客厅灯", "把灯关掉"]:
        woke, _ = fresh().feed(text)
        check(f"无唤醒词被忽略：{text}", not woke)

    # ③ 只有唤醒词 → 空指令（应回复"我在，说吧"）
    woke, cmd = fresh().feed("管家")
    check("仅唤醒词", woke and cmd == "", f"cmd={cmd!r}")

    # ④ 连续对话窗口
    g = WakeGate(follow_up_seconds=1.0)
    g.feed("贾维斯")
    ok1 = g.feed("打开客厅灯")[0]
    ok2 = g.feed("再暗一点")[0]
    check("窗口内免唤醒", ok1 and ok2)
    g2 = WakeGate(follow_up_seconds=0.05)
    g2.feed("贾维斯")
    time.sleep(0.1)
    check("窗口过期后需重新唤醒", not g2.feed("打开客厅灯")[0])

    # ⑤ 窗口语义：处理/播报期间不倒计时，播报结束后才重新开始计时
    g3 = WakeGate(follow_up_seconds=1.0)
    g3.touch()
    g3.hold()
    time.sleep(1.5)                     # 模拟 LLM 1.2s + 播报 0.3s，超过 1s 窗口
    held_awake = g3.awake
    g3.release()
    right_after = g3.awake
    time.sleep(0.5)
    still_ok = g3.awake
    time.sleep(0.8)
    expired = g3.awake
    check("处理期间不倒计时", held_awake, "hold 状态下 awake=True")
    check("播报结束立刻可用", right_after and still_ok, "release 后 0.5s 仍可续说")
    check("窗口到期后上锁", not expired, "release 后 1.3s 已过期")
    check("剩余窗口可读", g3.remaining() == 0.0, f"remaining={g3.remaining():.2f}s")

    # ⑥ 收线指令：先播报"好，有事叫我。"，播完再关窗口
    from jarvis.app import handle_sleep
    from jarvis.wake import DEFAULT_SLEEP_WORDS, is_sleep_request

    for text, expect in [("退下吧", True), ("退出", True), ("结束对话", True), ("再见", True),
                         ("好的，退下吧", True), ("把空调退出节能模式", False), ("客厅灯关掉", False)]:
        check(f"收线判定：{text}", is_sleep_request(text) == expect)

    g4 = WakeGate(follow_up_seconds=8)
    g4.touch()
    order: list[str] = []

    def _speak() -> None:
        order.append("speak")
        if g4.awake:
            order.append("window_open_during_speak")

    done = handle_sleep("退下吧", g4, DEFAULT_SLEEP_WORDS, _speak)
    check("识别为收线", done)
    check("播报时窗口仍开着", "window_open_during_speak" in order, str(order))
    check("播报后窗口关闭", not g4.awake)
    check("需重新唤醒", not g4.feed("打开客厅灯")[0])
    check("唤醒词可再唤起", g4.feed("贾维斯")[0])

    # ⑦ 首句先出声：应答回调必须早于模型最终回答
    events: list[tuple[str, float]] = []

    class _Stub:
        def chat(self, messages, tools=None, dispatch=None, force_tool=None,
                 on_tool_calls=None, **kw):  # noqa: ANN001
            call = ToolCall("c1", "control", {"device": "客厅灯", "action": "on"})
            if on_tool_calls:
                on_tool_calls([call])          # 工具刚决定 → 立刻“先出声”
            events.append(("ack", time.perf_counter()))
            time.sleep(0.4)                    # 模拟第二轮的模型耗时
            out = dispatch("control", {"device": "客厅灯", "action": "on"})
            events.append(("final", time.perf_counter()))
            return LLMResult(text="客厅灯已经打开了。", rounds=2,
                             tool_calls=[call], tool_results=[("control", out)])

    def _ack(calls) -> None:  # noqa: ANN001
        events.append(("ack_played", time.perf_counter()))

    res = chat_with_guard(_Stub(), [], [], lambda n, a: f"tool:{n}", "把客厅灯打开",
                          on_tool_calls=_ack)
    order = [e[0] for e in events if e[0] != "ack"]
    check("首句早于最终回答", order[:2] == ["ack_played", "final"], str(order))
    ts = dict(events)
    gap = (ts["final"] - ts["ack_played"]) * 1000
    check("应答提前量可观", gap > 300, f"提前 {gap:.0f}ms")

    ok = all(r[1] for r in results)
    print("-" * 52)
    print("全部通过" if ok else f"失败 {sum(1 for r in results if not r[1])} 项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
