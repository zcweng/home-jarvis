#!/usr/bin/env python3
"""TTS 热切换 / 回落自检（不依赖真实云账号 / 不加载 VITS）。"""

from __future__ import annotations

import sys
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jarvis.config import Settings  # noqa: E402
from jarvis.tts_registry import SwitchableTTS, build_backend, normalize_backend  # noqa: E402
import jarvis.tts_registry as registry  # noqa: E402


class FakeTTS:
    def __init__(self, name: str) -> None:
        self.mode = name
        self.voice = f"fake({name})"
        self.sample_rate = 16000
        self.load_ms = 0.0
        self.sid = 0
        self.voice_name = name
        self._speaking = False
        self.spoken: list[str] = []
        self.warmed: list[str] = []

    def synthesize(self, text: str):
        return np.zeros(1600, dtype=np.float32), self.sample_rate

    def speak(self, text: str, blocking: bool = True):
        self.spoken.append(text)
        return None

    def stop(self) -> None:
        self._speaking = False

    def warm(self, phrases: list[str]) -> int:
        self.warmed.extend([p for p in phrases if p])
        return len([p for p in phrases if p])

    def play_cached(self, text: str, blocking: bool = False) -> float:
        self.spoken.append(text)
        return 0.0

    def wait_idle(self, timeout: float = 2.0) -> None:
        return None

    @property
    def speaking(self) -> bool:
        return self._speaking


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ✅ {name}")
    else:
        print(f"  ❌ {name} {detail}")
        raise SystemExit(1)


def _fake_build(name, settings, speaker, **kwargs):
    backend = normalize_backend(name)
    if backend == "qwen" and not settings.dashscope_api_key:
        return FakeTTS("neural")
    return FakeTTS(backend)


def test_normalize() -> None:
    print("== normalize_backend ==")
    check("qianwen→qwen", normalize_backend("qianwen") == "qwen")
    check("cosyvoice→qwen", normalize_backend("cosyvoice") == "qwen")
    check("vits→neural", normalize_backend("vits") == "neural")


def test_switch_proxy() -> None:
    print("== SwitchableTTS.switch ==")
    st = replace(Settings.load(), tts_backend="neural", dashscope_api_key="")
    with patch.object(registry, "build_backend", side_effect=_fake_build):
        sw = SwitchableTTS(st, speaker=None, warm_phrases=["好的"])
        check("初始 neural", sw.mode == "neural", sw.mode)

        msg = sw.switch("say")
        check("切到 say", "系统" in msg or "say" in msg.lower(), msg)
        check("mode=say", sw.mode == "say", sw.mode)
        sw.speak("你好")
        check("代理 speak", sw._active.spoken == ["你好"], str(sw._active.spoken))

        msg2 = sw.switch("neural")
        check("切回 neural", "本地" in msg2 or "neural" in msg2.lower(), msg2)
        check("mode=neural", sw.mode == "neural", sw.mode)


def test_qwen_missing_key() -> None:
    print("== qwen 缺 Key ==")
    st = replace(Settings.load(), dashscope_api_key="", tts_backend="neural")
    with patch.object(registry, "build_backend", side_effect=_fake_build):
        sw = SwitchableTTS(st, speaker=None)
        msg = sw.switch("qwen")
        check("缺 Key 拒绝切换", "DASHSCOPE_API_KEY" in msg, msg)


def test_qwen_fallback_build() -> None:
    print("== build_backend qwen 无 Key 回落 ==")
    st = replace(Settings.load(), dashscope_api_key="")
    with patch.object(registry, "_build_sherpa", return_value=FakeTTS("neural")):
        tts = build_backend("qwen", st, None, with_fallback=False)
    check("回落 neural", getattr(tts, "mode", "") == "neural", getattr(tts, "mode", ""))


def test_tools_set_tts() -> None:
    print("== tools.set_tts ==")
    from jarvis.ha import HAClient
    from jarvis.tools import Tools

    st = replace(Settings.load(), dashscope_api_key="")
    ha = HAClient(st)
    tools = Tools(ha, settings=st, dry_run=True)
    with patch.object(registry, "build_backend", side_effect=_fake_build):
        sw = SwitchableTTS(st, speaker=None)
        tools.bind_tts(sw)
        out = tools.dispatch("set_tts", {"backend": "say"})
    check("dispatch set_tts", "已切换" in out, out)
    names = [s["function"]["name"] for s in tools.schemas()]
    check("schema 含 set_tts", "set_tts" in names, str(names))


def main() -> int:
    test_normalize()
    test_switch_proxy()
    test_qwen_missing_key()
    test_qwen_fallback_build()
    test_tools_set_tts()
    time.sleep(0.05)
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
