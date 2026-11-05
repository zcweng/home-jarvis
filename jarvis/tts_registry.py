"""可热切换的 TTS 装配：neural | nano | say | qwen。"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any

from .audio import Speaker
from .config import Settings


BACKEND_ALIASES = {
    "neural": "neural",
    "sherpa": "neural",
    "vits": "neural",
    "nano": "nano",
    "nano-tts": "nano",
    "moss-nano": "nano",
    "say": "say",
    "qwen": "qwen",
    "qianwen": "qwen",
    "cosyvoice": "qwen",
}


def normalize_backend(name: str) -> str:
    key = (name or "").strip().lower()
    return BACKEND_ALIASES.get(key, key)


def _build_say(settings: Settings, speaker: Speaker | None):
    from .tts import SayTTS

    mode = "wav" if speaker is not None else settings.tts_mode
    return SayTTS(settings.tts_voice_say, mode, speaker, rate=settings.say_rate)


def _build_sherpa(settings: Settings, speaker: Speaker | None, *, sid: int | None = None):
    from .tts_neural import SherpaTTS

    model_dir = Path(settings.tts_model_dir)
    if not model_dir.exists() or not any(model_dir.glob("*.onnx")):
        print(
            f"⚠️  找不到神经 TTS 模型 {model_dir}，暂时回落到 macOS say"
            f"（下载：./scripts/fetch_models.sh tts）",
            file=sys.stderr,
        )
        return _build_say(settings, speaker)
    use_sid = settings.tts_sid if sid is None else int(sid)
    tts = SherpaTTS(
        settings.tts_model_dir,
        num_threads=settings.tts_threads,
        sid=use_sid,
        speed=settings.tts_speed,
        speaker=speaker,
        output_device=settings.output_device,
    )
    if use_sid:
        tts.voice = f"{tts.voice}#{use_sid}"
    return tts


def _fallback_name(settings: Settings) -> str:
    name = (settings.tts_fallback or settings.tts_nano_fallback or "neural").lower()
    return name


def build_fallback(settings: Settings, speaker: Speaker | None):
    name = _fallback_name(settings)
    if name in ("none", "off", ""):
        return None
    if name == "say":
        return _build_say(settings, speaker)
    return _build_sherpa(settings, speaker)


def build_backend(
    name: str,
    settings: Settings,
    speaker: Speaker | None,
    *,
    voice: str | None = None,
    sid: int | None = None,
    with_fallback: bool = True,
):
    """按名字创建单个后端实例（不做 Switchable 包装）。"""
    backend = normalize_backend(name)
    if backend == "nano":
        from .tts_nano import NanoTTS, probe_nano

        if not probe_nano(settings.tts_nano_url):
            print(
                "⚠️  nano 侧车未就绪，回落本地 VITS（先 ./scripts/nano_tts_server.sh start --bg）",
                file=sys.stderr,
            )
            return _build_sherpa(settings, speaker, sid=sid)
        return NanoTTS(
            base_url=settings.tts_nano_url,
            ref=settings.tts_nano_ref_audio,
            stream=settings.tts_nano_stream,
            timeout=settings.tts_nano_timeout,
            speed=settings.tts_nano_speed,
            speaker=speaker,
            fallback=build_fallback(settings, speaker) if with_fallback else None,
            output_device=settings.output_device,
        )
    if backend == "neural":
        return _build_sherpa(settings, speaker, sid=sid)
    if backend == "qwen":
        from .tts_qwen import QwenRealtimeTTS

        api_key = settings.dashscope_api_key
        if not api_key:
            print("⚠️  缺少 DASHSCOPE_API_KEY，回落本地 VITS", file=sys.stderr)
            return _build_sherpa(settings, speaker, sid=sid)
        return QwenRealtimeTTS(
            api_key=api_key,
            ws_url=settings.tts_qwen_ws_url,
            model=settings.tts_qwen_model,
            voice=voice or settings.tts_qwen_voice,
            sample_rate=settings.tts_qwen_sample_rate,
            timeout=settings.tts_qwen_timeout,
            speed=settings.tts_qwen_speed,
            speaker=speaker,
            fallback=build_fallback(settings, speaker) if with_fallback else None,
            output_device=settings.output_device,
        )
    if backend == "say":
        tts = _build_say(settings, speaker)
        if voice:
            tts.voice = voice
        return tts
    raise ValueError(f"未知 TTS 后端：{name}")


class SwitchableTTS:
    """懒加载多后端，运行时可 switch；接口对齐 SayTTS / SherpaTTS。"""

    def __init__(
        self,
        settings: Settings,
        speaker: Speaker | None = None,
        *,
        initial: str | None = None,
        warm_phrases: list[str] | None = None,
    ) -> None:
        self.settings = settings
        self._speaker = speaker
        self._cache: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._warm_phrases = list(warm_phrases or [])
        start = normalize_backend(initial or settings.tts_backend or "neural")
        if start not in ("neural", "nano", "say", "qwen"):
            start = "neural"
        self._active = self._get_or_build(start)
        # nano/qwen 缺依赖时 build_backend 会静默回落，以实际 mode 为准
        actual = getattr(self._active, "mode", start)
        self._active_name = actual if actual in ("neural", "nano", "say", "qwen") else start
        self.load_ms = float(getattr(self._active, "load_ms", 0.0) or 0.0)

    def set_warm_phrases(self, phrases: list[str]) -> None:
        self._warm_phrases = [p for p in phrases if p]

    def _cache_key(self, backend: str, *, voice: str | None = None, sid: int | None = None) -> str:
        if backend == "qwen":
            return f"qwen:{voice or self.settings.tts_qwen_voice}"
        if backend == "say":
            return f"say:{voice or self.settings.tts_voice_say}"
        if backend == "neural":
            use_sid = self.settings.tts_sid if sid is None else int(sid)
            return f"neural:{self.settings.tts_model_dir}#{use_sid}"
        return backend

    def _get_or_build(
        self,
        backend: str,
        *,
        voice: str | None = None,
        sid: int | None = None,
        force: bool = False,
    ):
        key = self._cache_key(backend, voice=voice, sid=sid)
        if force or key not in self._cache:
            self._cache[key] = build_backend(
                backend, self.settings, self._speaker, voice=voice, sid=sid
            )
        return self._cache[key]

    def switch(
        self,
        backend: str,
        *,
        voice: str | None = None,
        sid: int | None = None,
    ) -> str:
        name = normalize_backend(backend)
        if name not in ("neural", "nano", "say", "qwen"):
            return f"不支持的 TTS 后端：{backend}"
        if name == "qwen" and not self.settings.dashscope_api_key:
            return "切换失败：缺少 DASHSCOPE_API_KEY"
        if name == "nano":
            from .tts_nano import probe_nano

            if not probe_nano(self.settings.tts_nano_url):
                return "切换失败：nano 侧车未就绪，先 ./scripts/nano_tts_server.sh start --bg"

        with self._lock:
            try:
                self.stop()
            except Exception:  # noqa: BLE001
                pass
            # 换 voice/sid 时强制重建
            force = bool(voice) or sid is not None
            try:
                tts = self._get_or_build(name, voice=voice, sid=sid, force=force)
            except Exception as exc:  # noqa: BLE001
                return f"切换失败：{exc}"
            if name == "say" and voice:
                tts.voice = voice
            if name == "neural" and sid is not None:
                tts.sid = int(sid)
                base = Path(self.settings.tts_model_dir).name
                tts.voice = f"{base}#{int(sid)}" if sid else base
            if name == "qwen" and voice:
                tts.voice_name = voice
                tts.voice = f"qwen({voice})"
            self._active = tts
            self._active_name = name

        phrases = list(self._warm_phrases)

        def _warm() -> None:
            try:
                n = tts.warm(phrases)
                if n:
                    print(f"  🎙  已预渲染 {n} 条（后端 {tts.mode}/{tts.voice}）", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001
                print(f"  ⚠️  切换后预渲染失败：{exc}", file=sys.stderr)

        if phrases:
            threading.Thread(target=_warm, name="tts-warm-switch", daemon=True).start()

        label = {
            "neural": "本地神经",
            "nano": "Nano 侧车",
            "say": "系统 say",
            "qwen": "千问云端",
        }.get(name, name)
        return f"已切换到{label}音色 {tts.voice}"

    @property
    def backend(self) -> str:
        return self._active_name

    @property
    def voice(self) -> str:
        return getattr(self._active, "voice", self._active_name)

    @property
    def mode(self) -> str:
        return getattr(self._active, "mode", self._active_name)

    @property
    def speaking(self) -> bool:
        return bool(getattr(self._active, "speaking", False))

    @property
    def sample_rate(self) -> int:
        return int(getattr(self._active, "sample_rate", 16000))

    def synthesize(self, text: str):
        return self._active.synthesize(text)

    def speak(self, text: str, blocking: bool = True):
        return self._active.speak(text, blocking=blocking)

    def stop(self) -> None:
        self._active.stop()

    def warm(self, phrases: list[str]) -> int:
        self.set_warm_phrases(phrases)
        return self._active.warm(phrases)

    def play_cached(self, text: str, blocking: bool = False) -> float:
        return self._active.play_cached(text, blocking=blocking)

    def wait_idle(self, timeout: float = 2.0) -> None:
        self._active.wait_idle(timeout)


def build_tts(
    settings: Settings,
    speaker: Speaker | None = None,
    *,
    warm_phrases: list[str] | None = None,
) -> SwitchableTTS:
    """唯一装配入口：返回可热切换包装。"""
    return SwitchableTTS(settings, speaker, warm_phrases=warm_phrases)
