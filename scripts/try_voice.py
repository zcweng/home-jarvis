#!/usr/bin/env python3
"""试试看：录 N 秒 → 识别 → 把你说的话念回来。

用来验证"真麦克风 + 真声卡"这条链路（自检脚本用的是合成语音，不覆盖麦克风）。
录完会出声，注意音量。

    .venv/bin/python scripts/try_voice.py            # 录 3 秒
    .venv/bin/python scripts/try_voice.py -d 5       # 录 5 秒
    .venv/bin/python scripts/try_voice.py --repeat   # 连续多轮，Ctrl+C 退出
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.asr import SenseVoice  # noqa: E402
from jarvis.audio import TARGET_RATE, Mic, Speaker  # noqa: E402
from jarvis.config import Settings  # noqa: E402
from jarvis.tts import SayTTS  # noqa: E402


def one_round(st: Settings, asr: SenseVoice, tts: SayTTS, seconds: float, idx: int) -> str:
    with Mic(st.input_device) as mic:
        mic.drain()
        t0 = time.perf_counter()
        print(f"\n[{idx}] 说话（{seconds:g} 秒）…", flush=True)
        pcm = mic.record(seconds)
        t_rec = (time.perf_counter() - t0) * 1000

    if pcm.size == 0:
        print("❌ 没录到音频")
        return ""

    text, t_asr = asr.transcribe(pcm)
    speech_ms = pcm.size / TARGET_RATE * 1000
    print(f"  录音 {speech_ms:.0f}ms（耗时 {t_rec:.0f}ms）· ASR {t_asr:.0f}ms")
    print(f"  识别：{text or '(空)'}")
    if not text:
        print("  提示：没识别出内容，检查麦克风权限、距离、音量，或换一副耳机")
        return ""

    t1 = time.perf_counter()
    tts.speak(f"你刚才说的是：{text}")
    print(f"  播报完成，本轮回合计耗时 {(time.perf_counter() - t1) * 1000:.0f}ms")
    return text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-d", "--duration", type=float, default=3.0)
    ap.add_argument("--repeat", action="store_true", help="连续多轮（Ctrl+C 退出）")
    args = ap.parse_args()

    st = Settings.load()
    print("加载模型…", file=sys.stderr)
    asr = SenseVoice(st.asr_model_dir, language=st.asr_language)
    speaker = Speaker(st.output_device) if st.tts_mode == "wav" else None
    tts = SayTTS(st.tts_voice_say, st.tts_mode, speaker)
    print(f"麦克风={st.input_device} · 音色={tts.voice} · TTS 模式={tts.mode}", file=sys.stderr)

    hist: list[str] = []
    i = 1
    try:
        while True:
            text = one_round(st, asr, tts, args.duration, i)
            if text:
                hist.append(text)
            if not args.repeat:
                break
            i += 1
    except KeyboardInterrupt:
        print("\n退出")

    if len(hist) > 1:
        print("\n本次会话识别记录：")
        for j, t in enumerate(hist, 1):
            print(f"  {j}. {t}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
