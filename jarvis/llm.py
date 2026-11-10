"""LLM 结果类型（编排层 / FakeLLM / Agents 运行时共用）。

真实模型调用已迁到 `agent_runtime.JarvisAgent`（OpenAI Agents SDK）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
    forced: bool = False  # 是否走了「强制工具」兜底


def no_thinking_body(settings: Any) -> dict[str, Any] | None:
    """DeepSeek 默认开思考：不显式关掉，正文会被 reasoning 吃光。

    实测 deepseek-flash 规划任务时 `finish_reason=length`，700 个 token 全是
    reasoning_tokens，`content` 是空串 → JSON 永远解析不出来。
    JarvisAgent 走 SDK 时也在 model_settings.extra_body 里塞同一份参数。
    """
    provider = (getattr(settings, "llm_provider", "") or "").strip().lower()
    if not provider:
        base = (getattr(settings, "llm_base_url", "") or "").lower()
        provider = "deepseek" if "deepseek" in base else "openai_compatible"
    if provider == "deepseek" or getattr(settings, "llm_disable_thinking", False):
        return {"thinking": {"type": "disabled"}}
    return None
