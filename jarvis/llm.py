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
