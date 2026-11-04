#!/usr/bin/env python3
"""A/B 试听：同一句 VITS vs Nano，弹窗记录偏好。

    ./scripts/nano_tts_server.sh start --bg
    .venv/bin/python scripts/ab_nano_vs_vits.py
"""

from __future__ import annotations

import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from jarvis.audio import Speaker  # noqa: E402
from jarvis.config import Settings  # noqa: E402
from jarvis.tts_nano import NanoTTS, probe_nano  # noqa: E402
from jarvis.tts_neural import SherpaTTS  # noqa: E402

TEXT = "老虎英文是 tiger，USB 插在左边。"


def _write_wav(path: Path, pcm: np.ndarray, sr: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes())


def main() -> int:
    import cue

    st = Settings.load()
    if not probe_nano(st.tts_nano_url):
        print("❌ nano 侧车未就绪：./scripts/nano_tts_server.sh start --bg", file=sys.stderr)
        return 1

    out = Path(tempfile.mkdtemp(prefix="ab-tts-"))
    vits = SherpaTTS(st.tts_model_dir, num_threads=st.tts_threads, sid=st.tts_sid, speed=st.tts_speed)
    nano = NanoTTS(st.tts_nano_url, ref=Path(st.tts_nano_ref_audio).stem, timeout=30)

    pcm_v, sr_v = vits.synthesize(TEXT)
    pcm_n, sr_n = nano.synthesize(TEXT)
    p_v, p_n = out / "vits.wav", out / "nano.wav"
    _write_wav(p_v, pcm_v, sr_v)
    _write_wav(p_n, pcm_n, sr_n)
    print(f"已生成：\n  A VITS  {p_v}\n  B Nano  {p_n}")

    spk = Speaker(st.output_device)

    def play(path: Path, label: str) -> None:
        cue.run_cue(f"即将播放 {label}\n\n{TEXT}", title="A/B 试听", ack_only=True, timeout=60)
        with wave.open(str(path), "rb") as w:
            sr = w.getframerate()
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
        spk.play(pcm, sr, blocking=True)

    play(p_v, "A · 本地 VITS（fanchen-C）")
    play(p_n, "B · MOSS-TTS-Nano（zh_1）")

    # 用 ack 对话框收集偏好：开始=偏 Nano，跳过=偏 VITS / 差不多
    print("弹窗：点【开始】=更喜欢 Nano；【跳过】=更喜欢 VITS 或差不多")
    rc = cue.run_cue(
        "听完了？\n\n【开始】= 更喜欢 Nano（中英混读）\n【跳过】= 更喜欢 VITS / 差不多",
        title="A/B 偏好",
        seconds=0.5,
        countdown_s=0,
        timeout=120,
    )
    if rc == cue.EXIT_UNCONFIRMED:
        pref = "vits_or_tie / 未确认跳过"
    elif rc == 0:
        pref = "nano"
    else:
        pref = f"unknown_rc={rc}"

    report = ROOT / "tools" / "nano-tts" / "bench_out" / "ab_preference.txt"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(f"text={TEXT}\npreference={pref}\nvits={p_v}\nnano={p_n}\n", encoding="utf-8")
    print(f"偏好记录：{pref} → {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
