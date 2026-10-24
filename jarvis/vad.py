"""语音活动检测（断句）：sherpa-onnx 内置的 silero-vad。

用 sherpa-onnx 而不是 silero-vad 官方包的理由：不用再拖一个 torch/onnxruntime，
模型只有 643KB，`sherpa_onnx.VoiceActivityDetector` 直接给"一句话"级别的切分。

关键参数（都在 .env 里可调）：
- threshold 0.5     语音概率阈值，噪声大就调高
- min_silence 0.5s  判定"说完了"所需静音，直接决定切句延迟
- min_speech 0.25s 过滤咳嗽/键盘声
- max_speech 12s    一句话最长，超了强制切

命令行自检：
    python -m jarvis.vad debug/rec.wav        # 看这段音频被切成几个语音段
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

from .audio import TARGET_RATE, read_wav


class Segmenter:
    """流式喂音频块，吐出完整的"一句话"。

        seg = Segmenter("models/silero-vad/silero_vad.onnx")
        for chunk in mic:
            for utt in seg.accept(chunk):
                asr.transcribe(utt)
        tail = seg.flush()      # 结束时把最后一句取出来
    """

    def __init__(
        self,
        model_path: str | Path,
        threshold: float = 0.5,
        min_silence: float = 0.5,
        min_speech: float = 0.25,
        max_speech: float = 12.0,
        buffer_seconds: int = 60,
    ) -> None:
        import sherpa_onnx

        model_path = Path(model_path)
        if not model_path.exists():
            raise SystemExit(f"缺少 VAD 模型：{model_path}\n先执行 ./scripts/fetch_models.sh silero-vad")

        cfg = sherpa_onnx.VadModelConfig()
        cfg.silero_vad.model = str(model_path)
        cfg.silero_vad.threshold = threshold
        cfg.silero_vad.min_silence_duration = min_silence
        cfg.silero_vad.min_speech_duration = min_speech
        cfg.silero_vad.max_speech_duration = max_speech
        cfg.silero_vad.window_size = 512
        cfg.sample_rate = TARGET_RATE

        self._vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=buffer_seconds)
        self.min_silence = min_silence

    def accept(self, pcm: np.ndarray) -> list[np.ndarray]:
        """喂一块 16k float32，返回本次切出来的完整语音段（可能为空）。"""
        if pcm.size == 0:
            return []
        self._vad.accept_waveform(pcm)
        return self._drain()

    def _drain(self) -> list[np.ndarray]:
        out: list[np.ndarray] = []
        while not self._vad.empty():
            seg = self._vad.front
            samples = np.asarray(seg.samples, dtype=np.float32)
            if samples.size:
                out.append(samples)
            self._vad.pop()
        return out

    def flush(self) -> np.ndarray | None:
        """结束输入：把缓冲区里剩下的最后一句取出。"""
        self._vad.flush()
        segs = self._drain()
        return segs[-1] if segs else None

    @property
    def speech_detected(self) -> bool:
        """当前是否正在说话（可用来做打断判定）。"""
        return bool(self._vad.is_speech_detected())


def main(argv: list[str] | None = None) -> int:
    import argparse

    from .config import Settings

    ap = argparse.ArgumentParser(prog="jarvis.vad", description="VAD 切句自检")
    ap.add_argument("path", help="wav 文件")
    args = ap.parse_args(argv)

    st = Settings.load()
    pcm, rate = read_wav(args.path)
    if rate != TARGET_RATE:
        import soxr

        pcm = soxr.resample(pcm, rate, TARGET_RATE)
        rate = TARGET_RATE

    seg = Segmenter(
        st.vad_model,
        threshold=st.vad_threshold,
        min_silence=st.vad_silence_ms / 1000,
    )
    print(f"音频 {pcm.size / rate:.2f}s，阈值 {st.vad_threshold}，静音判定 {st.vad_silence_ms}ms")

    chunks: list[np.ndarray] = []
    step = 1024  # 每 64ms 喂一次，模拟真实麦克风
    t = 0.0
    for i in range(0, pcm.size, step):
        for utt in seg.accept(pcm[i : i + step]):
            chunks.append(utt)
            t += utt.size / rate
            print(f"  ✂️  第 {len(chunks)} 段：{utt.size / rate:.2f}s")
    tail = seg.flush()
    if tail is not None:
        chunks.append(tail)
        print(f"  ✂️  flush 尾段：{tail.size / rate:.2f}s")

    print(f"共 {len(chunks)} 段，语音 {t:.2f}s / 总长 {pcm.size / rate:.2f}s"
          f"（语音占比 {t / (pcm.size / rate) * 100:.0f}%）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
