"""千问AI平台 Realtime TTS（CosyVoice Inference WebSocket）。

协议：run-task → task-started → continue-task → finish-task → task-finished
地址：wss://maas.qianwenaiapi.com/api-ws/v1/inference
鉴权：Authorization: Bearer $DASHSCOPE_API_KEY
"""

from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from typing import Any, Callable

import numpy as np

from .audio import Speaker
from .output import split_for_speech
from .textnorm import clean_for_speech

try:
    import websocket
except ImportError:  # pragma: no cover
    websocket = None  # type: ignore[assignment]


class QwenRealtimeTTS:
    """千问 CosyVoice Realtime WebSocket 客户端，接口对齐 SherpaTTS。"""

    def __init__(
        self,
        api_key: str,
        *,
        ws_url: str = "wss://maas.qianwenaiapi.com/api-ws/v1/inference",
        model: str = "cosyvoice-v3-flash",
        voice: str = "longanyang",
        sample_rate: int = 24000,
        timeout: float = 8.0,
        speed: float = 1.0,
        speaker: Speaker | None = None,
        fallback: Any = None,
        output_device: str = "",
        cool_down_s: float = 30.0,
    ) -> None:
        if websocket is None:
            raise SystemExit("缺少 websocket-client：uv add websocket-client")
        self.api_key = api_key
        self.ws_url = ws_url
        self.model = model
        self.voice_name = voice
        self.sample_rate = int(sample_rate)
        self.timeout = float(timeout)
        self.speed = float(speed)
        self.fallback = fallback
        self._output_device = output_device
        self._speaker = speaker
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cool_until = 0.0
        self._cool_down_s = cool_down_s
        self._ws_lock = threading.Lock()
        self._active_ws: Any = None
        self.voice = f"qwen({voice})"
        self.mode = "qwen"
        self.load_ms = 0.0
        self._cache: dict[tuple[str, str, float], tuple[np.ndarray, int]] = {}

    def _speaker_or_die(self) -> Speaker:
        if self._speaker is None:
            self._speaker = Speaker(self._output_device)
        return self._speaker

    def _key(self, text: str) -> tuple[str, str, float]:
        return (text, self.voice_name, self.speed)

    def _in_cooldown(self) -> bool:
        return time.time() < self._cool_until

    def _trip_cooldown(self) -> None:
        self._cool_until = time.time() + self._cool_down_s

    def _pcm_bytes_to_float(self, data: bytes) -> np.ndarray:
        if not data:
            return np.zeros((0,), dtype=np.float32)
        # 丢弃奇数尾字节，避免半个 sample
        n = len(data) - (len(data) % 2)
        if n <= 0:
            return np.zeros((0,), dtype=np.float32)
        return np.frombuffer(data[:n], dtype="<i2").astype(np.float32) / 32768.0

    def _run_task_payload(self, task_id: str) -> dict[str, Any]:
        return {
            "header": {
                "action": "run-task",
                "task_id": task_id,
                "streaming": "duplex",
            },
            "payload": {
                "task_group": "audio",
                "task": "tts",
                "function": "SpeechSynthesizer",
                "model": self.model,
                "parameters": {
                    "text_type": "PlainText",
                    "voice": self.voice_name,
                    "format": "pcm",
                    "sample_rate": self.sample_rate,
                    "volume": 50,
                    "rate": self.speed,
                    "pitch": 1.0,
                    "enable_ssml": False,
                },
                "input": {},
            },
        }

    def _continue_payload(self, task_id: str, text: str) -> dict[str, Any]:
        return {
            "header": {
                "action": "continue-task",
                "task_id": task_id,
                "streaming": "duplex",
            },
            "payload": {"input": {"text": text}},
        }

    def _finish_payload(self, task_id: str) -> dict[str, Any]:
        return {
            "header": {
                "action": "finish-task",
                "task_id": task_id,
                "streaming": "duplex",
            },
            "payload": {"input": {}},
        }

    def _synthesize_once(
        self,
        texts: list[str],
        *,
        on_chunk: Callable[[np.ndarray], None] | None = None,
    ) -> tuple[np.ndarray, int]:
        """完成一轮 run/continue/finish；收齐 binary 后再拼成连续 PCM。

        注意：不要把每个 WS 小包直接喂 Speaker.play——play 每次会 stop/重建流，
        听感必然卡顿。on_chunk 仅用于进度/调试，正式播报走整段 play。
        """
        texts = [t for t in (clean_for_speech(x) for x in texts) if t]
        if not texts:
            return np.zeros((0,), dtype=np.float32), self.sample_rate

        task_id = str(uuid.uuid4())
        chunks: list[bytes] = []
        err: list[str] = []
        started = threading.Event()
        finished = threading.Event()
        sent_text = threading.Event()

        def _send_json(ws: Any, payload: dict[str, Any]) -> None:
            ws.send(json.dumps(payload, ensure_ascii=False))

        def on_open(ws: Any) -> None:
            _send_json(ws, self._run_task_payload(task_id))

        def on_message(ws: Any, message: Any) -> None:
            if self._stop.is_set():
                try:
                    ws.close()
                except Exception:  # noqa: BLE001
                    pass
                finished.set()
                return
            if isinstance(message, (bytes, bytearray)):
                raw = bytes(message)
                chunks.append(raw)
                if on_chunk is not None:
                    pcm = self._pcm_bytes_to_float(raw)
                    if pcm.size:
                        on_chunk(pcm)
                return
            try:
                msg = json.loads(message)
            except Exception:  # noqa: BLE001
                return
            header = msg.get("header") or {}
            event = header.get("event") or ""
            if event == "task-started":
                started.set()
                if not sent_text.is_set():
                    for text in texts:
                        if self._stop.is_set():
                            break
                        _send_json(ws, self._continue_payload(task_id, text))
                    _send_json(ws, self._finish_payload(task_id))
                    sent_text.set()
            elif event == "task-finished":
                finished.set()
                try:
                    ws.close()
                except Exception:  # noqa: BLE001
                    pass
            elif event == "task-failed":
                err.append(
                    header.get("error_message")
                    or header.get("status_text")
                    or msg.get("payload", {}).get("message")
                    or "task-failed"
                )
                finished.set()
                try:
                    ws.close()
                except Exception:  # noqa: BLE001
                    pass

        def on_error(_ws: Any, error: Any) -> None:
            err.append(str(error) or "websocket error")
            finished.set()

        def on_close(_ws: Any, *_args: Any) -> None:
            finished.set()

        header = {
            "Authorization": f"Bearer {self.api_key}",
        }
        ws_app = websocket.WebSocketApp(
            self.ws_url,
            header=header,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )

        with self._ws_lock:
            self._active_ws = ws_app

        def _run() -> None:
            ws_app.run_forever(ping_interval=20, ping_timeout=10)

        th = threading.Thread(target=_run, name="qwen-tts-ws", daemon=True)
        th.start()
        ok = finished.wait(timeout=max(self.timeout, 2.0) + max(2.0, sum(len(t) for t in texts) * 0.08))
        try:
            ws_app.close()
        except Exception:  # noqa: BLE001
            pass
        th.join(timeout=2.0)
        with self._ws_lock:
            if self._active_ws is ws_app:
                self._active_ws = None

        if err:
            raise RuntimeError(err[0])
        if not ok and not chunks:
            raise TimeoutError(f"千问 TTS 超时（{self.timeout}s）")
        if not started.is_set() and not chunks:
            raise RuntimeError("千问 TTS 未收到 task-started")

        pcm = self._pcm_bytes_to_float(b"".join(chunks))
        return pcm, self.sample_rate

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        text = clean_for_speech(text)
        if not text:
            return np.zeros((0,), dtype=np.float32), self.sample_rate
        if self._in_cooldown() and self.fallback is not None:
            return self.fallback.synthesize(text)
        sentences = split_for_speech(text, 80) or [text]
        if len(text) <= 60:
            sentences = [text]
        try:
            return self._synthesize_once(sentences)
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠️  qwen 不可用（{exc}），回落 {getattr(self.fallback, 'mode', '?')}", file=sys.stderr)
            self._trip_cooldown()
            if self.fallback is None:
                raise
            return self.fallback.synthesize(text)

    def speak(self, text: str, blocking: bool = True) -> threading.Thread | None:
        text = (text or "").strip()
        if not text:
            return None
        self.stop()
        self._stop.clear()

        def _run() -> None:
            spk = self._speaker_or_die()
            if self._in_cooldown() and self.fallback is not None:
                self.fallback.speak(text, blocking=True)
                return
            try:
                # 必须整段合成后再一次 play：Speaker/EndpointSpeaker 每次 play 都会
                # 重建输出流，按 WS 小包边收边播会卡成一顿一顿。
                pcm, rate = self.synthesize(text)
            except Exception as exc:  # noqa: BLE001
                print(f"  ⚠️  qwen 合成失败：{exc}", file=sys.stderr)
                return
            if self._stop.is_set() or pcm.size == 0:
                return
            self.sample_rate = rate
            spk.play(pcm, rate, blocking=True)

        if blocking:
            _run()
            return None
        t = threading.Thread(target=_run, daemon=True)
        self._thread = t
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()
        with self._ws_lock:
            ws = self._active_ws
            self._active_ws = None
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._speaker is not None:
            self._speaker.stop()

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
    import argparse

    from .config import Settings

    ap = argparse.ArgumentParser(prog="jarvis.tts_qwen", description="千问 Realtime TTS 试听")
    ap.add_argument("text", nargs="?", default="你好，我是贾维斯。")
    ap.add_argument("--save", default="")
    args = ap.parse_args()
    st = Settings.load()
    if not st.dashscope_api_key:
        print("缺少 DASHSCOPE_API_KEY", file=sys.stderr)
        return 1
    tts = QwenRealtimeTTS(
        st.dashscope_api_key,
        ws_url=st.tts_qwen_ws_url,
        model=st.tts_qwen_model,
        voice=st.tts_qwen_voice,
        sample_rate=st.tts_qwen_sample_rate,
        timeout=st.tts_qwen_timeout,
        speed=st.tts_qwen_speed,
        output_device=st.output_device,
    )
    t0 = time.perf_counter()
    pcm, rate = tts.synthesize(args.text)
    print(f"音色 {tts.voice} · 合成 {(time.perf_counter()-t0)*1000:.0f}ms · {pcm.size/max(rate,1):.2f}s")
    if args.save:
        import wave

        with wave.open(args.save, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes((np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes())
        print(f"已保存 {args.save}")
    else:
        tts.speak(args.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
