"""专用 KWS 唤醒词（sherpa-onnx KeywordSpotter + 中文拼音模型）。

和 ASR 门控（wake.WakeGate）的区别：

| | ASR 门控 | 专用 KWS |
|---|---|---|
| 原理 | VAD 断句 → 整句识别 → 文本里找唤醒词 | 流式小模型，专盯几个词 |
| 何时亮灯 | **一句话说完之后** | **说到"贾维斯"最后半个音节时** |
| 静音开销 | 0 | 0（只在有音频时解码） |
| 额外模型 | 无 | 13MB（int8） |
| 支持任意词 | 是 | 需要生成拼音 token 文件 |

模型建模单元是「声母 + 带调韵母」，所以 keywords.txt 长这样：

    j iǎ w éi s ī @贾维斯

本模块用 pypinyin 把中文自动转成这种 token 序列。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

# 声母表：按长度降序匹配（zh/ch/sh 必须在 z/c/s 前面）
INITIALS = (
    "zh", "ch", "sh",
    "b", "p", "m", "f", "d", "t", "n", "l", "g", "k", "h",
    "j", "q", "x", "r", "z", "c", "s", "y", "w",
)

DEFAULT_MODEL = "models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"


def text_to_tokens(text: str, tokens: set[str]) -> list[str] | None:
    """中文 → 声母/韵母 token 序列；有不认识的音节就返回 None。"""
    from pypinyin import Style, pinyin

    out: list[str] = []
    for ch in text:
        if ch.strip() == "":
            continue
        py = pinyin(ch, style=Style.TONE, heteronym=False, errors="ignore")
        if not py or not py[0]:
            return None
        syl = py[0][0]
        initial = ""
        for ini in INITIALS:
            if syl.startswith(ini):
                initial = ini
                break
        final = syl[len(initial) :]
        if initial:
            if initial not in tokens:
                return None
            out.append(initial)
        if final not in tokens:
            return None
        out.append(final)
    return out or None


def build_keywords_file(words: list[str], model_dir: str | Path, out_path: str | Path | None = None) -> Path:
    """把中文唤醒词列表写成 keywords.txt（带 @原文）。"""
    model_dir = Path(model_dir)
    tokens_path = model_dir / "tokens.txt"
    tokens = {line.split()[0] for line in tokens_path.read_text(encoding="utf-8").splitlines() if line.strip()}
    out = Path(out_path) if out_path else model_dir / "keywords_user.txt"

    lines: list[str] = []
    skipped: list[str] = []
    for word in words:
        toks = text_to_tokens(word, tokens)
        if toks is None:
            skipped.append(word)
            continue
        lines.append(f"{' '.join(toks)} @{word}")
    if not lines:
        raise SystemExit(f"这些唤醒词无法转成拼音 token：{skipped}（换几个常用词试试）")
    if skipped:
        print(f"⚠️  跳过无法转换的唤醒词：{skipped}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


class KeywordWake:
    """流式关键词检测：喂音频块，命中返回关键词。"""

    def __init__(
        self,
        model_dir: str | Path = DEFAULT_MODEL,
        words: list[str] | None = None,
        threshold: float = 0.25,
        score: float = 1.0,
        num_threads: int = 2,
    ) -> None:
        import sherpa_onnx

        d = Path(model_dir)
        if not d.exists():
            raise SystemExit(
                f"缺少 KWS 模型：{d}\n执行 ./scripts/fetch_models.sh kws 下载"
            )

        kws_file = build_keywords_file(words or ["贾维斯"], d)
        self.words = words or ["贾维斯"]
        self._spotter = sherpa_onnx.KeywordSpotter(
            tokens=str(d / "tokens.txt"),
            encoder=str(next(d.glob("encoder-*int8*.onnx"))),
            decoder=str(next(d.glob("decoder-*int8*.onnx"))),
            joiner=str(next(d.glob("joiner-*int8*.onnx"))),
            keywords_file=str(kws_file),
            num_threads=num_threads,
            sample_rate=16000,
            keywords_score=score,
            keywords_threshold=threshold,
            provider="cpu",
        )
        self._stream = self._spotter.create_stream()
        self.last_latency_s = 0.0

    def feed(self, pcm: np.ndarray) -> str | None:
        """喂 16k float32；命中返回关键词原文，否则 None。"""
        if pcm.size == 0:
            return None
        self._stream.accept_waveform(16000, pcm)
        while self._spotter.is_ready(self._stream):
            self._spotter.decode_stream(self._stream)
            r = self._spotter.get_result(self._stream)
            if r:
                self._spotter.reset_stream(self._stream)
                return str(r)
        return None

    def reset(self) -> None:
        self._spotter.reset_stream(self._stream)

    def flush(self) -> list[str]:
        """文件/句末收尾：补 0.66s 静音 + input_finished，把最后的窗口解出来。

        实测：不补这段静音，0.8 秒的"贾维斯"**永远检不出来**——
        因为模型右侧需要约 0.64 秒上下文，末尾的音频没被解码就被丢掉了。
        实时麦克风场景里，用户说完后的自然静音天然充当这段 padding。
        """
        hits: list[str] = []
        self._stream.accept_waveform(16000, np.zeros(int(0.66 * 16000), dtype=np.float32))
        self._stream.input_finished()
        while self._spotter.is_ready(self._stream):
            self._spotter.decode_stream(self._stream)
            r = self._spotter.get_result(self._stream)
            if r:
                hits.append(str(r))
                self._spotter.reset_stream(self._stream)
        return hits

    @staticmethod
    def detect_file(wav_path: str | Path, model_dir: str | Path, words: list[str],
                    threshold: float = 0.25) -> list[str]:
        """整段音频检测（测试/离线用）。"""
        from .audio import read_wav

        pcm, rate = read_wav(wav_path)
        if rate != 16000:
            import soxr

            pcm = soxr.resample(pcm, rate, 16000)
        kw = KeywordWake(model_dir, words, threshold=threshold)
        hits: list[str] = []
        for i in range(0, pcm.size, 1024):
            r = kw.feed(pcm[i : i + 1024])
            if r:
                hits.append(r)
        hits.extend(kw.flush())
        return hits
