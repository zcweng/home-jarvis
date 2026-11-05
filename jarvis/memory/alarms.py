"""持久化闹钟调度：重启可恢复、到期补报。"""

from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timedelta
from typing import Callable

from .store import MemoryStore
from .types import ClassifyResult
from .writer import MemoryWriter

AnnounceFn = Callable[[str], None]


def _warn(msg: str) -> None:
    print(f"  ⚠️  memory alarms：{msg}", file=sys.stderr)


class AlarmScheduler:
    def __init__(
        self,
        store: MemoryStore,
        *,
        announce: AnnounceFn | None = None,
        writer: MemoryWriter | None = None,
        missed_grace: float = 600.0,
        tick: float = 1.0,
    ) -> None:
        self.store = store
        self.announce = announce
        self.writer = writer
        self.missed_grace = missed_grace
        self.tick = tick
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._mono0 = time.monotonic()
        self._wall0 = time.time()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._recover_on_boot()
        self._thread = threading.Thread(target=self._loop, name="jarvis-alarms", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def schedule_in(
        self,
        minutes: float,
        label: str = "",
        *,
        memory_id: str = "",
    ) -> tuple[str, float]:
        secs = max(1.0, float(minutes) * 60.0)
        fire_at = time.time() + secs
        aid = self.store.insert_alarm(
            fire_at=fire_at,
            label=label or "",
            repeat_rule="once",
            memory_id=memory_id,
        )
        return aid, fire_at

    def schedule_at(
        self,
        fire_at: float,
        label: str = "",
        *,
        repeat_rule: str = "once",
        memory_id: str = "",
    ) -> str:
        return self.store.insert_alarm(
            fire_at=fire_at,
            label=label,
            repeat_rule=repeat_rule,
            memory_id=memory_id,
        )

    def cancel(self, alarm_id: str) -> None:
        self.store.cancel_alarm(alarm_id)

    def cancel_all_pending(self) -> int:
        n = 0
        for a in self.store.pending_alarms():
            self.store.cancel_alarm(a["id"])
            n += 1
        return n

    def list_pending(self) -> list[dict]:
        return self.store.pending_alarms()

    # ------------------------------------------------------------------ internal

    def _now(self) -> float:
        # 墙钟为主；单调时钟用于察觉系统睡眠跳跃（目前仅记录）
        return time.time()

    def _recover_on_boot(self) -> None:
        now = self._now()
        for a in self.store.pending_alarms():
            fire_at = float(a["fire_at"])
            if fire_at > now:
                continue
            overdue = now - fire_at
            if overdue <= self.missed_grace:
                self._fire(a, now, makeup=True)
            else:
                self.store.update_alarm(a["id"], status="missed", missed_at=now)
                self._note_episode(f"错过提醒：{a.get('label') or '定时'}", a.get("memory_id") or "")

    def _loop(self) -> None:
        while not self._stop.wait(self.tick):
            try:
                now = self._now()
                due = self.store.pending_alarms(until=now + 0.05)
                for a in due:
                    self._fire(a, now, makeup=False)
            except Exception as e:  # noqa: BLE001
                _warn(f"调度异常：{e}")

    def _fire(self, alarm: dict, now: float, *, makeup: bool) -> None:
        aid = alarm["id"]
        label = (alarm.get("label") or "").strip()
        text = f"{label}" if label else "时间到了"
        if makeup:
            text = f"刚才的提醒：{text}"
        else:
            text = f"{text}。" if not text.endswith("。") else text
            # 与旧 timer 文案接近
            if label:
                text = f"时间到了，{label}。" if "到了" not in label else label
            else:
                text = "时间到了。"

        if self.announce:
            try:
                self.announce(text)
            except Exception as e:  # noqa: BLE001
                _warn(f"播报失败：{e}")
        else:
            print(f"⏰ {text}")

        repeat = (alarm.get("repeat_rule") or "once").lower()
        if repeat == "once":
            self.store.update_alarm(aid, status="fired", fired_at=now)
        else:
            nxt = _next_fire(float(alarm["fire_at"]), repeat, now=now)
            self.store.update_alarm(aid, fire_at=nxt, fired_at=now, status="pending")

        mid = alarm.get("memory_id") or ""
        self._note_episode(f"提醒已播报：{label or '定时'}", mid)
        if mid:
            try:
                self.store.mark_recalled([mid], now=now, heat_boost=0.1)
            except Exception:  # noqa: BLE001
                pass

    def _note_episode(self, summary: str, memory_id: str) -> None:
        if not self.writer:
            return
        try:
            self.writer.write(
                ClassifyResult(
                    should_store=True,
                    layer="short",
                    type="episode",
                    content=summary,
                    summary=summary,
                    importance=0.5,
                    tags=["alarm", "episode"],
                ),
                source="alarm",
            )
        except Exception:  # noqa: BLE001
            pass


def _next_fire(prev: float, rule: str, *, now: float) -> float:
    base = datetime.fromtimestamp(prev)
    if rule == "daily":
        nxt = base + timedelta(days=1)
        while nxt.timestamp() <= now:
            nxt += timedelta(days=1)
        return nxt.timestamp()
    if rule == "weekly":
        nxt = base + timedelta(days=7)
        while nxt.timestamp() <= now:
            nxt += timedelta(days=7)
        return nxt.timestamp()
    return now + 86400


def parse_relative_minutes(hint: str) -> float | None:
    """从自然语言粗提分钟数。"""
    import re

    m = re.search(r"(\d+(?:\.\d+)?)\s*分钟", hint or "")
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+)\s*秒", hint or "")
    if m:
        return float(m.group(1)) / 60.0
    m = re.search(r"(\d+(?:\.\d+)?)\s*小时", hint or "")
    if m:
        return float(m.group(1)) * 60.0
    return None
