"""唤醒词。

P0 用 **ASR 门控** 而不是专用 KWS 模型：
- 唤醒词成本为零（复用已有的 VAD + SenseVoice），中文任意词都行
- 静音时不耗 CPU（不像常开 ASR 那样一直解码）
- 天然支持"唤醒词 + 指令"一句话说完："贾维斯，打开客厅灯"

代价：必须等一句话说完才能判唤醒（没有专门 KWS 的"说到一半就亮灯"）。
专用 KWS 实现在 `jarvis/kws.py`（sherpa-onnx KeywordSpotter + 拼音 token），
现在就绪：`WAKE_BACKEND=kws`（默认）。本模块的 `WakeGate` 在两种后端里都用于
"剥离唤醒词 + 维护连续对话窗口"。

连续对话：唤醒后进入 FOLLOW_UP 窗口（默认 8 秒），窗口内说话不需要再说唤醒词。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

DEFAULT_WORDS = ("贾维斯", "小贾", "管家", "小智")

# 退出词：说完立刻关掉连续对话窗口，下次必须重新喊唤醒词
DEFAULT_SLEEP_WORDS = (
    "退下吧", "退下", "退下吧你", "退出", "退出对话", "结束", "结束对话",
    "再见", "拜拜", "不聊了", "没事了", "休息吧", "你去忙吧",
)

# 允许的虚词前后缀（"好的，退下吧" / "退下吧啊" 都算）
_LEAD_FILLERS = ("好的", "好", "行", "那", "嗯", "诶", "ok", "OK")
_TAIL_FILLERS = ("吧", "啊", "呀", "了", "呢", "哦", "哈", "啦")

# 唤醒词后面常见的连接词/标点，去掉它们才是真正的指令
_FILLERS = ("，", ",", "。", "！", "!", "啊", "呀", "呢", "那个", "帮我", "请")
# 判唤醒时先去掉的标点/空格（ASR 输出带标点，索引会错位）
_NOISE = "，。！？、,.!? \n\t"


def _clean(text: str) -> str:
    return "".join(ch for ch in text if ch not in _NOISE)


def _lev(a: str, b: str) -> int:
    if len(a) != len(b):
        return max(len(a), len(b))
    return sum(x != y for x, y in zip(a, b))


def _fuzzy_index(text: str, word: str) -> tuple[int, int] | None:
    """在 text 里找 word，允许 1 个字听错（3 字及以上的词）。

    实测："贾维斯" 被 ASR 识别成 "亚维斯"（jiǎ → yà），严格匹配会直接失效。
    少一个字就容 1 个错，2 字词不宽容（"管家"错成"关家"太容易误触发）。
    """
    n = len(word)
    if n == 0 or len(text) < n:
        return None
    allowed = 1 if n >= 3 else 0
    best: tuple[int, int] | None = None
    for i in range(len(text) - n + 1):
        seg = text[i : i + n]
        d = _lev(seg, word)
        if d <= allowed and (best is None or d < best[0]):
            best = (d, i)
    if best is None:
        return None
    return best[1], best[1] + n


def is_sleep_request(text: str, words: tuple[str, ...] = DEFAULT_SLEEP_WORDS) -> bool:
    """是不是"退下/结束"这类收线指令。

    只在**整句就是退出词**时才算（允许带"好的/吧/啊"这类虚词），
    否则"把空调退出节能模式"也会被当成让管家退下。
    """
    t = _clean(text)
    if not t:
        return False
    changed = True
    while changed:
        changed = False
        for f in _LEAD_FILLERS:
            if t.startswith(f) and len(t) > len(f):
                t = t[len(f) :]
                changed = True
        for f in _TAIL_FILLERS:
            if t.endswith(f) and len(t) > len(f):
                t = t[: -len(f)]
                changed = True
    if t in words:
        return True
    # ASR 偶尔多出一个字（"退下吧你"），短句允许一次前缀包含
    return any(t.startswith(w) and len(t) - len(w) <= 2 for w in words)


@dataclass
class WakeGate:
    words: tuple[str, ...] = DEFAULT_WORDS
    follow_up_seconds: float = 8.0
    _last_wake: float = field(default=0.0, init=False)
    _awake: bool = field(default=False, init=False)
    # 处理中（思考 + 播报）挂起窗口：否则 8 秒会被 LLM 和 TTS 吃掉一大半
    _holding: bool = field(default=False, init=False)

    def words_hint(self) -> str:
        return "、".join(self.words)

    @property
    def awake(self) -> bool:
        """是否仍在连续对话窗口内（处理中恒为真，窗口不在此期间倒计时）。"""
        if self._holding:
            return True
        if not self._awake:
            return False
        if time.monotonic() - self._last_wake > self.follow_up_seconds:
            self._awake = False
            return False
        return True

    def hold(self) -> None:
        """一轮开始处理（思考 + 播报）：窗口暂停计时。

        用户反馈：回复还没说完，8 秒就过完了，导致想接着说话得重新喊唤醒词。
        根因是窗口从"唤醒那一刻"开始算，而 LLM 1.5s + 播报 2~3s 都在吃这 8 秒。
        """
        self._holding = True
        self._awake = True

    def release(self) -> None:
        """一轮播报结束：窗口从**现在**重新开始计时。"""
        self._holding = False
        self._last_wake = time.monotonic()
        self._awake = True

    def remaining(self) -> float:
        """窗口还剩几秒（调试/提示用）。"""
        if self._holding:
            return self.follow_up_seconds
        if not self._awake:
            return 0.0
        return max(0.0, self.follow_up_seconds - (time.monotonic() - self._last_wake))

    def touch(self) -> None:
        """每完成一轮对话刷新窗口。"""
        self._last_wake = time.monotonic()
        self._awake = True

    def sleep(self) -> None:
        self._awake = False
        self._holding = False
        self._last_wake = 0.0

    def feed(self, text: str) -> tuple[bool, str]:
        """判断这句话是否该响应。

        返回 (是否响应, 去除唤醒词后的指令)。
        - 在连续对话窗口内：任何话都响应
        - 否则必须包含唤醒词；唤醒词后面的部分若不为空，直接当指令
        """
        t = (text or "").strip()
        if not t:
            return False, ""

        if self.awake:
            self.touch()
            return True, t

        cleaned = _clean(t)
        for word in self.words:
            span = _fuzzy_index(cleaned, word)
            if span is None:
                continue
            _start, end = span
            rest = cleaned[end:]
            for filler in _FILLERS:
                rest = rest[len(filler) :] if rest.startswith(filler) else rest
            self.touch()
            return True, rest.strip()

        return False, ""
