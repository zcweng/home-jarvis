"""MOSS-TTS-Nano 侧车客户端：接口与 SherpaTTS / SayTTS 对齐。"""

from __future__ import annotations

import queue
import sys
import threading
import time
from pathlib import Path

import httpx
import numpy as np

from .audio import Speaker
from .output import split_for_speech
from .textnorm import clean_for_speech


def probe_nano(base_url: str, timeout: float = 0.4) -> bool:
    url = base_url.rstrip("/") + "/health"
    try:
        r = httpx.get(url, timeout=timeout)
        if r.status_code != 200:
            return False
        data = r.json()
        return bool(data.get("ready"))
    except Exception:  # noqa: BLE001
        return False


class NanoTTS:
    """HTTP 客户端 → tools/nano-tts 侧车。"""

    def __init__(
        self,
        base_url: str,
        ref: str = "owner",
        *,
        stream: bool = False,
        timeout: float = 8.0,
        speed: float = 1.0,
        speaker: Speaker | None = None,
        fallback=None,
        output_device: str = "",
        cool_down_s: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.ref = ref
        self.stream = stream
        self.speed = speed
        self.timeout = timeout
        self.fallback = fallback
        self._output_device = output_device
        self._speaker = speaker
        self._client = httpx.Client(timeout=timeout)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cool_until = 0.0
        self._cool_down_s = cool_down_s
        self.sample_rate = 48000
        self.voice = f"nano({Path(ref).stem})"
        self.mode = "nano"
        self.load_ms = 0.0
        self._cache: dict[tuple[str, float], tuple[np.ndarray, int]] = {}

    def _speaker_or_die(self) -> Speaker:
        if self._speaker is None:
            self._speaker = Speaker(self._output_device)
        return self._speaker

    def _key(self, text: str) -> tuple[str, float]:
        return (text, self.speed)

    def _in_cooldown(self) -> bool:
        return time.time() < self._cool_until

    def _trip_cooldown(self) -> None:
        self._cool_until = time.time() + self._cool_down_s

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        text = clean_for_speech(text)
        if not text:
            return np.zeros((0,), dtype=np.float32), self.sample_rate

        if self._in_cooldown() and self.fallback is not None:
            return self.fallback.synthesize(text)

        payload = {
            "input": text,
            "ref_audio": Path(self.ref).stem,
            "response_format": "pcm",
            "stream": False,
            "speed": self.speed,
        }
        try:
            r = self._client.post(f"{self.base_url}/v1/audio/speech", json=payload)
            r.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠️  nano 不可用（{exc}），回落 {getattr(self.fallback, 'mode', '?')}", file=sys.stderr)
            self._trip_cooldown()
            if self.fallback is None:
                raise
            return self.fallback.synthesize(text)

        sr = int(r.headers.get("X-Sample-Rate") or self.sample_rate)
        pcm = np.frombuffer(r.content, dtype="<i2").astype(np.float32) / 32768.0
        self.sample_rate = sr
        return pcm, sr

    def speak(self, text: str, blocking: bool = True) -> threading.Thread | None:
        text = (text or "").strip()
        if not text:
            return None
        self.stop()
        self._stop.clear()

        def _run() -> None:
            # Nano 句间合成要 ~1s，再按句切会在中间留大空洞；整段一次合成更连贯。
            # 超长文本仍切开，避免侧车 max_new_frames 截断。
            spk = self._speaker_or_die()
            sentences = split_for_speech(text, 120) or [text]
            if len(text) <= 80:
                sentences = [text]
            produced: queue.Queue[tuple[np.ndarray, int] | None] = queue.Queue()

            def _worker() -> None:
                for sent in sentences:
                    if self._stop.is_set():
                        break
                    try:
                        pcm, rate = self.synthesize(sent)
                    except Exception as e:  # noqa: BLE001
                        print(f"  ⚠️  nano 合成失败：{e}", file=sys.stderr)
                        continue
                    produced.put((pcm, rate))
                produced.put(None)

            th = threading.Thread(target=_worker, daemon=True)
            th.start()
            while not self._stop.is_set():
                item = produced.get()
                if item is None:
                    break
                pcm, rate = item
                if pcm.size:
                    spk.play(pcm, rate, blocking=True)
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

    def _looks_runaway(self, text: str, pcm: np.ndarray, rate: int) -> bool:
        """短应答若被 Nano 幻出超长乱音，丢掉重合成 / 回落。"""
        if pcm.size == 0 or rate <= 0:
            return True
        dur = float(pcm.size) / float(rate)
        n = max(1, len((text or "").strip()))
        # 「我在」2 字正常 <1s；超过 0.45s/字 + 0.6s 视为失控
        return dur > max(1.2, n * 0.45 + 0.6)

    def warm(self, phrases: list[str]) -> int:
        n = 0
        for p in phrases:
            if p and self._key(p) not in self._cache:
                try:
                    pcm, rate = self.synthesize(p)
                    if self._looks_runaway(p, pcm, rate):
                        print(f"  ⚠️  nano 预渲染「{p}」过长 ({pcm.size/rate:.1f}s)，重试", file=sys.stderr)
                        pcm, rate = self.synthesize(p)
                    if self._looks_runaway(p, pcm, rate):
                        print(f"  ⚠️  nano 预缓存「{p}」仍异常，跳过缓存", file=sys.stderr)
                        continue
                    self._cache[self._key(p)] = (pcm, rate)
                    n += 1
                except Exception:  # noqa: BLE001
                    pass
        return n

    def play_cached(self, text: str, blocking: bool = False) -> float:
        t0 = time.perf_counter()
        hit = self._cache.get(self._key(text))
        if hit is not None and self._looks_runaway(text, hit[0], hit[1]):
            self._cache.pop(self._key(text), None)
            hit = None
        pcm, rate = hit if hit else self.synthesize(text)
        if self._looks_runaway(text, pcm, rate) and self.fallback is not None:
            print(f"  ⚠️  nano「{text}」过长，改用回落合成", file=sys.stderr)
            pcm, rate = self.fallback.synthesize(text)
        else:
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
