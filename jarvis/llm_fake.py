"""假模型：在没有可用 API key 的情况下，验证「编排」是否正确。

它**不验证模型聪明与否**，只验证我们这边的流程：
消息组装 → 工具调用 → 结果回填 → 会话记忆（指代）→ 回答选择。

用途：
    python -m jarvis.app --wav debug/q.wav --fake-llm
    scripts/selftest_chain.py

约定：脚本里可以用 "@last" 表示"最近一次操作的设备"，
它会被替换成 system prompt 里【最近一次操作】那一行解析出的设备名 ——
这样"再暗一点""关掉它"这类指代就能在离线状态下被测到。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .llm import LLMResult, ToolCall

LAST_RE = re.compile(r"【最近一次操作】(\S+?)\s*→")

DEFAULT_SCRIPT: list[tuple[str, str, dict[str, Any], str]] = [
    ("打开", "control", {"device": "客厅灯", "action": "on"}, "好，客厅灯打开了。"),
    ("关掉", "control", {"device": "@last", "action": "off"}, "关了。"),
    ("关闭", "control", {"device": "@last", "action": "off"}, "关了。"),
    ("暗", "control", {"device": "@last", "action": "brightness_delta", "value": -10}, "调暗了一点。"),
    ("低", "control", {"device": "@last", "action": "brightness_delta", "value": -10}, "调低了。"),
    ("亮", "control", {"device": "@last", "action": "brightness_delta", "value": 10}, "调亮了。"),
    ("多少", "query", {"device": "@last"}, ""),
    ("开着吗", "query", {"device": "@last"}, ""),
]


class FakeLLM:
    """按关键词匹配预设动作的假模型。"""

    def __init__(self, script: Iterable[tuple[str, str, dict[str, Any], str]] | None = None) -> None:
        self.script = list(script or DEFAULT_SCRIPT)
        self.latency_ms = 0.0
        self.calls = 0

    @staticmethod
    def _last_device(messages: list[dict[str, Any]]) -> str:
        for m in messages:
            if m.get("role") == "system":
                hit = LAST_RE.search(m.get("content", ""))
                if hit:
                    return hit.group(1)
        return "客厅灯"

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        dispatch=None,
        max_rounds: int = 3,
        temperature: float = 0.3,
        force_tool: str | None = None,
    ) -> LLMResult:
        self.calls += 1
        user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user = m.get("content", "")
                break

        res = LLMResult(rounds=1)
        matched = next((s for s in self.script if s[0] in user), None)
        if matched is None and force_tool == "control":
            matched = ("", "control", {"device": "@last", "action": "toggle"}, "好的。")

        if matched is None:
            res.text = "我在。"
            return res

        _kw, name, args, reply = matched
        args = dict(args)
        if args.get("device") == "@last":
            args["device"] = self._last_device(messages)

        call = ToolCall(id=f"fake_{self.calls}", name=name, arguments=args)
        res.tool_calls.append(call)
        if dispatch is not None:
            out = dispatch(name, args)
            res.tool_results.append((name, out))
            res.text = reply or out
        else:
            res.text = reply or "好的。"
        return res
