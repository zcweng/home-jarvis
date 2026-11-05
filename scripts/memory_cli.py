#!/usr/bin/env python3
"""记忆库 CLI：list / search / erase / export / rebuild-index。

用法：
  .venv/bin/python scripts/memory_cli.py list
  .venv/bin/python scripts/memory_cli.py search 香菜
  .venv/bin/python scripts/memory_cli.py erase --target 香菜
  .venv/bin/python scripts/memory_cli.py erase --slot food.香菜
  .venv/bin/python scripts/memory_cli.py erase --all --confirm
  .venv/bin/python scripts/memory_cli.py export -o /tmp/mem.jsonl
  .venv/bin/python scripts/memory_cli.py rebuild-index [--rebuild-vectors]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jarvis.config import Settings  # noqa: E402
from jarvis.memory.embedding import build_embedder  # noqa: E402
from jarvis.memory.eraser import MemoryEraser  # noqa: E402
from jarvis.memory.store import MemoryStore  # noqa: E402


def _db_path(args: argparse.Namespace) -> Path:
    st = Settings.load()
    p = Path(args.db or st.memory_db)
    if not p.is_absolute():
        p = ROOT / p
    return p


def cmd_list(args: argparse.Namespace) -> int:
    store = MemoryStore(_db_path(args))
    rows = store.list_active(
        layer=args.layer or None,
        include_archived=args.archived,
        limit=args.limit,
    )
    for r in rows:
        flag = " [archived]" if r.is_archived else ""
        print(f"[{r.id}] {r.layer}/{r.type} slot={r.slot or '-'} heat={r.heat:.2f}{flag}")
        print(f"    {r.text()}")
    print(f"— {len(rows)} 条 —")
    store.close()
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    store = MemoryStore(_db_path(args))
    q = args.query
    hits = store.fts_search(q, layer=args.layer or None, limit=args.limit)
    if not hits:
        # 回落字面
        hits = [r for r in store.list_active(limit=200) if q in r.text()]
    for r in hits:
        print(f"[{r.id}] {r.layer}/{r.type} {r.text()}")
    print(f"— {len(hits)} 条 —")
    store.close()
    return 0


def cmd_erase(args: argparse.Namespace) -> int:
    store = MemoryStore(_db_path(args))
    eraser = MemoryEraser(store)
    if args.all:
        if not args.confirm:
            print("清空全部需要 --confirm")
            store.close()
            return 2
        res = eraser.erase(all_=True, confirmed=True)
    else:
        res = eraser.erase(
            memory_id=args.id or "",
            slot=args.slot or "",
            target=args.target or "",
            confirmed=bool(args.confirm),
        )
        if res.need_confirm and args.confirm:
            res = eraser.confirm_pending()
    print(res.message)
    if res.erased_ids:
        print("已删：", ", ".join(res.erased_ids))
    store.close()
    return 0 if res.ok else 1


def cmd_export(args: argparse.Namespace) -> int:
    store = MemoryStore(_db_path(args))
    rows = store.list_active(include_archived=True, limit=100000)
    out = Path(args.output)
    with out.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(
                json.dumps(
                    {
                        "id": r.id,
                        "layer": r.layer,
                        "type": r.type,
                        "slot": r.slot,
                        "content": r.content,
                        "summary": r.summary,
                        "tags": r.tags,
                        "importance": r.importance,
                        "heat": r.heat,
                        "is_archived": r.is_archived,
                        "superseded_by": r.superseded_by,
                        "created_at": r.created_at,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    print(f"导出 {len(rows)} 条 → {out}")
    store.close()
    return 0


def cmd_rebuild(args: argparse.Namespace) -> int:
    st = Settings.load()
    store = MemoryStore(_db_path(args))
    rows = store.list_active(include_archived=True, limit=100000)
    # 重建 FTS：删了再插
    n = 0
    for r in rows:
        store._upsert_fts(r)  # noqa: SLF001 — CLI 维护入口
        n += 1
    store._conn.commit()  # noqa: SLF001
    print(f"已重建 FTS {n} 条")
    if args.rebuild_vectors:
        embedder = build_embedder(ROOT / (args.model_dir or st.memory_model_dir))
        if not embedder.warm():
            print("向量模型不可用，跳过")
            store.close()
            return 1
        ok = 0
        for r in rows:
            vec = embedder.encode_one(r.text())
            if vec is None:
                continue
            store.set_vector(r.id, vec, getattr(embedder, "model_name", "bge-small-zh-v1.5"))
            ok += 1
        print(f"已重建向量 {ok} 条")
    store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Jarvis 记忆库 CLI")
    ap.add_argument("--db", default="", help="覆盖 MEMORY_DB")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="列出记忆")
    p.add_argument("--layer", choices=["perm", "short"], default="")
    p.add_argument("--archived", action="store_true")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("search", help="搜索")
    p.add_argument("query")
    p.add_argument("--layer", choices=["perm", "short"], default="")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("erase", help="擦除")
    p.add_argument("--id", default="")
    p.add_argument("--slot", default="")
    p.add_argument("--target", default="")
    p.add_argument("--all", action="store_true")
    p.add_argument("--confirm", action="store_true")
    p.set_defaults(func=cmd_erase)

    p = sub.add_parser("export", help="导出 JSONL")
    p.add_argument("-o", "--output", required=True)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("rebuild-index", help="重建 FTS / 向量")
    p.add_argument("--rebuild-vectors", action="store_true")
    p.add_argument("--model-dir", default="")
    p.set_defaults(func=cmd_rebuild)

    args = ap.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
