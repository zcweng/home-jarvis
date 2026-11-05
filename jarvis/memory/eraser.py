"""主动擦除遗忘。"""

from __future__ import annotations

import re
from typing import Callable

from .store import MemoryStore, cosine
from .types import EraseResult, MemoryRecord

EmbedFn = Callable[[str], list[float] | None]

FORGET_REPLY = "好，我忘了"
CONFIRM_CLEAR_HINT = "要清空全部记忆的话，请再说一遍「确认清空」"
CONFIRM_MULTI_HINT = "找到多条相关记忆，确认要忘掉它们吗？再说一遍「确认忘掉」"


class MemoryEraser:
    def __init__(
        self,
        store: MemoryStore,
        *,
        embed: EmbedFn | None = None,
        model_name: str = "",
        sim_threshold: float = 0.85,
    ) -> None:
        self.store = store
        self.embed = embed
        self.model_name = model_name
        self.sim_threshold = sim_threshold
        self._pending_ids: list[str] = []
        self._pending_clear = False

    def erase(
        self,
        *,
        memory_id: str = "",
        slot: str = "",
        target: str = "",
        all_: bool = False,
        confirmed: bool = False,
    ) -> EraseResult:
        if all_:
            return self._erase_all(confirmed=confirmed)

        # 确认多条
        if confirmed and self._pending_ids:
            ids = list(self._pending_ids)
            self._pending_ids.clear()
            erased = self._delete_ids(ids)
            return EraseResult(ok=True, message=FORGET_REPLY, erased_ids=erased)

        if memory_id:
            rec = self.store.get(memory_id)
            if not rec:
                return EraseResult(ok=False, message="没找到这条记忆")
            self._delete_ids([memory_id])
            return EraseResult(ok=True, message=FORGET_REPLY, erased_ids=[memory_id])

        if slot:
            hits = self.store.find_by_slot(slot, active_only=True)
            return self._finish_hits(hits)

        target = (target or "").strip()
        if not target:
            return EraseResult(ok=False, message="没说要忘掉什么")

        # 显式 id
        m = re.search(r"\[?m[_:]([a-f0-9]{6,})\]?", target, re.I)
        if m:
            mid = f"m_{m.group(1)}" if not target.startswith("m_") else target
            # 尝试完整匹配
            for cand in self.store.list_active(limit=500):
                if cand.id in target or target in cand.id:
                    self._delete_ids([cand.id])
                    return EraseResult(ok=True, message=FORGET_REPLY, erased_ids=[cand.id])

        hits = self._search(target)
        return self._finish_hits(hits)

    def handle_classify_erase(self, erase: dict) -> EraseResult:
        if not erase:
            return EraseResult(ok=False, message="无擦除意图")
        return self.erase(
            memory_id=str(erase.get("id") or ""),
            slot=str(erase.get("slot") or ""),
            target=str(erase.get("target") or ""),
            all_=bool(erase.get("all")),
            confirmed=bool(erase.get("confirmed")),
        )

    def _finish_hits(self, hits: list[MemoryRecord]) -> EraseResult:
        if not hits:
            return EraseResult(ok=False, message="没找到要忘掉的内容")
        if len(hits) == 1:
            self._delete_ids([hits[0].id])
            return EraseResult(ok=True, message=FORGET_REPLY, erased_ids=[hits[0].id], candidates=hits)
        # 多条需确认
        self._pending_ids = [h.id for h in hits]
        preview = "、".join(h.text()[:20] for h in hits[:5])
        return EraseResult(
            ok=False,
            message=f"{CONFIRM_MULTI_HINT}（{preview}）",
            need_confirm=True,
            candidates=hits,
        )

    def _erase_all(self, *, confirmed: bool) -> EraseResult:
        if not confirmed and not self._pending_clear:
            self._pending_clear = True
            return EraseResult(ok=False, message=CONFIRM_CLEAR_HINT, need_confirm=True)
        if not confirmed and self._pending_clear:
            # 用户再说确认清空才清
            return EraseResult(ok=False, message=CONFIRM_CLEAR_HINT, need_confirm=True)
        self._pending_clear = False
        ids = [r.id for r in self.store.list_active(include_archived=True, limit=10000)]
        erased = self._delete_ids(ids)
        # 取消所有 pending 闹钟
        for a in self.store.pending_alarms():
            self.store.cancel_alarm(a["id"])
        self.store.log_event("erase_all", "")
        return EraseResult(ok=True, message=FORGET_REPLY, erased_ids=erased)

    def confirm_pending(self) -> EraseResult:
        if self._pending_clear:
            self._pending_clear = False
            return self._erase_all(confirmed=True)
        if self._pending_ids:
            return self.erase(confirmed=True)
        return EraseResult(ok=False, message="没有待确认的遗忘请求")

    def _delete_ids(self, ids: list[str]) -> list[str]:
        erased: list[str] = []
        for mid in ids:
            rec = self.store.delete(mid)
            if not rec:
                continue
            erased.append(mid)
            self.store.add_tombstone(slot=rec.slot, text=rec.content)
            if rec.summary and rec.summary != rec.content:
                self.store.add_tombstone(slot=rec.slot, text=rec.summary)
            self.store.cancel_alarms_for_memory(mid)
        return erased

    def _search(self, target: str) -> list[MemoryRecord]:
        # slot 猜测
        if "." in target and " " not in target:
            hits = self.store.find_by_slot(target, active_only=True)
            if hits:
                return hits

        fts = self.store.fts_search(target, limit=20)
        by_id = {r.id: r for r in fts}

        # 字面包含
        for r in self.store.list_active(limit=300):
            if target in r.text() or target in r.content:
                by_id[r.id] = r

        # 向量
        if self.embed:
            qv = self.embed(target)
            if qv is not None:
                for rec, v in self.store.all_vectors(model=self.model_name or None):
                    if cosine(qv, v) >= self.sim_threshold:
                        by_id[rec.id] = rec

        return list(by_id.values())
