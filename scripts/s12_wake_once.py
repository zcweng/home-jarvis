#!/usr/bin/env python3
"""S12 唤醒词联调：cue 引导的三步。

  ① 只说「贾维斯」→ KWS 命中
  ② 免唤醒说「关掉吸顶灯」→ DeepSeek + HA
  ③ 说「退下吧」→ 收线播报 + 关窗

    .venv/bin/python scripts/s12_wake_once.py
"""

from __future__ import annotations

import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from jarvis.app import Jarvis  # noqa: E402
from jarvis.config import Settings  # noqa: E402
from jarvis.ha import HAClient  # noqa: E402
from jarvis.kws import DEFAULT_MODEL as KWS_DEFAULT_MODEL  # noqa: E402
from jarvis.kws import KeywordWake  # noqa: E402
from jarvis.vad import Segmenter  # noqa: E402
from jarvis.wake import WakeGate, is_sleep_request  # noqa: E402

CEILING = "light.yeelink_cn_314782921_ceiling3_s_2_light"


def _light_snap(ha: HAClient) -> tuple[str, object]:
    s = ha.state(CEILING) or {}
    return str(s.get("state", "?")), (s.get("attributes") or {}).get("brightness")


def _feed_until(
    mic,
    seconds: float,
    on_chunk,
) -> object:
    """最多听 seconds；on_chunk 返回非 None 则立刻结束（供 cue seconds=0 提前收窗）。"""
    import numpy as np

    deadline = time.monotonic() + seconds
    n = 0
    while time.monotonic() < deadline:
        chunk = mic.read(timeout=0.25)
        if chunk is None:
            continue
        n += 1
        if n == 1 or n % 16 == 0:
            peak = float(np.max(np.abs(chunk))) if chunk.size else 0.0
            print(f"  …听中 {n} 块 peak={peak:.3f}", flush=True)
        hit = on_chunk(chunk)
        if hit is not None:
            return hit
    return None


def _kws_pad(kws: KeywordWake) -> str | None:
    """句末补约 0.66s 静音，让短唤醒词右侧上下文够解码（不 input_finished）。"""
    import numpy as np

    pad = np.zeros(int(0.66 * 16000), dtype=np.float32)
    for i in range(0, pad.size, 1024):
        hit = kws.feed(pad[i : i + 1024])
        if hit:
            return hit
    return None


def main() -> int:
    import cue

    st = Settings.load()
    st = replace(
        st,
        audio_source="speaker_endpoint",
        speaker_endpoint=st.speaker_endpoint or "192.168.1.3",
    )
    words = tuple(w.strip() for w in st.wake_words.split("|") if w.strip())
    gate = WakeGate(words=words, follow_up_seconds=st.follow_up_seconds)
    ha = HAClient(st)
    before = _light_snap(ha)
    print(f"吸顶灯开前：state={before[0]} bri={before[1]}", flush=True)

    app = Jarvis(st=st, use_fake_llm=False, speak=True)
    kws = KeywordWake(st.kws_model or KWS_DEFAULT_MODEL, list(words))

    woke_hit: dict = {}
    cmd_box: dict = {}
    sleep_box: dict = {}

    try:
        rc0 = cue.run_cue(
            "唤醒联调三步（请面对 S12）：\n\n"
            "① 只说「贾维斯」\n"
            "② 红框后再说「关掉吸顶灯」（不要再喊唤醒词）\n"
            "③ 最后说「退下吧」\n\n"
            "点【开始】进入第一步。",
            title="home-jarvis · S12 唤醒联调",
            ack_only=True,
            timeout=180,
        )
        if rc0 == cue.EXIT_UNCONFIRMED:
            print("未确认，跳过（未验证）", file=sys.stderr)
            return 2

        print("连接 S12 上行麦…", flush=True)
        with app._mic_factory() as mic:
            if not mic.reader.connected:
                # 再抢一次，避免首连偶发超时后整段空听
                if not mic.reader.connect(timeout=8.0):
                    print("❌ 无法连接 S12:8765，退出", file=sys.stderr)
                    return 1
            print(f"上行已连 · {mic.stats}", flush=True)

            # ① KWS
            def _phase_wake():
                mic.drain()
                kws.reset()
                print("  ① 开始听唤醒词…", flush=True)

                def on_chunk(chunk):
                    hit = kws.feed(chunk)
                    if hit:
                        gate.touch()
                        print(f"  🟢 唤醒（KWS）「{hit}」", flush=True)
                        return hit
                    return None

                hit = _feed_until(mic, 8.0, on_chunk)
                if hit is None:
                    # 说完就停嘴时，窗口内可能还差右侧上下文
                    hit = _kws_pad(kws)
                    if hit:
                        gate.touch()
                        print(f"  🟢 唤醒（KWS·补静音）「{hit}」", flush=True)
                if hit is None:
                    print("  ❌ 8s 内未检出唤醒词", flush=True)
                return hit

            # seconds=0：action 命中即收窗（勿传正数，否则 cue 会硬撑满整段）
            rc1 = cue.run_cue(
                "请只说：\n\n「贾维斯」\n\n"
                "红框出现后立刻说；检出后窗口马上结束（最多约 8 秒）。",
                title="home-jarvis · ① 唤醒词",
                seconds=0,
                countdown_s=3,
                action=_phase_wake,
                result_box=woke_hit,
                timeout=180,
            )
            if rc1 == cue.EXIT_UNCONFIRMED:
                print("① 未确认（未验证）", file=sys.stderr)
                return 2
            if not gate.awake or "value" not in woke_hit or not woke_hit["value"]:
                print("❌ KWS 未命中", file=sys.stderr)
                return 1
            print(f"① OK · 窗口 {gate.follow_up_seconds:g}s", flush=True)

            # ② 免唤醒指令
            def _phase_cmd():
                mic.drain()
                seg_cmd = Segmenter(
                    app.vad_model,
                    threshold=st.vad_threshold,
                    min_silence=st.vad_silence_ms / 1000,
                )

                print("  ② 开始听指令…", flush=True)

                def on_chunk(chunk):
                    gate.touch()  # 采集窗口内保持 awake
                    for utt in seg_cmd.accept(chunk):
                        return utt
                    return None

                utt = _feed_until(mic, 7.0, on_chunk)
                if utt is None:
                    utt = seg_cmd.flush()
                return utt

            rc2 = cue.run_cue(
                "已唤醒。请直接说（不要加唤醒词）：\n\n「关掉吸顶灯」\n\n"
                "红框出现后立刻说；断句后马上进入处理（最多约 7 秒）。",
                title="home-jarvis · ② 免唤醒指令",
                seconds=0,
                countdown_s=3,
                action=_phase_cmd,
                result_box=cmd_box,
                timeout=180,
            )
            if rc2 == cue.EXIT_UNCONFIRMED:
                print("② 未确认（未验证）", file=sys.stderr)
                return 2
            utt = cmd_box.get("value")
            if utt is None or getattr(utt, "size", 0) < 1600:
                print("❌ 未收到指令语音", file=sys.stderr)
                return 1

            text, ms = app.asr.transcribe(utt)
            print(f"  👤 ASR「{text}」({ms:.0f}ms)", flush=True)
            woke, cmd = gate.feed(text)
            if not woke:
                print(f"❌ 窗口内仍被判未唤醒：{text!r}", file=sys.stderr)
                return 1
            if len(cmd) < 2:
                print("❌ 指令为空", file=sys.stderr)
                return 1
            print(f"  → 指令「{cmd}」", flush=True)
            gate.hold()
            try:
                app.handle_text(cmd, mic=mic)
                if not app.interrupted:
                    mic.drain()
            finally:
                gate.release()
                print(f"  ⏳ 续聊窗口重启：{gate.follow_up_seconds:g}s", flush=True)

            after_cmd = _light_snap(ha)
            print(f"吸顶灯指令后：state={after_cmd[0]} bri={after_cmd[1]}", flush=True)
            if after_cmd[0] != "off":
                print("⚠️  HA 尚未显示 off（可能延迟），继续收线步骤", flush=True)

            # ③ 退下
            def _phase_sleep():
                mic.drain()
                seg_sl = Segmenter(
                    app.vad_model,
                    threshold=st.vad_threshold,
                    min_silence=st.vad_silence_ms / 1000,
                )

                print("  ③ 开始听收线…", flush=True)

                def on_chunk(chunk):
                    gate.touch()
                    for utt2 in seg_sl.accept(chunk):
                        return utt2
                    return None

                utt2 = _feed_until(mic, 5.0, on_chunk)
                if utt2 is None:
                    utt2 = seg_sl.flush()
                return utt2

            rc3 = cue.run_cue(
                "请说收线词：\n\n「退下吧」\n\n"
                "红框出现后立刻说；断句即结束（最多约 5 秒）。",
                title="home-jarvis · ③ 退下",
                seconds=0,
                countdown_s=3,
                action=_phase_sleep,
                result_box=sleep_box,
                timeout=180,
            )
            if rc3 == cue.EXIT_UNCONFIRMED:
                print("③ 未确认（未验证）", file=sys.stderr)
                return 2
            utt_s = sleep_box.get("value")
            if utt_s is None or getattr(utt_s, "size", 0) < 1600:
                print("❌ 未收到收线语音", file=sys.stderr)
                return 1
            text_s, ms_s = app.asr.transcribe(utt_s)
            print(f"  👤 ASR「{text_s}」({ms_s:.0f}ms)", flush=True)
            woke_s, cmd_s = gate.feed(text_s)
            if not woke_s:
                print(f"❌ 收线前窗口已过期：{text_s!r}", file=sys.stderr)
                return 1
            sleep_words = tuple(w.strip() for w in st.sleep_words.split("|") if w.strip())
            candidate = cmd_s or text_s
            if not (
                is_sleep_request(candidate, sleep_words) or is_sleep_request(text_s, sleep_words)
            ):
                print(f"❌ 未识别为收线：cmd={cmd_s!r} text={text_s!r}", file=sys.stderr)
                return 1
            if not app._try_sleep(candidate, gate):
                print("❌ _try_sleep 未关窗", file=sys.stderr)
                return 1
            if gate.awake:
                print("❌ 收线后仍 awake", file=sys.stderr)
                return 1
            print("③ OK · 已收线", flush=True)

        after = _light_snap(ha)
        print(f"最终吸顶灯：state={after[0]} bri={after[1]}", flush=True)
        print(
            f"汇总：唤醒={woke_hit.get('value')!r} · 灯 {before[0]}→{after[0]} · 收线=OK",
            flush=True,
        )
        return 0 if after[0] == "off" else 1
    finally:
        app.tools.cancel_timers()
        app.ha.close()
        ha.close()
        if hasattr(app.llm, "close"):
            app.llm.close()
        if app._speaker is not None and hasattr(app._speaker, "close"):
            app._speaker.close()


if __name__ == "__main__":
    raise SystemExit(main())
