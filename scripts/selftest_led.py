#!/usr/bin/env python3
"""S12 灯环烟测：按序演示 idle→listen→think→speak→error→idle。

    .venv/bin/python scripts/selftest_led.py
    .venv/bin/python scripts/selftest_led.py --host 192.168.1.3
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.config import Settings  # noqa: E402
from jarvis.endpoint import parse_endpoint  # noqa: E402
from jarvis.s12_led import S12Led  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--hold", type=float, default=2.5, help="每态停留秒")
    args = ap.parse_args()

    st = Settings.load()
    host = args.host or parse_endpoint(st.speaker_endpoint)[0]
    port = args.port or st.led_port
    led = S12Led(host, port, enabled=True)
    seq = [
        ("idle", "熄灭"),
        ("listen", "绿色呼吸"),
        ("think", "蓝色呼吸（原厂）"),
        ("speak", "固定蓝"),
        ("error", "红色呼吸"),
        ("idle", "熄灭"),
    ]
    print(f"灯环演示 → {host}（SSH /data/jarvis-led-set.sh）")
    ok_all = True
    for state, tip in seq:
        print(f"  → {state}（{tip}）", flush=True)
        ok = led.set_blocking(state)  # type: ignore[attr-defined]
        if not ok:
            print(f"    ❌ 下发失败", flush=True)
            ok_all = False
        time.sleep(args.hold)
    led.set_blocking("idle")  # type: ignore[attr-defined]
    print("完成" if ok_all else "有失败项")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
