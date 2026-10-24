#!/usr/bin/env python3
"""TTS 文本归一化自检：数字必须被念出来。

背景：本地神经 TTS（fanchen-C）的词典只有汉字音素，`10` 是 OOV，sherpa-onnx 会**静默丢弃**：

    Ignore OOV '10'
    "好，10秒后叫你。"  →  念成 "好，秒后叫你。"

这个脚本检查三件事：
1. 中文数字转换的正确性（壹拾/拾/一十 这些边界最容易错）
2. 神经 TTS 合成时**不再出现 OOV 日志**，且音频时长正常（不是把整句吞掉）
3. macOS `say` 本来就会自己念数字（对照组，确认没被我们改坏）

    .venv/bin/python scripts/selftest_tts_text.py          # 全跑（需要模型 + macOS 的 say）
    .venv/bin/python scripts/selftest_tts_text.py --pure   # 只跑第 1 项，纯 Python，CI 用
"""

from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.textnorm import _int_to_cn, normalize_for_tts  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  —— {detail}" if detail else ""))


INT_CASES = {
    0: "零", 10: "十", 11: "十一", 12: "十二", 20: "二十", 101: "一百零一",
    110: "一百一十", 1234: "一千二百三十四", 2005: "二千零五",
    10010: "一万零一十", 100000: "十万", 1000001: "一百万零一", 100000000: "一亿",
}

SENTENCE_CASES = {
    "好，10秒后叫你。": "十秒",
    "把亮度调到30%": "百分之三十",
    "空调26度": "二十六度",
    "下午3点15分提醒我": "三点十五分",
    "2026年10月24日": "二零二六年十月二十四日",
    "1.5米": "一点五米",
    "第3个灯": "第三个灯",
    "温度26℃": "二十六摄氏度",
    "12:00出发": "十二点整",
}


def main() -> int:
    # --pure：只跑纯文本归一化断言，不碰模型、不碰 `say`（CI / 无音频环境用）
    pure = "--pure" in sys.argv[1:]

    for n, want in INT_CASES.items():
        got = _int_to_cn(n)
        check(f"整数 {n} → {want}", got == want, got if got != want else "")

    for src, must in SENTENCE_CASES.items():
        out = normalize_for_tts(src)
        check(f"句子 {src}", must in out, out if must not in out else "")

    if pure:
        print("（--pure：跳过神经 TTS 与 say 的音频检查）")
        ok = all(r[1] for r in results)
        print("-" * 52)
        print("全部通过" if ok else f"失败 {sum(1 for r in results if not r[1])} 项")
        return 0 if ok else 1

    # 神经 TTS：不能出现 OOV，音频时长要跟字数大致匹配
    from jarvis.config import Settings

    st = Settings.load()
    model_dir = Path(ROOT / st.tts_model_dir)
    if model_dir.exists() and any(model_dir.glob("*.onnx")):
        from jarvis.tts_neural import SherpaTTS

        tts = SherpaTTS(str(model_dir), speed=st.tts_speed)
        for text in ["好，10秒后叫你。", "把亮度调到30%，空调26度。"]:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                pcm, rate = tts.synthesize(text)
            dur = pcm.size / rate
            check(f"神经 TTS 无 OOV：{text}", "OOV" not in err.getvalue(), err.getvalue().strip()[:60])
            check(f"音频时长合理：{text}", dur > 0.8, f"{dur:.2f}s")
    else:
        print(f"（跳过神经 TTS 检查：没有模型 {model_dir}）")

    # 对照组：say 自己会念数字
    from jarvis.tts import SayTTS

    say = SayTTS(st.tts_voice_say, "wav", rate=st.say_rate)
    pcm, rate = say.synthesize("10秒后叫你")
    check("say 能念数字", pcm.size / rate > 0.5, f"{pcm.size / rate:.2f}s")

    ok = all(r[1] for r in results)
    print("-" * 52)
    print("全部通过" if ok else f"失败 {sum(1 for r in results if not r[1])} 项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
