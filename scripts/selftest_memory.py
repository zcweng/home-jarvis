#!/usr/bin/env python3
"""分层记忆自检（不依赖真 LLM / 真 embedding）。

用法：
  .venv/bin/python scripts/selftest_memory.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jarvis.memory.alarms import AlarmScheduler  # noqa: E402
from jarvis.memory.classifier import classify, rule_classify  # noqa: E402
from jarvis.memory.decay import (  # noqa: E402
    apply_recall_boost,
    estimate_tokens,
    effective_heat,
    time_decay,
)
from jarvis.memory.digest import MemoryDigest  # noqa: E402
from jarvis.memory.eraser import MemoryEraser, FORGET_REPLY  # noqa: E402
from jarvis.memory.retriever import MemoryRetriever  # noqa: E402
from jarvis.memory.service import MemoryService, MemorySettings  # noqa: E402
from jarvis.memory.store import MemoryStore  # noqa: E402
from jarvis.memory.types import ClassifyResult, MemoryRecord  # noqa: E402
from jarvis.memory.writer import MemoryWriter  # noqa: E402


def ok(name: str) -> None:
    print(f"  ✅ {name}")


def fail(name: str, detail: str) -> None:
    print(f"  ❌ {name}: {detail}")
    raise SystemExit(1)


def test_decay() -> None:
    now = time.time()
    # 永久不衰减
    assert time_decay(now, now - 86400 * 365, layer="perm") == 1.0
    # 180 天半衰期：刚好一个半衰期 → 0.5
    half = time_decay(now, now - 180 * 86400, half_life_days=180, layer="short")
    if abs(half - 0.5) > 1e-6:
        fail("半衰期", f"got {half}")
    h = apply_recall_boost(0.5, 0.15)
    if abs(h - 0.65) > 1e-9:
        fail("召回加热", f"got {h}")
    assert estimate_tokens("你好世界") == 4
    ok("decay 公式")


def test_classifier_rules() -> None:
    r = rule_classify("记住我不吃香菜")
    assert r and r.should_store and r.type == "preference"
    r = rule_classify("我叫小明")
    assert r and r.slot == "name" and r.type == "profile"
    r = rule_classify("忘掉我不吃香菜")
    assert r and r.erase and r.erase.get("target")
    r = rule_classify("清空记忆")
    assert r and r.erase and r.erase.get("all") and not r.erase.get("confirmed")
    r = rule_classify("确认清空")
    assert r and r.erase and r.erase.get("confirmed")
    # 闲聊不走规则 → None，classify 无 llm 时 should_store=False
    assert rule_classify("今天天气真好") is None
    c = classify("哈哈", "", llm=None)
    assert not c.should_store
    ok("分类器规则快路径")


def test_conflict_and_tombstone() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = MemoryStore(Path(td) / "m.db")
        writer = MemoryWriter(store, half_life_days=180)

        d1 = ClassifyResult(
            should_store=True,
            layer="perm",
            type="preference",
            slot="food.苹果",
            content="用户不喜欢苹果",
            summary="用户不喜欢苹果",
            importance=0.8,
            tags=["preference", "food"],
        )
        r1 = writer.write(d1)
        assert r1.action == "insert"

        d2 = ClassifyResult(
            should_store=True,
            layer="perm",
            type="preference",
            slot="food.苹果",
            content="用户喜欢苹果",
            summary="用户喜欢苹果",
            importance=0.85,
            tags=["preference", "food"],
        )
        r2 = writer.write(d2)
        if r2.action != "supersede":
            fail("冲突纠正", f"action={r2.action}")
        old = store.get(r1.memory_id)
        assert old and old.is_archived and old.superseded_by == r2.memory_id
        active = store.find_by_slot("food.苹果", active_only=True)
        assert len(active) == 1 and "喜欢" in active[0].text()

        # 擦除 + tombstone
        eraser = MemoryEraser(store)
        er = eraser.erase(slot="food.苹果")
        assert er.ok and er.message == FORGET_REPLY
        blocked = writer.write(d1)
        if blocked.action != "blocked":
            fail("tombstone", f"action={blocked.action}")
        store.close()
    ok("冲突纠正 + tombstone")


def test_merge_refresh() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = MemoryStore(Path(td) / "m.db")
        writer = MemoryWriter(store)
        d = ClassifyResult(
            should_store=True,
            layer="perm",
            type="preference",
            slot="food.香菜",
            content="用户不吃香菜",
            summary="用户不吃香菜",
            importance=0.7,
        )
        a = writer.write(d)
        b = writer.write(d)
        if b.action != "refresh" or b.memory_id != a.memory_id:
            fail("去重刷新", f"{b.action} {b.memory_id}")
        assert store.count(layer="perm") == 1
        store.close()
    ok("相同记忆刷新")


def test_retrieve_order_and_budget() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = MemoryStore(Path(td) / "m.db")
        writer = MemoryWriter(store)
        writer.write(
            ClassifyResult(
                should_store=True, layer="perm", type="profile", slot="name",
                content="用户叫小明", summary="用户姓名：小明", importance=0.95, tags=["profile"],
            )
        )
        writer.write(
            ClassifyResult(
                should_store=True, layer="perm", type="preference", slot="food.香菜",
                content="用户不吃香菜", summary="用户不吃香菜", importance=0.8, tags=["food"],
            )
        )
        writer.write(
            ClassifyResult(
                should_store=True, layer="short", type="episode",
                content="刚才聊了晚饭", summary="刚才聊了晚饭", importance=0.5, tags=["episode"],
            )
        )
        ret = MemoryRetriever(store, token_budget=800, recall_k_short=8)
        block = ret.build_block("今天吃什么 香菜")
        if "【记忆】" not in block or "小明" not in block:
            fail("召回拼装", block)
        # profile 应出现在 preference 之前（文本顺序）
        i_name = block.find("小明")
        i_food = block.find("香菜")
        if i_name < 0 or i_food < 0 or i_name > i_food:
            fail("永久靠前", block)
        # 召回加热
        before = store.find_by_slot("food.香菜")[0].heat
        ret.retrieve("香菜")
        after = store.get(store.find_by_slot("food.香菜")[0].id)
        assert after and after.heat >= before
        store.close()
    ok("召回顺序 + 加热")


def test_digest_compress() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = MemoryStore(Path(td) / "m.db")
        writer = MemoryWriter(store)
        dig = MemoryDigest(store, writer, episode_every=3, compress_at_messages=6, keep_recent=2)
        sid = "s_test"
        for i in range(4):
            store.append_turn(sid, "user", f"用户话{i}")
            store.append_turn(sid, "assistant", f"回复{i}")
        ids = dig.maybe_episode(sid, force=True)
        assert ids
        assert store.count(layer="short") >= 1

        hist = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(10)]
        new_h, summary = dig.compress_history(sid, hist)
        assert summary and len(new_h) < len(hist)
        store.close()
    ok("摘要压缩")


def test_alarms_persist() -> None:
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "m.db"
        store = MemoryStore(db)
        fired: list[str] = []
        sch = AlarmScheduler(store, announce=lambda t: fired.append(t), missed_grace=600, tick=0.2)
        sch.start()
        aid, _ = sch.schedule_in(0.02, label="关火")  # ~1.2 秒
        deadline = time.time() + 3
        while time.time() < deadline and not fired:
            time.sleep(0.1)
        sch.close()
        if not fired:
            fail("闹钟触发", "未播报")
        # 模拟重启：写入一个已到期但仍 pending 的闹钟，应补报
        store2 = MemoryStore(db)
        store2.insert_alarm(fire_at=time.time() - 10, label="吃药", repeat_rule="once")
        fired2: list[str] = []
        sch2 = AlarmScheduler(store2, announce=lambda t: fired2.append(t), missed_grace=600, tick=0.2)
        sch2.start()
        time.sleep(0.5)
        sch2.close()
        store2.close()
        if not fired2:
            fail("重启补报", "未补报")
        store.close()
    ok("闹钟持久化与补报")


def test_service_facade() -> None:
    with tempfile.TemporaryDirectory() as td:
        ms = MemorySettings(
            enabled=True,
            db_path=str(Path(td) / "m.db"),
            model_dir=str(Path(td) / "no-model"),
            token_budget=400,
            episode_every=100,
        )
        svc = MemoryService(ms, root=Path(td))
        assert svc.enabled
        # 无模型也应能召回（关键词）
        svc.observe_turn("记住我不吃香菜", "好的，记住了")
        # 等后台
        deadline = time.time() + 3
        while time.time() < deadline:
            if svc.store and svc.store.count() > 0:
                break
            time.sleep(0.05)
        block = svc.build_context("晚饭吃什么")
        if "香菜" not in block and svc.store and svc.store.count() == 0:
            fail("service 写入", "后台未写入")
        # 即使 block 暂时为空（worker 慢），count 应 >0
        assert svc.store and svc.store.count() >= 1
        out = svc.tool_memory("list")
        assert "香菜" in out or "m_" in out
        svc.close()
    ok("MemoryService 门面")


def test_recall_latency() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = MemoryStore(Path(td) / "m.db")
        writer = MemoryWriter(store)
        for i in range(50):
            writer.write(
                ClassifyResult(
                    should_store=True,
                    layer="perm" if i % 5 == 0 else "short",
                    type="preference" if i % 5 == 0 else "episode",
                    slot=f"food.item{i}" if i % 5 == 0 else "",
                    content=f"记忆条目{i}关于饮食偏好",
                    summary=f"记忆条目{i}关于饮食偏好",
                    importance=0.6,
                )
            )
        ret = MemoryRetriever(store, token_budget=800)
        times = []
        for _ in range(20):
            t0 = time.perf_counter()
            ret.retrieve("饮食偏好")
            times.append((time.perf_counter() - t0) * 1000)
        times.sort()
        p95 = times[int(0.95 * (len(times) - 1))]
        print(f"     召回 P95={p95:.1f}ms（目标 <50ms，无向量时仅 FTS/热度）")
        if p95 > 200:
            fail("召回延迟", f"P95={p95:.1f}ms 过高")
        store.close()
    ok("召回延迟")


def main() -> int:
    print("selftest_memory…")
    test_decay()
    test_classifier_rules()
    test_conflict_and_tombstone()
    test_merge_refresh()
    test_retrieve_order_and_budget()
    test_digest_compress()
    test_alarms_persist()
    test_service_facade()
    test_recall_latency()
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
