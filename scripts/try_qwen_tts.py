#!/usr/bin/env python3
"""千问 Realtime TTS 冒烟：有 DASHSCOPE_API_KEY 才真正请求。

用法：
  DASHSCOPE_API_KEY=sk-... .venv/bin/python scripts/try_qwen_tts.py
  .venv/bin/python scripts/try_qwen_tts.py "你好" --save /tmp/qwen.wav
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jarvis.config import Settings  # noqa: E402
from jarvis.tts_qwen import QwenRealtimeTTS  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="千问 Realtime TTS 冒烟")
    ap.add_argument("text", nargs="?", default="你好，我是贾维斯。")
    ap.add_argument("--save", default="", help="保存 wav；留空则直接播放")
    args = ap.parse_args()

    st = Settings.load()
    if not st.dashscope_api_key:
        print("⏭️  未配置 DASHSCOPE_API_KEY，跳过真实请求", file=sys.stderr)
        return 0

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
    pcm, rate = tts.synthesize(args.text)
    print(f"音色 {tts.voice} · {pcm.size / max(rate, 1):.2f}s @ {rate}Hz")
    if args.save:
        import wave

        import numpy as np

        with wave.open(args.save, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes((np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes())
        print(f"已保存 {args.save}")
    else:
        # 直接播已合成 PCM，避免 speak 再请求一次；整段一次 play 才不卡顿
        from jarvis.audio import Speaker

        Speaker(st.output_device).play(pcm, rate, blocking=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
