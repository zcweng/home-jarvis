"""中国气象局 weather.cma.cn 天气查询（无 API key）。

搜索框对应：GET /api/autocomplete?q=城市
实况：GET /api/now/{station_id}
预报：GET /api/weather/{station_id}

页面 HTML 有 WAF，工具只打 JSON 接口。
"""

from __future__ import annotations

import re
from typing import Any

import httpx

BASE = "https://weather.cma.cn"
FAIL = "暂时查不到天气，请稍后再试。"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


def _headers() -> dict[str, str]:
    return {
        "User-Agent": _UA,
        "Referer": f"{BASE}/",
        "Accept": "application/json,text/plain,*/*",
    }


def _get_json(path: str, *, params: dict[str, str] | None = None, timeout: float) -> dict[str, Any]:
    with httpx.Client(headers=_headers(), timeout=timeout, follow_redirects=True) as client:
        r = client.get(f"{BASE}{path}", params=params)
        r.raise_for_status()
        data = r.json()
    if not isinstance(data, dict) or data.get("code") not in (0, "0"):
        raise RuntimeError(data.get("msg") if isinstance(data, dict) else "bad response")
    return data


def _normalize_city(city: str) -> str:
    t = (city or "").strip()
    t = re.sub(r"(市|地区|自治州|盟)$", "", t)
    return t.strip()


def autocomplete(query: str, *, timeout: float = 3.0) -> list[tuple[str, str, str, str]]:
    """返回 [(id, 中文名, 拼音/外文, 国家), ...]。"""
    q = (query or "").strip()
    if not q:
        return []
    data = _get_json("/api/autocomplete", params={"q": q}, timeout=timeout)
    out: list[tuple[str, str, str, str]] = []
    for raw in data.get("data") or []:
        parts = str(raw).split("|")
        if len(parts) < 4:
            continue
        out.append((parts[0], parts[1], parts[2], parts[3]))
    return out


def resolve_station(city: str, *, timeout: float = 3.0) -> tuple[str, str] | None:
    """城市名 → (station_id, 显示名)。优先中国境内精确匹配。"""
    q = _normalize_city(city)
    if not q:
        return None
    hits = autocomplete(q, timeout=timeout)
    if not hits:
        return None
    cn = [h for h in hits if h[3] == "中国"] or hits
    for hid, name, _en, _country in cn:
        if name == q or name == city.strip():
            return hid, name
    for hid, name, _en, _country in cn:
        if q in name or name in q:
            return hid, name
    hid, name, _en, _country = cn[0]
    return hid, name


def _fmt_temp(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "?"
    if abs(f - round(f)) < 0.05:
        return str(int(round(f)))
    return f"{f:.1f}".rstrip("0").rstrip(".")


def _day_label(date_s: str, idx: int) -> str:
    if idx == 0:
        return "今天"
    if idx == 1:
        return "明天"
    if idx == 2:
        return "后天"
    # 2026/11/03 → 11月3日
    m = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})", date_s or "")
    if m:
        return f"{int(m.group(2))}月{int(m.group(3))}日"
    return date_s or f"第{idx + 1}天"


def format_report(now_data: dict[str, Any], daily_data: dict[str, Any], *, days: int = 3) -> str:
    loc = (now_data.get("location") or daily_data.get("location") or {})
    name = loc.get("name") or "当地"
    now = now_data.get("now") or {}
    parts: list[str] = []
    if now:
        wind = (now.get("windDirection") or "").replace("9999", "")
        scale = now.get("windScale") or ""
        wind_s = f"{wind}{scale}" if wind and wind != "9999" else (scale or "")
        line = f"{name}现在{_fmt_temp(now.get('temperature'))}度"
        # dayText 不在 now 里，用今日预报补一句
        daily0 = ((daily_data.get("daily") or [None])[0]) or {}
        sky = daily0.get("dayText") or daily0.get("nightText") or ""
        if sky:
            line += f"，{sky}"
        if wind_s:
            line += f"，{wind_s}"
        hum = now.get("humidity")
        if hum is not None:
            try:
                line += f"，湿度{int(float(hum))}%"
            except (TypeError, ValueError):
                pass
        parts.append(line + "。")

    dailies = list(daily_data.get("daily") or [])[: max(1, days)]
    for i, d in enumerate(dailies):
        label = _day_label(str(d.get("date") or ""), i)
        day_t = d.get("dayText") or ""
        night_t = d.get("nightText") or ""
        if day_t and night_t and day_t != night_t:
            sky = f"{day_t}转{night_t}"
        else:
            sky = day_t or night_t or ""
        hi = _fmt_temp(d.get("high"))
        lo = _fmt_temp(d.get("low"))
        parts.append(f"{label}{sky}，{hi}到{lo}度。")

    text = "".join(parts).strip()
    return text or FAIL


def lookup(city: str, *, timeout: float = 3.0, days: int = 3) -> str:
    """查城市天气，返回可朗读短句。"""
    timeout = max(1.5, float(timeout))
    try:
        resolved = resolve_station(city, timeout=timeout)
        if resolved is None:
            return f"找不到「{city}」这个地方，换个城市名再试。"
        sid, _name = resolved
        now_pack = _get_json(f"/api/now/{sid}", timeout=timeout)
        daily_pack = _get_json(f"/api/weather/{sid}", timeout=timeout)
        now_data = now_pack.get("data") or {}
        daily_data = daily_pack.get("data") or {}
        if not now_data and not daily_data:
            return FAIL
        return format_report(now_data, daily_data, days=days)
    except Exception:  # noqa: BLE001
        return FAIL
