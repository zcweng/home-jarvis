"""联网搜索：SearXNG 摘要 + Crawl4AI 全文（local-web-search）。

给 LLM 工具用：search 返回带 result_id 的摘要；fetch 按需取全文切片。
失败句固定，让模型说「查不到」而不是编造。

注意：AgentRuntime 在常驻事件循环里同步调 Tools.dispatch，
因此这里不能裸调 asyncio.run（会撞「loop 已在运行」）——
无 loop 时用 asyncio.run，有 loop 时丢到独立线程再 run。
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

FAIL = "暂时搜不到相关结果，请稍后再试或换个说法。"
FAIL_FETCH = "暂时读不到该页面，请换一条结果或稍后再试。"
MISSING = (
    "网页搜索依赖未安装：请执行 uv sync --extra web，"
    "并 docker compose -f docker/docker-compose.yml up -d searxng。"
)

_DEFAULT_SEARXNG = "http://127.0.0.1:8888"
_DEFAULT_CACHE = ".cache/local_agentic_search.sqlite3"

_service: Any = None
_service_key: tuple[Any, ...] | None = None
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="web-search")


def _run_sync(coro: Any) -> Any:
    """在任意线程/loop 上下文安全地跑完一个 coroutine。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # 已在 Agents 常驻 loop 内：独立线程里 asyncio.run，避免嵌套
    return _executor.submit(asyncio.run, coro).result()


def _import_service():
    try:
        from local_agentic_search import LocalSearchService
        from local_agentic_search.config import LocalSearchConfig
    except ImportError as e:
        raise ImportError(MISSING) from e
    return LocalSearchService, LocalSearchConfig


def _get_service(
    *,
    searxng_base_url: str = _DEFAULT_SEARXNG,
    search_timeout: float = 8.0,
    fetch_timeout: float = 15.0,
    fetch_max_chars: int = 3000,
    cache_path: str = _DEFAULT_CACHE,
) -> Any:
    global _service, _service_key
    LocalSearchService, LocalSearchConfig = _import_service()
    base = (searxng_base_url or _DEFAULT_SEARXNG).rstrip("/")
    chars = max(1, int(fetch_max_chars))
    key = (base, float(search_timeout), float(fetch_timeout), chars, cache_path)
    if _service is not None and _service_key == key:
        return _service
    cfg = LocalSearchConfig(
        searxng_base_url=base,
        cache_path=Path(cache_path or _DEFAULT_CACHE),
        default_fetch_chars=chars,
        max_fetch_chars=max(chars, 20_000),
        request_timeout_seconds=max(2.0, float(search_timeout)),
        crawl_timeout_ms=int(max(2.0, float(fetch_timeout)) * 1000),
    )
    _service = LocalSearchService(cfg)
    _service_key = key
    return _service


def format_search_payload(data: dict[str, Any]) -> str:
    """把结构化搜索结果压成给模型的 JSON 字符串（自检可直接测）。"""
    results = data.get("results") or []
    if not results:
        return FAIL
    payload = {
        "query": data.get("query", ""),
        "count": len(results),
        "results": [
            {
                "result_id": r.get("result_id", ""),
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("snippet", ""),
                "full_text_command_text": r.get("full_text_command_text"),
            }
            for r in results
        ],
        "hint": "摘要不够用时再调 web_fetch(result_id=...) 取全文",
    }
    return json.dumps(payload, ensure_ascii=False)


def format_fetch_payload(data: dict[str, Any]) -> str:
    """把 fetch 结果压成给模型的 JSON 字符串。"""
    if not data.get("success"):
        return str(data.get("error") or FAIL_FETCH)
    payload = {
        "result_id": data.get("result_id"),
        "url": data.get("url", ""),
        "title": data.get("title"),
        "content": data.get("content", ""),
        "start": data.get("start", 0),
        "end": data.get("end", 0),
        "total_chars": data.get("total_chars", 0),
        "has_more": bool(data.get("has_more")),
        "next_fetch_command_text": data.get("next_fetch_command_text"),
    }
    return json.dumps(payload, ensure_ascii=False)


def search(
    query: str,
    *,
    count: int = 5,
    timeout: float = 8.0,
    language: str = "zh-CN",
    searxng_base_url: str = _DEFAULT_SEARXNG,
    cache_path: str = _DEFAULT_CACHE,
    fetch_timeout: float = 15.0,
    fetch_max_chars: int = 3000,
) -> str:
    """SearXNG 搜索，返回带 result_id 的摘要 JSON；失败返回 FAIL。"""
    q = (query or "").strip()
    if not q:
        return "搜索词为空。"
    count = max(1, min(int(count), 20))
    try:
        svc = _get_service(
            searxng_base_url=searxng_base_url,
            search_timeout=timeout,
            fetch_timeout=fetch_timeout,
            fetch_max_chars=fetch_max_chars,
            cache_path=cache_path,
        )
        resp = _run_sync(
            svc.search(q, max_results=count, language=language or "zh-CN")
        )
    except ImportError:
        return MISSING
    except Exception:  # noqa: BLE001
        return FAIL

    results = []
    for r in getattr(resp, "results", []) or []:
        results.append(
            {
                "result_id": getattr(r, "result_id", ""),
                "title": getattr(r, "title", ""),
                "url": getattr(r, "url", ""),
                "snippet": getattr(r, "snippet", ""),
                "full_text_command_text": getattr(r, "full_text_command_text", None),
            }
        )
    return format_search_payload({"query": q, "results": results})


def fetch(
    *,
    result_id: str | None = None,
    url: str | None = None,
    start: int = 0,
    max_chars: int | None = None,
    searxng_base_url: str = _DEFAULT_SEARXNG,
    search_timeout: float = 8.0,
    fetch_timeout: float = 15.0,
    fetch_max_chars: int = 3000,
    cache_path: str = _DEFAULT_CACHE,
) -> str:
    """按 result_id（或 url）取全文切片 JSON；失败返回 FAIL_FETCH。"""
    rid = (result_id or "").strip() or None
    u = (url or "").strip() or None
    if not rid and not u:
        return "请提供 result_id（来自 web_search）或 url。"
    try:
        svc = _get_service(
            searxng_base_url=searxng_base_url,
            search_timeout=search_timeout,
            fetch_timeout=fetch_timeout,
            fetch_max_chars=fetch_max_chars,
            cache_path=cache_path,
        )
        chars = int(max_chars) if max_chars is not None else fetch_max_chars
        resp = _run_sync(
            svc.fetch(
                result_id=rid,
                url=u,
                start=max(0, int(start or 0)),
                max_chars=chars,
            )
        )
    except ImportError:
        return MISSING
    except Exception as e:  # noqa: BLE001
        return f"{FAIL_FETCH}（{e}）"

    return format_fetch_payload(
        {
            "success": bool(getattr(resp, "success", False)),
            "result_id": getattr(resp, "result_id", rid),
            "url": getattr(resp, "url", u or ""),
            "title": getattr(resp, "title", None),
            "content": getattr(resp, "content", "") or "",
            "start": getattr(resp, "start", 0),
            "end": getattr(resp, "end", 0),
            "total_chars": getattr(resp, "total_chars", 0),
            "has_more": getattr(resp, "has_more", False),
            "next_fetch_command_text": getattr(resp, "next_fetch_command_text", None),
            "error": getattr(resp, "error", None),
        }
    )
