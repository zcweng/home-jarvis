#!/usr/bin/env python3
"""阶段 0：本机实测 MOSS-TTS-Nano ONNX 的加载 / 首音 / RTF / 内存。

独立于主 venv：默认用 tools/nano-tts/.venv。
用法：
  tools/nano-tts/.venv/bin/python scripts/bench_nano_tts.py
  tools/nano-tts/.venv/bin/python scripts/bench_nano_tts.py --sample-mode greedy
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
NANO_ROOT = ROOT / "tools" / "nano-tts" / "MOSS-TTS-Nano"
DEFAULT_MODEL_DIR = NANO_ROOT / "models"
DEFAULT_REF = NANO_ROOT / "assets" / "audio" / "zh_1.wav"
DEFAULT_OUT = ROOT / "tools" / "nano-tts" / "bench_out"

CASES = [
    ("zh_13", "今天天气怎么样，开一下客厅灯。"),  # ~13 字
    ("mix", "老虎英文是 tiger，USB 插在左边。"),
    ("en", "Hello, my name is Jarvis."),
    ("zh_short", "你好，我是贾维斯。"),
    ("zh_long", "请把卧室空调调到二十六度，然后关闭阳台的灯，谢谢。"),
]


def _rss_mb() -> float:
    # macOS ru_maxrss is bytes
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)


def _speech_duration(waveform: np.ndarray, sample_rate: int, thresh: float = 0.02) -> float:
    if waveform.ndim > 1:
        mono = np.mean(np.abs(waveform), axis=1)
    else:
        mono = np.abs(waveform)
    peak = float(mono.max()) + 1e-12
    active = np.where(mono > thresh * peak)[0]
    if len(active) == 0:
        return 0.0
    return float(active[-1] - active[0] + 1) / float(sample_rate)


def _trim_speech(waveform: np.ndarray, sample_rate: int, thresh: float = 0.02) -> np.ndarray:
    if waveform.ndim > 1:
        mono = np.mean(np.abs(waveform), axis=1)
    else:
        mono = np.abs(waveform)
    peak = float(mono.max()) + 1e-12
    active = np.where(mono > thresh * peak)[0]
    if len(active) == 0:
        return waveform[:0]
    start, end = int(active[0]), int(active[-1]) + 1
    pad = int(0.05 * sample_rate)
    start = max(0, start - pad)
    end = min(len(mono), end + pad)
    return waveform[start:end]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    ap.add_argument("--ref", type=Path, default=DEFAULT_REF)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--cpu-threads", type=int, default=4)
    ap.add_argument("--sample-mode", choices=("greedy", "fixed", "full"), default="fixed")
    ap.add_argument("--max-new-frames", type=int, default=200)
    ap.add_argument("--warmup", type=int, default=1)
    ap.add_argument("--repeats", type=int, default=3)
    args = ap.parse_args()

    if str(NANO_ROOT) not in sys.path:
        sys.path.insert(0, str(NANO_ROOT))

    from onnx_tts_runtime import OnnxTtsRuntime  # noqa: WPS433
    import soundfile as sf  # noqa: WPS433

    args.out_dir.mkdir(parents=True, exist_ok=True)
    if not args.ref.is_file():
        print(f"❌ 参考音不存在: {args.ref}", file=sys.stderr)
        return 2
    if not (args.model_dir / "MOSS-TTS-Nano-100M-ONNX").exists():
        print(f"❌ 模型目录不完整: {args.model_dir}", file=sys.stderr)
        return 2

    print(f"model_dir={args.model_dir}")
    print(f"ref={args.ref}")
    print(f"sample_mode={args.sample_mode} threads={args.cpu_threads} max_new_frames={args.max_new_frames}")
    print(f"rss_before_load={_rss_mb():.1f} MB")

    t0 = time.perf_counter()
    runtime = OnnxTtsRuntime(
        model_dir=str(args.model_dir),
        thread_count=args.cpu_threads,
        max_new_frames=args.max_new_frames,
        do_sample=args.sample_mode != "greedy",
        sample_mode=args.sample_mode,
        execution_provider="cpu",
    )
    load_s = time.perf_counter() - t0
    print(f"model_load={load_s*1000:.0f} ms  rss_after_load={_rss_mb():.1f} MB")

    t1 = time.perf_counter()
    prompt_codes = runtime.encode_reference_audio(args.ref)
    encode_s = time.perf_counter() - t1
    print(f"ref_encode={encode_s*1000:.0f} ms  prompt_frames={len(prompt_codes)}  rss={_rss_mb():.1f} MB")

    # Patch streaming decode to capture first-audio wall clock relative to synth start.
    first_audio_holder: dict[str, float | None] = {"t0": None, "first": None}
    orig_run = runtime.codec_streaming_session.run_frames

    def run_frames_timed(frames):  # type: ignore[no-untyped-def]
        out = orig_run(frames)
        if out is not None and first_audio_holder["first"] is None and first_audio_holder["t0"] is not None:
            audio, audio_length = out
            if audio_length > 0:
                first_audio_holder["first"] = time.perf_counter()
        return out

    runtime.codec_streaming_session.run_frames = run_frames_timed  # type: ignore[method-assign]

    results: list[dict] = []

    def one(name: str, text: str, *, save: bool) -> dict:
        first_audio_holder["t0"] = time.perf_counter()
        first_audio_holder["first"] = None
        t_start = first_audio_holder["t0"]
        assert t_start is not None
        chunk = runtime.synthesize_single_chunk(
            text=text,
            prompt_audio_codes=prompt_codes,
            streaming=True,
        )
        elapsed = time.perf_counter() - t_start
        waveform = np.asarray(chunk["waveform"], dtype=np.float32)
        sr = int(runtime.codec_meta["codec_config"]["sample_rate"])
        raw_dur = float(len(waveform) / sr) if len(waveform) else 0.0
        speech_dur = _speech_duration(waveform, sr)
        first_ms = None
        if first_audio_holder["first"] is not None:
            first_ms = (float(first_audio_holder["first"]) - t_start) * 1000.0
        rtf_raw = (elapsed / raw_dur) if raw_dur > 0 else None
        rtf_speech = (elapsed / speech_dur) if speech_dur > 0.05 else None
        if save:
            trimmed = _trim_speech(waveform, sr)
            out = args.out_dir / f"bench_{args.sample_mode}_{name}.wav"
            sf.write(out, trimmed if len(trimmed) else waveform, sr)
        return {
            "name": name,
            "text": text,
            "elapsed_ms": round(elapsed * 1000, 1),
            "first_audio_ms": None if first_ms is None else round(first_ms, 1),
            "raw_dur_s": round(raw_dur, 3),
            "speech_dur_s": round(speech_dur, 3),
            "rtf_raw": None if rtf_raw is None else round(rtf_raw, 3),
            "rtf_speech": None if rtf_speech is None else round(rtf_speech, 3),
            "frames": int(len(chunk["generated_frames"])),
        }

    # warmup
    for i in range(max(0, args.warmup)):
        one("warmup", CASES[0][1], save=False)
        print(f"warmup[{i}] done")

    print("---")
    for name, text in CASES:
        runs = []
        for r in range(args.repeats):
            row = one(name, text, save=(r == args.repeats - 1))
            runs.append(row)
            print(
                f"{name}[{r}] first={row['first_audio_ms']}ms "
                f"elapsed={row['elapsed_ms']}ms speech={row['speech_dur_s']}s "
                f"rtf_speech={row['rtf_speech']} frames={row['frames']}"
            )
        # median by first_audio
        firsts = [x["first_audio_ms"] for x in runs if x["first_audio_ms"] is not None]
        rtfs = [x["rtf_speech"] for x in runs if x["rtf_speech"] is not None]
        elapseds = [x["elapsed_ms"] for x in runs]
        summary = {
            "name": name,
            "text": text,
            "first_audio_ms_median": None if not firsts else round(float(np.median(firsts)), 1),
            "elapsed_ms_median": round(float(np.median(elapseds)), 1),
            "rtf_speech_median": None if not rtfs else round(float(np.median(rtfs)), 3),
            "speech_dur_s_last": runs[-1]["speech_dur_s"],
            "runs": runs,
        }
        results.append(summary)

    report = {
        "host": os.uname().machine,
        "sample_mode": args.sample_mode,
        "cpu_threads": args.cpu_threads,
        "model_load_ms": round(load_s * 1000, 1),
        "ref_encode_ms": round(encode_s * 1000, 1),
        "rss_mb": round(_rss_mb(), 1),
        "cases": results,
    }
    out_json = args.out_dir / f"bench_{args.sample_mode}.json"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("---")
    print(json.dumps({k: report[k] for k in ("sample_mode", "model_load_ms", "ref_encode_ms", "rss_mb")}, indent=2))
    for c in results:
        print(
            f"SUMMARY {c['name']}: first_med={c['first_audio_ms_median']}ms "
            f"elapsed_med={c['elapsed_ms_median']}ms rtf_med={c['rtf_speech_median']} "
            f"speech={c['speech_dur_s_last']}s"
        )
    print(f"wrote {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
