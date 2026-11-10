#!/usr/bin/env python3
"""波束成形离线 A/B：在同一份 6 麦原始录音上比较各条选路/加权策略。

存在的理由：设备端实现之前，必须先回答「波束成形到底能提多少」。
指标分两层：

  · **SNR**（语音段 RMS − 停顿段 RMS）：算法本身的物理增益
  · **CER**（SenseVoice 转写 vs 参考文本）：真正影响「耳朵灵不灵」的东西

对比的策略：

  ch0..ch5   单通道（固定一路）
  loudest    **当前实现**：每 10ms 帧取能量最大的一路（无平滑、无交叉淡化）
  loudest500 文档 V1：每 500ms 重选一次 + 20ms 交叉淡化
  avg6       六路直接平均（不对齐）
  beam       波束成形：GCC-PHAT 估 TDOA → 分数延迟对齐 → 加权求和
  oracle     每帧用互相关找最佳对齐（不依赖 PHAT，作为上界参考）

用法：

    .venv/bin/python scripts/s12_beam_eval.py /tmp/cap_speech.wav \
        --text "把客厅的灯打开。然后把亮度调到百分之六十。再帮我查一下明天的天气怎么样。"
    .venv/bin/python scripts/s12_beam_eval.py /tmp/cap_speech.wav --no-asr
"""

from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FS = 16000
MIC = 6


# --------------------------------------------------------------------------- 基础

def load(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != FS:
            raise SystemExit(f"采样率 {w.getframerate()} != {FS}")
        ch = w.getnchannels()
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype="<i2").reshape(-1, ch).astype(np.float32)[:, :MIC] / 32768.0


def dbfs(x: np.ndarray) -> float:
    return 20 * np.log10(max(float(np.sqrt(np.mean(np.square(x)))), 1e-12))


def frame_energy(x: np.ndarray, hop: int = 160) -> np.ndarray:
    """逐帧 RMS。1-D → (n,)；2-D (n,ch) → (n,ch)。"""
    one_d = x.ndim == 1
    if one_d:
        x = x[:, None]
    n = len(x) // hop
    seg = x[: n * hop].reshape(n, hop, -1)
    e = np.sqrt(np.mean(np.square(seg), axis=1))
    return e[:, 0] if one_d else e


def vad_mask(x: np.ndarray, hop: int = 160, rel_db: float = -18.0) -> np.ndarray:
    """能量 VAD：以 95 分位能量为「语音」参照，向下 rel_db 作门限。"""
    e = frame_energy(x, hop)
    e_db = 20 * np.log10(e + 1e-12)
    thr = np.percentile(e_db, 95) + rel_db
    mask = e_db > thr
    # 收尾：补齐 5 帧空洞、去掉孤立单帧
    k = 5
    pad = np.pad(mask.astype(int), (k, k), mode="edge")
    sm = np.convolve(pad, np.ones(2 * k + 1), mode="same")[k:-k]
    return sm > k * 0.6


# --------------------------------------------------------------------------- 时延

def gcc_phat_tau(a: np.ndarray, b: np.ndarray, maxlag: int = 8,
                 band: tuple[float, float] = (300.0, 3500.0)) -> tuple[float, float]:
    """a/b 等长；返回 (b 相对 a 的时延, 峰值锐度)。正 = b 落后。"""
    n = len(a)
    A = np.fft.rfft(a * np.hanning(n))
    B = np.fft.rfft(b * np.hanning(n))
    f = np.fft.rfftfreq(n, 1 / FS)
    R = A.conj() * B
    band_m = (f >= band[0]) & (f <= band[1])
    mag = np.abs(R)
    w = np.zeros_like(f)
    ok = band_m & (mag > 1e-12)
    w[ok] = 1.0 / mag[ok]
    cc = np.fft.irfft(R * w, n=n)
    lags = np.arange(-maxlag, maxlag + 1)
    vals = cc[lags % n]
    i = int(np.argmax(vals))
    frac = 0.0
    if 0 < i < len(vals) - 1:
        y0, y1, y2 = vals[i - 1], vals[i], vals[i + 1]
        den = y0 - 2 * y1 + y2
        if abs(den) > 1e-12:
            frac = float(np.clip(0.5 * (y0 - y2) / den, -1, 1))
    peak = float(vals[i])
    rest = np.delete(np.abs(vals), slice(max(0, i - 2), i + 3))
    ratio = peak / (rest.mean() + 1e-12) if rest.size else 0.0
    return float(lags[i] + frac), ratio


def estimate_taus(x: np.ndarray, win: int, hop: int, maxlag: int,
                  band: tuple[float, float], ref: int) -> tuple[np.ndarray, np.ndarray]:
    """逐帧估计各通道相对 ref 的时延。返回 (taus[frames, MIC], peak_ratio[frames])。"""
    n_frames = max(1, (len(x) - win) // hop + 1)
    taus = np.zeros((n_frames, MIC), np.float64)
    ratios = np.zeros(n_frames, np.float64)
    for k in range(n_frames):
        c = k * hop + win // 2
        i0 = max(0, min(len(x) - win, c - win // 2))
        seg = x[i0 : i0 + win]
        rr = []
        for m in range(MIC):
            if m == ref:
                taus[k, m] = 0.0
                rr.append(99.0)
                continue
            t, r = gcc_phat_tau(seg[:, ref], seg[:, m], maxlag, band)
            taus[k, m] = t
            rr.append(r)
        ratios[k] = min(rr)
    return taus, ratios


def smooth_taus(taus: np.ndarray, ratios: np.ndarray, min_ratio: float = 2.0) -> np.ndarray:
    """坏帧（峰值不锐）沿用上一帧，再做 5 帧中值 + EMA 平滑。"""
    out = taus.copy()
    last = np.zeros(MIC)
    for k in range(len(out)):
        if ratios[k] < min_ratio:
            out[k] = last
        else:
            last = out[k]
    # 中值滤波
    med = out.copy()
    for k in range(len(out)):
        lo, hi = max(0, k - 2), min(len(out), k + 3)
        med[k] = np.median(out[lo:hi], axis=0)
    # EMA
    alpha = 0.35
    ema = med.copy()
    for k in range(1, len(ema)):
        ema[k] = alpha * med[k] + (1 - alpha) * ema[k - 1]
    return ema


def apply_frac_delay(x: np.ndarray, delays: np.ndarray, hop: int, pre: int = 8) -> np.ndarray:
    """按每个 hop 块的时延做**分数延迟**（线性插值）。

    delays[k] > 0 = 整段往后推。设备端会用整数+小数抽头实现，这里用线性插值等价近似。
    """
    n = len(x)
    t = np.arange(n, dtype=np.float64)
    # 把每块的延迟展开成逐样本延迟（块内常数，边界外推）
    idx = np.minimum((t / hop).astype(np.int64), len(delays) - 1)
    d = delays[idx]
    # 加 pre 保证 d≥0，最后统一裁掉
    d = d + pre
    src = t - d
    i0 = np.floor(src).astype(np.int64)
    f = src - i0
    xp = np.concatenate([np.zeros(pre + 4, np.float32), x, np.zeros(4, np.float32)])
    i0 = np.clip(i0 + pre + 4, 0, len(xp) - 2)
    y = (1 - f) * xp[i0] + f * xp[i0 + 1]
    return y[pre:]


# --------------------------------------------------------------------------- 策略

def strat_loudest(x: np.ndarray, hop: int = 160) -> np.ndarray:
    """当前实现：逐帧取能量最大的一路。"""
    e = frame_energy(x, hop)
    n = x.shape[1]
    e = e[: len(x) // hop, :n]
    pick = np.argmax(e, axis=1)
    out = np.zeros(len(x), np.float32)
    for k in range(len(pick)):
        out[k * hop : (k + 1) * hop] = x[k * hop : (k + 1) * hop, pick[k]]
    out[len(pick) * hop :] = x[len(pick) * hop :, pick[-1] if len(pick) else 0]
    return out


def strat_loudest_slow(x: np.ndarray, hop: int = 160, block_ms: int = 500,
                       fade_ms: int = 20) -> np.ndarray:
    """文档 V1：按 block_ms 统计选路 + 交叉淡化。"""
    e = frame_energy(x, hop)
    n = x.shape[1]
    blk = max(1, block_ms // 10)
    picks = []
    for k0 in range(0, len(e), blk):
        picks.append(int(np.argmax(e[k0 : k0 + blk, :n].mean(axis=0))))
    out = np.zeros(len(x), np.float32)
    fade = max(1, fade_ms * FS // 1000)
    cur = picks[0]
    for k, p in enumerate(picks):
        a = k * blk * hop
        b = min(len(x), a + blk * hop)
        if b <= a:
            break
        if p != cur:
            # 在块边界做交叉淡化
            nfade = min(fade, b - a)
            if a > 0 and nfade > 1:
                w = np.linspace(0, 1, nfade, dtype=np.float32)
                out[a : a + nfade] = (
                    x[a : a + nfade, cur] * (1 - w) + x[a : a + nfade, p] * w
                )
                out[a + nfade : b] = x[a + nfade : b, p]
            else:
                out[a:b] = x[a:b, p]
            cur = p
        else:
            out[a:b] = x[a:b, cur]
    return out


def strat_loudest_band(x: np.ndarray, hop: int = 160, block_ms: int = 0,
                       fade_ms: int = 20) -> np.ndarray:
    """按**语音带内**能量选路（默认逐帧；block_ms>0 时按块选 + 交叉淡化）。

    当前实现用的是全带能量，而实测低频轰鸣（50~300Hz）能占到总功率的一半，
    于是「最响」经常等于「最轰」——选出来的是低频噪声最大的那一路。
    """
    bp = bandpass(x, 300, 3400)
    e = frame_energy(bp, hop)
    if block_ms <= 0:
        pick = np.argmax(e, axis=1)
        out = np.zeros(len(x), np.float32)
        for k in range(len(pick)):
            out[k * hop : (k + 1) * hop] = x[k * hop : (k + 1) * hop, pick[k]]
        return out
    blk = max(1, block_ms // 10)
    picks = [int(np.argmax(e[k0 : k0 + blk].mean(axis=0))) for k0 in range(0, len(e), blk)]
    out = np.zeros(len(x), np.float32)
    fade = max(1, fade_ms * FS // 1000)
    cur = picks[0]
    for k, pch in enumerate(picks):
        a = k * blk * hop
        b = min(len(x), a + blk * hop)
        if b <= a:
            break
        if pch != cur:
            nf = min(fade, b - a)
            if a > 0 and nf > 1:
                w = np.linspace(0, 1, nf, dtype=np.float32)
                out[a : a + nf] = x[a : a + nf, cur] * (1 - w) + x[a : a + nf, pch] * w
                out[a + nf : b] = x[a + nf : b, pch]
            else:
                out[a:b] = x[a:b, pch]
            cur = pch
        else:
            out[a:b] = x[a:b, cur]
    return out


def strat_avg(x: np.ndarray) -> np.ndarray:
    return x.mean(axis=1)


def _gain_equalize(x: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """按语音段的带内 RMS 把各通道拉到同一水平（只做整体增益，不改波形）。"""
    band = bandpass(x, 300, 3400)
    sel = band[mask.repeat(160)[: len(band)] > 0]
    if not len(sel):
        return x
    rms = np.sqrt(np.mean(sel**2, axis=0))
    ref = np.median(rms)
    g = np.clip(ref / (rms + 1e-12), 10 ** (-6 / 20), 10 ** (6 / 20))
    return x * g[None, :]


def bandpass(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    X = np.fft.rfft(x, axis=0)
    f = np.fft.rfftfreq(x.shape[0], 1 / FS)
    X[(f < lo) | (f > hi)] = 0
    return np.fft.irfft(X, n=x.shape[0], axis=0)


def strat_beam(x: np.ndarray, win: int = 1024, hop: int = 160, update_every: int = 10,
               maxlag: int = 8, band: tuple[float, float] = (300.0, 3500.0),
               ref: int = -1, equalize: bool = True, mask: np.ndarray | None = None
               ) -> tuple[np.ndarray, np.ndarray]:
    """波束成形：估 TDOA → 分数延迟对齐 → 等增益求和。"""
    if ref < 0:
        bp = bandpass(x, *band)
        ref = int(np.argmax(np.sqrt(np.mean(bp**2, axis=0))))
    taus, ratios = estimate_taus(x, win, hop, maxlag, band, ref)
    taus = smooth_taus(taus, ratios)
    if update_every > 1:
        k = np.arange(len(taus))
        taus = taus[(k // update_every) * update_every]
    xin = _gain_equalize(x, mask if mask is not None else np.ones(len(x) // hop, bool))
    # 以 ref 对齐其它通道：c 落后 τ 就提前 τ
    aligned = np.stack(
        [apply_frac_delay(xin[:, m], -taus[:, m], hop) for m in range(MIC)], axis=1
    )
    return aligned.mean(axis=1), taus


def strat_oracle(x: np.ndarray, win: int = 1024, hop: int = 160,
                 maxlag: int = 8) -> np.ndarray:
    """上界：每帧用互相关（不 PHAT）找最佳整数时延。"""
    if ref_ch := -1:
        pass
    bp = bandpass(x, 300, 3400)
    ref = int(np.argmax(np.sqrt(np.mean(bp**2, axis=0))))
    n_frames = max(1, (len(x) - win) // hop + 1)
    taus = np.zeros((n_frames, MIC))
    for k in range(n_frames):
        c = min(len(x) - win, k * hop)
        seg = bp[c : c + win]
        for m in range(MIC):
            if m == ref:
                continue
            best, bl = -1e30, 0
            for lag in range(-maxlag, maxlag + 1):
                if lag >= 0:
                    a, b = seg[lag:, ref], seg[: len(seg) - lag, m]
                else:
                    a, b = seg[: len(seg) + lag, ref], seg[-lag:, m]
                v = float(np.dot(a, b))
                if v > best:
                    best, bl = v, lag
            taus[k, m] = bl
    taus = smooth_taus(taus, np.full(n_frames, 99.0))
    aligned = np.stack([apply_frac_delay(x[:, m], -taus[:, m], hop) for m in range(MIC)], axis=1)
    return aligned.mean(axis=1)


# --------------------------------------------------------------------------- 指标

def cer(ref: str, hyp: str) -> float:
    ref = "".join(ch for ch in ref if ch not in "，。、！？,.!? \n")
    hyp = "".join(ch for ch in hyp if ch not in "，。、！？,.!? \n")
    if not ref:
        return 0.0
    d = np.zeros((len(ref) + 1, len(hyp) + 1), dtype=np.int32)
    d[:, 0] = np.arange(len(ref) + 1)
    d[0, :] = np.arange(len(hyp) + 1)
    for i in range(1, len(ref) + 1):
        for j in range(1, len(hyp) + 1):
            d[i, j] = min(
                d[i - 1, j] + 1,
                d[i, j - 1] + 1,
                d[i - 1, j - 1] + (ref[i - 1] != hyp[j - 1]),
            )
    return float(d[len(ref), len(hyp)]) / len(ref)


def snr_band(sig: np.ndarray, n0: float, n1: float) -> tuple[float, float, float]:
    """语音带内：整段 RMS / **同一变体**尾部静音段的 RMS。"""
    a = dbfs(bandpass(sig.reshape(-1, 1), 300, 3400))
    seg = sig[int(n0 * FS) : int(n1 * FS)]
    b = dbfs(bandpass(seg.reshape(-1, 1), 300, 3400))
    return a, b, a - b


def snr_of(sig: np.ndarray, mask: np.ndarray) -> tuple[float, float, float]:
    hop = 160
    n = min(len(mask), len(sig) // hop)
    sp = sig[: n * hop].reshape(n, hop)
    speech = sp[mask[:n]]
    pause = sp[~mask[:n]]
    if not len(pause) or not len(speech):
        return -120.0, -120.0, -120.0
    s = dbfs(speech)
    p = dbfs(pause)
    return s, p, s - p


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("wav", type=Path)
    ap.add_argument("--text", default="", help="参考文本（算 CER 用）")
    ap.add_argument("--no-asr", action="store_true")
    ap.add_argument("--skip", type=float, default=1.2, help="跳过开头 ALSA 起播瞬态")
    ap.add_argument("--show", default="", help="把指定变体写成 wav，逗号分隔")
    ap.add_argument("--noise-sec", nargs=2, type=float, default=None,
                    help="噪声参考窗口（秒），默认为文件最后 1.2s")
    args = ap.parse_args()

    x = load(args.wav)
    if args.skip > 0:
        x = x[int(args.skip * FS) :]
    print(f"{args.wav}  {len(x) / FS:.2f}s  6ch")

    hop = 160
    # VAD 以「最好的单通道」为准，避免选路策略影响语音段判定
    bp = bandpass(x, 300, 3400)
    ref = int(np.argmax(np.sqrt(np.mean(bp**2, axis=0))))
    mask = vad_mask(bp[:, ref], hop)
    print(f"参考通道 ch{ref}；语音帧 {mask.sum()}/{len(mask)} ({mask.mean() * 100:.0f}%)")

    # 噪声参考：文件尾部静音（一定没有说话声）
    if args.noise_sec:
        n0, n1 = args.noise_sec
    else:
        n0, n1 = max(0.0, len(x) / FS - 1.4), len(x) / FS - 0.1
    noise = x[int(n0 * FS) : int(n1 * FS), :]

    variants: dict[str, np.ndarray] = {}
    for c in range(MIC):
        variants[f"ch{c}"] = x[:, c]
    variants["loudest"] = strat_loudest(x, hop)
    variants["loudest500"] = strat_loudest_slow(x, hop)
    variants["loudestBand"] = strat_loudest_band(x, hop)
    variants["loudestB500"] = strat_loudest_band(x, hop, block_ms=500)
    variants["avg6"] = strat_avg(x)
    variants["beam"], taus = strat_beam(x, hop=hop, mask=mask)
    variants["oracle"] = strat_oracle(x, hop=hop)

    print("\n估计的 TDOA（相对 ch%d，每个 100ms 更新一次后的中位数）" % ref)
    for m in range(MIC):
        t = taus[:, m]
        print(f"  ch{m}: 中位 {np.median(t):+.2f}  p5..p95 {np.percentile(t,5):+.2f}..{np.percentile(t,95):+.2f} samples")

    print(f"\n噪声参考窗口 {n0:.2f}~{n1:.2f}s；语音带 300-3400Hz")
    print(f"{'变体':<12} {'语音段':>9} {'噪声差':>9} {'SNR':>8} {'Δvs loudest':>12}")
    base = None
    for name, sig in variants.items():
        sp, nz, snr = snr_band(sig, n0, n1)
        if name == "loudest":
            base = snr
        delta = "" if base is None else f"{snr - base:+.2f} dB"
        print(f"{name:<12} {sp:9.1f} {nz:9.1f} {snr:8.2f} {delta:>12}")

    if not args.no_asr:
        from jarvis.asr import SenseVoice

        asr = SenseVoice("models/sensevoice", num_threads=4, language="zh")
        print("\nASR（SenseVoice）")
        for name, sig in variants.items():
            txt, ms = asr.transcribe(sig)
            c = cer(args.text, txt) if args.text else float("nan")
            print(f"  {name:<12} CER={c * 100:5.1f}%  {ms:6.0f}ms  {txt}")

    if args.show:
        for name in args.show.split(","):
            if name not in variants:
                continue
            y = np.clip(variants[name], -1, 1)
            out = args.wav.with_name(f"{args.wav.stem}_{name}.wav")
            with wave.open(str(out), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(FS)
                w.writeframes((y * 32767).astype("<i2").tobytes())
            print(f"写出 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())