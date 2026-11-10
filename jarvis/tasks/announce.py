"""任务结果的语音播报：**严格串行、按完成先后**。

要点（对应需求「每个结果按返回先后顺序依次播报，不并发播报」）：

- 一个消费者线程 + 一个 FIFO 队列。任务节点谁先跑完谁先 push，
  消费者按队列顺序一次只念一条，天然不会叠音。
- 念之前先等主对话那边的喇叭空下来（`is_idle`），否则「三件事的结果」
  会和当前这句回答撞在一起，用户只听到一团噪音。
- 播报期间通知上层（`on_busy`）：麦克风必须把这段时间的输入当回声丢掉，
  否则 VAD 会把音箱/喇叭自己念的话当成用户说话（项目里踩过这个坑）。
- 任何一条播报失败都不能把消费者线程弄死，否则后面所有结果都哑了。

注意：这里只负责「什么时候说」，不负责「说什么」。文本由工具返回值直接给出——
工具契约本来就是「返回值就是要念出来的那句中文」，不再过一次模型，
省一次 1.2~1.9s 的 LLM 往返，也避免模型二次创作说错。
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable


def _warn(msg: str) -> None:
    print(f"  ⚠️  任务播报：{msg}", file=sys.stderr)


@dataclass
class AnnounceItem:
    run_id: str
    task_id: str
    text: str
    seq: int = 0
    title: str = ""
    status: str = ""
    at: float = 0.0


class Announcer:
    """单线程串行播报器。"""

    def __init__(
        self,
        speak: Callable[[str], None],
        *,
        is_idle: Callable[[], bool] | None = None,
        on_busy: Callable[[bool], None] | None = None,
        idle_wait: float = 12.0,
        min_gap: float = 0.25,
        enabled: bool = True,
    ) -> None:
        self._speak = speak
        self._is_idle = is_idle
        self._on_busy = on_busy
        self._idle_wait = max(0.0, float(idle_wait))
        self._min_gap = max(0.0, float(min_gap))
        self.enabled = enabled
        self._q: queue.Queue[AnnounceItem | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.spoken: list[AnnounceItem] = []  # 供自检/排障看「到底念了哪些、什么顺序」
        self.failures = 0

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name="jarvis-announcer", daemon=True)
            self._thread.start()

    def close(self, timeout: float = 5.0) -> None:
        self._q.put(None)
        t = self._thread
        if t is not None:
            t.join(timeout=timeout)

    # ---------------------------------------------------------------- 入队

    def push(self, item: AnnounceItem) -> bool:
        text = (item.text or "").strip()
        if not text:
            return False
        if not self.enabled:
            return False
        item = AnnounceItem(
            run_id=item.run_id,
            task_id=item.task_id,
            text=text,
            seq=item.seq,
            title=item.title,
            status=item.status,
            at=time.time(),
        )
        self.start()
        self._q.put(item)
        return True

    @property
    def pending(self) -> int:
        return self._q.qsize()

    # ---------------------------------------------------------------- 消费

    def _wait_idle(self) -> None:
        if self._is_idle is None:
            return
        deadline = time.monotonic() + self._idle_wait
        while time.monotonic() < deadline:
            try:
                if self._is_idle():
                    return
            except Exception:  # noqa: BLE001
                return
            time.sleep(0.1)
        _warn(f"主对话一直没停下来（>{self._idle_wait:g}s），仍然播报任务结果")

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            self._wait_idle()
            if self._on_busy is not None:
                try:
                    self._on_busy(True)
                except Exception:  # noqa: BLE001
                    pass
            try:
                self._speak(item.text)
                self.spoken.append(item)
                print(f"  🗣  任务播报 [{item.seq}] {item.title}：{item.text[:40]}")
            except Exception as e:  # noqa: BLE001
                self.failures += 1
                _warn(f"播报失败（{item.task_id}）：{e}")
            finally:
                if self._on_busy is not None:
                    try:
                        self._on_busy(False)
                    except Exception:  # noqa: BLE001
                        pass
            if self._min_gap:
                time.sleep(self._min_gap)