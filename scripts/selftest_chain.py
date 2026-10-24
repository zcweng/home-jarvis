#!/usr/bin/env python3
"""端到端链路自检：语音进 → 设备动 → 语音出。

用 `say` 合成"用户说话"，喂进完整链路（ASR → 会话 → 工具 → HA），
断言 HA 里的设备状态真的变了；模型用假模型，保证可重复、不花钱。

    .venv/bin/python scripts/selftest_chain.py          # 不朗读，只看状态
    .venv/bin/python scripts/selftest_chain.py --speak  # 顺便听一下回复

接真实米家设备前，这个脚本就是"链路是否通"的判据。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.app import Jarvis  # noqa: E402
from jarvis.audio import read_wav  # noqa: E402
from jarvis.config import Settings  # noqa: E402

def build_cases(name: str) -> list[tuple[str, str, object]]:
    return [
        (f"把{name}打开", f"{name} state == on", lambda st: st == "on"),
        ("把亮度调低一些", "亮度下降（只说调整、不说设备 → 必须命中上一台）", None),
        (f"把{name}关掉", f"{name} state == off", lambda st: st == "off"),
    ]


def synth(text: str) -> Path:
    out = Path(tempfile.mkdtemp(prefix="say-")) / "q.wav"
    subprocess.run(
        ["/usr/bin/say", "-v", "Tingting", "--file-format=WAVE",
         "--data-format=LEI16@16000", "-o", str(out), text],
        check=True, capture_output=True, timeout=30,
    )
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--speak", action="store_true", help="真的朗读回复")
    ap.add_argument("--real-llm", action="store_true", help="用真实 DeepSeek（需有效 key）")
    args = ap.parse_args()

    st = Settings.load()
    app = Jarvis(st, use_fake_llm=not args.real_llm, speak=args.speak)

    # 动态选一台在线的灯：demo 虚拟灯或真实米家设备都适用
    online = [
        d for d in app.tools.devices.values()
        if d.type == "light" and (app.ha.state(d.entity_id) or {}).get("state") not in (None, "unavailable")
    ]
    if not online:
        print("❌ 别名表里没有在线的灯（先完成米家 OAuth 或手动指定设备）")
        return 1
    dev = online[0]
    name = dev.name
    print(f"测试目标：{name}（{dev.entity_id}）")

    def light_state() -> str:
        return (app.ha.state(dev.entity_id) or {}).get("state", "?")

    def brightness() -> int:
        b = (app.ha.state(dev.entity_id) or {}).get("attributes", {}).get("brightness")
        return round(b / 255 * 100) if b is not None else -1

    # 前置条件：关灯、亮度设到 50%（否则相对调暗可能已经贴地板，测不出变化）
    app.tools.control(name, "off")
    app.tools.control(name, "brightness", 50)

    results: list[tuple[str, bool, str]] = []
    for i, (say_text, expect, check) in enumerate(build_cases(name), 1):
        print(f"\n=== [{i}] 语音输入：{say_text} ===")
        wav = synth(say_text)
        pcm, rate = read_wav(wav)
        if rate != 16000:
            import soxr

            pcm = soxr.resample(pcm, rate, 16000)

        before_b = brightness()
        app.handle(pcm)

        if i == 1:
            ok = light_state() == "on"
            results.append((f"{say_text} → 开灯", ok, f"state={light_state()}"))
        elif i == 2:
            after_b = brightness()
            ok = after_b < before_b or after_b == 10
            results.append((f"{say_text} → 调暗", ok, f"亮度 {before_b}% → {after_b}%"))
        else:
            ok = light_state() == "off"
            results.append((f"{say_text} → 关灯", ok, f"state={light_state()}"))

        print(f"  断言：{expect} → {'✅' if ok else '❌'}")

    print("\n" + "-" * 56)
    for name, ok, detail in results:
        print(f"{'✅' if ok else '❌'} {name}  —— {detail}")

    good = all(r[1] for r in results)
    print("-" * 56)
    print("端到端链路打通（语音 → 设备 → 语音）" if good else "有断言失败，先看上面的输出")
    app.ha.close()
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
