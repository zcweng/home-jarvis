"""Home Assistant REST 客户端：只做三件事——读状态、调服务、列实体。

用 REST 而不是 WebSocket：P0 只需要"下发指令 + 读一次状态"，REST 足够且好调试。
（P1 要做"灯被手动关了，助手要知道"时才需要订阅 WebSocket 事件流。）
"""

from __future__ import annotations

import httpx

from .config import Settings


class HAError(RuntimeError):
    pass


class HAClient:
    def __init__(self, settings: Settings | None = None, timeout: float = 8.0) -> None:
        st = settings or Settings.load()
        if not st.ha_token:
            raise HAError("缺少 HA_TOKEN，先执行 python3 scripts/set_ha_token.py")
        self.base = st.ha_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base,
            headers={"Authorization": f"Bearer {st.ha_token}", "Content-Type": "application/json"},
            timeout=timeout,
        )

    # ------------------------------------------------------------------ 读

    def ping(self) -> str:
        r = self._client.get("/api/")
        r.raise_for_status()
        return r.json().get("message", "")

    def states(self) -> list[dict]:
        r = self._client.get("/api/states")
        r.raise_for_status()
        return r.json()

    def state(self, entity_id: str) -> dict | None:
        r = self._client.get(f"/api/states/{entity_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------------ 写

    def call_service(self, domain: str, service: str, data: dict) -> list[dict]:
        r = self._client.post(f"/api/services/{domain}/{service}", json=data)
        if r.status_code >= 400:
            raise HAError(f"{domain}.{service} 失败 HTTP {r.status_code}: {r.text[:200]}")
        try:
            return r.json()
        except ValueError:
            return []

    def notify(self, entity_id: str, message: str) -> list[dict]:
        """调用 notify 实体（米家音箱的"播放文本"就是它）。"""
        return self.call_service("notify", "send_message", {"entity_id": entity_id, "message": message})

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HAClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
