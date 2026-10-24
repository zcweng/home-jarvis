"""大模型：DeepSeek + function calling。

两个必须知道的 DeepSeek 事实（本机实测踩过）：
1. `deepseek-flash` / `deepseek-v4-pro` **默认开启 thinking**，而 thinking 模式下
   `tool_choice` 不被支持 → 必须显式传 `thinking: {"type": "disabled"}`，否则工具调用会报错
2. 模型 ID 只有这两个；写 `deepseek-chat` 会被静默改写成 flash（不报错，但别依赖）

调用策略：
- 控制类走 `deepseek-flash`（快），闲聊可切 `deepseek-v4-pro`
- 非流式：TTS 要整句才能念，流式对本链路收益有限，先把正确性做扎实
- 工具最多 3 轮，防止模型在"再查一次"里打转
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from .config import Settings


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResult:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[tuple[str, str]] = field(default_factory=list)
    latency_ms: float = 0.0
    rounds: int = 0
    raw_error: str = ""
    forced: bool = False  # 是否走了"强制工具"兜底


class DeepSeek:
    def __init__(self, settings: Settings | None = None, timeout: float = 30.0) -> None:
        st = settings or Settings.load()
        if not st.deepseek_key:
            raise SystemExit("缺少 DEEPSEEK_KEY，填到 .env 里")
        self.model = st.llm_model
        self._client = httpx.Client(
            base_url=st.llm_base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {st.deepseek_key}"},
            timeout=timeout,
        )
        self._thinking_param_ok = True

    # ------------------------------------------------------------------ 单次

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        # thinking 关闭：DeepSeek 默认开思考模式，而思考模式下不支持工具选择
        if self._thinking_param_ok:
            payload = {**payload, "thinking": {"type": "disabled"}}
        r = self._client.post("/chat/completions", json=payload)
        if r.status_code == 400 and self._thinking_param_ok:
            body = r.text
            if "thinking" in body or "tool_choice" in body:
                # 该模型/网关不支持 thinking 字段，退回不带参数再试一次
                self._thinking_param_ok = False
                payload.pop("thinking", None)
                r = self._client.post("/chat/completions", json=payload)
        if r.status_code >= 400:
            raise RuntimeError(f"DeepSeek HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    @staticmethod
    def _parse(msg: dict[str, Any]) -> tuple[str, list[ToolCall]]:
        text = (msg.get("content") or "").strip()
        calls: list[ToolCall] = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function", {})
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(id=tc.get("id") or f"call_{i}", name=fn.get("name", ""), arguments=args))
        return text, calls

    # ------------------------------------------------------------------ 对话（带工具循环）

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        dispatch=None,
        max_rounds: int = 3,
        temperature: float = 0.3,
        force_tool: str | None = None,
        on_tool_calls: Callable[[list[ToolCall]], None] | None = None,
    ) -> LLMResult:
        """force_tool：把 tool_choice 锁到某个工具（用于兜底重问）。

        DeepSeek 在 thinking 模式下不支持 tool_choice，本类默认已关闭 thinking。
        """
        t0 = time.perf_counter()
        result = LLMResult()
        msgs = list(messages)

        for rnd in range(max_rounds):
            result.rounds = rnd + 1
            payload: dict[str, Any] = {
                "model": self.model,
                "messages": msgs,
                "temperature": temperature,
                "stream": False,
            }
            if tools:
                payload["tools"] = tools
                if force_tool:
                    payload["tool_choice"] = {"type": "function", "function": {"name": force_tool}}
                else:
                    payload["tool_choice"] = "auto"

            data = self._post(payload)
            choices = data.get("choices") or []
            if not choices:
                result.raw_error = json.dumps(data)[:300]
                break
            msg = choices[0].get("message") or {}
            text, calls = self._parse(msg)
            if text:
                result.text = text

            if not calls or dispatch is None:
                break

            # 首句先出声：工具刚决定、还没执行时先给个短应答，掩盖第二轮延迟
            if on_tool_calls is not None:
                try:
                    on_tool_calls(calls)
                except Exception:  # noqa: BLE001
                    pass

            # 把 assistant 的工具调用原样回填，再逐条执行
            msgs.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": msg.get("tool_calls")})
            for call in calls:
                output = dispatch(call.name, call.arguments)
                result.tool_results.append((call.name, output))
                msgs.append({"role": "tool", "tool_call_id": call.id, "content": output})
            result.tool_calls.extend(calls)

        result.latency_ms = (time.perf_counter() - t0) * 1000
        return result

    def close(self) -> None:
        self._client.close()
