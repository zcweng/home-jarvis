"""会话与上下文。

三层记忆，P0 只做前两层（第三层留给 OpenViking）：
- L0 最近 N 轮原文（给模型看）
- L1 最近一次操作（device / action / value / result），用于"再暗一点""关掉它"
- L2 长期偏好（P2 再接）

指代消解不靠模型猜：每轮都往 system prompt 里塞一行「最近操作」，
模型只要照着填就够了。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

PROMPT_FILE = Path(__file__).resolve().parent.parent / "config" / "prompt.md"


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
    max_turns: int = 8  # 保留最近 8 轮（16 条消息）

    @classmethod
    def load(cls, devices: str, prompt_file: Path = PROMPT_FILE) -> Session:
        template = ""
        if prompt_file.exists():
            template = prompt_file.read_text(encoding="utf-8")
        return cls(system_template=template or "你是家里的语音管家，回答简短口语化。", devices=devices)

    # ------------------------------------------------------------------ prompt

    def system_prompt(self) -> str:
        text = self.system_template.replace("{devices}", self.devices)
        if self.last.device:
            extra = f"\n\n【最近一次操作】{self.last.device} → {self.last.action}"
            if self.last.value is not None:
                extra += f" {self.last.value:g}"
            extra += f"（结果：{self.last.result}）\n用户说「它 / 那个 / 再…一点」时，指的就是这台设备。"
            text += extra
        return text

    def messages(self, user_text: str) -> list[dict[str, str]]:
        msgs: list[dict[str, str]] = [{"role": "system", "content": self.system_prompt()}]
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

    def note_action(self, device: str, action: str, value: float | None, result: str) -> None:
        self.last = LastAction(device=device, action=action, value=value, result=result)

    def _trim(self) -> None:
        keep = self.max_turns * 2
        if len(self.history) > keep:
            self.history = self.history[-keep:]

    def reset(self) -> None:
        self.history.clear()
        self.last = LastAction()


def load_prompt_default() -> str:
    return PROMPT_FILE.read_text(encoding="utf-8") if PROMPT_FILE.exists() else ""
