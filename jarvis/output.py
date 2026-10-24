"""语音输出路由：决定「贾维斯的声音」从哪出来。

为什么需要这一层：
- 电脑喇叭（本地 `say`）：<200ms 出声、可打断、音色可选，但只有坐在电脑前才听得到；
- 小爱音箱（米家 notify 播放文本）：全屋都听得到，但走小米云 TTS，约 1 秒延迟、音色固定、
  勿扰时段会被静音、而且**没法打断**（云端的音频不归我们控制）。

所以按场景切换，而不是二选一：
    TTS_TARGET=speaker   默认从音箱出声（本文件的重点）
    TTS_TARGET=local     只用电脑喇叭（原来的行为）
    TTS_TARGET=both      两边都说（调试用，会听到自己的回声）

`SpeechRouter` 故意做成和 `SayTTS` 同样的接口（speak/stop/warm/play_cached/wait_idle），
这样 `Jarvis` 里不用出现第二套分支逻辑。
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from .config import Settings
from .ha import HAError

# 小米云 TTS 每分钟大概能念这么多字；用来估算"说完要多久"（放回连续对话窗口的计时用）
CHARS_PER_SECOND = 4.5

_SENTENCE_ENDS = "。！？!?；;\n"


def split_for_speech(text: str, limit: int = 80) -> list[str]:
    """按句切成不超过 limit 字的片段——米家「播放文本」对单次长度有限制。"""
    text = (text or "").strip()
    if not text:
        return []
    parts: list[str] = []
    cur = ""
    for ch in text:
        cur += ch
        if ch in _SENTENCE_ENDS and len(cur) >= max(10, limit // 2):
            parts.append(cur)
            cur = ""
    if cur.strip():
        parts.append(cur)

    out: list[str] = []
    for part in parts:
        part = part.strip()
        while len(part) > limit:
            out.append(part[:limit])
            part = part[limit:]
        if part:
            out.append(part)
    return out


class SpeechRouter:
    """按配置把一句话送到音箱 / 电脑喇叭 / 两边都送。"""

    def __init__(
        self,
        settings: Settings,
        ha,
        local_tts,
        notify_entity: Callable[[], tuple[str, str]] | None = None,
    ) -> None:
        self.st = settings
        self.ha = ha
        self.local = local_tts
        self.target = (settings.tts_target or "speaker").lower()
        self.fallback_local = settings.speaker_fallback_local
        self._notify_entity = notify_entity
        self._speaking = False
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 对外接口（对齐 SayTTS）

    @property
    def voice(self) -> str:
        return "小爱（云端 TTS）" if self.target in ("speaker", "both") else self.local.voice

    @property
    def mode(self) -> str:
        return self.target

    @property
    def speaking(self) -> bool:
        return self._speaking or self.local.speaking

    def warm(self, phrases: list[str]) -> int:
        """只有本地播报才需要预渲染（音箱走云端，预渲染没有意义）。"""
        if self.target == "speaker":
            return 0
        return self.local.warm(phrases)

    def play_cached(self, text: str, blocking: bool = False) -> float:
        if self.target == "speaker":
            return 0.0
        return self.local.play_cached(text, blocking=blocking)

    def wait_idle(self, timeout: float = 2.0) -> None:
        self.local.wait_idle(timeout)

    def stop(self) -> None:
        self.local.stop()

    # ---------------------------------------------------------------- 播报

    def _speaker_entity(self) -> tuple[str, str]:
        if self._notify_entity is None:
            return "", ""
        try:
            return self._notify_entity()
        except Exception:  # noqa: BLE001
            return "", ""

    def _speak_to_speaker(self, text: str) -> bool:
        """送回 True 表示已经把话交给音箱了。"""
        entity_id, name = self._speaker_entity()
        if not entity_id:
            return False
        chunks = split_for_speech(text, max(20, self.st.announce_max_chars))
        try:
            for i, chunk in enumerate(chunks):
                self.ha.notify(entity_id, chunk)
                if i < len(chunks) - 1:
                    time.sleep(0.8)
            print(f"  🔊 音箱播报 → {name.split()[0]}（{len(chunks)} 段）")
            return True
        except HAError as e:
            print(f"  ⚠️  音箱播报失败：{e}")
            return False

    def speak(self, text: str, blocking: bool = True) -> threading.Thread | None:
        text = (text or "").strip()
        if not text:
            return None

        def _run() -> None:
            self._speaking = True
            try:
                delivered = False
                if self.target in ("speaker", "both"):
                    delivered = self._speak_to_speaker(text)
                    if delivered and blocking:
                        # 云端播报是异步的，按字数估个时长，避免窗口在它还没说完就关掉
                        time.sleep(min(15.0, max(1.0, len(text) / CHARS_PER_SECOND + 0.4)))
                if self.target == "both" or (not delivered and (self.fallback_local or self.target == "local")):
                    if delivered and self.target != "both":
                        return
                    self.local.speak(text, blocking=True)
            finally:
                self._speaking = False

        if blocking:
            _run()
            return None
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return t
