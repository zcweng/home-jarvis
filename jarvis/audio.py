"""录音与播放。

两条硬约定：
1. 设备一律按**名字子串**匹配（`pick_device`），不写索引 —— 插拔耳机、换到 MacMini 都不用改代码
2. 播放必须**可打断**（`Speaker.stop()`）—— 这是"像理想同学"的地基，后面 SPEAKING 状态要能被 barge-in 打断

命令行自测：
    python -m jarvis.audio list              # 列出设备
    python -m jarvis.audio level -d 3        # 3 秒电平检测（判断麦克风权限/接法是否正常）
    python -m jarvis.audio loop -d 3         # 录 3 秒立刻回放（端到端自测）
    python -m jarvis.audio record -d 3 a.wav
    python -m jarvis.audio play a.wav
"""

from __future__ import annotations

import argparse
import queue
import sys
import threading
import time
import wave
from pathlib import Path
from typing import Literal

import numpy as np
import sounddevice as sd
import soxr

from .config import Settings

SampleRate = int
Kind = Literal["input", "output"]

# ASR / VAD 统一用 16k 单声道；设备原生多半是 48k，用 soxr 流式重采样
TARGET_RATE: SampleRate = 16000
BLOCK_SIZE = 1024


# --------------------------------------------------------------------------- 设备


def list_devices() -> None:
    default_in, default_out = sd.default.device
    print(f"{'idx':>3}  {'in':>2} {'out':>3}  {'rate':>6}  name")
    for idx, dev in enumerate(sd.query_devices()):
        mark = ""
        if idx == default_in:
            mark = "  ← 默认输入"
        elif idx == default_out:
            mark = "  ← 默认输出"
        print(
            f"{idx:>3}  {dev['max_input_channels']:>2} {dev['max_output_channels']:>3}"
            f"  {int(dev['default_samplerate']):>6}  {dev['name']}{mark}"
        )


def pick_device(hint: str, kind: Kind) -> tuple[int, str]:
    """按名字子串挑设备；找不到就回落到系统默认，并打印实际选了谁。"""
    devices = sd.query_devices()
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    candidates = [(i, d) for i, d in enumerate(devices) if d[key] > 0]

    if hint:
        for idx, dev in candidates:
            if hint.lower() in dev["name"].lower():
                return idx, dev["name"]
        for idx, dev in candidates:
            if dev["name"].lower() in hint.lower():
                return idx, dev["name"]

    default = sd.default.device[0 if kind == "input" else 1]
    if default is not None and default >= 0:
        for idx, dev in candidates:
            if idx == default:
                print(f"⚠️  没找到「{hint}」，回落到系统默认{kind}：{dev['name']}", file=sys.stderr)
                return idx, dev["name"]
    if not candidates:
        raise SystemExit(f"系统里没有任何可用的{kind}设备")
    idx, dev = candidates[0]
    print(f"⚠️  没找到「{hint}」，回落到 {dev['name']}", file=sys.stderr)
    return idx, dev["name"]


# --------------------------------------------------------------------------- WAV


def write_wav(path: str | Path, pcm: np.ndarray, sample_rate: SampleRate) -> None:
    """float32 [-1,1] 单声道 → 16bit PCM WAV。用标准库，不引入 soundfile。"""
    pcm = np.clip(pcm, -1.0, 1.0)
    data = (pcm * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(data.tobytes())


def read_wav(path: str | Path) -> tuple[np.ndarray, SampleRate]:
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise SystemExit(f"{path} 不是 16bit PCM WAV")
        n = w.getnframes()
        rate = w.getframerate()
        raw = w.readframes(n)
        ch = w.getnchannels()
    pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        pcm = pcm.reshape(-1, ch).mean(axis=1)
    return pcm, rate


# --------------------------------------------------------------------------- 录音


class Mic:
    """常开麦克风：回调线程把音频塞进队列，主线程取 16k 单声道块。

    用法：
        with Mic(settings.input_device) as mic:
            pcm = mic.record(3.0)          # 录 3 秒
            chunk = mic.read(timeout=1.0)  # 或按块流式取（给 VAD 用）
    """

    def __init__(self, device_hint: str = "", target_rate: SampleRate = TARGET_RATE) -> None:
        self.device = pick_device(device_hint, "input")
        self.target_rate = target_rate
        capture_rate = int(sd.query_devices(self.device[0])["default_samplerate"])
        self.capture_rate = capture_rate or 48000
        self._q: queue.Queue[np.ndarray] = queue.Queue(maxsize=256)
        self._stream: sd.InputStream | None = None
        self._resampler = soxr.ResampleStream(
            self.capture_rate, self.target_rate, 1, dtype="float32", quality="HQ"
        )
        self.overflow = 0

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        if status:
            self.overflow += 1
        try:
            self._q.put_nowait(indata[:, 0].copy())
        except queue.Full:
            self.overflow += 1

    def __enter__(self) -> Mic:
        self._stream = sd.InputStream(
            device=self.device[0],
            samplerate=self.capture_rate,
            channels=1,
            dtype="float32",
            blocksize=BLOCK_SIZE,
            callback=self._callback,
        )
        self._stream.start()
        print(
            f"🎤 {self.device[1]} @ {self.capture_rate}Hz → {self.target_rate}Hz",
            file=sys.stderr,
        )
        return self

    def __exit__(self, *exc) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def read(self, timeout: float = 1.0) -> np.ndarray | None:
        """取一块 16k 单声道 float32；超时返回 None。

        注意：soxr 的流式重采样器有初始延迟，头几个 48k 块可能产出 0 个样本，
        所以这里在拿到非空结果前继续等，避免把空数组传给下游。
        """
        deadline = time.monotonic() + timeout
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                return None
            try:
                chunk = self._q.get(timeout=remain)
            except queue.Empty:
                return None
            out = self._resampler.resample_chunk(chunk)
            if out.size:
                return out

    def record(self, seconds: float) -> np.ndarray:
        """阻塞录固定时长（自测用；正式链路走 VAD 断句）。"""
        need = int(seconds * self.target_rate)
        chunks: list[np.ndarray] = []
        got = 0
        while got < need:
            c = self.read(timeout=2.0)
            if c is None:
                break
            chunks.append(c)
            got += c.size
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks)[:need]

    def drain(self) -> None:
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                return


def rms(pcm: np.ndarray) -> float:
    if pcm.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(pcm))))


def dbfs(pcm: np.ndarray) -> float:
    r = rms(pcm)
    return -120.0 if r <= 0 else 20.0 * np.log10(r)


# --------------------------------------------------------------------------- 播放


class Speaker:
    """可打断播放。

    stop() 会 abort 正在写的流，把"正在说话"这件事瞬间掐断 ——
    打断（barge-in）和"会话被打断后继承上下文"都靠它。
    """

    def __init__(self, device_hint: str = "") -> None:
        self.device = pick_device(device_hint, "output")
        self._stop = threading.Event()
        self._stream: sd.OutputStream | None = None
        self._lock = threading.Lock()
        self._aborted = False

    def play(
        self,
        pcm: np.ndarray,
        sample_rate: SampleRate,
        blocking: bool = True,
    ) -> threading.Thread | None:
        """播放 float32 单声道。blocking=False 时后台播，返回线程。"""
        if pcm.size == 0:
            return None
        self.stop()
        self._stop.clear()
        self._aborted = False

        def _run() -> None:
            stream = sd.OutputStream(
                device=self.device[0],
                samplerate=sample_rate,
                channels=1,
                dtype="float32",
                blocksize=BLOCK_SIZE,
            )
            with self._lock:
                self._stream = stream
            try:
                stream.start()
                for i in range(0, pcm.size, BLOCK_SIZE):
                    if self._stop.is_set():
                        break
                    chunk = pcm[i : i + BLOCK_SIZE]
                    if chunk.size < BLOCK_SIZE:
                        chunk = np.pad(chunk, (0, BLOCK_SIZE - chunk.size))
                    try:
                        stream.write(chunk.reshape(-1, 1))
                    except sd.PortAudioError:
                        # abort() 会让正在阻塞的 write 抛 -9986；这是打断的正常路径
                        if self._stop.is_set():
                            break
                        raise
            except sd.PortAudioError as e:
                if not self._stop.is_set():
                    print(f"⚠️  播放失败：{e}", file=sys.stderr)
            finally:
                with self._lock:
                    self._stream = None
                try:
                    # abort 过的流不要再 stop()（会等缓冲排空，白白多几百毫秒）
                    if not self._aborted:
                        stream.stop()
                    stream.close()
                except Exception:  # noqa: BLE001  已被 abort 的流再关会抛
                    pass

        if blocking:
            _run()
            return None
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        """打断当前播放（无播放时是空操作）。"""
        self._stop.set()
        with self._lock:
            stream = self._stream
        if stream is not None:
            self._aborted = True
            try:
                stream.abort()
            except Exception:  # noqa: BLE001
                pass

    @property
    def playing(self) -> bool:
        return self._stream is not None


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    settings = Settings.load()
    ap = argparse.ArgumentParser(prog="jarvis.audio", description="录音/播放自测")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出音频设备")

    for name, hlp in [
        ("level", "电平检测（判断麦克风是否真的在收音）"),
        ("loop", "录 N 秒立刻回放"),
        ("record", "录 N 秒存 wav"),
    ]:
        p = sub.add_parser(name, help=hlp)
        p.add_argument("-d", "--duration", type=float, default=3.0, help="秒数")
        if name == "record":
            p.add_argument("path", nargs="?", default="debug/rec.wav")

    p = sub.add_parser("play", help="播放 wav")
    p.add_argument("path")

    args = ap.parse_args(argv)

    if args.cmd == "list":
        list_devices()
        return 0

    if args.cmd == "level":
        with Mic(settings.input_device) as mic:
            print(f"测 {args.duration:g} 秒，对着麦克风正常说话…", file=sys.stderr)
            t0 = time.time()
            peak_all = 0.0
            while time.time() - t0 < args.duration:
                chunk = mic.read(timeout=1.0)
                if chunk is None or chunk.size == 0:
                    continue
                db = dbfs(chunk)
                peak_all = max(peak_all, float(np.abs(chunk).max()))
                bar = "█" * max(0, int((db + 60) / 3))
                print(f"  {db:6.1f} dBFS  {bar}")
            if peak_all < 1e-4:
                print(
                    "\n❌ 全程静音（peak≈0）。八成是麦克风权限没给："
                    "系统设置 → 隐私与安全性 → 麦克风，勾上你用的终端；"
                    "或耳机 TRRS 是 OMTP 接法不兼容。",
                    file=sys.stderr,
                )
                return 2
            print(f"\n✅ 收音正常（peak={peak_all:.3f}）", file=sys.stderr)
        return 0

    if args.cmd == "record":
        Path(args.path).parent.mkdir(parents=True, exist_ok=True)
        with Mic(settings.input_device) as mic:
            print(f"开始录音 {args.duration:g} 秒…", file=sys.stderr)
            pcm = mic.record(args.duration)
        write_wav(args.path, pcm, TARGET_RATE)
        print(f"✅ 写入 {args.path}（{pcm.size / TARGET_RATE:.2f}s, {dbfs(pcm):.1f} dBFS）")
        return 0

    if args.cmd == "loop":
        with Mic(settings.input_device) as mic:
            print(f"开始录音 {args.duration:g} 秒…", file=sys.stderr)
            pcm = mic.record(args.duration)
        print(f"录音 {pcm.size / TARGET_RATE:.2f}s，{dbfs(pcm):.1f} dBFS，开始回放", file=sys.stderr)
        Speaker(settings.output_device).play(pcm, TARGET_RATE)
        print("✅ 回放完毕")
        return 0

    if args.cmd == "play":
        pcm, rate = read_wav(args.path)
        print(f"播放 {args.path}（{pcm.size / rate:.2f}s @ {rate}Hz）", file=sys.stderr)
        Speaker(settings.output_device).play(pcm, rate)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
