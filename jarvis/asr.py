"""语音识别：sherpa-onnx + SenseVoice。

为什么是 SenseVoice：
- 中文/粤语强，自带标点，`use_itn` 后数字/单位正常
- **不依赖 torch**（纯 onnxruntime），M4 上 3 秒语音约 200~400ms
- 模型 228MB，可用 `scripts/fetch_models.sh` 拉到 models/sensevoice

命令行自测：
    python -m jarvis.asr debug/rec.wav       # 转写一个 wav
    python -m jarvis.asr --mic 3             # 录 3 秒再转写
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

from .audio import TARGET_RATE, Mic, read_wav
from .config import Settings


class SenseVoice:
    """离线整句识别器（非流式：等 VAD 断句后一次性解码）。"""

    def __init__(
        self,
        model_dir: str | Path,
        num_threads: int = 4,
        language: str = "zh",
        use_itn: bool = True,
    ) -> None:
        import sherpa_onnx

        model_dir = Path(model_dir)
        model = model_dir / "model.int8.onnx"
        tokens = model_dir / "tokens.txt"
        if not model.exists() or not tokens.exists():
            raise SystemExit(
                f"缺少模型：{model_dir}\n先执行 ./scripts/fetch_models.sh sensevoice"
            )

        t0 = time.perf_counter()
        self._rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model),
            tokens=str(tokens),
            num_threads=num_threads,
            sample_rate=TARGET_RATE,
            language=language,
            use_itn=use_itn,
        )
        self.load_ms = (time.perf_counter() - t0) * 1000

    def transcribe(self, pcm: np.ndarray) -> tuple[str, float]:
        """16k 单声道 float32 → (文本, 耗时ms)。"""
        if pcm.size == 0:
            return "", 0.0
        t0 = time.perf_counter()
        stream = self._rec.create_stream()
        stream.accept_waveform(TARGET_RATE, pcm)
        self._rec.decode_stream(stream)
        text = stream.result.text.strip()
        return text, (time.perf_counter() - t0) * 1000

    def transcribe_file(self, path: str | Path) -> tuple[str, float]:
        pcm, rate = read_wav(path)
        if rate != TARGET_RATE:
            import soxr

            pcm = soxr.resample(pcm, rate, TARGET_RATE)
        return self.transcribe(pcm)


def main(argv: list[str] | None = None) -> int:
    st = Settings.load()
    ap = argparse.ArgumentParser(prog="jarvis.asr", description="Speech-to-text 自测")
    ap.add_argument("path", nargs="?", help="wav 文件路径")
    ap.add_argument("--mic", type=float, metavar="SEC", help="录 N 秒再从麦克风转写")
    args = ap.parse_args(argv)

    if not args.path and not args.mic:
        ap.error("给一个 wav 路径，或用 --mic SEC")

    asr = SenseVoice(st.asr_model_dir, language=st.asr_language)
    print(f"模型加载 {asr.load_ms:.0f}ms（{st.asr_model_dir}）", file=sys.stderr)

    if args.mic:
        with Mic(st.input_device) as mic:
            print(f"录音 {args.mic:g} 秒…", file=sys.stderr)
            pcm = mic.record(args.mic)
        text, ms = asr.transcribe(pcm)
        print(f"ASR {ms:.0f}ms · {pcm.size / TARGET_RATE:.2f}s 音频")
        print(f"文本：{text}")
        return 0

    text, ms = asr.transcribe_file(args.path)
    print(f"ASR {ms:.0f}ms")
    print(f"文本：{text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
