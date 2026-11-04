"""状态机：把「收音 → 断句 → 识别 → 思考 → 说话」串起来。

    ┌────────┐  VAD 断句   ┌────────┐  工具/回答  ┌─────────┐
    │ LISTEN ├────────────→│ THINK  ├────────────→│ SPEAK   │
    └────┬───┘             └────────┘             └────┬────┘
         │                                             │
         │        FOLLOW_UP 窗口（默认 8 秒）            │
         └─────────────────────────────────────────────┘

三种输入方式：
    python -m jarvis.app --wav debug/q.wav   离线注入（回归测试用，不依赖麦克风）
    python -m jarvis.app --ptt               回车开始/结束说话（桌面场景最稳）
    python -m jarvis.app                     常开 VAD（安静环境可用，会跟电视抢话）

打断（--barge-in）默认关闭：没有 AEC 时喇叭串进麦克风会自己打断自己。
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

import httpx
import numpy as np

from .asr import SenseVoice
from .audio import TARGET_RATE, Mic, Speaker, read_wav, write_wav
from .bargein import BargeInConfig, BargeInDetector
from .config import Settings
from .endpoint import (
    DEFAULT_DOWN_PORT,
    EndpointMic,
    EndpointSpeaker,
    parse_endpoint,
)
from .ha import HAClient
from .intent import looks_like_command, looks_like_weather, looks_like_web_search
from .agent_runtime import JarvisAgent
from .output import SpeechRouter
from .session import Session
from .tools import Tools
from .vad import Segmenter
from .kws import DEFAULT_MODEL as KWS_DEFAULT_MODEL
from .kws import KeywordWake
from .s12_led import NullLed, S12Led
from .volume import VolumeController
from .wake import WakeGate, is_sleep_request


def chat_with_guard(
    llm,
    messages,
    schemas,
    dispatch,
    user_text,
    on_retry=None,
    on_tool_calls=None,
    *,
    web_search_enabled: bool = False,
    weather_enabled: bool = False,
):
    """调用模型；命令未调工具 / 天气未查 / 联网问句未搜索时，强制重问一次。

    实测模型会凭上下文说"客厅灯已经关了"，却一次没调 control —— 灯根本没动。
    也会直接说「没接天气」而不调工具。prompt 堵不住，编排层兜底。
    """
    res = llm.chat(messages, schemas, dispatch=dispatch, on_tool_calls=on_tool_calls)
    if res.tool_calls:
        return res
    force: str | None = None
    if looks_like_command(user_text):
        force = "control"
    elif weather_enabled and looks_like_weather(user_text):
        force = "weather"
    elif web_search_enabled and looks_like_web_search(user_text):
        force = "web_search"
    if not force:
        return res
    if on_retry:
        on_retry()
    retry = llm.chat(
        messages, schemas, dispatch=dispatch, force_tool=force, on_tool_calls=on_tool_calls
    )
    retry.forced = True
    return retry


def _is_transient_llm_error(exc: BaseException, msg: str) -> bool:
    """上游抖动（超时/连接/5xx/忙）当作可播报的网络故障，而不是致命错误。"""
    if isinstance(exc, httpx.TimeoutException | httpx.TransportError):
        return True
    text = (msg or "").lower()
    return (
        "http 5" in text
        or "http 429" in text
        or "too busy" in text
        or "unavailable" in text
        or "timed out" in text
        or "timeout" in text
        or "connection" in text
    )


def handle_sleep(cmd: str, gate, words: tuple[str, ...], speak) -> bool:
    """收线指令：先回一句，再关掉连续对话窗口。

    顺序很重要 —— 用户要求"说完『好，有事叫我。』之后再结束"，
    所以先 speak（此时窗口仍处于 hold 状态不倒计时），播完再 sleep。
    之后必须重新喊唤醒词才能唤起。
    """
    if not is_sleep_request(cmd, words):
        return False
    gate.hold()
    try:
        speak()
    finally:
        gate.sleep()
    return True


def _is_endpoint(source: str) -> bool:
    return (source or "").strip().lower() in {"speaker_endpoint", "endpoint", "s12"}


class Jarvis:
    def __init__(self, st: Settings | None = None, use_fake_llm: bool = False, speak: bool = True) -> None:
        self.st = st or Settings.load()
        self.speak_enabled = speak
        self.barge_in = False
        self._pending: list[np.ndarray] = []
        self._ack_phrases = [p.strip() for p in self.st.ack_phrases.split("|") if p.strip()]
        self._ack_idx = 0
        self._early_acked = False
        self.interrupted = False
        self._asleep_notice_at = 0.0
        self._mic_factory, self._speaker, router_st, audio_label = self._build_audio()
        self.led = self._build_led()
        self.volume = self._build_volume()
        print("加载模型…", file=sys.stderr)
        t0 = time.perf_counter()
        self.asr = SenseVoice(self.st.asr_model_dir, language=self.st.asr_language)
        self.vad_model = self.st.vad_model
        from .tts_neural import build_local_tts

        local_tts = build_local_tts(self.st, self._speaker)
        self.ha = HAClient(self.st)
        self.tools = Tools(self.ha, settings=self.st, announce=lambda text: self.tts.speak(text))
        self.tts = SpeechRouter(
            router_st,
            self.ha,
            local_tts,
            notify_entity=lambda: self.tools._speaker_notify(),
        )
        self._audio_label = audio_label
        self.session = Session.load(self.tools.catalog())
        if use_fake_llm:
            from .llm_fake import FakeLLM

            self.llm = FakeLLM(devices=list(self.tools.devices))
            print("⚠️  使用假模型（只验证编排，不代表模型能力）", file=sys.stderr)
        else:
            self.llm = JarvisAgent(self.st, self.tools, session=self.session)
        self._here_phrase = "我在"
        self._awake_announced_at = 0.0
        if speak and self._ack_phrases:
            if self.tts.target == "speaker":
                print("音箱播报模式：跳过「首句先出声」（云端 TTS 无法预渲染）", file=sys.stderr)
            else:
                n = self.tts.warm(self._ack_phrases + [self.st.sleep_reply, self._here_phrase])
                if n:
                    print(f"预渲染应答短语 {n} 条（首句可 0ms 出声）", file=sys.stderr)
        if self.tts.target in ("speaker", "both"):
            hint = self.tools._dnd_hint()
            if hint:
                print(f"⚠️  {hint.strip('（）')}——说「关掉勿扰」即可恢复播报", file=sys.stderr)
        print(
            f"就绪（{time.perf_counter() - t0:.1f}s）· 设备 {len(self.tools.devices)} 台 · "
            f"音色 {self.tts.voice} · TTS {self.tts.mode} · 音频 {self._audio_label}",
            file=sys.stderr,
        )

    def _build_audio(
        self,
    ) -> tuple[Callable[[], Any], Any, Settings, str]:
        """按 AUDIO_SOURCE 选本机或 S12 网络端点。返回 (mic_factory, speaker, router_settings, label)。"""
        if _is_endpoint(self.st.audio_source):
            host, up = parse_endpoint(self.st.speaker_endpoint)
            down = int(self.st.speaker_down_port or DEFAULT_DOWN_PORT)
            label = f"S12 {host} ↑{up} ↓{down}"
            print(f"音频端点：{label}（TTS 走本地合成 → 下行 PCM，不经小爱云）", file=sys.stderr)

            def factory() -> EndpointMic:
                return EndpointMic(host, up)

            # 端点模式下强制 local：用 EndpointSpeaker 播 PCM，避免再走米家 play_text
            return factory, EndpointSpeaker(host, down), replace(self.st, tts_target="local"), label

        def factory() -> Mic:
            return Mic(self.st.input_device)

        speaker = Speaker(self.st.output_device) if self.st.tts_mode == "wav" else None
        return factory, speaker, self.st, f"local:{self.st.input_device or 'default'}"

    def _build_led(self):
        """端点模式且 LED_ENABLED 时接 S12 灯环；否则空操作。"""
        if not (_is_endpoint(self.st.audio_source) and self.st.led_enabled):
            return NullLed()
        host, _ = parse_endpoint(self.st.speaker_endpoint)
        led = S12Led(host, self.st.led_port, enabled=True)
        led.set("idle")
        print(
            f"灯环：{host}（待命熄灭 / 唤醒常亮蓝 / 思考蓝呼吸 / 播报蓝 / 出错红 / 麦禁用黄呼吸）",
            file=sys.stderr,
        )
        return led

    @property
    def mic_muted(self) -> bool:
        return bool(self.volume and self.volume.muted)

    def _led(self, state: str) -> None:
        # 麦禁用时设备端保持黄呼吸，Mac 侧不要抢灯
        if self.mic_muted and state not in ("error",):
            return
        try:
            self.led.set(state)  # type: ignore[arg-type]
        except Exception as e:  # noqa: BLE001
            print(f"⚠️  灯环 {state}：{e}", file=sys.stderr)

    def _build_volume(self) -> VolumeController | None:
        if not (_is_endpoint(self.st.audio_source) and isinstance(self._speaker, EndpointSpeaker)):
            return None
        host, _ = parse_endpoint(self.st.speaker_endpoint)
        spk: EndpointSpeaker = self._speaker

        def gain(b: int) -> bool:
            return spk.set_volume_byte(b)

        def led_vol(p: int) -> None:
            if self.mic_muted:
                return
            self.led.volume(p)

        def on_mute(muted: bool) -> None:
            if muted:
                # 黄灯由设备 volumed 本地下发；这里只清会话态
                return
            self._led("idle")

        ctl = VolumeController(
            gain,
            led_vol,
            initial=self.st.volume_percent,
            listen_port=self.st.volume_udp_port,
            on_mute=on_mute,
        )
        # 告诉设备「往哪发 UDP」；本机路由到音箱的源地址
        try:
            s = __import__("socket").socket(__import__("socket").AF_INET, __import__("socket").SOCK_DGRAM)
            s.connect((host, 8765))
            mac_ip = s.getsockname()[0]
            s.close()
        except OSError:
            mac_ip = ""
        if mac_ip:
            import subprocess

            subprocess.run(
                [
                    "ssh",
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    "UserKnownHostsFile=/dev/null",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=3",
                    "-o",
                    "HostKeyAlgorithms=+ssh-rsa",
                    "-o",
                    "PubkeyAcceptedKeyTypes=+ssh-rsa",
                    f"root@{host}",
                    f"export PATH=/usr/sbin:/usr/bin:/sbin:/bin; printf '%s\\n' '{mac_ip}' > /tmp/jarvis-mac.ip",
                ],
                check=False,
                timeout=4,
                capture_output=True,
            )
        ctl.set_percent(self.st.volume_percent, led=True)
        ctl.start_udp_listener()
        print(
            f"音量：{self.st.volume_percent}%（环调节 → 蓝灯弧 + 播放增益；UDP :{self.st.volume_udp_port}）",
            file=sys.stderr,
        )
        return ctl

    # ------------------------------------------------------------------ 一轮

    def _announce_here(self) -> None:
        """唤醒后回一声「我在」（2s 内不重复，避免 KWS + 空指令各说一次）。"""
        now = time.monotonic()
        if now - self._awake_announced_at < 2.0:
            return
        self._awake_announced_at = now
        print("  🟢 我在，说吧")
        if not self.speak_enabled:
            self._led("listen")
            return
        if self.tts.target == "speaker":
            self._led("listen")
            return
        # 端点下行与终答共用一条 TCP：短句必须阻塞播完，避免与下一句抢锁
        self._led("speak")
        self.tts.play_cached(self._here_phrase, blocking=True)
        self._led("listen")

    def _post_announce_listen(self, mic, seg) -> None:  # noqa: ANN001
        """「我在」之后：排空回声 + 重置 VAD，避免把播报残段识别成「小小」之类。"""
        drain_s = 0.4 if _is_endpoint(self.st.audio_source) else 0.15
        try:
            mic.drain(drain_s)
        except TypeError:
            mic.drain()
        if hasattr(seg, "reset"):
            seg.reset()

    def _log_asr_utt(self, utt: np.ndarray, text: str, ms: float) -> None:
        """ASR 旁路：时长/电平 + 落盘，方便对照误识别。"""
        from .audio import dbfs as _dbfs

        dur = float(utt.size) / TARGET_RATE
        print(f"  🎙  ASR 段 {dur:.2f}s {_dbfs(utt):.0f}dBFS → 「{text}」 ({ms:.0f}ms)", flush=True)
        try:
            path = Path("debug/last_asr.wav")
            path.parent.mkdir(parents=True, exist_ok=True)
            write_wav(path, utt, TARGET_RATE)
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️  保存 last_asr.wav 失败：{e}", file=sys.stderr)
        if dur < 0.35 and text.strip():
            print("  ⚠️  切句过短，可能是回声/噪声（不是完整指令）", flush=True)

    def _early_ack(self, calls) -> None:  # noqa: ANN001
        """工具刚决定、还没执行时先播一个短应答，掩盖第二轮的 600ms。

        多轮 tool 循环（同一句用户话）只 ack 一次，避免「好的」「嗯」「收到」连播。
        """
        if self._early_acked:
            return
        if not (self.speak_enabled and self._ack_phrases):
            return
        if self.tts.target == "speaker":
            # 云端 TTS 没法预渲染，硬插一句"好的"只会让两句话更慢更乱
            return
        self._early_acked = True
        phrase = self._ack_phrases[self._ack_idx % len(self._ack_phrases)]
        self._ack_idx += 1
        # 端点模式用阻塞播：非阻塞会与终答抢 DownlinkWriter 锁导致死锁
        blocking = _is_endpoint(self.st.audio_source)
        self._led("speak")
        ms = self.tts.play_cached(phrase, blocking=blocking)
        print(f"  ⚡ 先出声「{phrase}」({ms:.1f}ms)")
        self._led("think")

    def handle(self, pcm: np.ndarray, mic: Mic | None = None) -> str:
        """一段音频 → 一句回答（同时朗读）。返回回答文本。"""
        self.interrupted = False
        self._asleep_notice_at = 0.0
        if pcm.size < int(0.2 * TARGET_RATE):
            return ""

        text, asr_ms = self.asr.transcribe(pcm)
        if not text.strip():
            print("  · 没听清，跳过")
            return ""

        print(f"  👤 {text}   ({asr_ms:.0f}ms ASR)")
        return self.handle_text(text, mic=mic)

    def handle_text(self, text: str, mic: Mic | None = None) -> str:
        """已经拿到文本时直接进思考+说话（唤醒门控走这条路，避免二次 ASR）。"""
        self._early_acked = False
        self._led("think")
        hint = self.llm.call_hint() if hasattr(self.llm, "call_hint") else ""
        print(f"  🔗 LLM 调用：user=「{text}」 {hint}", flush=True)
        try:
            msgs = self.session.messages(text)
            schemas = self.tools.schemas()
            res = chat_with_guard(
                self.llm,
                msgs,
                schemas,
                self._dispatch,
                text,
                on_retry=lambda: print("  ⚠️  未调工具，强制重问一次"),
                on_tool_calls=self._early_ack,
                web_search_enabled=self.st.web_search_enabled,
                weather_enabled=self.st.weather_enabled,
            )
        except httpx.TimeoutException as e:
            print(f"  ⚠️  LLM 超时：{e}", file=sys.stderr)
            return self._speak_network_glitch(mic)
        except httpx.TransportError as e:
            print(f"  ⚠️  LLM 网络错误：{e}", file=sys.stderr)
            return self._speak_network_glitch(mic)
        except Exception as e:  # noqa: BLE001
            # 运行时已把 SDK/openai 异常归一化成 httpx / RuntimeError；
            # 这里仍按文案兜一层，避免换运行时后又把主循环带走。
            msg = str(e)
            if _is_transient_llm_error(e, msg):
                print(f"  ⚠️  LLM 服务异常：{e}", file=sys.stderr)
                return self._speak_network_glitch(mic)
            self._led("error")
            time.sleep(1.2)
            self._led("listen")
            raise

        reply = res.text or (res.tool_results[-1][1] if res.tool_results else "")
        if not reply:
            reply = "抱歉，我没想好怎么回答。"

        self.session.add_user(text)
        self.session.add_assistant(reply)

        calls = "、".join(c[0] for c in res.tool_results) or "无"
        print(f"  🤖 {reply}   ({res.latency_ms:.0f}ms LLM · {res.rounds} 轮 · 工具 {calls})")

        if self.speak_enabled:
            self.tts.wait_idle()  # 先出声的"好的"还没播完就先等等，别两句叠在一起
            t1 = time.perf_counter()
            self._led("speak")
            if mic is not None and self.barge_in:
                self._speak_barge_in(reply, mic)
            else:
                self.tts.speak(reply)
            print(f"  🔊 播报 {(time.perf_counter() - t1) * 1000:.0f}ms")
            # 播报期间麦克风一直在往队列里塞自己的声音，不排空的话
            # 下一轮会把喇叭回声当成用户说话（VAD 说是"有声"，其实是自己）
            if mic is not None and not self.interrupted:
                mic.drain()
        self._led("listen")
        return reply

    def _speak_network_glitch(self, mic: Mic | None) -> str:
        """LLM 超时/断网：播一句提示，不把主循环打崩。"""
        reply = "网络遇到点问题"
        self._led("error")
        if self.speak_enabled:
            try:
                self.tts.wait_idle()
                self._led("speak")
                self.tts.speak(reply)
            except Exception as e:  # noqa: BLE001
                print(f"  ⚠️  网络提示播报失败：{e}", file=sys.stderr)
        if mic is not None:
            try:
                mic.drain()
            except Exception:  # noqa: BLE001
                pass
        self._led("listen")
        print(f"  🤖 {reply}")
        return reply

    def _speak_barge_in(self, text: str, mic: Mic) -> None:
        """边说边听：用户插话就立刻掐断播报，并把打断的那段音频留下来继续说。"""
        det = BargeInDetector(
            BargeInConfig(
                margin_db=self.st.barge_in_margin_db,
                min_speech_ms=500.0,
                warmup_ms=400.0,
                baseline_percentile=85.0,
            )
        )
        th = self.tts.speak(text, blocking=False)
        captured: list[np.ndarray] = []
        while th is not None and th.is_alive():
            chunk = mic.read(timeout=0.1)
            if chunk is None:
                continue
            captured.append(chunk)
            if det.feed(chunk):
                self.tts.stop()
                self.interrupted = True
                tail = captured[-40:]  # 约最近 2.5 秒，够覆盖整句打断
                self._pending.append(np.concatenate(tail) if tail else chunk)
                print("  ✋ 被打断（回声基线 "
                      f"{det.last_baseline_db:.0f}dBFS / 当前 {det.last_level_db:.0f}dBFS）")
                break
        if th is not None:
            th.join(timeout=5.0)

    def _barge_in_supported(self) -> bool:
        """云端播报（小爱）没法被打断，麦克风还会收到它的声音，所以音箱模式禁用 barge-in。"""
        if self.tts.target == "speaker":
            print("⚠️  音箱播报模式下不支持打断（云端音频不受控），已自动关闭 --barge-in")
            return False
        return True

    def _note_asleep(self) -> None:
        """未唤醒时听到人声，给一次提示（3 秒内不重复）。"""
        now = time.monotonic()
        if now - self._asleep_notice_at < 3.0:
            return
        self._asleep_notice_at = now
        print("  💤 未唤醒，已忽略（喊唤醒词才能唤起）")

    def _try_sleep(self, cmd: str, gate) -> bool:
        """处理"退下/退出"：回一句 → 关闭窗口 → 下次需重新唤醒。"""
        reply = self.st.sleep_reply
        words = tuple(w.strip() for w in self.st.sleep_words.split("|") if w.strip())

        def _speak() -> None:
            print(f"  🤖 {reply}")
            if self.speak_enabled:
                self._led("speak")
                self.tts.speak(reply)

        done = handle_sleep(cmd, gate, words, _speak)
        if done:
            self._led("idle")
            print("  🌙 已退出连续对话，需要重新喊唤醒词")
        return done

    def _dispatch(self, name: str, args: dict) -> str:
        out = self.tools.dispatch(name, args)
        if name == "control":
            dev = self.tools.resolve(str(args.get("device", "")))
            self.session.note_action(
                device=dev.name if dev else str(args.get("device", "")),
                action=str(args.get("action", "")),
                value=args.get("value"),
                result=out,
            )
        elif name == "query":
            dev = self.tools.resolve(str(args.get("device", "")))
            self.session.note_action(
                device=dev.name if dev else str(args.get("device", "")), action="query", value=None, result=out
            )
        return out

    # ------------------------------------------------------------------ 三种输入

    def run_wav(self, path: str | Path) -> int:
        """离线注入：把 wav 切成 64ms 块喂给 VAD，逐段处理（回归测试用）。"""
        pcm, rate = read_wav(path)
        if rate != TARGET_RATE:
            import soxr

            pcm = soxr.resample(pcm, rate, TARGET_RATE)
        print(f"\n注入 {path}（{pcm.size / TARGET_RATE:.2f}s）")
        seg = Segmenter(self.vad_model, threshold=self.st.vad_threshold,
                        min_silence=self.st.vad_silence_ms / 1000)
        n = 0
        for i in range(0, pcm.size, 1024):
            for utt in seg.accept(pcm[i : i + 1024]):
                n += 1
                print(f"\n[{n}] 语音段 {utt.size / TARGET_RATE:.2f}s")
                self.handle(utt)
        tail = seg.flush()
        if tail is not None:
            n += 1
            print(f"\n[{n}] 尾段 {tail.size / TARGET_RATE:.2f}s")
            self.handle(tail)
        print(f"\n共处理 {n} 段")
        return n

    def run_vad(self, once: bool = False, barge_in: bool = False) -> None:
        """常开 VAD：插着耳机、屋里安静时最好用。"""
        seg = Segmenter(self.vad_model, threshold=self.st.vad_threshold,
                        min_silence=self.st.vad_silence_ms / 1000)
        barge_in = barge_in and self._barge_in_supported()
        with self._mic_factory() as mic:
            print(f"\n常开监听中…（Ctrl+C 退出）{'(处理一句就退出)' if once else ''}", flush=True)
            while True:
                if self._pending:
                    utt = self._pending.pop(0)
                    print("\n[打断后续]")
                    self.handle(utt, mic=mic)
                    if once:
                        return
                    continue
                chunk = mic.read(timeout=1.0)
                if chunk is None:
                    continue
                for utt in seg.accept(chunk):
                    print()
                    self.handle(utt, mic=mic)
                    if once:
                        return

    def run_ptt(self, once: bool = False) -> None:
        """回车说话：按回车开始录，再按回车结束并立即回答。"""
        with self._mic_factory() as mic:
            while True:
                try:
                    input("\n按回车开始说话（Ctrl+C 退出）…")
                except EOFError:
                    return
                mic.drain()
                stop = threading.Event()

                def _wait_enter() -> None:
                    try:
                        input("🎙  录音中，再按回车结束…\n")
                    except EOFError:
                        pass
                    stop.set()

                threading.Thread(target=_wait_enter, daemon=True).start()
                chunks: list[np.ndarray] = []
                while not stop.is_set():
                    c = mic.read(timeout=0.2)
                    if c is not None:
                        chunks.append(c)
                if not chunks:
                    continue
                pcm = np.concatenate(chunks)
                self.handle(pcm, mic=mic)
                if once:
                    return


    def run_wake(self, path: str | Path | None = None, once: bool = False,
                 backend: str = "") -> int:
        """唤醒词模式：先说唤醒词，之后 8 秒内可以直接连续对话。

        backend：
          kws —— 专用关键词模型，说到唤醒词最后半个音节就亮（快，推荐）
          asr —— ASR 门控，整句识别后再找唤醒词（不需要额外模型）
        """
        backend = (backend or self.st.wake_backend or "kws").lower()
        words = tuple(w.strip() for w in self.st.wake_words.split("|") if w.strip())
        gate = WakeGate(words=words, follow_up_seconds=self.st.follow_up_seconds)
        if backend == "asr":
            return self._run_wake_asr(path, once, gate, words)
        return self._run_wake_kws(path, once, gate, words)

    def _run_wake_kws(self, path, once, gate: WakeGate, words: tuple[str, ...]) -> int:  # noqa: ANN001
        kws = KeywordWake(self.st.kws_model or KWS_DEFAULT_MODEL, list(words))
        # 端点 Wi-Fi 抖动容易在句中挖空，静音门限加长减少半截切句
        silence_ms = self.st.vad_silence_ms
        if _is_endpoint(self.st.audio_source):
            silence_ms = max(silence_ms, 800)
        seg = Segmenter(
            self.vad_model,
            threshold=self.st.vad_threshold,
            min_silence=silence_ms / 1000,
        )
        print(f"\n唤醒词（KWS 专用模型）：{gate.words_hint()} · 唤醒后 {gate.follow_up_seconds:g}s 免唤醒")
        if silence_ms != self.st.vad_silence_ms:
            print(f"  VAD 静音门限 {silence_ms}ms（端点加长）", file=sys.stderr)

        def _take(utt, mic):  # noqa: ANN001
            text, ms = self.asr.transcribe(utt)
            self._log_asr_utt(utt, text, ms)
            if not text.strip():
                return False
            woke, cmd = gate.feed(text)
            if not woke or len(cmd) < 2:
                if woke:
                    self._announce_here()
                    if mic is not None:
                        self._post_announce_listen(mic, seg)
                return False
            print(f"  👤 {cmd}   ({ms:.0f}ms ASR)")
            if self._try_sleep(cmd, gate):
                return True
            gate.hold()  # 思考和播报期间窗口不倒计时
            try:
                self.handle_text(cmd, mic=mic)
                # 播报中被打断：立刻处理打断段，不要等下一轮 VAD
                while self._pending:
                    utt = self._pending.pop(0)
                    print("\n[打断后续]")
                    self.handle(utt, mic=mic)
                if mic is not None and not self.interrupted:
                    mic.drain()
            finally:
                gate.release()
                print(f"  ⏳ 窗口从播报结束后重新计时：{gate.follow_up_seconds:g} 秒内可直接说下一句")
            return True

        if path is not None:
            pcm, rate = read_wav(path)
            if rate != TARGET_RATE:
                import soxr

                pcm = soxr.resample(pcm, rate, TARGET_RATE)
            n = 0
            for i in range(0, pcm.size, 1024):
                chunk = pcm[i : i + 1024]
                if not gate.awake and kws.feed(chunk):
                    gate.touch()
                    print(f"  🟢 唤醒（KWS）· 等整句说完再识别")
                    # 不立刻「我在」：同一句里的指令留给 VAD+ASR
                for utt in seg.accept(chunk):
                    if gate.awake:
                        n += 1 if _take(utt, None) else 0
                    else:
                        self._note_asleep()
            for _ in kws.flush():
                gate.touch()
            tail = seg.flush()
            if tail is not None:
                if gate.awake:
                    n += 1 if _take(tail, None) else 0
                else:
                    self._note_asleep()
            return n

        with self._mic_factory() as mic:
            print("（Ctrl+C 退出）")
            was_awake = False
            try:
                while True:
                    chunk = mic.read(timeout=0.5)
                    if self.mic_muted:
                        if gate.awake:
                            gate.sleep()
                        if was_awake:
                            was_awake = False
                        continue
                    if chunk is None:
                        if was_awake and not gate.awake:
                            self._led("idle")
                            was_awake = False
                        continue
                    if not gate.awake:
                        hit = kws.feed(chunk)
                        if hit:
                            gate.touch()
                            was_awake = True
                            print(f"  🟢 唤醒（KWS）「{hit}」· 等整句说完再识别")
                            self._led("listen")
                            # 不播「我在」、不 reset VAD：
                            # 「小贾今天几号」要整句切完再 ASR，否则后半截被丢掉
                        elif was_awake:
                            self._led("idle")
                            was_awake = False
                    else:
                        was_awake = True
                    for utt in seg.accept(chunk):
                        if not gate.awake:
                            self._note_asleep()
                            continue
                        if _take(utt, mic) and once:
                            return 1
            finally:
                self._led("idle")
        return 0

    def _run_wake_asr(self, path, once, gate: WakeGate, words: tuple[str, ...]) -> int:  # noqa: ANN001
        """ASR 门控后端（不需要 KWS 模型）。"""
        print(f"\n唤醒词：{gate.words_hint()}（唤醒后 {gate.follow_up_seconds:g} 秒内免唤醒）")

        def _process(utt: np.ndarray, mic: Any | None) -> bool:
            text, ms = self.asr.transcribe(utt)
            if not text.strip():
                return False
            woke, cmd = gate.feed(text)
            if not woke:
                print(f"  💤 未唤醒，忽略：{text}")
                return False
            if len(cmd) < 2:
                self._announce_here()
                return False
            print(f"  👤 {cmd}   ({ms:.0f}ms ASR · 唤醒词已剥离)")
            if self._try_sleep(cmd, gate):
                return True
            gate.hold()
            try:
                self.handle_text(cmd, mic=mic)
                while self._pending:
                    utt = self._pending.pop(0)
                    print("\n[打断后续]")
                    self.handle(utt, mic=mic)
                if mic is not None and not self.interrupted:
                    mic.drain()
            finally:
                gate.release()
                print(f"  ⏳ 窗口从播报结束后重新计时：{gate.follow_up_seconds:g} 秒内可直接说下一句")
            return True

        if path is not None:
            pcm, rate = read_wav(path)
            if rate != TARGET_RATE:
                import soxr

                pcm = soxr.resample(pcm, rate, TARGET_RATE)
            seg = Segmenter(self.vad_model, threshold=self.st.vad_threshold,
                            min_silence=self.st.vad_silence_ms / 1000)
            n = 0
            for i in range(0, pcm.size, 1024):
                for utt in seg.accept(pcm[i : i + 1024]):
                    n += 1 if _process(utt, None) else 0
            tail = seg.flush()
            if tail is not None:
                n += 1 if _process(tail, None) else 0
            return n

        seg = Segmenter(self.vad_model, threshold=self.st.vad_threshold,
                        min_silence=self.st.vad_silence_ms / 1000)
        with self._mic_factory() as mic:
            print("（Ctrl+C 退出）")
            while True:
                chunk = mic.read(timeout=1.0)
                if self.mic_muted:
                    if gate.awake:
                        gate.sleep()
                    continue
                if chunk is None:
                    continue
                for utt in seg.accept(chunk):
                    if _process(utt, mic) and once:
                        return 1
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="jarvis.app", description="语音管家主程序")
    ap.add_argument("--wav", help="离线注入 wav（回归测试）")
    ap.add_argument("--ptt", action="store_true", help="回车说话模式")
    ap.add_argument("--once", action="store_true", help="处理一段就退出")
    ap.add_argument("--barge-in", action="store_true", help="允许说话时被打断（无 AEC 时不建议）")
    ap.add_argument("--fake-llm", action="store_true", help="用假模型跑通编排（无 key 时可用）")
    ap.add_argument("--no-speak", action="store_true", help="只打印不朗读（批量自检用）")
    ap.add_argument("--wake", action="store_true", help="唤醒词模式（先用唤醒词，之后连续对话）")
    ap.add_argument("--wake-backend", choices=["kws", "asr"], default="", help="唤醒后端，默认 kws")
    ap.add_argument(
        "--endpoint",
        nargs="?",
        const="192.168.1.3",
        default="",
        metavar="HOST",
        help="用 S12 网络端点做麦+喇叭（等价 AUDIO_SOURCE=speaker_endpoint）",
    )
    args = ap.parse_args(argv)

    st = Settings.load()
    if args.endpoint:
        st = replace(
            st,
            audio_source="speaker_endpoint",
            speaker_endpoint=args.endpoint,
        )
    app = Jarvis(st=st, use_fake_llm=args.fake_llm, speak=not args.no_speak)
    try:
        app.barge_in = bool(args.barge_in)
        if app.barge_in and not app._barge_in_supported():
            app.barge_in = False
        if args.wake:
            if app.barge_in:
                print("打断：开（播报中可插话）", file=sys.stderr)
            return 0 if app.run_wake(args.wav, backend=args.wake_backend) else 1
        if args.wav:
            return 0 if app.run_wav(args.wav) else 1
        if args.ptt:
            app.run_ptt(once=args.once)
        else:
            app.run_vad(once=args.once, barge_in=app.barge_in)
    except KeyboardInterrupt:
        print("\n再见")
    finally:
        app.tools.cancel_timers()
        app.ha.close()
        if hasattr(app.llm, "close"):
            app.llm.close()
        spk = getattr(app, "_speaker", None)
        if spk is not None and hasattr(spk, "close"):
            try:
                spk.close()
            except Exception:  # noqa: BLE001
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
