#!/usr/bin/env python3
"""联调：往 S12 下行推一段 tone，同时 SSH 采 ch7 / STATS。"""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.endpoint import TARGET_RATE, EndpointSpeaker, _tone  # noqa: E402

SSH = [
    "ssh",
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "BatchMode=yes",
    "-o",
    "HostKeyAlgorithms=+ssh-rsa",
    "-o",
    "PubkeyAcceptedKeyTypes=+ssh-rsa",
    "root@192.168.1.3",
]


def remote(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        SSH + [f"export PATH=/usr/sbin:/usr/bin:/sbin:/bin; {script}"],
        text=True,
        capture_output=True,
    )


def probes(seconds: int) -> None:
    remote(
        "PID=$(ps | grep '[.]/s12audio' | grep -v grep | awk '{print $1}' | head -1); "
        f"echo PROBE_PID=$PID; "
        f"for i in $(seq 1 {seconds}); do kill -USR2 $PID; sleep 1; done; "
        "kill -USR1 $PID"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--db", type=float, default=-9.0)
    ap.add_argument("--freq", type=float, default=1000.0)
    args = ap.parse_args()

    pcm = _tone(args.seconds, args.freq, args.db)
    spk = EndpointSpeaker("192.168.1.3")
    if not spk.writer.connect():
        print("❌ 连不上 8766")
        return 1
    spk.writer.volume(0.0)

    th = threading.Thread(target=probes, args=(int(args.seconds) + 2,), daemon=True)
    th.start()
    time.sleep(0.25)
    print(f"PLAY {args.freq:g}Hz {args.db:g}dBFS {args.seconds:g}s", flush=True)
    spk.play(pcm, TARGET_RATE, blocking=True)
    time.sleep(0.5)
    spk.close()
    th.join(timeout=args.seconds + 5)

    out = remote("grep -E '下行|STATS|ch7' /tmp/s12audio.log | tail -25")
    print(out.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
