"""百度网页搜索（免费 SERP 抓取，无按次 API 费）。

给 LLM 工具用：返回可朗读的短摘要素材，不是原始 HTML。
页面改版或反爬时可能失败——失败句固定，让模型说「查不到」而不是编造。
"""

from __future__ import annotations

import re
from html import unescape

import httpx

BAIDU_MOBILE = "https://m.baidu.com/s"
BAIDU_DESKTOP = "https://www.baidu.com/s"
FAIL = "暂时搜不到相关结果，请稍后再试或换个说法。"
_MAX_CHARS = 300

_UA_MOBILE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)
_UA_DESKTOP = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


def _strip_tags(s: str) -> str:
    s = re.sub(r"<script[\s\S]*?</script>", "", s, flags=re.I)
    s = re.sub(r"<style[\s\S]*?</style>", "", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _is_junk_title(title: str) -> bool:
    if len(title) < 4:
        return True
    junk = ("生活服务", "大家都在搜", "相关搜索", "广告", "Microsoft Internet", "百度一下")
    return any(j in title for j in junk)


def parse_hits(html: str, *, count: int = 5) -> list[tuple[str, str]]:
    """从百度 HTML 抽 (标题, 摘要)。桌面/移动共用。供自检/mock 直接调用。"""
    hits: list[tuple[str, str]] = []
    for m in re.finditer(r"<h3[^>]*>([\s\S]*?)</h3>", html, flags=re.I):
        title = _strip_tags(m.group(1))
        if _is_junk_title(title):
            continue
        window = html[m.end() : m.end() + 600]
        abstract = ""
        for pat in (
            r'class="[^"]*c-abstract[^"]*"[^>]*>([\s\S]*?)</(?:span|div)',
            r'class="[^"]*cu-line-clamp[^"]*"[^>]*>([\s\S]*?)</(?:span|div)',
        ):
            am = re.search(pat, window, flags=re.I)
            if am:
                abstract = _strip_tags(am.group(1))
                if abstract:
                    break
        if not abstract:
            for raw in re.findall(r">([^<]{12,160})<", window):
                t = _strip_tags(raw)
                if not t or t == title or t.startswith("http"):
                    continue
                if any(x in t for x in ("广告", "相关搜索", "查看更多", "百度一下")):
                    continue
                abstract = t
                break
        hits.append((title[:80], abstract[:160]))
        if len(hits) >= count:
            break
    return hits


def format_hits(hits: list[tuple[str, str]], *, max_chars: int = _MAX_CHARS) -> str:
    if not hits:
        return FAIL
    parts: list[str] = []
    for title, abstract in hits:
        if abstract:
            parts.append(f"「{title}」：{abstract}")
        else:
            parts.append(f"「{title}」")
    text = "。".join(parts)
    if not text.endswith(("。", "！", "？", ".", "!", "?")):
        text += "。"
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip("，,、；; ") + "…"
    return text


def _fetch(url: str, params: dict[str, str], ua: str, timeout: float) -> str:
    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    with httpx.Client(
        headers=headers,
        timeout=timeout,
        follow_redirects=True,
        trust_env=False,
    ) as client:
        if "m.baidu.com" not in url:
            try:
                client.get("https://www.baidu.com/", timeout=min(3.0, timeout))
            except Exception:  # noqa: BLE001
                pass
        r = client.get(url, params=params)
        r.raise_for_status()
        return r.text


def search(query: str, *, count: int = 5, timeout: float = 5.0) -> str:
    """搜百度，返回给模型的短中文素材；失败返回 FAIL 固定句。"""
    q = (query or "").strip()
    if not q:
        return "搜索词为空。"
    count = max(1, min(int(count), 8))
    timeout = max(2.0, float(timeout))

    attempts = (
        (BAIDU_MOBILE, {"word": q}, _UA_MOBILE),
        (BAIDU_DESKTOP, {"wd": q, "ie": "utf-8", "rn": str(count)}, _UA_DESKTOP),
    )
    for url, params, ua in attempts:
        try:
            html = _fetch(url, params, ua, timeout)
            hits = parse_hits(html, count=count)
            if hits:
                return format_hits(hits)
        except Exception:  # noqa: BLE001
            continue
    return FAIL
