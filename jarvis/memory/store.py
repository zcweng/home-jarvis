"""SQLite 记忆存储：memories / vec / fts / turn_log / alarms / tombstones / events。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from .types import DEFAULT_HALF_LIFE_DAYS, MemoryRecord, TOMBSTONE_TTL_DAYS

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    layer TEXT NOT NULL CHECK(layer IN ('perm','short')),
    type TEXT NOT NULL,
    slot TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    importance REAL NOT NULL DEFAULT 0.5,
    heat REAL NOT NULL DEFAULT 0.5,
    recall_count INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    last_activity_at REAL NOT NULL,
    last_recall_at REAL NOT NULL DEFAULT 0,
    half_life_days REAL NOT NULL DEFAULT 180,
    is_archived INTEGER NOT NULL DEFAULT 0,
    superseded_by TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'observe',
    ttl_at REAL
);

CREATE INDEX IF NOT EXISTS idx_mem_layer_arch ON memories(layer, is_archived);
CREATE INDEX IF NOT EXISTS idx_mem_type_slot ON memories(type, slot);
CREATE INDEX IF NOT EXISTS idx_mem_session ON memories(session_id);
CREATE INDEX IF NOT EXISTS idx_mem_heat ON memories(heat DESC);

CREATE TABLE IF NOT EXISTS memory_vec (
    memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vec BLOB NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    content,
    summary,
    tags,
    tokenize='unicode61'
);

CREATE TABLE IF NOT EXISTS turn_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    digested INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_turn_session ON turn_log(session_id, id);

CREATE TABLE IF NOT EXISTS alarms (
    id TEXT PRIMARY KEY,
    fire_at REAL NOT NULL,
    repeat_rule TEXT NOT NULL DEFAULT 'once',
    label TEXT NOT NULL DEFAULT '',
    memory_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    fired_at REAL,
    missed_at REAL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alarms_status ON alarms(status, fire_at);

CREATE TABLE IF NOT EXISTS tombstones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slot TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tomb_hash ON tombstones(content_hash);
CREATE INDEX IF NOT EXISTS idx_tomb_slot ON tombstones(slot);

CREATE TABLE IF NOT EXISTS memory_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    memory_id TEXT NOT NULL DEFAULT '',
    related_id TEXT NOT NULL DEFAULT '',
    summary_hash TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
"""


def new_id(prefix: str = "m") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()[:32]


def fts_tokenize(text: str) -> str:
    """中文逐字切分；英文/数字按词保留。写入与查询共用。"""
    if not text:
        return ""
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            out.append("".join(buf).lower())
            buf.clear()

    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            flush()
            out.append(ch)
        elif ch.isalnum():
            buf.append(ch)
        else:
            flush()
    flush()
    return " ".join(out)


def pack_vec(vec: list[float] | tuple[float, ...]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def unpack_vec(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / ((na**0.5) * (nb**0.5))


def _row_to_record(row: sqlite3.Row) -> MemoryRecord:
    tags_raw = row["tags"] or "[]"
    try:
        tags = json.loads(tags_raw) if isinstance(tags_raw, str) else list(tags_raw)
    except json.JSONDecodeError:
        tags = []
    if not isinstance(tags, list):
        tags = []
    return MemoryRecord(
        id=row["id"],
        layer=row["layer"],
        type=row["type"],
        slot=row["slot"] or "",
        content=row["content"] or "",
        summary=row["summary"] or "",
        tags=[str(t) for t in tags],
        importance=float(row["importance"] or 0.5),
        heat=float(row["heat"] or 0.5),
        recall_count=int(row["recall_count"] or 0),
        created_at=float(row["created_at"] or 0),
        last_activity_at=float(row["last_activity_at"] or 0),
        last_recall_at=float(row["last_recall_at"] or 0),
        half_life_days=float(row["half_life_days"] or DEFAULT_HALF_LIFE_DAYS),
        is_archived=bool(row["is_archived"]),
        superseded_by=row["superseded_by"] or "",
        session_id=row["session_id"] or "",
        source=row["source"] or "observe",
        ttl_at=row["ttl_at"],
    )


class MemoryStore:
    """线程安全的记忆库（一把写锁）。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ CRUD

    def insert(self, rec: MemoryRecord, vec: list[float] | None = None, model: str = "") -> MemoryRecord:
        now = time.time()
        if not rec.id:
            rec.id = new_id()
        if not rec.created_at:
            rec.created_at = now
        if not rec.last_activity_at:
            rec.last_activity_at = now
        with self._lock:
            self._conn.execute(
                """INSERT INTO memories(
                    id, layer, type, slot, content, summary, tags, importance, heat,
                    recall_count, created_at, last_activity_at, last_recall_at,
                    half_life_days, is_archived, superseded_by, session_id, source, ttl_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    rec.id,
                    rec.layer,
                    rec.type,
                    rec.slot or "",
                    rec.content,
                    rec.summary or "",
                    json.dumps(rec.tags, ensure_ascii=False),
                    float(rec.importance),
                    float(rec.heat),
                    int(rec.recall_count),
                    float(rec.created_at),
                    float(rec.last_activity_at),
                    float(rec.last_recall_at or 0),
                    float(rec.half_life_days or DEFAULT_HALF_LIFE_DAYS),
                    1 if rec.is_archived else 0,
                    rec.superseded_by or "",
                    rec.session_id or "",
                    rec.source or "observe",
                    rec.ttl_at,
                ),
            )
            self._upsert_fts(rec)
            if vec is not None and model:
                self._upsert_vec(rec.id, vec, model)
            self._event("insert", rec.id, summary_hash=content_hash(rec.text()))
            self._conn.commit()
        return rec

    def update_fields(self, memory_id: str, **fields: Any) -> None:
        if not fields:
            return
        allowed = {
            "content",
            "summary",
            "tags",
            "importance",
            "heat",
            "recall_count",
            "last_activity_at",
            "last_recall_at",
            "is_archived",
            "superseded_by",
            "slot",
            "ttl_at",
            "half_life_days",
        }
        cols = []
        vals: list[Any] = []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "tags" and not isinstance(v, str):
                v = json.dumps(v, ensure_ascii=False)
            if k == "is_archived":
                v = 1 if v else 0
            cols.append(f"{k}=?")
            vals.append(v)
        if not cols:
            return
        vals.append(memory_id)
        with self._lock:
            self._conn.execute(f"UPDATE memories SET {', '.join(cols)} WHERE id=?", vals)
            if any(k in fields for k in ("content", "summary", "tags")):
                rec = self.get(memory_id)
                if rec:
                    self._upsert_fts(rec)
            self._conn.commit()

    def get(self, memory_id: str) -> MemoryRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        return _row_to_record(row) if row else None

    def list_active(
        self,
        *,
        layer: str | None = None,
        types: Iterable[str] | None = None,
        slot: str | None = None,
        include_archived: bool = False,
        limit: int = 200,
    ) -> list[MemoryRecord]:
        clauses = []
        args: list[Any] = []
        if not include_archived:
            clauses.append("is_archived=0")
        if layer:
            clauses.append("layer=?")
            args.append(layer)
        if types:
            tlist = list(types)
            clauses.append(f"type IN ({','.join('?' * len(tlist))})")
            args.extend(tlist)
        if slot is not None:
            clauses.append("slot=?")
            args.append(slot)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        args.append(int(limit))
        sql = f"SELECT * FROM memories{where} ORDER BY last_activity_at DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [_row_to_record(r) for r in rows]

    def archive(self, memory_id: str, superseded_by: str = "") -> None:
        fields: dict[str, Any] = {"is_archived": 1}
        if superseded_by:
            fields["superseded_by"] = superseded_by
        self.update_fields(memory_id, **fields)
        self._event("archive", memory_id, related_id=superseded_by)

    def delete(self, memory_id: str) -> MemoryRecord | None:
        rec = self.get(memory_id)
        if not rec:
            return None
        with self._lock:
            self._conn.execute("DELETE FROM memory_vec WHERE memory_id=?", (memory_id,))
            # FTS external-content 风格：按行内容删（我们用独立 fts，按 tokenize 内容匹配删）
            self._delete_fts(rec)
            self._conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
            self._event("erase", memory_id, summary_hash=content_hash(rec.text()))
            self._conn.commit()
        return rec

    def mark_recalled(self, memory_ids: Iterable[str], now: float | None = None, heat_boost: float = 0.15) -> None:
        ids = [i for i in memory_ids if i]
        if not ids:
            return
        now = now or time.time()
        with self._lock:
            for mid in ids:
                row = self._conn.execute(
                    "SELECT heat, recall_count FROM memories WHERE id=?", (mid,)
                ).fetchone()
                if not row:
                    continue
                heat = min(1.0, float(row["heat"] or 0) + heat_boost)
                self._conn.execute(
                    """UPDATE memories SET heat=?, recall_count=?, last_recall_at=?, last_activity_at=?
                       WHERE id=?""",
                    (heat, int(row["recall_count"] or 0) + 1, now, now, mid),
                )
            self._conn.commit()

    # ------------------------------------------------------------------ vector / fts

    def _upsert_vec(self, memory_id: str, vec: list[float], model: str) -> None:
        blob = pack_vec(vec)
        self._conn.execute(
            """INSERT INTO memory_vec(memory_id, model, dim, vec) VALUES(?,?,?,?)
               ON CONFLICT(memory_id) DO UPDATE SET model=excluded.model, dim=excluded.dim, vec=excluded.vec""",
            (memory_id, model, len(vec), blob),
        )

    def set_vector(self, memory_id: str, vec: list[float], model: str) -> None:
        with self._lock:
            self._upsert_vec(memory_id, vec, model)
            self._conn.commit()

    def get_vector(self, memory_id: str, model: str | None = None) -> list[float] | None:
        with self._lock:
            if model:
                row = self._conn.execute(
                    "SELECT vec, model, dim FROM memory_vec WHERE memory_id=? AND model=?",
                    (memory_id, model),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT vec, model, dim FROM memory_vec WHERE memory_id=?",
                    (memory_id,),
                ).fetchone()
        if not row:
            return None
        return unpack_vec(row["vec"])

    def all_vectors(
        self,
        *,
        layer: str | None = None,
        include_archived: bool = False,
        model: str | None = None,
    ) -> list[tuple[MemoryRecord, list[float]]]:
        clauses = ["1=1"]
        args: list[Any] = []
        if not include_archived:
            clauses.append("m.is_archived=0")
        if layer:
            clauses.append("m.layer=?")
            args.append(layer)
        if model:
            clauses.append("v.model=?")
            args.append(model)
        where = " AND ".join(clauses)
        sql = f"""
            SELECT m.*, v.vec AS _vec, v.model AS _model, v.dim AS _dim
            FROM memories m
            JOIN memory_vec v ON v.memory_id = m.id
            WHERE {where}
        """
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        out: list[tuple[MemoryRecord, list[float]]] = []
        for r in rows:
            rec = _row_to_record(r)
            out.append((rec, unpack_vec(r["_vec"])))
        return out

    def _ensure_fts_map(self) -> None:
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS fts_map(
                rowid INTEGER PRIMARY KEY,
                memory_id TEXT NOT NULL UNIQUE
            )"""
        )

    def _upsert_fts(self, rec: MemoryRecord) -> None:
        self._ensure_fts_map()
        self._delete_fts(rec)
        tags = " ".join(rec.tags)
        self._conn.execute(
            "INSERT INTO memory_fts(content, summary, tags) VALUES(?,?,?)",
            (fts_tokenize(rec.content), fts_tokenize(rec.summary), fts_tokenize(tags)),
        )
        rid = int(self._conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        self._conn.execute(
            "INSERT INTO fts_map(rowid, memory_id) VALUES(?,?)",
            (rid, rec.id),
        )

    def _delete_fts(self, rec: MemoryRecord) -> None:
        self._ensure_fts_map()
        row = self._conn.execute(
            "SELECT rowid FROM fts_map WHERE memory_id=?", (rec.id,)
        ).fetchone()
        if row:
            rid = int(row["rowid"] if isinstance(row, sqlite3.Row) else row[0])
            self._conn.execute("DELETE FROM memory_fts WHERE rowid=?", (rid,))
            self._conn.execute("DELETE FROM fts_map WHERE memory_id=?", (rec.id,))

    def fts_search(self, query: str, *, layer: str | None = None, limit: int = 20) -> list[MemoryRecord]:
        tok = fts_tokenize(query)
        if not tok:
            return []
        # FTS5：空格 AND
        match = " ".join(tok.split())
        sql = """
            SELECT m.* FROM memory_fts f
            JOIN fts_map map ON map.rowid = f.rowid
            JOIN memories m ON m.id = map.memory_id
            WHERE memory_fts MATCH ? AND m.is_archived=0
        """
        args: list[Any] = [match]
        if layer:
            sql += " AND m.layer=?"
            args.append(layer)
        sql += " LIMIT ?"
        args.append(int(limit))
        with self._lock:
            try:
                rows = self._conn.execute(sql, args).fetchall()
            except sqlite3.OperationalError:
                return []
        return [_row_to_record(r) for r in rows]

    # ------------------------------------------------------------------ turn_log

    def append_turn(self, session_id: str, role: str, content: str, *, keep: int = 80) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO turn_log(session_id, role, content, created_at, digested) VALUES(?,?,?,?,0)",
                (session_id, role, content, now),
            )
            # 环形：只保留最近 keep 条
            self._conn.execute(
                """DELETE FROM turn_log WHERE session_id=? AND id NOT IN (
                    SELECT id FROM turn_log WHERE session_id=? ORDER BY id DESC LIMIT ?
                )""",
                (session_id, session_id, keep),
            )
            self._conn.commit()

    def undigested_turns(self, session_id: str, limit: int = 40) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, role, content, created_at FROM turn_log
                   WHERE session_id=? AND digested=0 ORDER BY id ASC LIMIT ?""",
                (session_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_turns_digested(self, turn_ids: Iterable[int]) -> None:
        ids = list(turn_ids)
        if not ids:
            return
        with self._lock:
            self._conn.executemany(
                "UPDATE turn_log SET digested=1 WHERE id=?",
                [(i,) for i in ids],
            )
            self._conn.commit()

    def count_undigested(self, session_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM turn_log WHERE session_id=? AND digested=0",
                (session_id,),
            ).fetchone()
        return int(row["c"] if row else 0)

    # ------------------------------------------------------------------ alarms

    def insert_alarm(
        self,
        *,
        fire_at: float,
        label: str = "",
        repeat_rule: str = "once",
        memory_id: str = "",
        alarm_id: str | None = None,
    ) -> str:
        aid = alarm_id or new_id("al")
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO alarms(id, fire_at, repeat_rule, label, memory_id, status, created_at)
                   VALUES(?,?,?,?,?,'pending',?)""",
                (aid, float(fire_at), repeat_rule, label, memory_id, now),
            )
            self._conn.commit()
        return aid

    def pending_alarms(self, until: float | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if until is None:
                rows = self._conn.execute(
                    "SELECT * FROM alarms WHERE status='pending' ORDER BY fire_at ASC"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM alarms WHERE status='pending' AND fire_at<=? ORDER BY fire_at ASC",
                    (until,),
                ).fetchall()
        return [dict(r) for r in rows]

    def update_alarm(self, alarm_id: str, **fields: Any) -> None:
        allowed = {"fire_at", "status", "fired_at", "missed_at", "label", "repeat_rule", "memory_id"}
        cols, vals = [], []
        for k, v in fields.items():
            if k in allowed:
                cols.append(f"{k}=?")
                vals.append(v)
        if not cols:
            return
        vals.append(alarm_id)
        with self._lock:
            self._conn.execute(f"UPDATE alarms SET {', '.join(cols)} WHERE id=?", vals)
            self._conn.commit()

    def cancel_alarms_for_memory(self, memory_id: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE alarms SET status='cancelled' WHERE memory_id=? AND status='pending'",
                (memory_id,),
            )
            self._conn.commit()
            return cur.rowcount

    def cancel_alarm(self, alarm_id: str) -> None:
        # fired_at 兼作关闭时间，供 7 天保留期计算（避免回落到 created_at）
        self.update_alarm(alarm_id, status="cancelled", fired_at=time.time())

    def get_alarm(self, alarm_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM alarms WHERE id=?", (alarm_id,)).fetchone()
        return dict(row) if row else None

    def purge_terminal_alarms(self, cutoff: float) -> int:
        """物理删除已结束且结束时刻早于 cutoff 的闹钟行。"""
        with self._lock:
            cur = self._conn.execute(
                """DELETE FROM alarms
                   WHERE status IN ('fired','missed','cancelled')
                     AND COALESCE(fired_at, missed_at, created_at) < ?""",
                (float(cutoff),),
            )
            self._conn.commit()
            return int(cur.rowcount or 0)

    # ------------------------------------------------------------------ tombstones / events

    def add_tombstone(self, *, slot: str, text: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO tombstones(slot, content_hash, created_at) VALUES(?,?,?)",
                (slot or "", content_hash(text), time.time()),
            )
            self._conn.commit()

    def is_tombstoned(self, *, slot: str, text: str, ttl_days: float = TOMBSTONE_TTL_DAYS) -> bool:
        cutoff = time.time() - ttl_days * 86400
        h = content_hash(text)
        with self._lock:
            if slot:
                row = self._conn.execute(
                    """SELECT 1 FROM tombstones WHERE created_at>=? AND (
                         content_hash=? OR (slot<>'' AND slot=?)
                       ) LIMIT 1""",
                    (cutoff, h, slot),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT 1 FROM tombstones WHERE created_at>=? AND content_hash=? LIMIT 1",
                    (cutoff, h),
                ).fetchone()
        return row is not None

    def _event(
        self,
        event_type: str,
        memory_id: str = "",
        related_id: str = "",
        summary_hash: str = "",
    ) -> None:
        self._conn.execute(
            """INSERT INTO memory_events(event_type, memory_id, related_id, summary_hash, created_at)
               VALUES(?,?,?,?,?)""",
            (event_type, memory_id, related_id, summary_hash, time.time()),
        )

    def log_event(
        self,
        event_type: str,
        memory_id: str = "",
        related_id: str = "",
        summary_hash: str = "",
    ) -> None:
        with self._lock:
            self._event(event_type, memory_id, related_id, summary_hash)
            self._conn.commit()

    def find_by_slot(self, slot: str, *, layer: str | None = None, active_only: bool = True) -> list[MemoryRecord]:
        return self.list_active(layer=layer, slot=slot, include_archived=not active_only)

    def count(self, *, layer: str | None = None, include_archived: bool = False) -> int:
        clauses = []
        args: list[Any] = []
        if not include_archived:
            clauses.append("is_archived=0")
        if layer:
            clauses.append("layer=?")
            args.append(layer)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            row = self._conn.execute(f"SELECT COUNT(*) AS c FROM memories{where}", args).fetchone()
        return int(row["c"] if row else 0)
