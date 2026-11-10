#!/usr/bin/env python3
"""A/B 对比：同一段刺激，分别用「波束」和「旧的每帧选最响」跑一遍，
比较**产品上行**（AEC + 降噪 + 选路之后真正送 ASR 的东西）。

为什么要在设备上跑真守护进程而不是离线仿真：
  产品链里 AEC/降噪是设备上的定点 speex，Mac 上复现不了；双麦对齐也依赖
  真实采集时序。离线只能证明「算法有效」，这里证明「发上去的音频变好了」。

指标（都在 Mac 侧算）：
  · 语音段 / 尾部静音段的频带电平与 SNR
  · 10ms 帧能量跳变 |ΔE|（选路抖动的直接证据，p95 越小越好）
  · SenseVoice 识别结果与 CER

    .venv/bin/python scripts/s12_ab_test.py --stimulus /tmp/speech_test.wav
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import s12cap as cap  # noqa: E402

FS = 16000
LOCAL_BIN = ROOT / "tools/s12audio/build/s12audio"
REMOTE_BIN = "/data/s12audio/s12audio"


def deploy(host: str) -> None:
    """推新二进制（先备份旧的），md5 对齐。"""
    cap.sh(f"cp {REMOTE_BIN} {REMOTE_BIN}.old 2>/dev/null || true", host, check=False)
    data = LOCAL_BIN.read_bytes()
    p = subprocess.run(
        ["ssh", *cap.SSH_OPTS, f"root@{host}",
         f"{cap.DPATH}; /bin/cat > {REMOTE_BIN}.new && chmod +x {REMOTE_BIN}.new "
         f"&& mv {REMOTE_BIN}.new {REMOTE_BIN}"],
        input=data, capture_output=True,
    )
    if p.returncode != 0:
        raise SystemExit(f"推送失败: {p.stderr.decode()[:300]}")
    out = cap.sh(f"md5sum {REMOTE_BIN} | cut -d' ' -f1", host).strip()
    import hashlib
    if out != hashlib.md5(data).hexdigest():
        raise SystemExit(f"md5 不一致：设备 {out}")
    print(f"[ab] 已部署 {len(data) / 1024:.0f}KB → {REMOTE_BIN}")


def run_variant(host: str, args: str, tag: str, seconds: float, stimulus: str) -> dict:
    """手动起一个守护进程，录一段上行，收日志。"""
    cap.stop_audio(host)
    log = f"/tmp/ab_{tag}.log"
    cap.sh(f"rm -f {log}", host, check=False)
    # 设备上没有 nohup/setsid，start-stop-daemon 也不行；最稳的是让这个
    # ssh 会话**前台**跑守护进程（Popen 持有它），日志落到设备文件，收完再取。
    holder = subprocess.Popen(
        ["ssh", *cap.SSH_OPTS, f"root@{host}",
         f"{cap.DPATH}; cd /data/s12audio && exec ./s12audio {args} > {log} 2>&1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    ok = False
    for _ in range(40):
        time.sleep(0.5)
        if ":8765" in cap.sh("netstat -ltn 2>/dev/null | grep 8765 || true", host, check=False):
            ok = True
            break
    if not ok:
        holder.kill()
        raise SystemExit(f"[{tag}] 守护进程没起来，日志：\n{cap.sh(f'cat {log}', host, check=False)}")

    out = f"/tmp/up_{tag}.wav"
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts/s12_uplink_rec.py"),
         "--seconds", str(seconds), "--play", stimulus, "--out", out],
        capture_output=True, text=True,
    )
    print(r.stdout.strip()[-400:])
    if r.returncode != 0:
        print(r.stderr[-400:])

    # 让守护进程打一次 STATS（非 -q 时才有）
    cap.sh("kill -USR1 $(pidof s12audio) 2>/dev/null || true", host, check=False)
    time.sleep(0.4)
    logtxt = cap.sh(f"cat {log} 2>/dev/null || true", host, check=False)
    cap.sh("killall s12audio 2>/dev/null || true", host, check=False)
    holder.terminate()
    try:
        holder.wait(timeout=5)
    except subprocess.TimeoutExpired:
        holder.kill()
    time.sleep(0.6)
    return {"tag": tag, "wav": Path(out), "log": logtxt}


def segoe(path: Path) -> tuple[np.ndarray, float]:
    with wave.open(str(path)) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768
    return x, len(x) / FS


def bandpass(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    n = len(x)
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(n, 1 / FS)
    X[(f < lo) | (f > hi)] = 0
    return np.fft.irfft(X, n=n)


def metrics(x: np.ndarray) -> dict:
    """语音段取最响的 40% 帧，噪声取尾部 1.2s。"""
    n = len(x) // 160
    e = np.sqrt(np.mean(x[: n * 160].reshape(n, 160) ** 2, axis=1))
    edb = 20 * np.log10(e + 1e-12)
    thr = np.percentile(edb, 60)
    speech = np.where(edb >= thr)[0]
    bp = bandpass(x, 300, 3400)
    sp = np.concatenate([bp[i * 160 : (i + 1) * 160] for i in speech]) if speech.size else bp
    tail = bp[max(0, int((len(x) / FS - 1.2) * FS)) :]
    sp_db = 20 * np.log10(np.sqrt(np.mean(sp**2)) + 1e-12)
    nz_db = 20 * np.log10(np.sqrt(np.mean(tail**2)) + 1e-12)
    # 只统计「有声」帧之间的跳变：静音帧的能量被降噪压到 0，
    # |ΔE| 会出现 140dB 这种假台阶（实测踩过），对选路抖动没有解释力。
    act = edb > -70.0
    pair = act[1:] & act[:-1]
    d = np.abs(np.diff(edb))[pair] if pair.any() else np.zeros(1)
    return {
        "语音": sp_db, "噪声": nz_db, "SNR": sp_db - nz_db,
        "ΔE中位": float(np.median(d)), "ΔE_p95": float(np.percentile(d, 95)),
        "rms": float(20 * np.log10(np.sqrt(np.mean(x**2)) + 1e-12)),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.1.3")
    ap.add_argument("--stimulus", default="/tmp/speech_test.wav")
    ap.add_argument("--seconds", type=float, default=12.0)
    ap.add_argument("--text", default="把客厅的灯打开。然后把亮度调到百分之六十。再帮我查一下明天的天气怎么样。")
    ap.add_argument("--skip-deploy", action="store_true")
    ap.add_argument("--wake-ladder", action="store_true",
                    help="刺激是唤醒词电平梯度：额外报每一级是否被唤醒（KWS）")
    ap.add_argument("--ladder-json", default="/tmp/wake_ladder.json")
    ap.add_argument("--variants", default="-B:old,-b:beam",
                    help="逗号分隔的「参数:标签」，标签只用 ASCII（会进设备端文件名）")
    args = ap.parse_args()

    if not args.skip_deploy:
        deploy(args.host)

    results = []
    try:
        for spec in args.variants.split(","):
            vargs, tag = spec.split(":", 1)
            print(f"\n===== {tag}  ({vargs}) =====")
            results.append(run_variant(args.host, vargs, tag, args.seconds, args.stimulus))
    finally:
        print("\n[ab] 恢复 jarvis-audio 服务…")
        cap.start_audio(args.host)

    # 统计
    try:
        from jarvis.asr import SenseVoice
        asr = SenseVoice(str(ROOT / "models/sensevoice"), num_threads=4)
    except Exception as e:  # noqa: BLE001
        asr = None
        print(f"⚠️  ASR 不可用：{e}")

    print(f"\n{'变体':<12} {'语音段':>8} {'噪声':>8} {'SNR':>7} {'ΔE中位':>7} {'ΔE_p95':>7} {'ASR':>8}")
    for r in results:
        x, _dur = segoe(r["wav"])
        m = metrics(x)
        cer_s = "—"
        if asr is not None and x.size:
            txt, ms = asr.transcribe(x)
            cer_s = f"{ms}ms"
            r["asr"] = txt
        print(f"{r['tag']:<12} {m['语音']:8.1f} {m['噪声']:8.1f} {m['SNR']:7.2f} "
              f"{m['ΔE中位']:7.2f} {m['ΔE_p95']:7.2f} {cer_s:>8}")
        if r.get("asr"):
            print(f"    ASR: {r['asr']}")

    if args.wake_ladder:
        import json
        from s12_wake_eval import kws_hits_at, marker_offset
        from jarvis.config import Settings
        from jarvis.kws import KeywordWake  # noqa: F401
        md = ROOT / Settings.load().kws_model
        marks = json.loads(Path(args.ladder_json).read_text())["marks"]
        levels = sorted({m[0] for m in marks}, reverse=True)
        print("\n唤醒率（每级重复 %d 次，KWS 阈值走默认）" % max(1, len(marks) // len(levels)))
        print(f"{'变体':<10} " + " ".join(f"{L:+5.0f}dB" for L in levels))
        for r in results:
            x, _d = segoe(r["wav"])
            off = marker_offset(x, 0.2)
            ts = kws_hits_at(x, md)
            cells = []
            for L in levels:
                ms = [m[1] for m in marks if m[0] == L]
                hit = sum(any(a + off - 0.2 <= t <= a + off + 2.2 for t in ts) for a in ms)
                cells.append(f"{hit}/{len(ms)}".rjust(7))
            print(f"{r['tag']:<10} " + " ".join(cells))

    # BEAM 诊断行
    for r in results:
        for line in r["log"].splitlines():
            if "BEAM" in line or "就绪" in line:
                print(f"[{r['tag']}] {line.strip()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())