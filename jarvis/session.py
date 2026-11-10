"""会话与上下文。

三层记忆：
- L0 最近 N 轮原文（给模型看）
- L1 最近一次操作（device / action / value / result），用于"再暗一点""关掉它"
- L2 分层记忆（永久 + 短期，见 jarvis/memory/）——由 MemoryService 召回后注入 system prompt

指代消解不靠模型猜：每轮都往 system prompt 里塞一行「最近操作」，
模型只要照着填就够了。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

PROMPT_FILE = Path(__file__).resolve().parent.parent / "config" / "prompt.md"

CompressFn = Callable[[list[dict[str, str]]], list[dict[str, str]]]


@dataclass
class LastAction:
    device: str = ""
    action: str = ""
    value: float | None = None
    result: str = ""


@dataclass
class Session:
    system_template: str
    devices: str
    history: list[dict[str, str]] = field(default_factory=list)
    last: LastAction = field(default_factory=LastAction)
    max_turns: int = 8  # 保留最近 8 轮（16 条消息）；记忆开启时可被 compress 覆盖
    on_compress: CompressFn | None = None

    @classmethod
    def load(cls, devices: str, prompt_file: Path = PROMPT_FILE) -> Session:
        template = ""
        if prompt_file.exists():
            template = prompt_file.read_text(encoding="utf-8")
        return cls(system_template=template or "你是家里的语音管家，回答简短口语化。", devices=devices)

    # ------------------------------------------------------------------ prompt

    def system_prompt(self, memory_block: str = "") -> str:
        text = self.system_template.replace("{devices}", self.devices)
        block = (memory_block or "").strip()
        if block:
            text += "\n\n" + block
        if self.last.device:
            extra = f"\n\n【最近一次操作】{self.last.device} → {self.last.action}"
            if self.last.value is not None:
                extra += f" {self.last.value:g}"
            extra += f"（结果：{self.last.result}）\n用户说「它 / 那个 / 再…一点」时，指的就是这台设备。"
            text += extra
        return text

    def messages(self, user_text: str, memory_block: str = "") -> list[dict[str, str]]:
        msgs: list[dict[str, str]] = [{"role": "system", "content": self.system_prompt(memory_block)}]
        msgs.extend(self.history[-self.max_turns * 2 :])
        msgs.append({"role": "user", "content": user_text})
        return msgs

    # ------------------------------------------------------------------ 记录

    def add_user(self, text: str) -> None:
        self.history.append({"role": "user", "content": text})
        self._trim()

    def add_assistant(self, text: str) -> None:
        if text:
            self.history.append({"role": "assistant", "content": text})
            self._trim()

    def last_result_hint(self) -> str:
        """给规划器的一行上下文（指代消解：用户说「再暗一点」指的是哪台）。"""
        if not self.last.device:
            return ""
        return f"最近一次操作：{self.last.device} → {self.last.action}（结果：{self.last.result}）"

    def note_action(self, device: str, action: str, value: float | None, result: str) -> None:
        self.last = LastAction(device=device, action=action, value=value, result=result)

    def _trim(self) -> None:
        if self.on_compress is not None:
            try:
                self.history = self.on_compress(self.history)
                return
            except Exception:  # noqa: BLE001
                pass
        keep = self.max_turns * 2
        if len(self.history) > keep:
            self.history = self.history[-keep:]

    def reset(self) -> None:
        self.history.clear()
        self.last = LastAction()


def load_prompt_default() -> str:
    return PROMPT_FILE.read_text(encoding="utf-8") if PROMPT_FILE.exists() else ""
