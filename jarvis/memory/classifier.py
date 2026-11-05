"""记忆分类器：规则快路径 + 可选 LLM JSON 分类。"""

from __future__ import annotations

import json
import re
import sys
from typing import Any, Callable

from .types import ClassifyResult, PERM_TYPES, SHORT_TYPES

LLMFn = Callable[[str], str]  # prompt → raw text

_REMEMBER = re.compile(r"(记住|记得|记一下|别忘了|以后都|从今以后|往后)")
_FORGET = re.compile(r"(忘掉|忘记|别再记|不要记|擦掉|删除记忆|清空记忆)")
_NAME = re.compile(r"(?:我叫|我的名字是|叫我)\s*([^\s，。！？,.]{1,12})")
_CITY = re.compile(r"(?:我家在|我住在|我在)\s*([^\s，。！？,.]+)")
_LIKE = re.compile(r"(不?喜欢|不?爱|不?想吃|不?吃|讨厌|偏好)\s*(.+)")
_RULE = re.compile(r"(以后|从今|永远|务必|必须|不要再|别再).{0,20}(叫我|称呼|用|说|做|开|关)")
_REMIND = re.compile(
    r"(提醒我|叫我|闹钟|定时).{0,30}|"
    r"(每天|每周|明天|后天|今晚|早上|中午|晚上).{0,10}(\d{1,2}\s*[点:：]\s*\d{0,2}|\d+\s*分钟)"
)
_CONFIRM_CLEAR = re.compile(r"(确认清空|确认删除全部|确认忘掉全部)")


def _warn(msg: str) -> None:
    print(f"  ⚠️  memory classifier：{msg}", file=sys.stderr)


def _slot_food(item: str) -> str:
    item = re.sub(r"[的了啊呀呢吧]", "", item.strip())
    item = re.sub(r"^(吃|喝|用)", "", item)
    item = item[:16]
    return f"food.{item}" if item else "food.unknown"


def rule_classify(user_text: str, reply: str = "") -> ClassifyResult | None:
    """命中规则则立刻返回；未命中返回 None 交给 LLM。"""
    t = (user_text or "").strip()
    if not t:
        return None

    if _CONFIRM_CLEAR.search(t):
        return ClassifyResult(
            should_store=False,
            erase={"all": True, "confirmed": True},
            content=t,
            summary="确认清空全部记忆",
        )

    if _FORGET.search(t):
        # 「清空所有记忆」需二次确认
        if re.search(r"(全部|所有).{0,4}(记忆|记得的)", t) or re.search(r"清空记忆", t):
            return ClassifyResult(
                should_store=False,
                erase={"all": True, "confirmed": False},
                content=t,
                summary="请求清空全部记忆",
            )
        target = re.sub(r"^(请|帮我|给我)?(忘掉|忘记|别再记|不要记|擦掉)", "", t).strip(" 。，,.!")
        return ClassifyResult(
            should_store=False,
            erase={"target": target or t},
            content=t,
            summary=f"请求遗忘：{target or t}",
        )

    m = _NAME.search(t)
    if m:
        name = m.group(1).strip()
        return ClassifyResult(
            should_store=True,
            layer="perm",
            type="profile",
            slot="name",
            profile_key="name",
            content=f"用户名字叫{name}",
            summary=f"用户姓名：{name}",
            importance=0.95,
            tags=["profile", "name"],
        )

    m = _CITY.search(t)
    if m and ("家" in t or "住" in t):
        city = m.group(1).strip()
        return ClassifyResult(
            should_store=True,
            layer="perm",
            type="profile",
            slot="city",
            profile_key="city",
            content=f"用户住在{city}",
            summary=f"用户城市：{city}",
            importance=0.9,
            tags=["profile", "city"],
        )

    if _REMIND.search(t):
        return ClassifyResult(
            should_store=True,
            layer="perm",
            type="task",
            slot="task.reminder",
            content=t,
            summary=t,
            importance=0.85,
            tags=["task", "reminder"],
            schedule={"hint": t, "repeat": "once"},
        )

    if _RULE.search(t) or ( _REMEMBER.search(t) and re.search(r"(叫我|称呼|用|必须|不要|别)", t) ):
        return ClassifyResult(
            should_store=True,
            layer="perm",
            type="rule",
            slot="rule.user",
            content=t,
            summary=t,
            importance=0.9,
            tags=["rule"],
        )

    # 偏好：喜欢/不喜欢 + 记住
    if _REMEMBER.search(t) or re.search(r"(喜欢|不喜欢|爱吃|不吃|讨厌)", t):
        m = _LIKE.search(t)
        if m:
            verb = m.group(1)
            item = m.group(2).strip(" 。，,.!")
            # 截断到短语，并去掉「吃/喝」前缀（「不喜欢吃苹果」→ 苹果）
            item = re.split(r"[，,。！？\s]", item)[0][:20]
            item = re.sub(r"^(吃|喝|用)", "", item).strip()
            neg = verb.startswith("不") or "讨厌" in verb or verb in ("不吃", "不想吃")
            # 「不吃香菜」类：verb 已含吃
            if verb in ("不吃", "吃", "不想吃", "爱吃"):
                content = f"用户{verb}{item}"
            else:
                content = f"用户{'不' if neg else ''}喜欢{item}"
            is_food = bool(re.search(r"(吃|喝|味道|食物|菜|水果)", t + verb)) or bool(
                re.search(r"(苹果|香蕉|香菜|辣椒|葱|蒜|咖啡|茶|牛奶)", item)
            )
            slot = _slot_food(item) if is_food else f"pref.{item}"
            return ClassifyResult(
                should_store=True,
                layer="perm",
                type="preference",
                slot=slot,
                content=content,
                summary=content,
                importance=0.8,
                tags=["preference", "food" if is_food else "pref"],
                conflict_hint="preference",
            )
        if _REMEMBER.search(t):
            body = re.sub(r"^(请|帮我)?(记住|记得|记一下|别忘了)", "", t).strip(" ：:。")
            return ClassifyResult(
                should_store=True,
                layer="perm",
                type="preference",
                slot="",
                content=body or t,
                summary=body or t,
                importance=0.75,
                tags=["preference"],
            )

    return None


_LLM_PROMPT = """你是家庭语音管家的记忆分类器。根据用户话和助手回复，判断要不要写入长期记忆。
只输出 JSON，不要其它文字。schema：
{{
  "should_store": bool,
  "layer": "perm"|"short",
  "type": "profile"|"preference"|"rule"|"task"|"episode"|"context"|"digest",
  "slot": "string，可空；档案用 name/city；偏好用 food.苹果 这种",
  "content": "要存的原文事实",
  "summary": "一句话摘要",
  "importance": 0.0-1.0,
  "tags": ["..."],
  "conflict_hint": "",
  "profile_key": "",
  "schedule": null 或 {{"hint":"...","at":null,"repeat":"once|daily|weekly"}},
  "erase": null 或 {{"target":"...","slot":"...","all":false}}
}}
纯闲聊、情绪、工具回执、设备瞬时状态 → should_store=false。
用户：{user}
助手：{reply}
"""


def _parse_llm_json(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if not text:
        return None
    # 剥 code fence
    if "```" in text:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if m:
            text = m.group(1).strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        # 尝试截取第一个 { ... }
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None


def llm_classify(user_text: str, reply: str, llm: LLMFn) -> ClassifyResult | None:
    try:
        raw = llm(_LLM_PROMPT.format(user=user_text, reply=reply or ""))
    except Exception as e:  # noqa: BLE001
        _warn(f"LLM 调用失败：{e}")
        return None
    data = _parse_llm_json(raw)
    if not data:
        _warn("LLM 返回非 JSON，已忽略")
        return None
    layer = data.get("layer") or "short"
    typ = data.get("type") or "episode"
    if layer == "perm" and typ not in PERM_TYPES:
        typ = "preference"
    if layer == "short" and typ not in SHORT_TYPES:
        typ = "episode"
    return ClassifyResult(
        should_store=bool(data.get("should_store")),
        layer=layer,  # type: ignore[arg-type]
        type=typ,  # type: ignore[arg-type]
        slot=str(data.get("slot") or data.get("profile_key") or ""),
        content=str(data.get("content") or user_text),
        summary=str(data.get("summary") or data.get("content") or user_text),
        importance=float(data.get("importance") or 0.5),
        tags=[str(x) for x in (data.get("tags") or [])],
        conflict_hint=str(data.get("conflict_hint") or ""),
        profile_key=str(data.get("profile_key") or ""),
        schedule=data.get("schedule") if isinstance(data.get("schedule"), dict) else None,
        erase=data.get("erase") if isinstance(data.get("erase"), dict) else None,
        raw=data,
    )


def classify(
    user_text: str,
    reply: str = "",
    *,
    llm: LLMFn | None = None,
    min_importance: float = 0.45,
) -> ClassifyResult:
    """统一入口：规则优先，否则 LLM；失败则 should_store=False。"""
    hit = rule_classify(user_text, reply)
    if hit is not None:
        if hit.erase:
            return hit
        if hit.should_store and hit.importance < min_importance:
            hit.should_store = False
        return hit
    if llm is None:
        return ClassifyResult(should_store=False, content=user_text)
    result = llm_classify(user_text, reply, llm)
    if result is None:
        return ClassifyResult(should_store=False, content=user_text)
    if result.erase:
        return result
    if result.should_store and result.importance < min_importance:
        result.should_store = False
    return result
