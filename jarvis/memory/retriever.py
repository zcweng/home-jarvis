"""双路召回 + 打分重排 + token 预算拼装。"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from .decay import effective_heat, estimate_tokens
from .store import MemoryStore, cosine
from .types import (
    MemoryRecord,
    PACK_ORDER,
    SIM_DEDUP,
    TYPE_PRIOR,
)

EmbedFn = Callable[[str], list[float] | None]


class MemoryRetriever:
    def __init__(
        self,
        store: MemoryStore,
        *,
        embed: EmbedFn | None = None,
        model_name: str = "",
        token_budget: int = 800,
        recall_k_perm: int = 12,
        recall_k_short: int = 8,
        heat_boost: float = 0.15,
    ) -> None:
        self.store = store
        self.embed = embed
        self.model_name = model_name
        self.token_budget = token_budget
        self.recall_k_perm = recall_k_perm
        self.recall_k_short = recall_k_short
        self.heat_boost = heat_boost

    def retrieve(self, query: str, *, deep: bool = False) -> list[MemoryRecord]:
        q = (query or "").strip()
        now = time.time()
        qvec = self.embed(q) if self.embed and q else None

        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_p = pool.submit(self._arm_perm, q, qvec, now, deep)
            fut_s = pool.submit(self._arm_short, q, qvec, now, deep)
            perm = fut_p.result()
            short = fut_s.result()

        fused = self._fuse(perm, short, qvec, now)
        packed = self._budget(fused)
        # 召回加热（异步安全：直接写库，量小）
        if packed:
            self.store.mark_recalled([r.id for r in packed], now=now, heat_boost=self.heat_boost)
        return packed

    def build_block(self, query: str, *, deep: bool = False) -> str:
        items = self.retrieve(query, deep=deep)
        if not items:
            return ""
        lines = ["【记忆】"]
        for r in items:
            tag = r.type
            updated = "（已更新）" if r.superseded_by else ""
            # 被 supersede 的不会出现；若有归档旁注则不加
            text = r.text()
            lines.append(f"- [{r.id}] ({tag}){updated} {text}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ arms

    def _arm_perm(
        self,
        query: str,
        qvec: list[float] | None,
        now: float,
        deep: bool,
    ) -> list[MemoryRecord]:
        # profile / rule 全量（量小）
        base = self.store.list_active(layer="perm", types=["profile", "rule"], limit=50)
        # preference / task：FTS + 向量
        pref_task = self.store.list_active(layer="perm", types=["preference", "task"], limit=100)
        scored = self._score_candidates(base + pref_task, query, qvec, now)
        # 永久：保留全部 profile/rule，其余按分取 TopK
        keep: list[MemoryRecord] = []
        others: list[MemoryRecord] = []
        for r in scored:
            if r.type in ("profile", "rule"):
                keep.append(r)
            else:
                others.append(r)
        others.sort(key=lambda x: -x.score)
        return keep + others[: self.recall_k_perm]

    def _arm_short(
        self,
        query: str,
        qvec: list[float] | None,
        now: float,
        deep: bool,
    ) -> list[MemoryRecord]:
        # FTS
        fts_hits = self.store.fts_search(query, layer="short", limit=20) if query else []
        # 热度列表
        hot = self.store.list_active(layer="short", limit=40)
        if deep:
            archived = self.store.list_active(layer="short", include_archived=True, limit=20)
            hot = hot + [r for r in archived if r.is_archived]
        by_id = {r.id: r for r in hot}
        for r in fts_hits:
            by_id[r.id] = r
        scored = self._score_candidates(list(by_id.values()), query, qvec, now)
        scored.sort(key=lambda x: (-x.heat, -x.score))
        return scored[: self.recall_k_short]

    def _score_candidates(
        self,
        recs: list[MemoryRecord],
        query: str,
        qvec: list[float] | None,
        now: float,
    ) -> list[MemoryRecord]:
        # 批量取向量
        vec_map: dict[str, list[float]] = {}
        if qvec is not None and self.model_name:
            for rec, v in self.store.all_vectors(model=self.model_name):
                vec_map[rec.id] = v

        out: list[MemoryRecord] = []
        q_low = (query or "").lower()
        for r in recs:
            if r.is_archived:
                continue
            heat = effective_heat(r, now)
            r.heat = heat
            sim = 0.0
            if qvec is not None and r.id in vec_map:
                sim = cosine(qvec, vec_map[r.id])
            elif q_low and q_low in (r.text() or "").lower():
                sim = 0.55
            elif query and any(ch in r.text() for ch in query if "\u4e00" <= ch <= "\u9fff"):
                # 粗粒度字面重合
                overlap = sum(1 for ch in set(query) if ch in r.text() and "\u4e00" <= ch <= "\u9fff")
                sim = min(0.5, overlap * 0.08)
            prior = TYPE_PRIOR.get(r.type, 0.4)
            age_days = max(0.0, (now - r.last_activity_at) / 86400)
            recency = 2.0 ** (-age_days / 14.0)
            r.sim = sim
            r.score = 0.55 * sim + 0.25 * heat + 0.15 * prior + 0.05 * recency
            out.append(r)
        return out

    def _fuse(
        self,
        perm: list[MemoryRecord],
        short: list[MemoryRecord],
        qvec: list[float] | None,
        now: float,
    ) -> list[MemoryRecord]:
        # 同 slot 取最高分
        by_slot: dict[str, MemoryRecord] = {}
        plain: list[MemoryRecord] = []
        for r in perm + short:
            if r.slot:
                prev = by_slot.get(r.slot)
                if prev is None or r.score > prev.score:
                    by_slot[r.slot] = r
            else:
                plain.append(r)
        candidates = list(by_slot.values()) + plain

        # 近似去重
        candidates.sort(key=lambda x: -x.score)
        kept: list[MemoryRecord] = []
        kept_vecs: list[list[float]] = []
        vec_cache: dict[str, list[float]] = {}
        if qvec is not None and self.model_name:
            for rec, v in self.store.all_vectors(model=self.model_name):
                vec_cache[rec.id] = v

        for r in candidates:
            v = vec_cache.get(r.id)
            dup = False
            if v is not None:
                for kv in kept_vecs:
                    if cosine(v, kv) >= SIM_DEDUP:
                        dup = True
                        break
            else:
                for k in kept:
                    if r.text() and r.text() == k.text():
                        dup = True
                        break
            if dup:
                continue
            kept.append(r)
            if v is not None:
                kept_vecs.append(v)

        # 轻量 MMR：按 pack 顺序 + score
        kept.sort(key=lambda x: (PACK_ORDER.get(x.type, 50), -x.score))
        return kept

    def _budget(self, items: list[MemoryRecord]) -> list[MemoryRecord]:
        # 留 20% 余量
        budget = int(self.token_budget * 0.8)
        used = estimate_tokens("【记忆】")
        out: list[MemoryRecord] = []
        for r in items:
            line = f"- [{r.id}] ({r.type}) {r.text()}"
            cost = estimate_tokens(line)
            if used + cost > budget and out:
                break
            out.append(r)
            used += cost
        return out
