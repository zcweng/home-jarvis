"""短期情景摘要 + 长对话压缩。"""

from __future__ import annotations

import time
from typing import Callable

from .store import MemoryStore
from .types import ClassifyResult
from .writer import MemoryWriter

SummarizeFn = Callable[[str], str]


class MemoryDigest:
    def __init__(
        self,
        store: MemoryStore,
        writer: MemoryWriter,
        *,
        summarize: SummarizeFn | None = None,
        episode_every: int = 6,
        compress_at_messages: int = 24,
        keep_recent: int = 8,
    ) -> None:
        self.store = store
        self.writer = writer
        self.summarize = summarize
        self.episode_every = max(2, episode_every)
        self.compress_at_messages = max(keep_recent + 2, compress_at_messages)
        self.keep_recent = keep_recent
        self._last_activity: dict[str, float] = {}

    def note_activity(self, session_id: str) -> None:
        self._last_activity[session_id] = time.time()

    def maybe_episode(self, session_id: str, *, force: bool = False) -> list[str]:
        """轮次达到阈值或强制时，把未消化 turn_log 压成 episode。"""
        n = self.store.count_undigested(session_id)
        idle = time.time() - self._last_activity.get(session_id, time.time())
        if not force and n < self.episode_every and idle < 90:
            return []
        if n <= 0:
            return []
        turns = self.store.undigested_turns(session_id, limit=40)
        if not turns:
            return []
        blob = "\n".join(f"{t['role']}: {t['content']}" for t in turns)
        summary = self._summarize(blob, fallback_prefix="近期对话")
        draft = ClassifyResult(
            should_store=True,
            layer="short",
            type="episode",
            content=summary,
            summary=summary,
            importance=0.55,
            tags=["episode", "auto"],
        )
        result = self.writer.write(draft, session_id=session_id, source="digest")
        self.store.mark_turns_digested([int(t["id"]) for t in turns])
        return [result.memory_id] if result.memory_id else []

    def compress_history(
        self,
        session_id: str,
        history: list[dict[str, str]],
    ) -> tuple[list[dict[str, str]], str | None]:
        """超长 history → 保留最近 keep_recent 条，其余压成 digest。

        返回 (新 history, digest 文本或 None)。
        """
        if len(history) <= self.compress_at_messages:
            return history, None
        old = history[: -self.keep_recent]
        recent = history[-self.keep_recent :]
        blob = "\n".join(f"{m.get('role')}: {m.get('content')}" for m in old)
        summary = self._summarize(blob, fallback_prefix="较早对话摘要")
        draft = ClassifyResult(
            should_store=True,
            layer="short",
            type="digest",
            content=summary,
            summary=summary,
            importance=0.6,
            tags=["digest", "auto"],
        )
        self.writer.write(draft, session_id=session_id, source="compress")
        # 占位：压缩未完成时也保证截断
        placeholder = {"role": "assistant", "content": f"（较早对话已压缩）{summary[:80]}"}
        return [placeholder, *recent], summary

    def _summarize(self, blob: str, *, fallback_prefix: str) -> str:
        text = (blob or "").strip()
        if not text:
            return fallback_prefix
        if self.summarize:
            try:
                out = (self.summarize(text[:3000]) or "").strip()
                if out:
                    return out[:400]
            except Exception:  # noqa: BLE001
                pass
        # 无 LLM：取首尾各一句
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if len(lines) <= 3:
            return f"{fallback_prefix}：" + "；".join(lines)[:200]
        return f"{fallback_prefix}：" + "；".join(lines[:2] + ["…"] + lines[-2:])[:300]


def history_needs_compress(history: list[dict[str, str]], threshold: int = 24) -> bool:
    return len(history) > threshold
