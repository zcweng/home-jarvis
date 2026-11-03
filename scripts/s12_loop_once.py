#!/usr/bin/env python3
"""S12 端点一次闭环：cue 提示 → 定长录音 → ASR/LLM → 回答从音箱播出。

用定长录音而不是常开 VAD，避免联调时「人已说完但 VAD 还在等」或提示不同步。

    .venv/bin/python scripts/s12_loop_once.py --fake-llm
    .venv/bin/python scripts/s12_loop_once.py --fake-llm --prompt "打开客厅灯" --seconds 4
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from jarvis.app import Jarvis  # noqa: E402
from jarvis.config import Settings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="S12 说→听→答 一次闭环")
    ap.add_argument("--host", default="", help="覆盖 SPEAKER_ENDPOINT")
    ap.add_argument("--fake-llm", action="store_true")
    ap.add_argument("--prompt", default="打开客厅灯", help="提示用户说的话")
    ap.add_argument("--seconds", type=float, default=4.0, help="录音时长")
    args = ap.parse_args()

    st = Settings.load()
    st = replace(
        st,
        audio_source="speaker_endpoint",
        speaker_endpoint=args.host or st.speaker_endpoint or "192.168.1.3",
    )
    app = Jarvis(st=st, use_fake_llm=args.fake_llm, speak=True)

    try:
        import cue

        box: dict = {}

        def _record():
            with app._mic_factory() as mic:
                mic.drain()
                return mic.record(args.seconds)

        rc = cue.run_cue(
            f"请面对 S12 音箱，正常音量说：\n\n「{args.prompt}」\n\n"
            f"红框「采集中」出现后立刻说，约 {args.seconds:g} 秒；说完等音箱回答。",
            title="home-jarvis · S12 说→听→答",
            seconds=args.seconds,
            countdown_s=3,
            action=_record,
            result_box=box,
            timeout=180,
        )
        if rc == cue.EXIT_UNCONFIRMED:
            print("未确认，跳过（未验证）", file=sys.stderr)
            return 2
        if rc != 0 or "value" not in box:
            print("录音失败", file=sys.stderr)
            return 1

        pcm = box["value"]
        print(f"录音 {getattr(pcm, 'size', 0) / 16000:.2f}s，开始识别…", flush=True)
        app.handle(pcm)
        return 0
    finally:
        app.tools.cancel_timers()
        app.ha.close()
        if hasattr(app.llm, "close"):
            app.llm.close()
        if app._speaker is not None and hasattr(app._speaker, "close"):
            app._speaker.close()


if __name__ == "__main__":
    raise SystemExit(main())
