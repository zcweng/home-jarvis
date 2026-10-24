"""轻量意图判定：只回答一个问题——"这句话是不是在命令设备？"

用途：作为**编排层的保险**。实测发现模型偶尔会"凭上下文回答"：
用户说"把客厅灯关掉"，它直接回"客厅灯已经关了"，一次工具都没调，灯根本没动。
这种幻觉只靠 prompt 是堵不住的，所以加一道规则兜底：

    看起来是命令 + 模型没调工具  →  强制重问一次（tool_choice 锁到 control）

规则故意写得"宁可多判"：多判的代价只是失败时多一次请求，
漏判的代价是"说做了但没做"——后者更不可接受。
"""

from __future__ import annotations

import re

# 命令动词/结构（宁可多判）
_CONTROL = re.compile(
    r"(打开|开启|关掉|关上|关闭|关一下|开一下|调|设成|设为|换成|开到|调到|"
    r"亮一点|亮一些|暗一点|暗一些|低一点|高一点|温度|亮度|色温|定时|提醒)"
)

# 纯提问/陈述，不算命令
_QUESTION = re.compile(r"(吗|？|\?|多少|什么状态|开着没|是不是|有没有|怎么样)")


def looks_like_command(text: str) -> bool:
    """是否像一条控制指令（用于失败重试兜底）。"""
    t = (text or "").strip()
    if not t:
        return False
    if _CONTROL.search(t) is None:
        return False
    # "灯开着吗"这类疑问句交给模型自由判断，不强制工具
    if _QUESTION.search(t) and not re.search(r"(帮我|给我|把|请|麻烦)", t):
        return False
    return True


def looks_like_state_question(text: str) -> bool:
    t = (text or "").strip()
    return bool(_QUESTION.search(t) or re.search(r"(开着|关着|状态|几度)", t))
