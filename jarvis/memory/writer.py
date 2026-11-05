"""记忆写入：冲突纠正、合并、档案槽覆盖、tombstone 阻断。"""

from __future__ import annotations

import time
from typing import Callable

from .decay import clamp
from .store import MemoryStore, cosine
from .types import (
    ClassifyResult,
    DEFAULT_HALF_LIFE_DAYS,
    MemoryRecord,
    SIM_CANDIDATE,
    SIM_CLUSTER,
    SIM_MERGE,
    WriteResult,
)

EmbedFn = Callable[[str], list[float] | None]
MergeFn = Callable[[list[str]], str]  # 多条 → 合并摘要


class MemoryWriter:
    def __init__(
        self,
        store: MemoryStore,
        *,
        embed: EmbedFn | None = None,
        merge_llm: MergeFn | None = None,
        half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
        model_name: str = "",
    ) -> None:
        self.store = store
        self.embed = embed
        self.merge_llm = merge_llm
        self.half_life_days = half_life_days
        self.model_name = model_name

    def write(self, draft: ClassifyResult, *, session_id: str = "", source: str = "observe") -> WriteResult:
        if draft.erase:
            return WriteResult(action="skip", message="erase intent，交给 eraser")
        if not draft.should_store:
            return WriteResult(action="skip", message="classifier 判定不存储")

        content = (draft.content or "").strip()
        summary = (draft.summary or content).strip()
        if not content:
            return WriteResult(action="skip", message="空内容")

        slot = (draft.slot or draft.profile_key or "").strip()
        # tombstone 阻断
        if self.store.is_tombstoned(slot=slot, text=content) or self.store.is_tombstoned(
            slot=slot, text=summary
        ):
            return WriteResult(action="blocked", message="tombstone 阻断回写")

        now = time.time()
        layer = draft.layer
        typ = draft.type
        vec = self.embed(summary or content) if self.embed else None

        # profile：同 slot 覆盖
        if typ == "profile" and slot:
            return self._write_profile(draft, slot, vec, session_id, source, now)

        # rule：永不自动合并，只按近似刷新或新增
        if typ == "rule":
            return self._write_rule(draft, slot, vec, session_id, source, now)

        # 同 slot 冲突 / 刷新
        if slot:
            existing = self.store.find_by_slot(slot, layer=layer, active_only=True)
            if existing:
                return self._resolve_slot_conflict(existing[0], draft, vec, session_id, source, now)

        # 向量近邻
        if vec is not None:
            near = self._nearest(layer, typ, vec, threshold=SIM_CANDIDATE)
            if near:
                rec, sim = near
                if sim >= SIM_MERGE or _same_meaning(rec.text(), summary):
                    return self._refresh(rec, draft, vec, now)
                if slot and rec.slot == slot and not _same_meaning(rec.text(), summary):
                    return self._supersede(rec, draft, vec, session_id, source, now)

        # 聚类合并（非 rule）：同 layer 近似条目过多
        if typ != "rule" and vec is not None:
            cluster = self._cluster(layer, typ, vec)
            if len(cluster) >= 3:
                return self._merge_cluster(cluster, draft, vec, session_id, source, now)

        return self._insert(draft, slot, vec, session_id, source, now)

    # ------------------------------------------------------------------ helpers

    def _insert(
        self,
        draft: ClassifyResult,
        slot: str,
        vec: list[float] | None,
        session_id: str,
        source: str,
        now: float,
    ) -> WriteResult:
        half = 0.0 if draft.layer == "perm" else self.half_life_days
        rec = MemoryRecord(
            id="",
            layer=draft.layer,
            type=draft.type,
            slot=slot,
            content=draft.content,
            summary=draft.summary or draft.content,
            tags=list(draft.tags),
            importance=clamp(draft.importance),
            heat=clamp(draft.importance),
            created_at=now,
            last_activity_at=now,
            half_life_days=half,
            session_id=session_id,
            source=source,
        )
        self.store.insert(rec, vec=vec, model=self.model_name if vec else "")
        return WriteResult(action="insert", memory_id=rec.id, message="已写入")

    def _refresh(
        self,
        old: MemoryRecord,
        draft: ClassifyResult,
        vec: list[float] | None,
        now: float,
    ) -> WriteResult:
        imp = max(old.importance, draft.importance)
        heat = clamp(max(old.heat, imp) + 0.05)
        fields = {
            "importance": imp,
            "heat": heat,
            "last_activity_at": now,
            "content": draft.content or old.content,
            "summary": draft.summary or old.summary,
        }
        if draft.tags:
            fields["tags"] = list(dict.fromkeys([*old.tags, *draft.tags]))
        self.store.update_fields(old.id, **fields)
        if vec is not None and self.model_name:
            self.store.set_vector(old.id, vec, self.model_name)
        self.store.log_event("refresh", old.id)
        return WriteResult(action="refresh", memory_id=old.id, message="已刷新")

    def _supersede(
        self,
        old: MemoryRecord,
        draft: ClassifyResult,
        vec: list[float] | None,
        session_id: str,
        source: str,
        now: float,
    ) -> WriteResult:
        inserted = self._insert(draft, draft.slot or old.slot, vec, session_id, source, now)
        self.store.archive(old.id, superseded_by=inserted.memory_id)
        self.store.log_event("supersede", inserted.memory_id, related_id=old.id)
        return WriteResult(
            action="supersede",
            memory_id=inserted.memory_id,
            message="冲突已纠正，旧记忆归档",
            archived_ids=[old.id],
        )

    def _write_profile(
        self,
        draft: ClassifyResult,
        slot: str,
        vec: list[float] | None,
        session_id: str,
        source: str,
        now: float,
    ) -> WriteResult:
        existing = self.store.find_by_slot(slot, layer="perm", active_only=True)
        if not existing:
            return self._insert(draft, slot, vec, session_id, source, now)
        old = existing[0]
        if _same_meaning(old.text(), draft.summary or draft.content):
            return self._refresh(old, draft, vec, now)
        return self._supersede(old, draft, vec, session_id, source, now)

    def _write_rule(
        self,
        draft: ClassifyResult,
        slot: str,
        vec: list[float] | None,
        session_id: str,
        source: str,
        now: float,
    ) -> WriteResult:
        if vec is not None:
            near = self._nearest("perm", "rule", vec, threshold=SIM_MERGE)
            if near:
                return self._refresh(near[0], draft, vec, now)
        # 显式纠正：同 slot 不同值才 supersede
        if slot:
            existing = self.store.find_by_slot(slot, layer="perm", active_only=True)
            for old in existing:
                if old.type != "rule":
                    continue
                if _same_meaning(old.text(), draft.summary or draft.content):
                    return self._refresh(old, draft, vec, now)
                # 用户明确改规则
                if draft.conflict_hint or "改" in draft.content:
                    return self._supersede(old, draft, vec, session_id, source, now)
        return self._insert(draft, slot or "rule.user", vec, session_id, source, now)

    def _resolve_slot_conflict(
        self,
        old: MemoryRecord,
        draft: ClassifyResult,
        vec: list[float] | None,
        session_id: str,
        source: str,
        now: float,
    ) -> WriteResult:
        if _same_meaning(old.text(), draft.summary or draft.content):
            return self._refresh(old, draft, vec, now)
        return self._supersede(old, draft, vec, session_id, source, now)

    def _nearest(
        self,
        layer: str,
        typ: str,
        vec: list[float],
        *,
        threshold: float,
    ) -> tuple[MemoryRecord, float] | None:
        best: tuple[MemoryRecord, float] | None = None
        for rec, v in self.store.all_vectors(layer=layer, model=self.model_name or None):
            if rec.type != typ:
                continue
            sim = cosine(vec, v)
            if sim < threshold:
                continue
            if best is None or sim > best[1]:
                best = (rec, sim)
        # 无向量时退回同 type 文本扫描
        if best is None and not self.model_name:
            for rec in self.store.list_active(layer=layer, types=[typ], limit=50):
                if _same_meaning(rec.text(), ""):
                    continue
        return best

    def _cluster(self, layer: str, typ: str, vec: list[float]) -> list[tuple[MemoryRecord, float]]:
        hits = []
        for rec, v in self.store.all_vectors(layer=layer, model=self.model_name or None):
            if rec.type != typ:
                continue
            sim = cosine(vec, v)
            if sim >= SIM_CLUSTER:
                hits.append((rec, sim))
        hits.sort(key=lambda x: -x[1])
        return hits

    def _merge_cluster(
        self,
        cluster: list[tuple[MemoryRecord, float]],
        draft: ClassifyResult,
        vec: list[float] | None,
        session_id: str,
        source: str,
        now: float,
    ) -> WriteResult:
        texts = [r.text() for r, _ in cluster] + [draft.summary or draft.content]
        if self.merge_llm:
            try:
                merged = self.merge_llm(texts)
            except Exception:  # noqa: BLE001
                merged = "；".join(dict.fromkeys(texts))[:200]
        else:
            merged = "；".join(dict.fromkeys(texts))[:200]
        draft2 = ClassifyResult(
            should_store=True,
            layer=draft.layer,
            type=draft.type,
            slot=draft.slot,
            content=merged,
            summary=merged,
            importance=max(draft.importance, max(r.importance for r, _ in cluster)),
            tags=list(dict.fromkeys([*draft.tags, *(t for r, _ in cluster for t in r.tags)])),
        )
        inserted = self._insert(draft2, draft.slot, vec, session_id, source, now)
        archived = []
        for rec, _ in cluster:
            self.store.archive(rec.id, superseded_by=inserted.memory_id)
            archived.append(rec.id)
        self.store.log_event("merge", inserted.memory_id)
        return WriteResult(
            action="merge",
            memory_id=inserted.memory_id,
            message=f"已合并 {len(archived)} 条",
            archived_ids=archived,
        )


def _same_meaning(a: str, b: str) -> bool:
    def norm(s: str) -> str:
        s = (s or "").strip().lower()
        for ch in "，。！？、,.!;:：； \t\n":
            s = s.replace(ch, "")
        return s

    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    # 互相包含且长度接近
    if na in nb or nb in na:
        return abs(len(na) - len(nb)) <= max(4, int(0.3 * max(len(na), len(nb))))
    return False
