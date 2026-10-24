#!/usr/bin/env python3
"""语音链路自检：TTS → WAV → ASR 往返。

为什么要用 TTS 反喂 ASR：手头没有"标准语音"，但 `say` 合成的音频走完整
（重采样 → 16k → SenseVoice 解码）链路，能真实暴露采样率、通道、静音、文本规范化等问题。
人声只影响识别精度，不影响链路正确性。

    .venv/bin/python scripts/selftest_speech.py
    .venv/bin/python scripts/selftest_speech.py --mic 3   # 额外测一次真麦克风
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.asr import SenseVoice  # noqa: E402
from jarvis.audio import TARGET_RATE, Mic  # noqa: E402
from jarvis.config import Settings  # noqa: E402
from jarvis.tts import SayTTS, probe_voice  # noqa: E402

CASES = [
    ("打开客厅的灯", "高频指令"),
    ("把主卧空调调到二十六度", "含数字"),
    ("帮我把灯调暗一点，然后放点轻音乐", "多意图 + 指代"),
    ("明天成都天气怎么样", "问答"),
    ("客厅的灯已经打开了", "状态回报"),
]


_NUM_RUN = re.compile(r"[零一二两三四五六七八九十百千0-9]+")


def normalize(text: str) -> str:
    """去标点 + 数字统一成 N。

    ASR 开了 ITN，会把"二十六"写成"26"；两边统一成同一个符号，
    才不会被这种正确行为误判成识别错误。
    """
    drop = "，。！？、,.!?《》“”\"'‘’：:；;（）() \n\t"
    t = "".join(ch for ch in text if ch not in drop).lower()
    return _NUM_RUN.sub("N", t)


def cer(ref: str, hyp: str) -> float:
    """字符级编辑距离 / 参考长度。"""
    a, b = normalize(ref), normalize(hyp)
    if not a:
        return 0.0 if not b else 1.0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1] / len(a)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mic", type=float, metavar="SEC", default=0.0, help="额外录 N 秒真麦克风做转写")
    args = ap.parse_args()

    st = Settings.load()

    # 1) 音色体检
    print("== 音色体检 ==")
    for v in dict.fromkeys([st.tts_voice_say, "Tingting", "Meijia", "Eddy"]):
        dur = probe_voice(v)
        flag = "✅" if dur >= 0.2 else "❌ 不可用（渲染为空）"
        print(f"  {v:<10} {dur * 1000:7.0f}ms  {flag}")
    print()

    # 用临时缓存目录，保证"冷启动合成"这个数字是真实的（否则全是缓存命中 1ms）
    tts = SayTTS(st.tts_voice_say, mode="wav", cache_dir=Path(tempfile.mkdtemp(prefix="tts-")))
    print(f"== TTS→ASR 往返（音色 {tts.voice}） ==")

    t_asr0 = time.perf_counter()
    asr = SenseVoice(st.asr_model_dir, language=st.asr_language)
    print(f"ASR 模型加载 {(time.perf_counter() - t_asr0) * 1000:.0f}ms\n")

    rows: list[tuple[str, str, str, float, float, float]] = []
    for text, tag in CASES:
        t0 = time.perf_counter()
        pcm, rate = tts.synthesize(text)
        t_synth = (time.perf_counter() - t0) * 1000

        hyp, t_asr = asr.transcribe(pcm)
        rows.append((text, hyp, tag, cer(text, hyp), t_synth, t_asr))

    print(f"{'参考文本':<26} {'识别结果':<26} {'CER':>6} {'合成ms':>7} {'ASRms':>6}")
    print("-" * 80)
    for ref, hyp, _tag, c, ts, ta in rows:
        mark = "" if c == 0 else ("  ⚠️" if c < 0.3 else "  ❌")
        print(f"{ref:<26} {hyp:<26} {c:>6.2f} {ts:>7.0f} {ta:>6.0f}{mark}")

    avg_cer = sum(r[3] for r in rows) / len(rows)
    avg_asr = sum(r[5] for r in rows) / len(rows)
    cold = rows[0][4]
    t0 = time.perf_counter()
    tts.synthesize(rows[0][0])  # 同一句再渲染一次 → 命中缓存
    warm = (time.perf_counter() - t0) * 1000
    print("-" * 80)
    print(f"平均 CER {avg_cer:.3f} · 平均 ASR {avg_asr:.0f}ms · 合成冷启动 {cold:.0f}ms · 缓存命中 {warm:.1f}ms")
    print(f"缓存目录：{tts._cache}")

    ok = avg_cer <= 0.15

    if args.mic > 0:
        print(f"\n== 真麦克风 {args.mic:g}s ==")
        with Mic(st.input_device) as mic:
            print("现在说话…", file=sys.stderr)
            pcm = mic.record(args.mic)
        hyp, ms = asr.transcribe(pcm)
        print(f"识别：{hyp or '(空)'}  · {ms:.0f}ms · 音频 {pcm.size / TARGET_RATE:.2f}s")
        if not hyp:
            print("提示：完全没识别出内容，检查麦克风权限/距离/音量")

    print()
    print("✅ 语音链路可用（TTS 与 ASR 已打通）" if ok else "❌ 识别质量不达标，先排查音频链路")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
