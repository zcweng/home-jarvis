#!/usr/bin/env python3
"""Nano TTS 侧车自检：健康检查 / 合成 / 中英混读 / 不可达回落。"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.tts_nano import NanoTTS, probe_nano  # noqa: E402

URL = os.environ.get("TTS_NANO_URL", "http://127.0.0.1:8091")


def main() -> int:
    if not probe_nano(URL):
        print(f"⏭️  侧车未就绪（{URL}），跳过合成用例；先 ./scripts/nano_tts_server.sh start --bg")
        # 不可达回落仍要测
        class FB:
            mode = "fake"

            def synthesize(self, text):  # noqa: ANN001
                import numpy as np

                return np.zeros(800, dtype=np.float32), 16000

        tts = NanoTTS("http://127.0.0.1:1", timeout=0.2, cool_down_s=2, fallback=FB())
        pcm, sr = tts.synthesize("x")
        assert sr == 16000 and pcm.size == 800
        print("✅ 不可达回落")
        return 0

    tts = NanoTTS(URL, ref="zh_1", timeout=30)
    pcm, sr = tts.synthesize("老虎英文是 tiger。")
    dur = pcm.size / sr if sr else 0
    print(f"✅ 合成 tiger：{dur:.2f}s @ {sr}Hz")
    assert dur > 0.5

    # 可选 ASR 往返（有主 venv SenseVoice 时）
    try:
        import numpy as np
        import soxr
        from jarvis.asr import SenseVoice
        from jarvis.config import Settings

        st = Settings.load()
        asr = SenseVoice(st.asr_model_dir)
        audio16 = soxr.resample(pcm.astype(np.float32), sr, 16000)
        hyp, _ = asr.transcribe(audio16)
        print(f"  ASR: {hyp!r}")
        assert "tiger" in hyp.lower(), hyp
        print("✅ 中英混读 ASR 含 tiger")
    except Exception as exc:  # noqa: BLE001
        print(f"⏭️  ASR 往返跳过：{exc}")

    tts.stop()
    t0 = time.perf_counter()
    # speaking 在无 speaker 时为 False
    while tts.speaking and time.perf_counter() - t0 < 0.5:
        time.sleep(0.02)
    print("✅ stop 后 speaking=False")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
