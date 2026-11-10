"""多任务编排：语义解析 → 任务拆分 → 依赖 DAG → 并发调度 → 顺序播报。

入口是 `TaskService`（service.py）；外层只需要三件事：

    svc = TaskService(TaskSettings.from_settings(st), tools, llm, speak=tts.speak, ...)
    svc.submit("今天天气如何？打开客厅灯，关闭晾衣架")   # 异步执行
    svc.describe() / svc.cancel("全部")                 # 语音查询、取消

可选依赖 langgraph（`uv sync --extra tasks`）；没装则 `svc.enabled()` 为 False，
主语音链路完全不受影响。
"""

from __future__ import annotations

from .model import (
    CANCELLED,
    DONE,
    FAILED,
    QUEUED,
    RUNNING,
    SKIPPED,
    STATUS_LABEL,
    TaskRecord,
    TaskRun,
    TaskSpec,
)
from .service import SubmitResult, TaskService, TaskSettings, cn_num

__all__ = [
    "CANCELLED",
    "DONE",
    "FAILED",
    "QUEUED",
    "RUNNING",
    "SKIPPED",
    "STATUS_LABEL",
    "SubmitResult",
    "TaskRecord",
    "TaskRun",
    "TaskService",
    "TaskSettings",
    "TaskSpec",
    "cn_num",
]
