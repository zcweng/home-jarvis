"""配置：极简 .env 加载 + 运行参数。

设计约定：
- 一切从环境变量来，代码里零硬编码（迁移到 MacMini 只改 .env）
- 设备用「名字子串」描述，不用索引（插拔一次索引就变）
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def load_env(path: Path = ENV_FILE) -> None:
    """读取 .env。已存在的进程环境变量优先（方便临时覆盖）。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        # 去掉行尾注释与引号
        value = re.sub(r"\s+#.*$", "", value).strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # 服务
    ha_url: str = "http://127.0.0.1:8123"
    ha_token: str = ""
    llm_model: str = "deepseek-flash"
    llm_base_url: str = "https://api.deepseek.com"
    deepseek_key: str = ""
    # 音频
    input_device: str = "外置麦克风"
    output_device: str = "MacBook Pro扬声器"
    # ASR
    asr_model_dir: str = "models/sensevoice"
    asr_language: str = "zh"
    # TTS
    tts_backend: str = "neural"       # neural（本地 VITS，自然）| say（macOS 系统合成，快但生硬）
    tts_model_dir: str = "models/vits-zh-hf-fanchen-C"
    tts_threads: int = 4
    tts_sid: int = 0                  # 多说话人模型选第几个
    tts_speed: float = 1.35           # 1.0 明显偏慢；1.3~1.5 接近日常语速
    say_rate: int = 170               # macOS say 语速（默认 175 偏快）
    tts_mode: str = "direct"
    tts_voice_say: str = "Tingting"
    # VAD
    vad_model: str = "models/silero-vad/silero_vad.onnx"
    vad_threshold: float = 0.5
    vad_silence_ms: int = 500
    # 唤醒
    wake_words: str = "贾维斯|小贾|管家"
    wake_backend: str = "kws"
    sleep_words: str = "退下吧|退下|退出|退出对话|结束|结束对话|再见|拜拜|不聊了|没事了|休息吧|你去忙吧"
    sleep_reply: str = "好，有事叫我。"
    kws_model: str = "models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
    # 打断与首句
    ack_phrases: str = "好的|嗯|收到"
    barge_in_margin_db: float = 12.0
    # 语音输出路由：speaker（小爱优先）| local（电脑喇叭）| both
    tts_target: str = "speaker"
    speaker_fallback_local: bool = True
    # 播报（小爱音箱等）
    announce_entity: str = ""          # 留空=自动找带 play_text 的 notify 实体
    announce_max_chars: int = 80       # 单条文本上限，超了按句切分
    # 会话
    follow_up_seconds: float = 8.0

    @classmethod
    def load(cls) -> Settings:
        load_env()
        return cls(
            ha_url=_env("HA_URL", cls.ha_url),
            ha_token=_env("HA_TOKEN", cls.ha_token),
            llm_model=_env("LLM_MODEL", cls.llm_model),
            llm_base_url=_env("LLM_BASE_URL", cls.llm_base_url),
            deepseek_key=_env("DEEPSEEK_KEY", cls.deepseek_key),
            input_device=_env("INPUT_DEVICE", cls.input_device),
            output_device=_env("OUTPUT_DEVICE", cls.output_device),
            asr_model_dir=_env("ASR_MODEL_DIR", cls.asr_model_dir),
            asr_language=_env("ASR_LANGUAGE", cls.asr_language),
            tts_backend=_env("TTS_BACKEND", cls.tts_backend),
            tts_model_dir=_env("TTS_MODEL_DIR", cls.tts_model_dir),
            tts_threads=_env_int("TTS_THREADS", cls.tts_threads),
            tts_sid=_env_int("TTS_SID", cls.tts_sid),
            tts_speed=_env_float("TTS_SPEED", cls.tts_speed),
            say_rate=_env_int("SAY_RATE", cls.say_rate),
            tts_mode=_env("TTS_MODE", cls.tts_mode),
            tts_voice_say=_env("TTS_VOICE_SAY", cls.tts_voice_say),
            vad_model=_env("VAD_MODEL", cls.vad_model),
            vad_threshold=_env_float("VAD_THRESHOLD", cls.vad_threshold),
            vad_silence_ms=_env_int("VAD_SILENCE_MS", cls.vad_silence_ms),
            wake_words=_env("WAKE_WORDS", cls.wake_words),
            wake_backend=_env("WAKE_BACKEND", cls.wake_backend),
            sleep_words=_env("SLEEP_WORDS", cls.sleep_words),
            sleep_reply=_env("SLEEP_REPLY", cls.sleep_reply),
            kws_model=_env("KWS_MODEL", cls.kws_model),
            ack_phrases=_env("ACK_PHRASES", cls.ack_phrases),
            barge_in_margin_db=_env_float("BARGE_IN_MARGIN_DB", cls.barge_in_margin_db),
            tts_target=_env("TTS_TARGET", cls.tts_target),
            speaker_fallback_local=_env("SPEAKER_FALLBACK_LOCAL", "true").lower() in ("1", "true", "yes"),
            announce_entity=_env("ANNOUNCE_ENTITY", cls.announce_entity),
            announce_max_chars=_env_int("ANNOUNCE_MAX_CHARS", cls.announce_max_chars),
            follow_up_seconds=_env_float("FOLLOW_UP_SECONDS", cls.follow_up_seconds),
        )

    def require_ha(self) -> None:
        if not self.ha_token:
            raise SystemExit("缺少 HA_TOKEN：执行 python3 scripts/set_ha_token.py")
