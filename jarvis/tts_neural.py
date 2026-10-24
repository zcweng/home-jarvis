"""神经 TTS（本地离线）：sherpa-onnx + VITS。

为什么加这个：macOS `say` 是共振峰拼接式合成，音节之间是"拼"出来的，所以生硬、没有起伏。
神经 TTS（VITS）用声学模型生成波形，中文自然度明显高一档，而且**完全离线**——
不像 edge-tts 要绕到境外服务器（实测首包 1695ms，国内不可用）。

实测（MacBook Pro M4）：
    sherpa-onnx-vits-zh-ll   首包 ~?ms
    macOS say                首包 ~750ms（但音质生硬）

模型放 models/ 下，`./scripts/fetch_models.sh tts` 下载。
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from pathlib import Path

import numpy as np

from .audio import Speaker
from .config import Settings
from .output import split_for_speech
from .textnorm import normalize_for_tts


class SherpaTTS:
    """sherpa-onnx OfflineTts 封装，接口与 SayTTS 对齐。"""

    def __init__(
        self,
        model_dir: str | Path,
        num_threads: int = 4,
        sid: int = 0,
        speed: float = 1.0,
        speaker: Speaker | None = None,
        output_device: str = "",
    ) -> None:
        import sherpa_onnx

        d = Path(model_dir)
        if not d.exists():
            raise SystemExit(f"缺少 TTS 模型：{d}\n先执行 ./scripts/fetch_models.sh tts")

        model = next(d.glob("*.onnx"), None)
        tokens = d / "tokens.txt"
        if model is None or not tokens.exists():
            raise SystemExit(f"{d} 里没有 model.onnx / tokens.txt")
        lexicon = d / "lexicon.txt"
        data_dir = d / "espeak-ng-data"

        cfg = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                    model=str(model),
                    lexicon=str(lexicon) if lexicon.exists() else "",
                    tokens=str(tokens),
                    data_dir=str(data_dir) if data_dir.exists() else "",
                ),
                num_threads=num_threads,
                provider="cpu",
            ),
            max_num_sentences=1,
        )
        t0 = time.perf_counter()
        self._tts = sherpa_onnx.OfflineTts(cfg)
        self.load_ms = (time.perf_counter() - t0) * 1000
        self.sample_rate = self._tts.sample_rate
        self._output_device = output_device
        self.sid = sid
        self.speed = speed
        self.voice = d.name
        self.mode = "neural"
        self._speaker = speaker
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- 合成

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        # 这个模型的词典里没有阿拉伯数字，直接喂 "10" 会被静默丢掉（日志里是 Ignore OOV '10'）
        audio = self._tts.generate(normalize_for_tts(text), sid=self.sid, speed=self.speed)
        pcm = np.asarray(audio.samples, dtype=np.float32)
        return pcm, audio.sample_rate

    # ---------------------------------------------------------------- 播放（句级流水）

    def _speaker_or_die(self) -> Speaker:
        if self._speaker is None:
            self._speaker = Speaker(self._output_device)  # 按名字匹配设备，留空则用系统默认
        return self._speaker

    def speak(self, text: str, blocking: bool = True) -> threading.Thread | None:
        text = (text or "").strip()
        if not text:
            return None
        self.stop()
        self._stop.clear()

        def _run() -> None:
            spk = self._speaker_or_die()
            sentences = split_for_speech(text, 60) or [text]
            produced: queue.Queue[np.ndarray | None] = queue.Queue()

            def _worker() -> None:
                for sent in sentences:
                    if self._stop.is_set():
                        break
                    try:
                        pcm, _ = self.synthesize(sent)
                    except Exception as e:  # noqa: BLE001
                        print(f"  ⚠️  合成失败：{e}", file=sys.stderr)
                        continue
                    produced.put(pcm)
                produced.put(None)

            th = threading.Thread(target=_worker, daemon=True)
            th.start()
            while not self._stop.is_set():
                pcm = produced.get()
                if pcm is None:
                    break
                spk.play(pcm, self.sample_rate, blocking=True)
            th.join(timeout=10)

        if blocking:
            _run()
            return None
        t = threading.Thread(target=_run, daemon=True)
        self._thread = t
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()
        if self._speaker is not None:
            self._speaker.stop()

    # ---------------------------------------------------------------- 与 SayTTS 对齐的杂项

    # 缓存键必须是 (文本, 语速)：改了 TTS_SPEED 之后不能还播旧速度的音频
    _cache: dict[tuple[str, float], tuple[np.ndarray, int]] = {}

    def _key(self, text: str) -> tuple[str, float]:
        return (text, self.speed)

    def warm(self, phrases: list[str]) -> int:
        n = 0
        for p in phrases:
            if p and self._key(p) not in self._cache:
                try:
                    self._cache[self._key(p)] = self.synthesize(p)
                    n += 1
                except Exception:  # noqa: BLE001
                    pass
        return n

    def play_cached(self, text: str, blocking: bool = False) -> float:
        t0 = time.perf_counter()
        hit = self._cache.get(self._key(text))
        pcm, rate = hit if hit else self.synthesize(text)
        self._cache[self._key(text)] = (pcm, rate)
        ms = (time.perf_counter() - t0) * 1000
        self._speaker_or_die().play(pcm, rate, blocking=blocking)
        return ms

    def wait_idle(self, timeout: float = 2.0) -> None:
        sp = self._speaker
        if sp is None:
            return
        t0 = time.perf_counter()
        while sp.playing and time.perf_counter() - t0 < timeout:
            time.sleep(0.02)

    @property
    def speaking(self) -> bool:
        return self._speaker is not None and self._speaker.playing


def _main() -> int:
    """试听：python -m jarvis.tts_neural "文本" --speed 1.4"""
    import argparse

    from .config import Settings as _S

    ap = argparse.ArgumentParser(prog="jarvis.tts_neural", description="本地神经 TTS 试听")
    ap.add_argument("text", nargs="?", default="好的，客厅的灯已经打开了。要不要把亮度也调低一点？")
    ap.add_argument("--speed", type=float, default=None, help="语速倍数，1.0 原始，1.3~1.5 更接近人说话")
    ap.add_argument("--model", default=None)
    ap.add_argument("--save", default="", help="另存为 wav 路径")
    args = ap.parse_args()

    st = _S.load()
    model = args.model or st.tts_model_dir
    speed = args.speed if args.speed is not None else st.tts_speed
    tts = SherpaTTS(
        model, num_threads=st.tts_threads, sid=st.tts_sid, speed=speed,
        output_device=st.output_device,
    )
    t0 = time.perf_counter()
    pcm, rate = tts.synthesize(args.text)
    ms = (time.perf_counter() - t0) * 1000
    print(
        f"模型 {model} · 语速 {speed} · 合成 {ms:.0f}ms · "
        f"音频 {pcm.size / rate:.2f}s（{len(args.text) / (pcm.size / rate):.1f} 字/秒）"
    )
    if args.save:
        import wave

        with wave.open(args.save, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes((np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes())
        print(f"已保存 {args.save}")
    tts.speak(args.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


def build_local_tts(settings: Settings, speaker: Speaker | None):
    """按 TTS_BACKEND 选本地合成器：say（默认，快但生硬）| neural（自然，稍慢）。"""
    backend = (settings.tts_backend or "say").lower()
    if backend in ("neural", "sherpa", "vits"):
        model_dir = Path(settings.tts_model_dir)
        if not model_dir.exists() or not any(model_dir.glob("*.onnx")):
            print(
                f"⚠️  找不到神经 TTS 模型 {model_dir}，暂时回落到 macOS say"
                f"（下载：./scripts/fetch_models.sh tts）",
                file=sys.stderr,
            )
            from .tts import SayTTS

            return SayTTS(settings.tts_voice_say, settings.tts_mode, speaker, rate=settings.say_rate)
        return SherpaTTS(
            settings.tts_model_dir,
            num_threads=settings.tts_threads,
            sid=settings.tts_sid,
            speed=settings.tts_speed,
            speaker=speaker,
            output_device=settings.output_device,
        )
    from .tts import SayTTS

    return SayTTS(settings.tts_voice_say, settings.tts_mode, speaker, rate=settings.say_rate)
