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


# 天气专用（中国气象局工具）；与 web_search 分流
# 「跑步/出门/穿外套」这类没有天气词但在问户外条件的也要算，
# 否则模型不调 weather 时守卫不会兜底（selftest_llm 第 6 条原先就是被换语料绕开的）。
_WEATHER = re.compile(
    r"(天气|气温|下雨|下雪|雨雪|预报|几度|穿什么|穿(外套|多厚|多少)|带伞|带不带伞|"
    r"湿度|风力|冷不冷|热不热|适合.*(出门|出去|跑步|运动|洗车|晾|晒))"
)

# 本地时间/日期（本机时钟）；须排在 web_search 之前，否则「几点了」会被「几点」拖去联网
_TIME = re.compile(
    r"((现在|当前).{0,4}(几点|时间|日期|几号|星期几|周几)|"
    r"(几点了|几点钟了|几时了|几号了|星期几了|周几了)|"
    r"(今天|现在)(是)?(星期几|周几|几号|几月几号|什么日子)|"
    r"本地时间|报时)"
)
_TIME_EXTERNAL = re.compile(r"(开门|营业|起飞|发车|开场|放映|截止|到点|班次)")

# 需要联网的时效/外部事实（宁可多判；误判只会多一次搜索）
_WEB = re.compile(
    r"(新闻|头条|热搜|股价|汇率|比分|"
    r"几点|营业|门票|攻略|好玩|怎么走|多少钱|最新|今天|明天|昨天|"
    r"查一下|搜一下|网上|百度|百科|谁是|哪年|发生了什么|"
    r"天气|气温|下雨|下雪|预报)"  # 天气工具关闭时仍可走搜索兜底
)


def looks_like_weather(text: str) -> bool:
    """是否像查天气（用于强制调 weather 工具）。"""
    t = (text or "").strip()
    if not t:
        return False
    if looks_like_command(t):
        return False
    return _WEATHER.search(t) is not None


def looks_like_time(text: str) -> bool:
    """是否像问本机时间/日期/星期（用于强制调 get_time）。"""
    t = (text or "").strip()
    if not t:
        return False
    if looks_like_command(t):
        return False
    # 「电影院几点开门」走联网，不抢成本地时钟
    if _TIME_EXTERNAL.search(t):
        return False
    return _TIME.search(t) is not None


def looks_like_web_search(text: str) -> bool:
    """是否像需要联网搜索的问句（用于未调 web_search 时强制重问）。"""
    t = (text or "").strip()
    if not t:
        return False
    # 家电控制优先走 control，不抢成搜索
    if looks_like_command(t):
        return False
    # 本地报时优先 get_time
    if looks_like_time(t):
        return False
    return _WEB.search(t) is not None
