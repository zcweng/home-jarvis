"""记忆服务门面：召回热路径 + 后台写入队列。"""

from __future__ import annotations

import queue
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .alarms import AlarmScheduler, parse_relative_minutes
from .classifier import classify
from .decay import should_archive
from .digest import MemoryDigest
from .embedding import Embedder, NullEmbedder, build_embedder
from .eraser import MemoryEraser
from .retriever import MemoryRetriever
from .store import MemoryStore
from .types import ClassifyResult
from .writer import MemoryWriter

LLMFn = Callable[[str], str]
AnnounceFn = Callable[[str], None]


def _warn(msg: str) -> None:
    print(f"  ⚠️  memory：{msg}", file=sys.stderr)


@dataclass
class MemorySettings:
    enabled: bool = False
    db_path: str = "data/memory/jarvis_memory.sqlite3"
    model_dir: str = "models/bge-small-zh-v1.5"
    token_budget: int = 800
    short_half_life_days: float = 180.0
    min_importance: float = 0.45
    rerank: str = "score"  # score|llm|off
    recall_k: int = 8
    episode_every: int = 6
    compress_at_messages: int = 24
    missed_grace: float = 600.0
    llm_model: str = ""  # 覆盖分类/摘要模型名（由外部 llm 闭包处理）


class MemoryService:
    """对外唯一入口。enabled=False 时所有方法空操作。"""

    def __init__(
        self,
        settings: MemorySettings,
        *,
        llm: LLMFn | None = None,
        announce: AnnounceFn | None = None,
        root: Path | None = None,
    ) -> None:
        self.settings = settings
        self.enabled = bool(settings.enabled)
        self.session_id = f"s_{uuid.uuid4().hex[:10]}"
        self._llm = llm
        self._announce = announce
        self._root = root or Path.cwd()
        self._queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=64)
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._pending_erase_confirm = False

        self.store: MemoryStore | None = None
        self.embedder: Embedder | NullEmbedder = NullEmbedder()
        self.writer: MemoryWriter | None = None
        self.retriever: MemoryRetriever | None = None
        self.digest: MemoryDigest | None = None
        self.eraser: MemoryEraser | None = None
        self.alarms: AlarmScheduler | None = None

        if not self.enabled:
            return

        db = Path(settings.db_path)
        if not db.is_absolute():
            db = self._root / db
        model_dir = Path(settings.model_dir)
        if not model_dir.is_absolute():
            model_dir = self._root / model_dir

        try:
            self.store = MemoryStore(db)
            self.embedder = build_embedder(model_dir)
            model_name = getattr(self.embedder, "model_name", "") or ""

            def _embed(text: str) -> list[float] | None:
                return self.embedder.encode_one(text)

            def _merge(texts: list[str]) -> str:
                if not self._llm:
                    return "；".join(texts)[:200]
                prompt = "把下列记忆合并成一句不丢关键信息的中文摘要，只输出摘要：\n" + "\n".join(
                    f"- {t}" for t in texts
                )
                return (self._llm(prompt) or "").strip()[:400] or "；".join(texts)[:200]

            def _summarize(blob: str) -> str:
                if not self._llm:
                    return ""
                prompt = "把对话压缩成一两句中文摘要，只输出摘要：\n" + blob[:2500]
                return (self._llm(prompt) or "").strip()[:400]

            # llm 可能在 Jarvis 装配后才注入；闭包内读 self._llm，故始终挂上
            self.writer = MemoryWriter(
                self.store,
                embed=_embed,
                merge_llm=_merge,
                half_life_days=settings.short_half_life_days,
                model_name=model_name,
            )
            self.retriever = MemoryRetriever(
                self.store,
                embed=_embed,
                model_name=model_name,
                token_budget=settings.token_budget,
                recall_k_short=settings.recall_k,
            )
            self.digest = MemoryDigest(
                self.store,
                self.writer,
                summarize=_summarize,
                episode_every=settings.episode_every,
                compress_at_messages=settings.compress_at_messages,
            )
            self.eraser = MemoryEraser(self.store, embed=_embed, model_name=model_name)
            self.alarms = AlarmScheduler(
                self.store,
                announce=announce,
                writer=self.writer,
                missed_grace=settings.missed_grace,
            )
            self.alarms.start()
            self._worker = threading.Thread(target=self._run_worker, name="jarvis-memory", daemon=True)
            self._worker.start()
            # 后台预热 embedding
            threading.Thread(target=self.embedder.warm, name="jarvis-embed-warm", daemon=True).start()
        except Exception as e:  # noqa: BLE001
            _warn(f"初始化失败，已禁用记忆：{e}")
            self.enabled = False

    # ------------------------------------------------------------------ public hot path

    def build_context(self, user_text: str) -> str:
        if not self.enabled or not self.retriever:
            return ""
        try:
            return self.retriever.build_block(user_text)
        except Exception as e:  # noqa: BLE001
            _warn(f"召回失败：{e}")
            return ""

    def observe_turn(self, user_text: str, reply: str) -> None:
        if not self.enabled or not self.store:
            return
        try:
            self.store.append_turn(self.session_id, "user", user_text)
            self.store.append_turn(self.session_id, "assistant", reply or "")
            if self.digest:
                self.digest.note_activity(self.session_id)
            self._enqueue("observe", (user_text, reply))
        except Exception as e:  # noqa: BLE001
            _warn(f"observe 失败：{e}")

    def maybe_compress_history(self, history: list[dict[str, str]]) -> list[dict[str, str]]:
        if not self.enabled or not self.digest:
            return history
        try:
            if len(history) <= self.settings.compress_at_messages:
                return history
            new_hist, _ = self.digest.compress_history(self.session_id, history)
            return new_hist
        except Exception as e:  # noqa: BLE001
            _warn(f"压缩失败：{e}")
            # 至少截断
            keep = 8
            return history[-keep:]

    def close(self) -> None:
        self._stop.set()
        try:
            self._enqueue("stop", None)
        except Exception:  # noqa: BLE001
            pass
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=2)
        if self.alarms:
            self.alarms.close()
        if self.store:
            try:
                self.store.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ tools API

    def tool_memory(
        self,
        action: str,
        *,
        content: str = "",
        memory_id: str = "",
        slot: str = "",
        target: str = "",
    ) -> str:
        if not self.enabled:
            return "记忆功能未启用。"
        action = (action or "").strip().lower()
        if action == "remember":
            draft = ClassifyResult(
                should_store=True,
                layer="perm",
                type="preference",
                slot=slot,
                content=content,
                summary=content,
                importance=0.85,
                tags=["manual"],
            )
            if self.writer:
                r = self.writer.write(draft, session_id=self.session_id, source="tool")
                return f"已记住（{r.action}）。" if r.memory_id else r.message
            return "写入失败。"
        if action == "forget":
            # 确认清空 / 确认忘掉
            if content.strip() in ("确认清空", "确认忘掉") or target.strip() in ("确认清空", "确认忘掉"):
                if self.eraser:
                    return self.eraser.confirm_pending().message
            if self.eraser:
                res = self.eraser.erase(
                    memory_id=memory_id,
                    slot=slot,
                    target=target or content,
                    all_=bool(re_all(content or target)),
                    confirmed=False,
                )
                return res.message
            return "擦除失败。"
        if action == "list":
            if not self.store:
                return "无存储。"
            rows = self.store.list_active(limit=30)
            if not rows:
                return "目前没有记忆。"
            lines = [f"[{r.id}] ({r.type}/{r.layer}) {r.text()}" for r in rows]
            return "\n".join(lines)
        if action == "correct":
            if not content:
                return "请提供纠正后的内容。"
            draft = classify(content, "", llm=self._llm, min_importance=self.settings.min_importance)
            draft.should_store = True
            if slot:
                draft.slot = slot
            if self.writer:
                r = self.writer.write(draft, session_id=self.session_id, source="correct")
                return f"已纠正（{r.action}）。"
            return "纠正失败。"
        if action == "recall_alarms":
            if not self.alarms:
                return "无闹钟服务。"
            pending = self.alarms.list_pending()
            if not pending:
                return "没有待触发的提醒。"
            lines = []
            for a in pending[:20]:
                ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(a["fire_at"])))
                lines.append(f"[{a['id']}] {ts} {a.get('label') or ''} ({a.get('repeat_rule')})")
            return "\n".join(lines)
        return f"不支持的 memory action：{action}"

    def schedule_timer(self, minutes: float, label: str = "") -> str:
        """供 Tools.timer 委托。"""
        if not self.enabled or not self.alarms or not self.writer:
            return ""
        # 同步写一条 task 永久记忆
        draft = ClassifyResult(
            should_store=True,
            layer="perm",
            type="task",
            slot="task.reminder",
            content=label or f"{minutes:g} 分钟后提醒",
            summary=label or f"{minutes:g} 分钟后提醒",
            importance=0.85,
            tags=["task", "timer"],
            schedule={"hint": label, "repeat": "once"},
        )
        wr = self.writer.write(draft, session_id=self.session_id, source="timer")
        self.alarms.schedule_in(minutes, label=label, memory_id=wr.memory_id)
        if minutes < 1:
            spoken = f"{round(minutes * 60):g} 秒"
        else:
            spoken = f"{minutes:g} 分钟"
        return f"好，{spoken}后提醒你。"

    # ------------------------------------------------------------------ worker

    def _enqueue(self, kind: str, payload: Any) -> None:
        if not self.enabled:
            return
        try:
            self._queue.put_nowait((kind, payload))
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            _warn("后台队列满，丢弃最旧任务")
            try:
                self._queue.put_nowait((kind, payload))
            except queue.Full:
                pass

    def _run_worker(self) -> None:
        while not self._stop.is_set():
            try:
                kind, payload = self._queue.get(timeout=0.5)
            except queue.Empty:
                # 空闲时尝试 episode / 归档扫描
                try:
                    if self.digest:
                        self.digest.maybe_episode(self.session_id)
                    self._archive_pass()
                except Exception:  # noqa: BLE001
                    pass
                continue
            if kind == "stop":
                break
            try:
                if kind == "observe":
                    user_text, reply = payload
                    self._handle_observe(user_text, reply)
            except Exception as e:  # noqa: BLE001
                _warn(f"后台任务失败：{e}")

    def _handle_observe(self, user_text: str, reply: str) -> None:
        assert self.store and self.writer and self.eraser
        # 确认类短语
        if user_text.strip() in ("确认清空", "确认忘掉", "确认删除"):
            res = self.eraser.confirm_pending()
            return

        draft = classify(
            user_text,
            reply,
            llm=self._llm,
            min_importance=self.settings.min_importance,
        )
        if draft.erase:
            res = self.eraser.handle_classify_erase(draft.erase)
            # 擦除结果不自动改 reply（语音回合已结束）；仅落库
            return
        if draft.should_store:
            self.writer.write(draft, session_id=self.session_id, source="observe")
            # 若带 schedule 且有相对时间，额外建闹钟
            if draft.schedule and self.alarms:
                mins = parse_relative_minutes(str(draft.schedule.get("hint") or user_text))
                if mins and mins > 0:
                    self.alarms.schedule_in(mins, label=draft.summary or draft.content)
        if self.digest:
            self.digest.maybe_episode(self.session_id)

    def _archive_pass(self) -> None:
        if not self.store:
            return
        now = time.time()
        for rec in self.store.list_active(layer="short", limit=100):
            if should_archive(rec, now):
                self.store.archive(rec.id)


def re_all(text: str) -> bool:
    import re

    return bool(re.search(r"(全部|所有).{0,4}(记忆)|清空记忆", text or ""))


def null_service() -> MemoryService:
    return MemoryService(MemorySettings(enabled=False))
