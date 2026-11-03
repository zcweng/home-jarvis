"""网络音频端点：把 S12 音箱当成麦克风 + 喇叭用。

与 `jarvis.audio` 的 `Mic` / `Speaker` **鸭子类型完全对齐**，所以 `app.py`
与 `bargein.py` 一行不改就能切过来：

    Mic.read(timeout) -> np.ndarray|None   ← 16k 单声道 float32
    Mic.drain()
    Speaker.play(pcm, rate, blocking) -> Thread|None
    Speaker.stop()

协议见 `docs/S12-AEC数据流方案.md` §6：

  上行 设备→Mac : 裸流，**16k mono S16_LE**（不写帧头）
  下行 Mac→设备 : 8 字节帧头 {type, flags, reserved, length}

AEC / 通道选择 / 降噪都跑在设备端（`tools/s12audio/`）：
实测 16k 下 ch7 仍是播放 PCM 的逐样本数字拷贝（corr=1.000000, max|err|=0），
所以设备本地做 AEC 不需要把 8 通道裸流传上来（那是 3.9 Mbps）。
这样上行只有 256 kbps，并且省掉 Mac 侧每条链路的 6 路相干计算。
"""

from __future__ import annotations

import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass

import numpy as np
import soxr

# --------------------------------------------------------------------------- 协议常量

TARGET_RATE = 16000           # 与 jarvis.audio.TARGET_RATE 一致
FRAME_SAMPLES = 160           # 10ms @16k，与设备端 FRAME_SAMPLES 对应
FRAME_BYTES = FRAME_SAMPLES * 2

# 下行帧 type
F_PCM = 1
F_FLUSH = 2
F_VOLUME = 3
F_PING = 4

HDR = struct.Struct("<BBHI")

DEFAULT_HOST = "192.168.1.3"
DEFAULT_UP_PORT = 8765
DEFAULT_DOWN_PORT = 8766


def parse_endpoint(spec: str, default_port: int = DEFAULT_UP_PORT) -> tuple[str, int]:
    """解析 `192.168.1.3` / `tcp://192.168.1.3:8765` → (host, port)。"""
    raw = (spec or "").strip()
    if not raw:
        return DEFAULT_HOST, default_port
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    raw = raw.split("/", 1)[0]
    if ":" in raw:
        host, port_s = raw.rsplit(":", 1)
        try:
            return host.strip() or DEFAULT_HOST, int(port_s)
        except ValueError:
            return raw.strip() or DEFAULT_HOST, default_port
    return raw, default_port


def _now() -> float:
    return time.monotonic()


# --------------------------------------------------------------------------- 上行收流


@dataclass
class Capture:
    """一段上行音频。内容已经是设备端 AEC / 降噪后的 16k 单声道。"""

    mono: np.ndarray  # (n,) float32 [-1,1]


class UplinkReader:
    """连设备 8765，把 16k mono S16 字节流切成 float32 块。"""

    def __init__(self, host: str, port: int = 8765, rate: int = TARGET_RATE) -> None:
        self.host = host
        self.port = port
        self.rate = rate
        self._sock: socket.socket | None = None
        self._buf = bytearray()
        self.reconnects = 0

    # -- 连接

    def connect(self, timeout: float = 5.0) -> bool:
        self.close()
        try:
            s = socket.create_connection((self.host, self.port), timeout=timeout)
        except OSError as e:
            print(f"⚠️  上行连接失败 {self.host}:{self.port} — {e}", file=sys.stderr)
            return False
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.settimeout(0.05)  # 短超时，攒帧交给外层循环
        self._sock = s
        self._buf.clear()
        return True

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    @property
    def connected(self) -> bool:
        return self._sock is not None

    # -- 读

    def read_capture(self, timeout: float = 0.5, min_samples: int = 800) -> Capture | None:
        """攒够 min_samples（默认 800 = 50ms）就返回；超时返回已有的部分。

        min_samples/rate 必须明显小于 timeout，否则永远攒不满。
        """
        want = min_samples * 2
        deadline = _now() + timeout
        while len(self._buf) < want and _now() < deadline:
            if self._sock is None:
                if not self.connect():
                    time.sleep(0.5)
                    continue
                self.reconnects += 1
            try:
                chunk = self._sock.recv(262144)
            except socket.timeout:
                continue
            except OSError:
                self.close()
                continue
            if not chunk:
                self.close()
                continue
            self._buf.extend(chunk)

        n = len(self._buf) // 2
        if n == 0:
            return None
        usable = n * 2
        raw = bytes(self._buf[:usable])
        del self._buf[:usable]
        return Capture(mono=np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0)


# --------------------------------------------------------------------------- 下行发流


class DownlinkWriter:
    """连设备 8766 的下行音频通道，发 framed PCM / FLUSH / VOLUME。

    与上行分开连接：FLUSH 必须能立刻发出去，不能被上行的读阻塞拖住。
    """

    def __init__(self, host: str, port: int = 8766) -> None:
        self.host = host
        self.port = port
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()

    def connect(self, timeout: float = 3.0) -> bool:
        self.close()
        with self._lock:
            return self._connect_unlocked(timeout=timeout)

    def close(self) -> None:
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None

    def _send(self, ftype: int, payload: bytes = b"", flags: int = 0) -> bool:
        with self._lock:
            if self._sock is None:
                # connect 内部也会抢锁——这里走无锁版本
                if not self._connect_unlocked(timeout=3.0):
                    return False
            try:
                self._sock.sendall(HDR.pack(ftype, flags, 0, len(payload)) + payload)
                return True
            except OSError:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None
                return False

    def _connect_unlocked(self, timeout: float = 3.0) -> bool:
        if self._sock is not None:
            return True
        try:
            s = socket.create_connection((self.host, self.port), timeout=timeout)
        except OSError as e:
            print(f"⚠️  下行连接失败 {self.host}:{self.port} — {e}", file=sys.stderr)
            return False
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.settimeout(2.0)
        self._sock = s
        return True

    def pcm(self, samples: np.ndarray) -> bool:
        """发 16k mono float32 → S16LE。长度对齐到 10ms 的整数倍由调用方保证。"""
        s16 = np.clip(samples, -1.0, 1.0)
        data = (s16 * 32767.0).astype("<i2").tobytes()
        return self._send(F_PCM, data)

    def flush(self) -> bool:
        return self._send(F_FLUSH)

    def volume(self, db: float) -> bool:
        v = int(round(max(-60.0, min(0.0, db)) + 60.0))
        return self._send(F_VOLUME, bytes([v & 0xFF]))

    def ping(self) -> bool:
        return self._send(F_PING)


# --------------------------------------------------------------------------- 鸭子类型端点


class EndpointMic:
    """网络麦克风。接口与 `jarvis.audio.Mic` 一致。

    音频已经是设备端 AEC + 降噪后的 16k 单声道，这里不再做通道选择。
    """

    def __init__(
        self,
        host: str,
        port: int = 8765,
        rate: int = TARGET_RATE,
    ) -> None:
        self.reader = UplinkReader(host, port, rate)
        self.target_rate = TARGET_RATE
        self.overflow = 0
        self.frames = 0
        self._pending: list[np.ndarray] = []

    def _process(self, cap: Capture) -> np.ndarray:
        return cap.mono

    # -- 上下文管理（让 `with EndpointMic(...)` 与 `with Mic(...)` 一致）

    def __enter__(self) -> EndpointMic:
        if not self.reader.connect():
            print("⚠️  上行未连上，会在首次 read() 时重试", file=sys.stderr)
        return self

    def __exit__(self, *exc) -> None:
        self.reader.close()

    # -- 对上接口

    def read(self, timeout: float = 1.0) -> np.ndarray | None:
        if self._pending:
            return self._pending.pop(0)
        cap = self.reader.read_capture(timeout=timeout)
        if cap is None:
            return None
        self.frames += 1
        return cap.mono

    def record(self, seconds: float) -> np.ndarray:
        """阻塞录固定时长。直接攒块，避免 read() 的额外开销导致设备 ring 溢出。"""
        need = int(seconds * self.target_rate)
        chunks: list[np.ndarray] = []
        got = 0
        deadline = _now() + seconds + 3.0
        while got < need and _now() < deadline:
            cap = self.reader.read_capture(timeout=0.3)
            if cap is None:
                continue
            self.frames += 1
            chunks.append(cap.mono)
            got += cap.mono.size
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks)[:need]

    def drain(self) -> None:
        """丢掉积压。设备是连续推流，读空一小段时间即可。"""
        self._pending.clear()
        t_end = _now() + 0.08
        while _now() < t_end:
            cap = self.reader.read_capture(timeout=0.02)
            if cap is None:
                break

    @property
    def stats(self) -> str:
        return f"frames={self.frames} reconnects={self.reader.reconnects}"


class EndpointSpeaker:
    """网络喇叭。接口与 `jarvis.audio.Speaker` 一致。"""

    def __init__(
        self,
        host: str,
        port: int = 8766,
        *,
        peak_db: float = -12.0,
        prebuffer_ms: float = 250.0,
        lead_ms: float = 150.0,
    ) -> None:
        self.writer = DownlinkWriter(host, port)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._resampler: soxr.ResampleStream | None = None
        # 设备 play_ring ≈640ms：先突发灌缓冲，再保持略超前，避免严格实时节流一抖就 underrun 卡顿
        self._pace = True
        self.prebuffer_ms = prebuffer_ms
        self.lead_ms = lead_ms
        # 下行峰值归一到 peak_db（默认 -12dBFS）。神经 TTS 平均只有 -27dBFS，不抬一下远场听不清。
        self.peak_db = peak_db

    def play(
        self,
        pcm: np.ndarray,
        sample_rate: int,
        blocking: bool = True,
    ) -> threading.Thread | None:
        if pcm.size == 0:
            return None
        # 仅在真正打断时 FLUSH。每次 play 开头都 FLUSH 会 snd_pcm_drop，
        # 设备端若未 prepare，后续 writei 会一直失败 → ch7 全 0、完全无声。
        if self.playing:
            self.stop()
        self._stop.clear()

        # 任何采样率（TTS 常见 22050/24000）统一重采样到 16k 再发
        if sample_rate != TARGET_RATE:
            pcm = soxr.resample(pcm, sample_rate, TARGET_RATE)

        if self.peak_db is not None:
            peak = float(np.max(np.abs(pcm))) if pcm.size else 0.0
            if peak > 1e-6:
                target = float(10.0 ** (self.peak_db / 20.0))
                pcm = np.clip(pcm * (target / peak), -1.0, 1.0).astype(np.float32)

        def _run() -> None:
            block = 640  # 40ms @16k，减少 syscall / 调度抖动
            prebuffer = int(self.prebuffer_ms * TARGET_RATE / 1000.0)
            lead = self.lead_ms / 1000.0
            t0 = time.perf_counter()
            sent = 0
            for i in range(0, pcm.size, block):
                if self._stop.is_set():
                    return
                chunk = pcm[i : i + block]
                if chunk.size < block:
                    chunk = np.pad(chunk, (0, block - chunk.size))
                if not self.writer.pcm(chunk):
                    print("⚠️  下行发送失败，放弃本次播放", file=sys.stderr)
                    return
                sent += chunk.size
                if not self._pace or sent < prebuffer:
                    continue
                # 保持 lead 秒超前：吸收 Mac 调度抖动，又不超过设备 ~640ms ring
                due = t0 + sent / TARGET_RATE - lead
                delay = due - time.perf_counter()
                if delay > 0.002:
                    time.sleep(delay)

        if blocking:
            _run()
            return None
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        self._thread = t
        return t

    def stop(self) -> None:
        """打断当前播放。

        仅在「确实有在播」时发 FLUSH。SherpaTTS/SayTTS 每次 speak 开头都会 stop()，
        若空闲也 FLUSH → 设备 snd_pcm_drop 后旧二进制若不 prepare，之后 writei 全失败，
        Mac 侧仍显示播了 2s，音箱却完全无声。
        """
        was_playing = self.playing
        self._stop.set()
        if was_playing:
            self.writer.flush()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._thread = None

    @property
    def playing(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def close(self) -> None:
        # 正常播完不要 FLUSH，让设备 ring 自行排空；只有打断才走 stop()
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._thread = None
        self.writer.close()


# --------------------------------------------------------------------------- CLI


def _tone(seconds: float = 2.0, freq: float = 1000.0, db: float = -12.0) -> np.ndarray:
    """生成 16k mono 正弦，默认 -12 dBFS（联调听感清晰、不易削顶）。"""
    n = int(seconds * TARGET_RATE)
    t = np.arange(n, dtype=np.float32) / TARGET_RATE
    amp = float(10.0 ** (db / 20.0))
    return (amp * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)


def _load_wav_mono(path: str) -> tuple[np.ndarray, int]:
    import wave

    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        nch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw == 2:
        pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif sw == 4:
        pcm = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise SystemExit(f"不支持的采样宽度 {sw}")
    if nch > 1:
        pcm = pcm.reshape(-1, nch).mean(axis=1)
    return pcm.astype(np.float32), rate


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="jarvis.endpoint", description="S12 网络音频端点自测")
    ap.add_argument("host", nargs="?", default="192.168.1.3")
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--play", action="store_true", help="录完立刻播回去")
    ap.add_argument("--tone", action="store_true", help="只播一段 1kHz 测试音（下行联调）")
    ap.add_argument("--wav", metavar="PATH", help="播放本地 wav（自动重采样到 16k）")
    ap.add_argument("--freq", type=float, default=1000.0, help="--tone 频率 Hz")
    ap.add_argument("--db", type=float, default=-12.0, help="--tone 幅度 dBFS")
    ap.add_argument("--vol", type=float, default=None, help="先发 VOLUME（dB，0=满，-60=静音）")
    args = ap.parse_args(argv)

    if args.tone or args.wav:
        if args.wav:
            pcm, rate = _load_wav_mono(args.wav)
            print(f"播 wav {args.wav}  {pcm.size / rate:.2f}s @ {rate}Hz")
        else:
            pcm = _tone(args.seconds, args.freq, args.db)
            rate = TARGET_RATE
            print(f"播 tone {args.freq:g}Hz {args.db:g}dBFS  {args.seconds:g}s")
        spk = EndpointSpeaker(args.host)
        if not spk.writer.connect():
            print("❌ 连不上下行 8766")
            return 1
        if args.vol is not None:
            spk.writer.volume(args.vol)
            print(f"VOLUME {args.vol:g} dB")
        # play() 已按实时节流；阻塞返回时 PCM 已基本推完，再留一点给设备预缓冲排空
        spk.play(pcm, rate, blocking=True)
        time.sleep(0.4)
        spk.close()
        print("✅ 播放完毕")
        return 0

    mic = EndpointMic(args.host)
    with mic:
        print(f"录 {args.seconds:g} 秒…")
        pcm = mic.record(args.seconds)
    if pcm.size == 0:
        print("❌ 没收到音频")
        return 1

    r = float(np.sqrt(np.mean(pcm**2)))
    db = -120.0 if r <= 0 else 20 * np.log10(r)
    print(f"✅ {pcm.size / TARGET_RATE:.2f}s  {db:.1f} dBFS  peak={np.abs(pcm).max():.3f}")
    print(f"   {mic.stats}")

    if args.play:
        spk = EndpointSpeaker(args.host)
        print("播回去…")
        spk.play(pcm, TARGET_RATE)
        spk.close()
        print("✅ 播放完毕")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())