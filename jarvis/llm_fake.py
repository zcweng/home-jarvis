"""假模型：在没有可用 API key 的情况下，验证「编排」是否正确。

它**不验证模型聪明与否**，只验证我们这边的流程：
消息组装 → 工具调用 → 结果回填 → 会话记忆（指代）→ 回答选择。

用途：
    python -m jarvis.app --wav debug/q.wav --fake-llm
    scripts/selftest_chain.py

约定：脚本里可以用 "@last" 表示"最近一次操作的设备"，用 "@device" 表示
"用户这句话里点名的设备"。两者都会被替换成真实设备名 —— 前者取自 system
prompt 里【最近一次操作】那一行，后者在 `devices` 候选里做**最长匹配**
（"吸顶灯2" 不能退化成 "吸顶灯"）。这样"再暗一点""关掉它"这类指代就能在
离线状态下被测到。

工具返回失败（没找到/指代不清/参数不对）时，回复直接采用工具结果，
绝不回"好，打开了。" —— 否则假模型自己就在演"说做了但没做"。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .llm import LLMResult, ToolCall

LAST_RE = re.compile(r"【最近一次操作】(\S+?)\s*→")

DEFAULT_SCRIPT: list[tuple[str, str, dict[str, Any], str]] = [
    ("打开", "control", {"device": "@device", "action": "on"}, "好，打开了。"),
    ("关掉", "control", {"device": "@device", "action": "off"}, "关了。"),
    ("关闭", "control", {"device": "@device", "action": "off"}, "关了。"),
    ("暗", "control", {"device": "@last", "action": "brightness_delta", "value": -10}, "调暗了一点。"),
    ("低", "control", {"device": "@last", "action": "brightness_delta", "value": -10}, "调低了。"),
    ("亮", "control", {"device": "@last", "action": "brightness_delta", "value": 10}, "调亮了。"),
    ("多少", "query", {"device": "@last"}, ""),
    ("开着吗", "query", {"device": "@last"}, ""),
]

# 没有设备清单时的兜底猜测（只用于"随便挑一台"，不做精确匹配）
_DEVICE_HINT = re.compile(r"(吸顶灯|客厅灯|卧室灯|主卧灯|次卧灯|餐厅灯|走廊灯|灯)")

# 工具结果里出现这些前缀 = 动作没生效
_FAIL_MARKS = ("没找到", "指代不清", "不在线", "参数不对", "执行异常", "未启用", "没有可用")


def _picked_failed(out: str) -> bool:
    return any(m in out for m in _FAIL_MARKS)


class FakeLLM:
    """按关键词匹配预设动作的假模型。"""

    def __init__(
        self,
        script: Iterable[tuple[str, str, dict[str, Any], str]] | None = None,
        devices: Iterable[str] | None = None,
    ) -> None:
        self.script = list(script or DEFAULT_SCRIPT)
        # 真实设备名/别名候选，按长度倒序便于最长匹配
        self.devices = sorted({d for d in (devices or []) if d}, key=len, reverse=True)
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

    def _pick_device(self, user: str, messages: list[dict[str, Any]]) -> str:
        """用户话里点名的设备：真实设备名优先（最长匹配），再退到兜底猜测。"""
        for name in self.devices:
            if name in user:
                return name
        hint = _DEVICE_HINT.search(user)
        if hint:
            return hint.group(1)
        return self._last_device(messages)

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        dispatch=None,
        max_rounds: int = 3,
        temperature: float = 0.3,
        force_tool: str | None = None,
        on_tool_calls=None,
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
            matched = ("", "control", {"device": "@device", "action": "toggle"}, "好的。")
        if matched is None and force_tool == "weather":
            matched = ("", "weather", {"city": ""}, "")
        if matched is None and force_tool == "get_time":
            matched = ("", "get_time", {}, "")
        if matched is None and force_tool == "web_search":
            matched = ("", "web_search", {"query": user}, "")

        if matched is None:
            res.text = "我在。"
            return res

        _kw, name, args, reply = matched
        args = dict(args)
        if args.get("device") == "@device":
            args["device"] = self._pick_device(user, messages)
        elif args.get("device") == "@last":
            args["device"] = self._last_device(messages)

        call = ToolCall(id=f"fake_{self.calls}", name=name, arguments=args)
        res.tool_calls.append(call)
        if on_tool_calls is not None:
            try:
                on_tool_calls([call])
            except Exception:  # noqa: BLE001
                pass
        if dispatch is not None:
            out = dispatch(name, args)
            res.tool_results.append((name, out))
            # 动作没生效就照实说，别回"好，打开了。"
            res.text = out if _picked_failed(out) else (reply or out)
        else:
            res.text = reply or "好的。"
        return res
