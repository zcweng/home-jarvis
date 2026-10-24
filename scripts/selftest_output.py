#!/usr/bin/env python3
"""语音输出路由自检（不需要真音箱，不会出声）。

覆盖：
1. TTS_TARGET=speaker 时走 HA notify，不碰电脑喇叭
2. 找不到音箱 / notify 报错时回落到电脑喇叭（SPEAKER_FALLBACK_LOCAL）
3. TTS_TARGET=local 时只走电脑喇叭
4. TTS_TARGET=both 时两边都走
5. 长文本按句切分（米家播放文本有长度限制）

    .venv/bin/python scripts/selftest_output.py
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.config import Settings  # noqa: E402
from jarvis.ha import HAError  # noqa: E402
from jarvis.output import SpeechRouter, split_for_speech  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  —— {detail}" if detail else ""))


class FakeHA:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail = fail

    def notify(self, entity_id: str, message: str):
        if self.fail:
            raise HAError("模拟云端失败")
        self.calls.append((entity_id, message))
        return []


class FakeLocal:
    """替身：只记录，不出声。"""

    voice = "Tingting"
    mode = "direct"

    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.speaking = False

    def speak(self, text: str, blocking: bool = True):
        self.spoken.append(text)
        return None

    def warm(self, phrases):
        return 0

    def play_cached(self, text: str, blocking: bool = False) -> float:
        self.spoken.append(text)
        return 0.0

    def wait_idle(self, timeout: float = 2.0) -> None:
        return None

    def stop(self) -> None:
        return None


def make(target: str, *, entity=True, fail=False, fallback=True, max_chars: int = 80):
    st = replace(Settings.load(), tts_target=target, speaker_fallback_local=fallback,
                 announce_max_chars=max_chars)
    ha = FakeHA(fail=fail)
    local = FakeLocal()
    router = SpeechRouter(
        st, ha, local,
        notify_entity=lambda: ("notify.xiaomi_x_play_text", "Xiaomi 智能音箱 Pro") if entity else ("", ""),
    )
    return router, ha, local


def main() -> int:
    # 1. speaker 优先
    router, ha, local = make("speaker")
    router.speak("客厅灯已经打开了。")
    check("speaker：走 HA notify", len(ha.calls) == 1, str(ha.calls))
    check("speaker：不碰电脑喇叭", local.spoken == [], str(local.spoken))

    # 2. 找不到音箱 → 回落本地
    router, ha, local = make("speaker", entity=False)
    router.speak("回落测试")
    check("无音箱：回落电脑喇叭", local.spoken == ["回落测试"], str(local.spoken))

    # 3. notify 报错 → 回落本地
    router, ha, local = make("speaker", fail=True)
    router.speak("云端失败回落")
    check("notify 报错：回落电脑喇叭", local.spoken == ["云端失败回落"], str(local.spoken))

    # 4. local 模式
    router, ha, local = make("local")
    router.speak("只走本机")
    check("local：只走电脑喇叭", local.spoken == ["只走本机"] and not ha.calls, str(local.spoken))

    # 5. both 模式
    router, ha, local = make("both")
    router.speak("两边都说")
    check("both：两边都走", len(ha.calls) == 1 and local.spoken == ["两边都说"],
          f"notify={len(ha.calls)} local={local.spoken}")

    # 6. 长文本切分
    long_text = "今天下午三点有暴雨，记得把阳台的衣服收进来。晚上七点小区会停水，提前接好水。明天早上八点你有个会。"
    parts = split_for_speech(long_text, 40)
    check("长文本按句切分", len(parts) >= 2 and all(len(p) <= 40 for p in parts), f"{len(parts)} 段")
    router, ha, local = make("speaker", max_chars=40)
    router.speak(long_text)
    check("长文本分多条 notify", len(ha.calls) == len(parts) and len(ha.calls) >= 2,
          f"notify {len(ha.calls)} 条 / 切分 {len(parts)} 段")

    # 7. 空文本不出声
    router, ha, local = make("both")
    router.speak("   ")
    check("空文本不倒腾", not ha.calls and not local.spoken)

    ok = all(r[1] for r in results)
    print("-" * 52)
    print("全部通过" if ok else f"失败 {sum(1 for r in results if not r[1])} 项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
