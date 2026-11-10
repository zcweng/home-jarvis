#!/usr/bin/env python3
"""唤醒词**电平梯度**实测：同一段「贾维斯」逐级衰减 6dB，看哪一级还能唤醒。

这是回答「离得远就叫不醒」的唯一硬指标——SNR 提高 1dB 听感上没意义，
但唤醒阈值下移 6dB 意味着「原来要喊，现在正常说话就行」。

输入两种录音（可选）：
  --array  6 路裸麦克风（s12cap 录的），用于比较选路/波束方案
  --uplink 产品上行（s12_uplink_rec 录的），即 AEC+降噪之后真正送 ASR 的东西

    .venv/bin/python scripts/s12_wake_eval.py \
        --array /tmp/cap_ladder.wav --uplink /tmp/up_ladder.wav \
        --ladder /tmp/wake_ladder.wav
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import s12_beam_eval as E  # noqa: E402

from jarvis.config import Settings  # noqa: E402
from jarvis.kws import KeywordWake  # noqa: E402

POSITIVE = ["贾维斯", "小贾", "管家"]


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        channels = w.getnchannels()
        raw = w.readframes(w.getnframes())
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    return x.reshape(-1, channels) if channels > 1 else x


def ladder_segments(path: Path) -> list[tuple[float, float, float]]:
    """从梯度文件里切出每级唤醒词：(衰减 dB, 起, 止)。"""
    x = read_wav(path).reshape(-1)
    env = E.frame_energy(E.bandpass(x.reshape(-1, 1), 200, 4000)[:, 0], 160)
    loud = 20 * np.log10(env + 1e-12)
    # 每级是一段 0.83s 的孤立词，静音间隔 2.2s → 用「比峰值低 40dB」粗切
    thr = loud.max() - 45.0
    segs: list[tuple[float, float, float]] = []
    i = 0
    n = len(loud)
    while i < n:
        if loud[i] > thr:
            j = i
            while j < n and loud[j] > thr - 6:
                j += 1
            i0, i1 = i * 160 / E.FS, j * 160 / E.FS
            if i1 - i0 > 0.3:
                peak = loud[i:j].max()
                segs.append((float(peak - loud.max()), i0, i1))
            i = j
        else:
            i += 1
    return segs


def align_offset(rec: np.ndarray, ref: np.ndarray, max_lag_s: float = 2.0) -> float:
    """录音相对参考文件的整体时延（用语音带包络互相关，粗到 1 个 10ms 帧即可）。"""
    a = E.frame_energy(E.bandpass(rec.reshape(-1, 1), 200, 4000)[:, 0], 160)
    b = E.frame_energy(E.bandpass(ref.reshape(-1, 1), 200, 4000)[:, 0], 160)
    a = (a - a.mean()) / (a.std() + 1e-9)
    b = (b - b.mean()) / (b.std() + 1e-9)
    nfft = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    cc = np.fft.irfft(np.fft.rfft(a, nfft) * np.conj(np.fft.rfft(b, nfft)), nfft)
    lag = int(np.argmax(cc[: len(a)]))
    return lag * 160 / E.FS


def marker_offset(rec: np.ndarray, src_marker_s: float = 0.2) -> float:
    """用 1kHz 标记音求整体时延。比包络互相关可靠：6 个重复词自相似，
    包络相关会锁到相邻级上（错 4.5s 的整数倍）。"""
    x = np.asarray(rec, dtype=np.float32).reshape(-1)
    n = 512
    hop = 160
    m = (len(x) - n) // hop
    if m <= 2:
        return 0.0
    idx = np.arange(n)
    win = np.hanning(n)
    # 直接算 1kHz 的 Goertzel 能量，够用且比 FFT 快
    kern = np.exp(-2j * np.pi * 1000 * idx / E.FS) * win
    e = np.abs(np.array([np.dot(x[i * hop : i * hop + n], kern) for i in range(m)]))
    head = e[: int(6 * E.FS / hop)]
    k = int(np.argmax(head))
    peak = head[k]
    # 往前退到能量降到 30% 的位置，作为标记起点
    j = k
    while j > 0 and head[j] > peak * 0.3:
        j -= 1
    return j * hop / E.FS - src_marker_s


def kws_hits_at(pcm: np.ndarray, model_dir: Path) -> list[float]:
    """整条连续流解码，返回每次命中的时间（秒）。

    不能切片送 KWS：实测同一段干净语音，切 2.3s（词前 0.6s 静音）检不出，
    切 0.9s（紧贴词）反而能检出——切法本身会改变结果。连续流才和生产一致。
    """
    kw = KeywordWake(model_dir, POSITIVE)
    times: list[float] = []
    fed = 0
    for i in range(0, pcm.size, 1024):
        chunk = pcm[i : i + 1024]
        r = kw.feed(chunk)
        fed += chunk.size
        if r:
            times.append(fed / E.FS)
    if kw.flush():
        times.append(fed / E.FS)
    return times


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--array", default="", help="6 路裸麦录音（s12cap）")
    ap.add_argument("--uplink", default="", help="产品上行录音（s12_uplink_rec）")
    ap.add_argument("--ladder", default="/tmp/wake_ladder.wav", help="我放的梯度源文件")
    ap.add_argument("--equalize", type=float, default=0.0,
                    help="把每段语音带 RMS 归一到这个 dBFS 再送 KWS，用于只比「音质」不比「电平」")
    args = ap.parse_args()

    st = Settings.load()
    model_dir = ROOT / st.kws_model
    if not (model_dir / "tokens.txt").exists():
        print(f"❌ 缺少 KWS 模型：{model_dir}")
        return 1

    ref = read_wav(Path(args.ladder)).reshape(-1)
    side = Path(args.ladder).with_suffix(".json")
    if side.exists():
        meta = json.loads(side.read_text())
        segs = [(float(L), float(t), float(t) + 2.1) for L, t in meta["marks"]]
    else:
        segs = ladder_segments(Path(args.ladder))
    print(f"梯度源：{args.ladder}  共 {len(segs)} 级")
    for L, a, b in segs:
        print(f"  {L:+5.0f} dB  {a:6.2f}~{b:6.2f}s")

    tracks: dict[str, np.ndarray] = {}
    if args.array:
        x = read_wav(Path(args.array))[:, :6]
        off = marker_offset(x[:, 3], 0.2)
        print(f"\n6 路录音整体时延 {off * 1000:+.0f} ms")
        # 尾部 1.2s 当噪声参考
        n0 = max(0.0, len(x) / E.FS - 1.3)
        tracks["ch3"] = x[:, 3]
        tracks["loudest"] = E.strat_loudest(x)
        tracks["loudestBand"] = E.strat_loudest_band(x)
        tracks["loudestB500"] = E.strat_loudest_band(x, block_ms=500)
        tracks["avg6"] = E.strat_avg(x)
        tracks["beam"] = E.strat_beam(x)[0]
        track_off = {k: off for k in tracks}
        _ = n0
    if args.uplink:
        u = read_wav(Path(args.uplink)).reshape(-1)
        off = marker_offset(u, 0.2)
        print(f"上行录音整体时延 {off * 1000:+.0f} ms")
        tracks["产品上行"] = u
        track_off = {k: off for k in tracks}

    # 数字增益对照组：只放大不改算法，用来区分「电平不够」和「算法不够」
    gain_rows: dict[str, np.ndarray] = {}
    for name in ("ch3", "beam", "loudestBand"):
        if name in tracks:
            for gdb in (12, 18, 24):
                gain_rows[f"{name}+{gdb}dB"] = tracks[name] * (10 ** (gdb / 20.0))
    for name, sig in gain_rows.items():
        tracks[name] = sig
        track_off[name] = track_off["ch3"] if "ch3" in tracks else 0.0

    # 每级 + 每变体：唤醒成功与否（整条流解码，按命中时刻归档）
    print(f"\n{'变体':<20} " + " ".join(f"{L:+5.0f}dB" for L, _, _ in segs))
    results: dict[str, list[bool]] = {}
    for name, sig in tracks.items():
        off = track_off[name]
        sig = np.asarray(sig, dtype=np.float32).reshape(-1)
        if args.equalize:
            bp = E.bandpass(sig.reshape(-1, 1), 200, 4000)[:, 0]
            cur = 20 * np.log10(np.sqrt(np.mean(bp**2)) + 1e-12)
            sig = sig * (10 ** ((args.equalize - cur) / 20.0))
        times = kws_hits_at(sig, model_dir)
        row = []
        for L, a, b in segs:
            # 命中时刻落在「该级词开始 ~ 词结束+1.2s」之间即算这一级
            row.append(any(a + off - 0.1 <= t <= a + off + 1.9 for t in times))
        results[name] = row
        print(f"{name:<20} " + " ".join(f"{'✅' if h else '❌':>6}" for h in row))

    # 唤醒阈值：还能唤醒的最低声级
    print("\n唤醒阈值（能被叫醒的最小声级，越靠下越好）")
    for name, row in results.items():
        ok = [L for (L, _, _), h in zip(segs, row) if h]
        print(f"  {name:<20} {'无' if not ok else f'{max(ok):+.0f} dB 仍可唤醒'}"
              f"   命中 {sum(row)}/{len(row)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())