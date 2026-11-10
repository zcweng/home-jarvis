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
# 含升降：ASR 常把「上升」听成「上身」；「再升一次/再生一次」也要强制走 control
_CONTROL = re.compile(
    r"(打开|开启|关掉|关上|关闭|关一下|开一下|调|设成|设为|换成|开到|调到|"
    r"亮一点|亮一些|暗一点|暗一些|低一点|高一点|温度|亮度|色温|定时|提醒|"
    r"上升|下降|升起|降下|升上去|降下来|收起|放下|上身|下身|"
    r"再(升|降|开|关|来|做|试|整)|再生一次|再来一次|停一下|停下)"
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


# ---------------------------------------------------------------- 多意图门控
#
# 只有「一句话里明显有好几件事」才值得走任务编排（先花一次规划 LLM）。
# 判定方式是**按标点切小句、逐句看像不像一件事**，而不是数动词——
# 数动词会把「把亮度调低一点」当成两件事，用户会莫名其妙听到两句播报。
_CLAUSE = re.compile(r"[^。！？!?；;\n，,]+")

# 已经在聊任务本身（查询/取消）时不编排：那属于 tasks 工具的活
_TASK_META = re.compile(
    r"(任务|进度|做到哪|还有几件|排队|剩下的|刚才.{0,4}(事|任务)|"
    r"取消.{0,6}(任务|刚才|剩下)|全部取消|都取消)"
)


# 口语里的命令比 _CONTROL 松：「把客厅灯关了吧」「开一下」都不在里面。
# 这里只用于"数几件事"，宁可多算一句，代价仅仅是多一次规划请求。
_LOOSE_TASK = re.compile(
    r"(打开|开一下|开启|开开|关掉|关上|关一下|关了|关闭|调到|调成|设成|设为|切到|切回|"
    r"把.{0,10}(开|关|调|设)|升上去|降下来|升起|降下|收起来|放下来|亮一点|暗一点|"
    r"播报|广播|提醒我|查一下|搜一下|看一下|"
    r"几点|天气|下雨|气温)"
)


def _is_a_task(clause: str) -> bool:
    return bool(
        looks_like_command(clause)
        or looks_like_weather(clause)
        or looks_like_time(clause)
        or looks_like_web_search(clause)
        or _LOOSE_TASK.search(clause)
    )


def count_task_intents(text: str) -> int:
    """数一句话里像「一件事」的小句有几条。"""
    n = 0
    for raw in _CLAUSE.findall(text or ""):
        clause = raw.strip()
        if len(clause) < 2:
            continue
        if _is_a_task(clause):
            n += 1
    return n


def looks_like_multi_task(text: str) -> bool:
    """是否该交给任务编排器（≥2 件互不相干的事）。"""
    t = (text or "").strip()
    if len(t) < 4:
        return False
    if _TASK_META.search(t):
        return False
    return count_task_intents(t) >= 2
