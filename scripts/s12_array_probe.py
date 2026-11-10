#!/usr/bin/env python3
"""S12 六麦阵列几何探针：从一份 8ch 原始录音里读出「这个阵列到底能用吗」。

回答四个问题（按顺序）：

1. **各路是不是同一时刻采的**：成对 GCC-PHAT 的 TDOA 是否稳定在几何上限内，
   还是散布到几十个样本（那就说明有固定通道偏移或串扰，波束成形必须先补偿）。
2. **口径多大**：稳态 TDOA 的绝对值上限 → 物理口径 = max|τ| × 343 m/s。
   1 sample @16k = 62.5 µs = 21.4 mm 路程差。
3. **通道间相干性**：噪声场的通道间相干随频率下降的快慢能反推间距
   （扩散声场下 d 间距的第一零点在 c/(2d)）。
4. **值得做波束成形吗**：把测出来的时延对齐后直接求和，看单通道 RSS 提升多少 dB。
   这是延迟求和的上界（不含降噪/后置滤波）。

用法：

    .venv/bin/python scripts/s12_array_probe.py /tmp/cap_white.wav
    .venv/bin/python scripts/s12_array_probe.py /tmp/cap_speech.wav --band 200 3400
"""

from __future__ import annotations

import argparse
import wave
from pathlib import Path

import numpy as np

FS = 16000
MIC = 6
SPEED_OF_SOUND = 343.0
SAMPLE_MM = SPEED_OF_SOUND / FS * 1000.0  # 1 sample = 21.4 mm 路程差


def load(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != FS:
            raise SystemExit(f"采样率 {w.getframerate()} != {FS}")
        ch = w.getnchannels()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    return x.reshape(-1, ch).astype(np.float32) / 32768.0


def db(x: float) -> float:
    return 20 * np.log10(max(float(x), 1e-12))


def band_rms(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """按列做带通后的 RMS（FFT 域置零，够用且不用 scipy）。"""
    X = np.fft.rfft(x, axis=0)
    f = np.fft.rfftfreq(x.shape[0], 1 / FS)
    X[(f < lo) | (f > hi)] = 0
    y = np.fft.irfft(X, n=x.shape[0], axis=0)
    return np.sqrt(np.mean(y * y, axis=0))


def gcc_phat(a: np.ndarray, b: np.ndarray, maxlag: int, lo: float, hi: float) -> float:
    """返回 b 相对 a 的时延（正 = b 落后）。限带 + PHAT 加权。"""
    n = len(a)
    A = np.fft.rfft(a)
    B = np.fft.rfft(b)
    f = np.fft.rfftfreq(n, 1 / FS)
    R = A.conj() * B
    m = (f >= lo) & (f <= hi)
    w = np.zeros_like(f)
    mag = np.abs(R)
    ok = m & (mag > 1e-9)
    w[ok] = 1.0 / mag[ok]
    cc = np.fft.irfft(R * w, n=n)
    lags = np.arange(-maxlag, maxlag + 1)
    vals = np.array([cc[l % n] for l in lags])
    i = int(np.argmax(vals))
    frac = 0.0
    if 0 < i < len(vals) - 1:
        y0, y1, y2 = vals[i - 1], vals[i], vals[i + 1]
        den = y0 - 2 * y1 + y2
        if abs(den) > 1e-12:
            frac = float(np.clip(0.5 * (y0 - y2) / den, -1, 1))
    return float(lags[i] + frac)


def frame_windows(x: np.ndarray, n: int, hop: int):
    for i in range(0, len(x) - n, hop):
        yield i, x[i : i + n]


def shift_frac(x: np.ndarray, tau: float, lo: float, hi: float) -> np.ndarray:
    """整段信号的分数延迟（FFT 相位旋转）。tau>0 = 延后。"""
    n = len(x)
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(n, 1 / FS)
    band = (f >= lo) & (f <= hi)
    Y = np.zeros_like(X)
    Y[band] = X[band] * np.exp(-2j * np.pi * f[band] * tau / FS)
    return np.fft.irfft(Y, n=n)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("wav", type=Path)
    ap.add_argument("--band", nargs=2, type=float, default=[300.0, 3400.0])
    ap.add_argument("--maxlag", type=int, default=16, help="TDOA 搜索半径（样本）")
    ap.add_argument("--win", type=int, default=1024, help="GCC 分析窗（样本）")
    ap.add_argument("--hop", type=int, default=512)
    ap.add_argument("--floor-db", type=float, default=-45.0,
                    help="只统计超过该门限的活跃窗（默认 -45 dBFS）")
    ap.add_argument("--ref", type=int, default=-1, help="参考通道，-1=自动取最响")
    ap.add_argument("--skip", type=float, default=1.2, help="跳过开头 N 秒（ALSA 起播瞬态）")
    args = ap.parse_args()

    lo, hi = args.band
    x = load(args.wav)
    n_ch = x.shape[1]
    if args.skip > 0:
        x = x[int(args.skip * FS) :]
        print(f"跳过开头 {args.skip:g}s（起播瞬态：实测首帧会削顶）")
    if n_ch < MIC:
        raise SystemExit(f"需要至少 {MIC} 通道，实际 {n_ch}")

    print(f"文件 {args.wav}  {len(x) / FS:.2f}s  {n_ch}ch")
    full = np.sqrt(np.mean(x[:, :MIC] ** 2, axis=0))
    brms = band_rms(x[:, :MIC], lo, hi)
    for c in range(MIC):
        print(f"  ch{c}: 全带 {db(full[c]):7.1f} dBFS   {lo:.0f}-{hi:.0f}Hz {db(brms[c]):7.1f} dBFS")

    # 每个分析窗：是否活跃 + 相对参考通道的时延
    wins = []
    for i, w in frame_windows(x, args.win, args.hop):
        lvl = db(np.sqrt(np.mean(w[:, :MIC] ** 2)))
        wins.append((i, w, lvl))
    active_idx = [k for k, (_, _, lvl) in enumerate(wins) if lvl > args.floor_db]
    print(f"\n活跃窗（> {args.floor_db} dBFS）：{len(active_idx)}/{len(wins)}")

    # 参考通道：默认取活跃窗里最响的一路
    if args.ref >= 0:
        ref = args.ref
    else:
        ref = int(np.argmax(brms))

    pairs = [(ref, c) for c in range(MIC) if c != ref]
    est: dict[tuple[int, int], list[float]] = {p: [] for p in pairs}
    per_win_taus: dict[int, dict[int, float]] = {}
    for k in active_idx:
        _, w, _ = wins[k]
        seg = w * np.hanning(len(w))[:, None]
        taus = {ref: 0.0}
        for a, b in pairs:
            t = gcc_phat(seg[:, a], seg[:, b], args.maxlag, lo, hi)
            est[(a, b)].append(t)
            taus[b] = t
        per_win_taus[k] = taus

    print(f"\n成对 TDOA（参考 ch{ref}，正=该路落后；1 sample = {SAMPLE_MM:.1f} mm）")
    print(f"{'对':>8} {'中位数':>8} {'IQR':>10} {'p5..p95':>16} {'|τ|≤6占比':>10}")
    for (a, b), v in est.items():
        v = np.array(v)
        if not len(v):
            print(f"  {a}->{b}  无活跃窗")
            continue
        q5, q95 = np.percentile(v, [5, 95])
        print(
            f"  ch{a}->ch{b} {np.median(v):8.2f} {np.subtract(*np.percentile(v, [75, 25])):10.2f}"
            f" {q5:7.2f}..{q95:6.2f} {np.mean(np.abs(v) <= 6) * 100:9.0f}%"
        )
    allt = np.concatenate([np.abs(np.array(v)) for v in est.values() if len(v)])
    if len(allt):
        p99 = np.percentile(allt, 99)
        print(f"\n  |TDOA| p99 = {p99:.2f} samples ≈ {p99 * SAMPLE_MM:.0f} mm 路程差"
              f" → 阵列口径 ≳ {p99 * SAMPLE_MM:.0f} mm")

    # ---- 通道间相干（噪声场反推间距）：噪声场散射时相干 ≈ sinc(2πfd/c)
    seg = x[:, :MIC]
    nfft = 2048
    F = np.array([np.fft.rfft(seg[i : i + nfft].T * np.hanning(nfft), axis=1)
                  for i in range(0, len(seg) - nfft, nfft // 2)])
    Sxx = np.mean(np.abs(F) ** 2, axis=0) + 1e-20
    Sxy = np.mean(F[:, None, 0, :].conj() * F, axis=0)
    coh = np.abs(Sxy) ** 2 / (Sxx[0][None, :] * Sxx)
    f = np.fft.rfftfreq(nfft, 1 / FS)
    print("\n通道间相干 |γ|²（对 ch0）")
    for name, bad in (("300-800Hz", (f >= 300) & (f < 800)),
                      ("800-1600Hz", (f >= 800) & (f < 1600)),
                      ("1.6-3.2kHz", (f >= 1600) & (f < 3200)),
                      ("3.2-6.4kHz", (f >= 3200) & (f < 6400))):
        vals = [f"ch{c}:{coh[c][bad].mean():.2f}" for c in range(1, MIC)]
        print(f"  {name:>11}  " + "  ".join(vals))

    # ---- 延迟求和上界：每窗用自身估计的时延对齐后相加，看相对单通道的 RSS 增益
    gains = []
    for k, taus in per_win_taus.items():
        _, w, _ = wins[k]
        seg = w[:, :MIC]
        # 符号：taus[c] 是「c 相对 ref 的落后量」，要对齐到 ref 必须**提前** τ
        aligned = np.stack([shift_frac(seg[:, c], -taus[c], lo, hi) for c in range(MIC)], axis=1)
        beam = aligned.mean(axis=1)
        p_beam = float(np.mean(beam**2))
        p_ref = float(np.mean(seg[:, ref] ** 2))
        if p_ref > 1e-12:
            gains.append(10 * np.log10((p_beam + 1e-20) / p_ref))
    if gains:
        g = np.array(gains)
        print(f"\n延迟求和（对齐后平均）相对 ch{ref} 的 RSS 增益："
              f"中位 {np.median(g):+.2f} dB  p25 {np.percentile(g, 25):+.2f}"
              f"  p75 {np.percentile(g, 75):+.2f}  （理论上限 {10 * np.log10(MIC):.1f} dB）")
        print("  注：相干源理想 20log10(6)=15.6dB，噪声完全不相干时 10log10(6)=7.8dB；"
              "实测落在两者之间，明显 >0 才说明阵列可用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())