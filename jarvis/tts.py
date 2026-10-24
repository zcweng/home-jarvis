"""语音合成。

P0 用 macOS 自带 `say`，两种模式各有用处：

| 模式 | 出声延迟 | 能否指定输出设备 | 打断方式 |
|---|---|---|---|
| `direct`（默认） | ~50ms（边合成边说） | 否，永远走系统默认输出 | 杀掉 say 进程 |
| `wav` | ~460ms（整段渲染完再播） | 是，走 Speaker 的设备名匹配 | Speaker.stop()（120ms） |

`say` 的固定开销全在进程启动上（实测长短句都是 ~0.46s），所以说话用 `direct` 最快；
需要把声音送到指定设备（比如日后投到小爱音箱）时才用 `wav`。

文本一律走 stdin（`say -f -`），避免以 `-` 开头的文本被当成命令行选项。

命令行自测：
    python -m jarvis.tts "你好，我是贾维斯"            # 直接说
    python -m jarvis.tts -m wav "渲染成 wav 再播"
    python -m jarvis.tts --list-voices                # 看有哪些中文音色
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

from .audio import Speaker, read_wav
from .config import ROOT, Settings

SAY = "/usr/bin/say"
SAMPLE_RATE = 24000
# 实测：新版「个人语音」类音色（Eddy / Flo / Reed / Sandy / Rocko / Grandma / Grandpa / Shelley）
# 在 say 下只渲染出 16ms 空音频（直接说话也只有 0.5s），必须用经典音色。
CLASSIC_VOICES = ("Tingting", "Meijia", "Sinji", "Ting-Ting")


def probe_voice(voice: str) -> float:
    """渲染一句短文本，返回音频时长（秒）。<0.2s 说明该音色在本机不可用。"""
    import tempfile
    from pathlib import Path as _P

    with tempfile.TemporaryDirectory() as d:
        out = _P(d) / "probe.wav"
        subprocess.run(
            [SAY, "-v", voice, "--file-format=WAVE", f"--data-format=LEI16@{SAMPLE_RATE}",
             "-o", str(out), "测试一下"],
            capture_output=True, timeout=30,
        )
        if not out.exists():
            return 0.0
        try:
            pcm, rate = read_wav(out)
        except Exception:  # noqa: BLE001
            return 0.0
        return pcm.size / rate if rate else 0.0


class SayTTS:
    """macOS `say` 合成器，支持打断。"""

    def __init__(
        self,
        voice: str = "Tingting",
        mode: str = "direct",
        speaker: Speaker | None = None,
        cache_dir: Path | None = None,
        self_check: bool = True,
        output_device: str = "",
        rate: int = 170,
    ) -> None:
        if mode not in ("direct", "wav"):
            raise ValueError(f"未知模式 {mode}，只支持 direct / wav")
        self.voice = voice
        self.mode = mode
        self._speaker = speaker
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._cache = cache_dir or (ROOT / "debug" / "tts_cache")
        self._out_device = output_device
        self.rate = rate
        self._cache.mkdir(parents=True, exist_ok=True)

        # 音色自检：静默失败比报错更可怕（听起来像"程序没反应"）
        if self_check and voice not in CLASSIC_VOICES:
            dur = probe_voice(voice)
            if dur < 0.2:
                fallback = CLASSIC_VOICES[0]
                print(
                    f"⚠️  音色「{voice}」在 say 下只渲染出 {dur * 1000:.0f}ms 音频（不可用），"
                    f"已自动切换到「{fallback}」。可在 .env 里改 TTS_VOICE_SAY。",
                    file=sys.stderr,
                )
                self.voice = fallback

    # ---------------------------------------------------------------- 内部

    def _cmd(self, out: Path | None = None) -> list[str]:
        cmd = [SAY, "-v", self.voice, "-r", str(self.rate)]
        if out is not None:
            cmd += ["--file-format=WAVE", f"--data-format=LEI16@{SAMPLE_RATE}", "-o", str(out)]
        cmd += ["-f", "-"]  # 从 stdin 读文本
        return cmd

    @staticmethod
    def _prosody(text: str, pause_ms: int = 200) -> str:
        """给句末加短停顿——`say` 的默认韵律是"一口气念完"，听着就假。

        `[[slnc N]]` 是 macOS say 内嵌的静音指令（实测有效：加两处 400/300ms 后
        同一句话时长从 4.41s 变成 5.14s）。
        """
        if pause_ms <= 0:
            return text
        import re

        return re.sub(r"([。！？!?；;])", rf"\1[[slnc {pause_ms}]]", text)

    def _cache_path(self, text: str) -> Path:
        key = hashlib.sha1(f"{self.voice}|{self.rate}|{text}".encode()).hexdigest()[:16]
        return self._cache / f"{key}.wav"

    # ---------------------------------------------------------------- 公开

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        """渲染成 PCM（带缓存），返回 (float32 单声道, 采样率)。"""
        out = self._cache_path(text)
        if not out.exists():
            tmp = out.with_suffix(".part.wav")
            subprocess.run(
                self._cmd(tmp), input=text.encode("utf-8"), check=True, timeout=30
            )
            tmp.replace(out)
        pcm, rate = read_wav(out)
        return pcm, rate

    def speak(self, text: str, blocking: bool = True) -> threading.Thread | None:
        """说话。blocking=False 时返回线程，可 stop() 打断。"""
        text = text.strip()
        if not text:
            return None
        text = self._prosody(text)
        self.stop()

        if self.mode == "direct":
            def _run() -> None:
                with self._lock:
                    if self._proc is not None and self._proc.poll() is None:
                        self._proc.terminate()
                    proc = subprocess.Popen(
                        self._cmd(), stdin=subprocess.PIPE, stderr=subprocess.DEVNULL
                    )
                    self._proc = proc
                try:
                    proc.communicate(input=text.encode("utf-8"), timeout=60)
                except subprocess.TimeoutExpired:
                    proc.kill()

            if blocking:
                _run()
                return None
            t = threading.Thread(target=_run, daemon=True)
            t.start()
            return t

        # wav 模式：渲染 → 走 Speaker（可指定设备）
        speaker = self._speaker
        if speaker is None:
            raise RuntimeError("wav 模式需要传入 Speaker 实例")

        def _run_wav() -> None:
            pcm, rate = self.synthesize(text)
            speaker.play(pcm, rate, blocking=True)

        if blocking:
            _run_wav()
            return None
        t = threading.Thread(target=_run_wav, daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        """打断当前说话。"""
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        if self._speaker is not None:
            self._speaker.stop()

    # ---------------------------------------------------------------- 首句先出声

    def warm(self, phrases: list[str]) -> int:
        """预渲染固定短语（如"好的"），让它们能 0ms 出声。返回预渲染条数。"""
        n = 0
        for ph in phrases:
            if not ph:
                continue
            if not self._cache_path(ph).exists():
                try:
                    self.synthesize(ph)
                    n += 1
                except Exception:  # noqa: BLE001
                    pass
        return n

    def play_cached(self, text: str, blocking: bool = False) -> float:
        """用 wav 缓存播一句固定话术（不受 750ms 冷启动影响）。返回合成耗时ms。"""
        t0 = time.perf_counter()
        pcm, rate = self.synthesize(text)
        synth_ms = (time.perf_counter() - t0) * 1000
        speaker = self._speaker
        if speaker is None:
            from .audio import Speaker as _Sp

            speaker = _Sp(self._out_device or "MacBook Pro扬声器")
            self._speaker = speaker
        speaker.play(pcm, rate, blocking=blocking)
        return synth_ms

    def wait_idle(self, timeout: float = 2.0) -> None:
        """等缓存播放结束，避免和紧接着的正式回答叠在一起。"""
        sp = self._speaker
        if sp is None:
            return
        t0 = time.perf_counter()
        while getattr(sp, "playing", False) and time.perf_counter() - t0 < timeout:
            time.sleep(0.02)

    @property
    def speaking(self) -> bool:
        with self._lock:
            proc = self._proc
        return proc is not None and proc.poll() is None


def list_voices() -> None:
    out = subprocess.run([SAY, "-v", "?"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if "zh_" in line:
            print(line)


def main(argv: list[str] | None = None) -> int:
    st = Settings.load()
    ap = argparse.ArgumentParser(prog="jarvis.tts", description="TTS 自测")
    ap.add_argument("text", nargs="?", default="你好，我是贾维斯，正在为你服务。")
    ap.add_argument("-m", "--mode", choices=["direct", "wav"], default=st.tts_mode)
    ap.add_argument("-v", "--voice", default=st.tts_voice_say)
    ap.add_argument("--list-voices", action="store_true")
    ap.add_argument("--measure", action="store_true", help="测首字延迟")
    args = ap.parse_args(argv)

    if args.list_voices:
        list_voices()
        return 0

    speaker = Speaker(st.output_device) if args.mode == "wav" else None
    tts = SayTTS(args.voice, args.mode, speaker)

    if args.measure:
        t0 = time.perf_counter()
        pcm, rate = tts.synthesize(args.text)
        print(f"渲染 {pcm.size / rate:.2f}s 音频，耗时 {(time.perf_counter() - t0) * 1000:.0f}ms")
        return 0

    print(f"模式={args.mode} 音色={args.voice} 文本={args.text}", file=sys.stderr)
    t0 = time.perf_counter()
    tts.speak(args.text)
    print(f"说完，总耗时 {(time.perf_counter() - t0) * 1000:.0f}ms", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
