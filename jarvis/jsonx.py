"""从模型输出里抠 JSON 对象（分类器、任务规划器共用）。

模型经常不老实：包一层 ```json 围栏、前后加一句「好的，结果如下」、
或者干脆输出半个对象。这里只做一件事——尽力拿到第一个合法 JSON 对象，
拿不到就返回 None，让调用方走保守分支。
"""

from __future__ import annotations

import json
import re
from typing import Any


def parse_json_object(raw: str) -> dict[str, Any] | None:
    """尽力把一段文本解析成 JSON 对象；失败返回 None。"""
    text = (raw or "").strip()
    if not text:
        return None
    # 剥 ```json ... ``` 围栏
    if "```" in text:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if m:
            text = m.group(1).strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        # 退一步：截取第一个 { ... }（贪婪匹配到最后一个 }）
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None