#!/usr/bin/env python3
"""专用 KWS 唤醒词自检。

覆盖三件事：
1. 中文 → 拼音 token 的转换是否正确（贾维斯 = j iǎ w éi s ī）
2. 合成语音能不能被真正检测到，且相似句/普通句不会误触发
3. **末尾 padding 是必需的**：不补 0.66s 静音 + input_finished，
   0.8 秒的短唤醒词永远检不出来（这是本项目踩过的坑）

    .venv/bin/python scripts/selftest_kws.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.config import Settings  # noqa: E402
from jarvis.kws import KeywordWake, text_to_tokens  # noqa: E402

POSITIVE = ["贾维斯", "小贾", "管家"]
NEGATIVE = ["今天天气不错", "把客厅灯打开", "关掉卧室的灯"]

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  —— {detail}" if detail else ""))


def synth(text: str) -> Path:
    out = Path(tempfile.mkdtemp(prefix="kws-")) / "q.wav"
    subprocess.run(
        ["/usr/bin/say", "-v", "Tingting", "--file-format=WAVE",
         "--data-format=LEI16@16000", "-o", str(out), text],
        check=True, capture_output=True, timeout=30,
    )
    return out


def main() -> int:
    st = Settings.load()
    model_dir = Path(ROOT / st.kws_model)
    if not (model_dir / "tokens.txt").exists():
        print(f"❌ 缺少 KWS 模型：{model_dir}\n   ./scripts/fetch_models.sh kws")
        return 1

    # ① 拼音 token 转换
    tokens = {l.split()[0] for l in (model_dir / "tokens.txt").read_text(encoding="utf-8").splitlines() if l.strip()}
    toks = text_to_tokens("贾维斯", tokens)
    check("拼音切分", toks == ["j", "iǎ", "w", "éi", "s", "ī"], str(toks))
    check("无法转换时礼貌返回", text_to_tokens("😀", tokens) is None, "emoji/非中文返回 None 而不是崩")

    # ② 生成关键词文件
    from jarvis.kws import build_keywords_file

    kwf = build_keywords_file(POSITIVE, model_dir)
    content = kwf.read_text(encoding="utf-8")
    check("关键词文件", "@贾维斯" in content and "@小贾" in content, content.replace("\n", " | "))

    # ③ 正例：合成语音应命中
    for text in POSITIVE:
        hits = KeywordWake.detect_file(synth(text), model_dir, POSITIVE)
        check(f"命中「{text}」", text in hits, str(hits))

    # ④ 反例：相似但不含唤醒词的句子不应命中
    for text in NEGATIVE:
        hits = KeywordWake.detect_file(synth(text), model_dir, POSITIVE)
        check(f"不误触「{text}」", not hits, str(hits))

    # ⑤ padding 的必要性：不 flush 时短唤醒词检不出
    from jarvis.audio import read_wav

    wav = synth("贾维斯")
    pcm, _rate = read_wav(wav)
    kw = KeywordWake(model_dir, POSITIVE)
    no_flush = []
    for i in range(0, pcm.size, 1024):
        r = kw.feed(pcm[i : i + 1024])
        if r:
            no_flush.append(r)
    kw2 = KeywordWake(model_dir, POSITIVE)
    for i in range(0, pcm.size, 1024):
        kw2.feed(pcm[i : i + 1024])
    with_flush = kw2.flush()
    check("末尾 padding 决定成败", not no_flush and "贾维斯" in with_flush,
          f"不 flush={no_flush} · flush 后={with_flush}")

    ok = all(r[1] for r in results)
    print("-" * 52)
    print("全部通过" if ok else f"失败 {sum(1 for r in results if not r[1])} 项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
