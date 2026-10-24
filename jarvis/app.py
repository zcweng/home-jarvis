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
from pathlib import Path

import numpy as np

from .asr import SenseVoice
from .audio import TARGET_RATE, Mic, Speaker, read_wav
from .bargein import BargeInConfig, BargeInDetector
from .config import Settings
from .ha import HAClient
from .intent import looks_like_command
from .llm import DeepSeek
from .output import SpeechRouter
from .session import Session
from .tools import Tools
from .tts import SayTTS
from .vad import Segmenter
from .kws import DEFAULT_MODEL as KWS_DEFAULT_MODEL
from .kws import KeywordWake
from .wake import WakeGate, is_sleep_request


def chat_with_guard(llm, messages, schemas, dispatch, user_text, on_retry=None, on_tool_calls=None):
    """调用模型；若"看起来是命令但一个工具都没调"，强制重问一次。

    实测模型会凭上下文说"客厅灯已经关了"，却一次没调 control —— 灯根本没动。
    prompt 堵不住这类幻觉，只能靠编排层兜底。
    """
    res = llm.chat(messages, schemas, dispatch=dispatch, on_tool_calls=on_tool_calls)
    if res.tool_calls or not looks_like_command(user_text):
        return res
    if on_retry:
        on_retry()
    retry = llm.chat(messages, schemas, dispatch=dispatch, force_tool="control",
                     on_tool_calls=on_tool_calls)
    retry.forced = True
    return retry


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


class Jarvis:
    def __init__(self, st: Settings | None = None, use_fake_llm: bool = False, speak: bool = True) -> None:
        self.st = st or Settings.load()
        self.speak_enabled = speak
        self.barge_in = False
        self._pending: list[np.ndarray] = []
        self._ack_phrases = [p.strip() for p in self.st.ack_phrases.split("|") if p.strip()]
        self._ack_idx = 0
        self.interrupted = False
        self._asleep_notice_at = 0.0
        print("加载模型…", file=sys.stderr)
        t0 = time.perf_counter()
        self.asr = SenseVoice(self.st.asr_model_dir, language=self.st.asr_language)
        self.vad_model = self.st.vad_model
        speaker = Speaker(self.st.output_device) if self.st.tts_mode == "wav" else None
        from .tts_neural import build_local_tts

        local_tts = build_local_tts(self.st, speaker)
        self.ha = HAClient(self.st)
        self.tools = Tools(self.ha, settings=self.st, announce=lambda text: self.tts.speak(text))
        self.tts = SpeechRouter(
            self.st,
            self.ha,
            local_tts,
            notify_entity=lambda: self.tools._speaker_notify(),
        )
        if use_fake_llm:
            from .llm_fake import FakeLLM

            self.llm = FakeLLM()
            print("⚠️  使用假模型（只验证编排，不代表模型能力）", file=sys.stderr)
        else:
            self.llm = DeepSeek(self.st)
        self.session = Session.load(self.tools.catalog())
        if speak and self._ack_phrases:
            if self.tts.target == "speaker":
                print("音箱播报模式：跳过「首句先出声」（云端 TTS 无法预渲染）", file=sys.stderr)
            else:
                n = self.tts.warm(self._ack_phrases + [self.st.sleep_reply])
                if n:
                    print(f"预渲染应答短语 {n} 条（首句可 0ms 出声）", file=sys.stderr)
        if self.tts.target in ("speaker", "both"):
            hint = self.tools._dnd_hint()
            if hint:
                print(f"⚠️  {hint.strip('（）')}——说「关掉勿扰」即可恢复播报", file=sys.stderr)
        print(
            f"就绪（{time.perf_counter() - t0:.1f}s）· 设备 {len(self.tools.devices)} 台 · "
            f"音色 {self.tts.voice} · TTS {self.tts.mode}",
            file=sys.stderr,
        )

    # ------------------------------------------------------------------ 一轮

    def _early_ack(self, calls) -> None:  # noqa: ANN001
        """工具刚决定、还没执行时先播一个短应答，掩盖第二轮的 600ms。"""
        if not (self.speak_enabled and self._ack_phrases):
            return
        if self.tts.target == "speaker":
            # 云端 TTS 没法预渲染，硬插一句"好的"只会让两句话更慢更乱
            return
        phrase = self._ack_phrases[self._ack_idx % len(self._ack_phrases)]
        self._ack_idx += 1
        ms = self.tts.play_cached(phrase)
        print(f"  ⚡ 先出声「{phrase}」({ms:.1f}ms)")

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
        msgs = self.session.messages(text)
        res = chat_with_guard(
            self.llm, msgs, Tools.schemas(), self._dispatch, text,
            on_retry=lambda: print("  ⚠️  看起来是命令但没调用工具，强制重问一次"),
            on_tool_calls=self._early_ack,
        )

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
            if mic is not None and self.barge_in:
                self._speak_barge_in(reply, mic)
            else:
                self.tts.speak(reply)
            print(f"  🔊 播报 {(time.perf_counter() - t1) * 1000:.0f}ms")
            # 播报期间麦克风一直在往队列里塞自己的声音，不排空的话
            # 下一轮会把喇叭回声当成用户说话（VAD 说是"有声"，其实是自己）
            if mic is not None and not self.interrupted:
                mic.drain()
        return reply

    def _speak_barge_in(self, text: str, mic: Mic) -> None:
        """边说边听：用户插话就立刻掐断播报，并把打断的那段音频留下来继续说。"""
        det = BargeInDetector(BargeInConfig(margin_db=self.st.barge_in_margin_db))
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
        if not self.barge_in:
            return False
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
                self.tts.speak(reply)

        done = handle_sleep(cmd, gate, words, _speak)
        if done:
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
        with Mic(self.st.input_device) as mic:
            print(f"\n常开监听中…（Ctrl+C 退出）{'(处理一句就退出)' if once else ''}")
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
        with Mic(self.st.input_device) as mic:
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
        seg = Segmenter(self.vad_model, threshold=self.st.vad_threshold,
                        min_silence=self.st.vad_silence_ms / 1000)
        print(f"\n唤醒词（KWS 专用模型）：{gate.words_hint()} · 唤醒后 {gate.follow_up_seconds:g}s 免唤醒")

        def _take(utt, mic):  # noqa: ANN001
            text, ms = self.asr.transcribe(utt)
            if not text.strip():
                return False
            woke, cmd = gate.feed(text)
            if not woke or len(cmd) < 2:
                if woke:
                    print("  🟢 我在，说吧")
                return False
            print(f"  👤 {cmd}   ({ms:.0f}ms ASR)")
            if self._try_sleep(cmd, gate):
                return True
            gate.hold()  # 思考和播报期间窗口不倒计时
            try:
                self.handle_text(cmd, mic=mic)
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
                    print("  🟢 唤醒（KWS）")
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

        with Mic(self.st.input_device) as mic:
            print("（Ctrl+C 退出）")
            while True:
                chunk = mic.read(timeout=0.5)
                if chunk is None:
                    continue
                if not gate.awake:
                    hit = kws.feed(chunk)
                    if hit:
                        gate.touch()
                        print(f"  🟢 唤醒「{hit}」")
                for utt in seg.accept(chunk):
                    if not gate.awake:
                        self._note_asleep()
                        continue
                    if _take(utt, mic) and once:
                        return 1
        return 0

    def _run_wake_asr(self, path, once, gate: WakeGate, words: tuple[str, ...]) -> int:  # noqa: ANN001
        """ASR 门控后端（不需要 KWS 模型）。"""
        print(f"\n唤醒词：{gate.words_hint()}（唤醒后 {gate.follow_up_seconds:g} 秒内免唤醒）")

        def _process(utt: np.ndarray, mic: Mic | None) -> bool:
            text, ms = self.asr.transcribe(utt)
            if not text.strip():
                return False
            woke, cmd = gate.feed(text)
            if not woke:
                print(f"  💤 未唤醒，忽略：{text}")
                return False
            if len(cmd) < 2:
                print("  🟢 我在，说吧")
                return False
            print(f"  👤 {cmd}   ({ms:.0f}ms ASR · 唤醒词已剥离)")
            if self._try_sleep(cmd, gate):
                return True
            gate.hold()
            try:
                self.handle_text(cmd, mic=mic)
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
        with Mic(self.st.input_device) as mic:
            print("（Ctrl+C 退出）")
            while True:
                chunk = mic.read(timeout=1.0)
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
    args = ap.parse_args(argv)

    app = Jarvis(use_fake_llm=args.fake_llm, speak=not args.no_speak)
    try:
        if args.wake:
            return 0 if app.run_wake(args.wav, backend=args.wake_backend) else 1
        if args.wav:
            return 0 if app.run_wav(args.wav) else 1
        app.barge_in = args.barge_in
        if args.ptt:
            app.run_ptt(once=args.once)
        else:
            app.run_vad(once=args.once, barge_in=args.barge_in)
    except KeyboardInterrupt:
        print("\n再见")
    finally:
        app.tools.cancel_timers()
        app.ha.close()
        if hasattr(app.llm, "close"):
            app.llm.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
