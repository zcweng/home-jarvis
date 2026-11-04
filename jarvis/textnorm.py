"""TTS 前的文本归一化：把阿拉伯数字、拉丁字母读成中文。

为什么需要：本地神经 TTS（fanchen-C）的发音词典里只有汉字/拼音音素，
`10` / `tiger` 这类是 **OOV**，sherpa-onnx 会静默丢掉它：

    sherpa-onnx ... Ignore OOV '10'
    "好，10秒后叫你。"  →  实际念成 "好，秒后叫你。"
    Ignore OOV 'tiger'
    "老虎英文是 tiger。" → 实际念成 "老虎英文是。"

macOS `say` 和云端 TTS 会自己做数字/英文展开，所以这层主要给本地神经 TTS 用；
但函数本身是通用的，别的地方想用也行。

覆盖：整数、小数、百分比、时间、年月日、温度、长数字串（电话/编号）、
拉丁单词（常见词音译，其余按英文字母中文读名逐个念）。
"""

from __future__ import annotations

import re

_CN_DIGITS = "零一二三四五六七八九"
_CN_UNITS = ("", "十", "百", "千")
_CN_BIG = ("", "万", "亿", "万亿")


def _group_to_cn(g: int, leading: bool) -> str:
    """0..9999 一组，leading 表示这是整个数字的最高组（决定"十"要不要补"一"）。"""
    seg = ""
    zero_pending = False
    for ui in range(3, -1, -1):
        digit = (g // (10 ** ui)) % 10
        if digit == 0:
            if seg:
                zero_pending = True
            continue
        if zero_pending:
            seg += "零"
            zero_pending = False
        seg += _CN_DIGITS[digit] + _CN_UNITS[ui]
    if leading and seg.startswith("一十"):
        seg = seg[1:]  # 十、十一、十二…… 不说"一十"
    return seg


def _int_to_cn(n: int) -> str:
    """0 <= n < 1e16 → 中文读法。"""
    if n == 0:
        return "零"
    if n < 0:
        return "负" + _int_to_cn(-n)

    groups: list[int] = []
    while n > 0:
        groups.append(n % 10000)
        n //= 10000

    parts: list[str] = []
    need_zero = False
    highest = len(groups) - 1
    for gi in range(highest, -1, -1):
        g = groups[gi]
        if g == 0:
            # 中间空出的组要补一个"零"（如 10010 = 一万零一十）
            if parts:
                need_zero = True
            continue
        seg = _group_to_cn(g, leading=(gi == highest))
        if parts and (need_zero or g < 1000):
            seg = "零" + seg
        need_zero = False
        parts.append(seg + _CN_BIG[gi])

    return "".join(parts)


def _digits_to_cn(s: str) -> str:
    """逐位读：2026 → 二零二六（年份、电话号码用）。"""
    return "".join(_CN_DIGITS[int(c)] if c.isdigit() else c for c in s)


def _number_to_cn(token: str) -> str:
    """'10' → 十；'1.5' → 一点五；'0.5' → 零点五。"""
    if "." in token:
        whole, frac = token.split(".", 1)
        whole_part = _int_to_cn(int(whole)) if whole else "零"
        return whole_part + "点" + _digits_to_cn(frac)
    # 太长的纯数字（电话、编号、身份证）逐位读更像人话
    if len(token) >= 7:
        return _digits_to_cn(token)
    return _int_to_cn(int(token))


_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_TIME = re.compile(r"(\d{1,2}):(\d{2})")
_YEAR = re.compile(r"(\d{4})\s*年")
_MONTH_DAY = re.compile(r"(\d{1,2})\s*([月日号])")
_TEMP = re.compile(r"(\d+(?:\.\d+)?)\s*(?:℃|°C|摄氏度)")
_CELSIUS = "摄氏度"
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_LATIN_WORD = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)*")

# 英文字母中文读名（足够落在汉字词典里，避免 OOV）
_LETTER_CN = {
    "a": "诶",
    "b": "比",
    "c": "西",
    "d": "迪",
    "e": "伊",
    "f": "艾弗",
    "g": "吉",
    "h": "艾尺",
    "i": "艾",
    "j": "杰",
    "k": "开",
    "l": "艾勒",
    "m": "艾姆",
    "n": "恩",
    "o": "欧",
    "p": "批",
    "q": "吉优",
    "r": "艾儿",
    "s": "艾斯",
    "t": "提",
    "u": "优",
    "v": "维",
    "w": "达布刘",
    "x": "艾克斯",
    "y": "歪",
    "z": "贼德",
}

# 常见词音译：比逐字母拼更像「怎么念」；不够再回落到字母读名
_EN_WORD_CN = {
    "tiger": "泰格",
    "apple": "艾普尔",
    "banana": "巴纳纳",
    "hello": "哈喽",
    "ok": "欧凯",
    "okay": "欧凯",
    "wifi": "歪法",
    "ai": "诶艾",
    "jarvis": "贾维斯",
    "ios": "艾欧艾斯",
    "iphone": "艾风",
    "ipad": "艾派德",
    "usb": "优艾斯比",
    "cpu": "西批优",
    "gps": "吉批艾斯",
    "led": "艾勒伊迪",
    "tv": "提维",
}


def _latin_to_cn(word: str) -> str:
    key = re.sub(r"['’-]", "", word).lower()
    if not key:
        return word
    if key in _EN_WORD_CN:
        return _EN_WORD_CN[key]
    # 缩写/短词：逐字母；较长未知词也逐字母（中文 TTS 无法真念英文）
    return "".join(_LETTER_CN.get(ch, ch) for ch in key)


def clean_for_speech(text: str) -> str:
    """给多语种 TTS（Nano 等）用的薄清洗：去掉 markdown / emoji，保留中英文原词。"""
    if not text:
        return ""
    out = text.strip()
    out = re.sub(r"[*_`#~>]", "", out)
    out = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", out)  # ![alt](url)
    out = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", out)  # [text](url) → text
    # 去掉大部分 emoji / 杂符号，保留常用中英文标点
    out = re.sub(
        r"[\U0001F300-\U0001FAFF\U00002700-\U000027BF\U0001F000-\U0001F0FF]",
        "",
        out,
    )
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out.strip()


def normalize_for_tts(text: str, *, numbers: bool = True, latin: bool = True) -> str:
    """把文本里会被 TTS 丢掉的阿拉伯数字、拉丁词换成中文读法。

    Nano 等多语种后端应 latin=False（或根本不调用），否则 tiger 会被念成「泰格」。
    """
    if not text:
        return ""
    out = text.strip()
    # 全角数字 / 全角拉丁
    out = out.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    out = out.translate(
        str.maketrans(
            "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
            "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ",
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "abcdefghijklmnopqrstuvwxyz",
        )
    )

    if numbers:
        # 顺序很重要：先处理复合语义（百分比/时间/年月日/温度），最后才处理裸数字
        out = _TEMP.sub(lambda m: _number_to_cn(m.group(1)) + _CELSIUS, out)
        out = _PERCENT.sub(lambda m: "百分之" + _number_to_cn(m.group(1)), out)
        out = _TIME.sub(
            lambda m: _number_to_cn(m.group(1)) + "点" + (_number_to_cn(m.group(2)) if m.group(2) != "00" else "整"),
            out,
        )
        out = _YEAR.sub(lambda m: _digits_to_cn(m.group(1)) + "年", out)
        out = _MONTH_DAY.sub(lambda m: _number_to_cn(m.group(1)) + m.group(2), out)
        out = _NUMBER.sub(lambda m: _number_to_cn(m.group(0)), out)
    if latin:
        out = _LATIN_WORD.sub(lambda m: _latin_to_cn(m.group(0)), out)
    return out
