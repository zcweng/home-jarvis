#!/usr/bin/env python3
"""MOSS-TTS-Nano ONNX 侧车：OpenAI 风格 /v1/audio/speech + /health。

独立进程，串行推理。默认参考音官方 zh_1.wav。
可用环境变量 NANO_TTS_REF / --ref 覆盖（例如自录 refs/owner.wav）。
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterator

import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
NANO_ROOT = ROOT / "MOSS-TTS-Nano"
DEFAULT_MODEL_DIR = NANO_ROOT / "models"
DEFAULT_REF = NANO_ROOT / "assets" / "audio" / "zh_1.wav"
DEFAULT_OWNER_REF = ROOT / "refs" / "owner.wav"  # 可选自录覆盖

if str(NANO_ROOT) not in sys.path:
    sys.path.insert(0, str(NANO_ROOT))


def _to_mono(waveform: np.ndarray) -> np.ndarray:
    w = np.asarray(waveform, dtype=np.float32)
    if w.ndim == 1:
        return w
    return np.mean(w, axis=1).astype(np.float32, copy=False)


def _trim_speech(mono: np.ndarray, sample_rate: int, thresh: float = 0.02) -> np.ndarray:
    if mono.size == 0:
        return mono
    peak = float(np.max(np.abs(mono))) + 1e-12
    active = np.where(np.abs(mono) > thresh * peak)[0]
    if active.size == 0:
        return mono[:0]
    pad = int(0.05 * sample_rate)
    start = max(0, int(active[0]) - pad)
    end = min(mono.size, int(active[-1]) + 1 + pad)
    return mono[start:end]


def _collapse_internal_silence(
    mono: np.ndarray,
    sample_rate: int,
    *,
    thresh: float = 0.03,
    max_gap_s: float = 0.12,
    hop_s: float = 0.01,
) -> np.ndarray:
    """压缩句中超长静音。Nano 常在字间插入 0.5~1s 空洞，听感像「蹦词」。"""
    if mono.size == 0:
        return mono
    peak = float(np.max(np.abs(mono))) + 1e-12
    hop = max(1, int(hop_s * sample_rate))
    max_gap = max(1, int(max_gap_s * sample_rate))
    n = mono.size // hop
    if n == 0:
        return mono
    energy = np.abs(mono[: n * hop]).reshape(n, hop).mean(axis=1)
    silent = energy < thresh * peak
    pieces: list[np.ndarray] = []
    i = 0
    while i < n:
        if silent[i]:
            j = i
            while j < n and silent[j]:
                j += 1
            gap = mono[i * hop : j * hop]
            if gap.size > max_gap:
                gap = gap[:max_gap]
            if gap.size:
                pieces.append(gap)
            i = j
        else:
            j = i
            while j < n and not silent[j]:
                j += 1
            pieces.append(mono[i * hop : j * hop])
            i = j
    tail = mono[n * hop :]
    if tail.size:
        pieces.append(tail)
    return np.concatenate(pieces) if pieces else mono


def _float_to_pcm16(mono: np.ndarray) -> bytes:
    clipped = np.clip(mono, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


def _estimate_max_seconds(text: str) -> float:
    """短句防幻觉：Nano 对「我在」这类词偶发拖出 10s+ 乱音。"""
    t = (text or "").strip()
    if not t:
        return 1.0
    # 中文约 0.28s/字，英文按字母 0.07s；加 0.45s 余量，下限 0.8s
    cjk = sum(1 for ch in t if "\u4e00" <= ch <= "\u9fff")
    latin = sum(1 for ch in t if ch.isalpha() and not ("\u4e00" <= ch <= "\u9fff"))
    other = max(0, len(t) - cjk - latin)
    sec = cjk * 0.28 + latin * 0.07 + other * 0.05 + 0.45
    return float(min(12.0, max(0.8, sec)))


def _frame_budget(text: str, hard_cap: int) -> int:
    # ≈12.5 frame/s；按预估时长换算再留 30% 余量
    frames = int(_estimate_max_seconds(text) * 12.5 * 1.3)
    return int(min(hard_cap, max(24, frames)))


def _hard_cap_duration(mono: np.ndarray, sample_rate: int, text: str) -> np.ndarray:
    if mono.size == 0:
        return mono
    max_n = int(_estimate_max_seconds(text) * sample_rate)
    if mono.size <= max_n:
        return mono
    # 在上限附近找低能量点切断，避免咔哒一声
    search = mono[max(0, max_n - sample_rate // 10) : max_n]
    if search.size:
        rel = np.abs(search)
        cut = int(np.argmin(rel)) + max(0, max_n - sample_rate // 10)
        return mono[: max(1, cut)]
    return mono[:max_n]


def _apply_speed(mono: np.ndarray, sample_rate: int, speed: float) -> tuple[np.ndarray, int]:
    if abs(speed - 1.0) < 1e-3:
        return mono, sample_rate
    # 简单时域变速：重采样到 speed*sr 再标回原 sr（音调略变，阶段 1 够用）
    import torch
    import torchaudio

    t = torch.from_numpy(mono).unsqueeze(0)
    stretched = torchaudio.functional.resample(t, sample_rate, max(1, int(round(sample_rate / speed))))
    out = torchaudio.functional.resample(stretched, max(1, int(round(sample_rate / speed))), sample_rate)
    return out.squeeze(0).numpy().astype(np.float32), sample_rate


class SpeechRequest(BaseModel):
    input: str = Field(..., min_length=1)
    ref_audio: str | None = None  # "owner" | 绝对/相对路径 | base64 data URL（阶段 1 只用缓存 id/路径）
    response_format: str = "pcm"
    stream: bool = False
    speed: float = 1.0


class Engine:
    def __init__(
        self,
        *,
        model_dir: Path,
        ref_path: Path,
        cpu_threads: int,
        sample_mode: str,
        max_new_frames: int,
    ) -> None:
        from onnx_tts_runtime import OnnxTtsRuntime

        self.lock = threading.Lock()
        self.ref_path = ref_path.resolve()
        self.voice_id = self.ref_path.stem
        self.sample_mode = sample_mode
        self.max_new_frames = max_new_frames
        self.last_rtf: float | None = None
        self.ready = False
        self.load_ms = 0.0
        self.encode_ms = 0.0

        t0 = time.perf_counter()
        self.runtime = OnnxTtsRuntime(
            model_dir=str(model_dir),
            thread_count=cpu_threads,
            max_new_frames=max_new_frames,
            do_sample=sample_mode != "greedy",
            sample_mode=sample_mode,
            execution_provider="cpu",
        )
        self.load_ms = (time.perf_counter() - t0) * 1000

        t1 = time.perf_counter()
        self.prompt_codes = self.runtime.encode_reference_audio(self.ref_path)
        self.encode_ms = (time.perf_counter() - t1) * 1000
        self.sample_rate = int(self.runtime.codec_meta["codec_config"]["sample_rate"])
        self.ready = True

    def synthesize(self, text: str, *, speed: float = 1.0) -> tuple[np.ndarray, int, dict[str, Any]]:
        prepared = self.runtime.prepare_synthesis_text(
            text=text,
            voice=self.voice_id,
            enable_wetext=False,
            enable_normalize_tts_text=True,
        )
        prepared_text = str(prepared["text"])
        chunks = self.runtime.split_voice_clone_text(prepared_text, max_tokens=75)
        if not chunks:
            chunks = [prepared_text]

        t0 = time.perf_counter()
        parts: list[np.ndarray] = []
        total_frames = 0
        with self.lock:
            defaults = self.runtime.manifest["generation_defaults"]
            saved_max = int(defaults.get("max_new_frames", self.max_new_frames))
            try:
                for i, chunk in enumerate(chunks):
                    budget = _frame_budget(chunk, self.max_new_frames)
                    defaults["max_new_frames"] = budget
                    result = self.runtime.synthesize_single_chunk(
                        text=chunk,
                        prompt_audio_codes=self.prompt_codes,
                        streaming=True,
                    )
                    wave = np.asarray(result["waveform"], dtype=np.float32)
                    parts.append(wave)
                    total_frames += len(result["generated_frames"])
                    if i < len(chunks) - 1:
                        pause = self.runtime.estimate_voice_clone_inter_chunk_pause_seconds(chunk)
                        n = max(0, int(round(self.sample_rate * pause)))
                        if n:
                            ch = 1 if wave.ndim == 1 else wave.shape[1]
                            parts.append(np.zeros((n, ch) if ch > 1 else (n,), dtype=np.float32))
            finally:
                defaults["max_new_frames"] = saved_max
        elapsed = time.perf_counter() - t0
        if not parts:
            mono = np.zeros((0,), dtype=np.float32)
        else:
            # 统一到 2D 再拼
            normed = []
            for p in parts:
                if p.ndim == 1:
                    normed.append(p[:, None])
                else:
                    normed.append(p)
            stereo_or_mono = np.concatenate(normed, axis=0)
            # 双声道几乎同相，取左声道即可
            mono = stereo_or_mono[:, 0]
        mono = _trim_speech(mono, self.sample_rate)
        mono = _collapse_internal_silence(mono, self.sample_rate)
        mono = _hard_cap_duration(mono, self.sample_rate, prepared_text)
        mono, sr = _apply_speed(mono, self.sample_rate, speed)
        speech_dur = float(mono.size / sr) if mono.size else 0.0
        rtf = (elapsed / speech_dur) if speech_dur > 0.05 else None
        self.last_rtf = rtf
        meta = {
            "elapsed_ms": round(elapsed * 1000, 1),
            "speech_dur_s": round(speech_dur, 3),
            "rtf": None if rtf is None else round(rtf, 3),
            "frames": total_frames,
            "chunks": len(chunks),
        }
        return mono, sr, meta


def _resolve_ref(cli_ref: str | None) -> Path:
    env_ref = (os.environ.get("NANO_TTS_REF") or "").strip()
    raw = (cli_ref or env_ref or str(DEFAULT_REF)).strip()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        # 相对路径：先按 CWD，再按 tools/nano-tts、仓库根试
        candidates = [path, ROOT / path, ROOT.parent.parent / path]
        path = next((c.resolve() for c in candidates if c.exists()), (ROOT / path).resolve())
    else:
        path = path.resolve()
    if not path.is_file():
        print(
            f"❌ 参考音不存在：{path}\n"
            f"   默认应为：{DEFAULT_REF}\n"
            f"   自录可：NANO_TTS_REF={DEFAULT_OWNER_REF} ./scripts/nano_tts_server.sh start",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return path


def build_app(engine: Engine) -> FastAPI:
    app = FastAPI(title="nano-tts sidecar", version="0.1.0")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "ready": engine.ready,
            "voice": engine.voice_id,
            "ref": str(engine.ref_path),
            "sample_rate": engine.sample_rate,
            "sample_mode": engine.sample_mode,
            "rtf": engine.last_rtf,
            "load_ms": round(engine.load_ms, 1),
            "encode_ms": round(engine.encode_ms, 1),
        }

    @app.post("/v1/audio/speech")
    def speech(req: SpeechRequest) -> Response:
        text = (req.input or "").strip()
        if not text:
            raise HTTPException(400, "input is empty")
        if req.response_format not in ("pcm", "wav"):
            raise HTTPException(400, "only pcm|wav supported")
        # 阶段 1：忽略请求里临时换 ref（避免每句重编码）；换音色请重启侧车
        try:
            mono, sr, meta = engine.synthesize(text, speed=float(req.speed or 1.0))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"synthesize failed: {exc}") from exc
        if mono.size == 0:
            raise HTTPException(500, "empty audio")

        pcm = _float_to_pcm16(mono)
        headers = {
            "X-Sample-Rate": str(sr),
            "X-Channels": "1",
            "X-Elapsed-Ms": str(meta["elapsed_ms"]),
            "X-Speech-Dur": str(meta["speech_dur_s"]),
            "X-RTF": str(meta["rtf"] if meta["rtf"] is not None else ""),
            "X-Voice": engine.voice_id,
        }

        if req.stream:
            # 简单 chunked：按 ~100ms 切片吐出（客户端可边收边播；阶段 2 先整段收）
            frame_bytes = int(sr * 0.1) * 2

            def gen() -> Iterator[bytes]:
                for i in range(0, len(pcm), frame_bytes):
                    yield pcm[i : i + frame_bytes]

            return StreamingResponse(gen(), media_type="application/octet-stream", headers=headers)

        if req.response_format == "wav":
            import io
            import wave

            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(sr)
                w.writeframes(pcm)
            return Response(buf.getvalue(), media_type="audio/wav", headers=headers)

        return Response(pcm, media_type="application/octet-stream", headers=headers)

    return app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="nano-tts sidecar")
    ap.add_argument("--host", default=os.environ.get("NANO_TTS_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("NANO_TTS_PORT", "8091")))
    ap.add_argument("--model-dir", type=Path, default=Path(os.environ.get("NANO_TTS_MODEL_DIR", str(DEFAULT_MODEL_DIR))))
    ap.add_argument("--ref", default=None, help="参考 wav；默认官方 zh_1.wav；可用 NANO_TTS_REF")
    ap.add_argument("--threads", type=int, default=int(os.environ.get("NANO_TTS_THREADS", "4")))
    ap.add_argument("--sample-mode", default=os.environ.get("NANO_TTS_SAMPLE_MODE", "fixed"), choices=("fixed", "greedy", "full"))
    ap.add_argument("--max-new-frames", type=int, default=int(os.environ.get("NANO_TTS_MAX_NEW_FRAMES", "200")))
    args = ap.parse_args(argv)

    ref = _resolve_ref(args.ref)
    print(f"nano-tts loading model_dir={args.model_dir} ref={ref} mode={args.sample_mode} threads={args.threads}")
    engine = Engine(
        model_dir=args.model_dir,
        ref_path=ref,
        cpu_threads=args.threads,
        sample_mode=args.sample_mode,
        max_new_frames=args.max_new_frames,
    )
    print(
        f"nano-tts ready voice={engine.voice_id} load={engine.load_ms:.0f}ms "
        f"encode={engine.encode_ms:.0f}ms sr={engine.sample_rate} → http://{args.host}:{args.port}"
    )
    app = build_app(engine)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
