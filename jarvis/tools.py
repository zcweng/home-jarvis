"""家电与本地工具契约 —— 大模型能看到的全部世界就这些函数。

设计原则（对应方案 §5.4）：
1. **少而稳**：control / query / scene / timer / get_time 等固定集合，不给模型自由发挥的空间
2. **别名优先**：模型只需输出"客厅灯"这种中文名，实体映射在本地完成，模型不可能编出 entity_id
3. **结果说人话**：工具返回值就是要念出来的那句确认，避免模型二次创作
4. **失败不撒谎**：找不到设备/实体离线都返回明确中文，让模型去反问而不是瞎猜
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import yaml

from .config import ROOT, Settings
from .ha import HAError, HAClient
from .output import split_for_speech

_WEEKDAYS = ("一", "二", "三", "四", "五", "六", "日")

ALIASES_FILE = ROOT / "config" / "aliases.yaml"


@dataclass(frozen=True)
class Device:
    name: str
    entity_id: str
    type: str
    room: str
    aliases: tuple[str, ...] = ()

    def label(self) -> str:
        # "客厅灯" 已经在名字里带了房间，就别再拼一次成 "客厅·客厅灯"
        if not self.room or self.room in self.name:
            return self.name
        return f"{self.room}{self.name}"


class Tools:
    def __init__(
        self,
        ha: HAClient,
        aliases_path: Path = ALIASES_FILE,
        announce: Callable[[str], None] | None = None,
        settings: Settings | None = None,
        dry_run: bool = False,
        memory: Any = None,
    ) -> None:
        self.ha = ha
        self._announce = announce  # 定时器到点时本地朗读用
        self.settings = settings or Settings.load()
        self.dry_run = dry_run
        self.devices: dict[str, Device] = self._load(aliases_path)
        self._timers: list[threading.Timer] = []
        self.memory = memory  # MemoryService | None；timer / memory 工具委托
        self._tts: Any = None  # SwitchableTTS；set_tts 热切换

    def bind_memory(self, memory: Any) -> None:
        self.memory = memory

    def bind_tts(self, tts: Any) -> None:
        self._tts = tts

    # ---------------------------------------------------------------- 加载

    @staticmethod
    def _load(path: Path) -> dict[str, Device]:
        if not path.exists():
            raise SystemExit(
                f"缺少别名表：{path}\n"
                "  A. 自动生成（推荐，需要 HA 已接好设备）：python scripts/gen_aliases.py\n"
                "  B. 手动起步：cp config/aliases.example.yaml config/aliases.yaml 后改成自己的 entity_id"
            )
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        out: dict[str, Device] = {}
        for name, item in raw.items():
            if not isinstance(item, dict) or "entity" not in item:
                continue
            out[str(name)] = Device(
                name=str(name),
                entity_id=str(item["entity"]),
                type=str(item.get("type", "light")),
                room=str(item.get("room", "")),
                aliases=tuple(str(a) for a in (item.get("aliases") or [])),
            )
        return out

    # ---------------------------------------------------------------- 解析

    def resolve(self, query: str) -> Device | None:
        """中文说法 → 设备。精确 > 别名 > 子串；多个候选一律不猜（返回 None）。"""
        q = (query or "").strip()
        if not q:
            return None
        if q in self.devices:
            return self.devices[q]
        for dev in self.devices.values():
            if q in dev.aliases:
                return dev
        hits = [d for d in self.devices.values() if q in d.name or d.name in q or any(q in a or a in q for a in d.aliases)]
        return hits[0] if len(hits) == 1 else None

    def candidates(self, query: str) -> list[str]:
        q = (query or "").strip()
        return [d.name for d in self.devices.values() if q in d.name or d.name in q or any(q in a for a in d.aliases)]

    def catalog(self) -> str:
        """给 system prompt 的设备清单（一行一台，尽量短）。"""
        lines = []
        for d in self.devices.values():
            extra = "/".join(d.aliases) if d.aliases else ""
            lines.append(f"- {d.name}（{d.room or '未分房间'}，{d.type}）{(' 又叫：' + extra) if extra else ''}")
        return "\n".join(lines)

    # ---------------------------------------------------------------- 工具实现

    def control(self, device: str, action: str, value: float | None = None) -> str:
        dev = self.resolve(device)
        if dev is None:
            cands = self.candidates(device)
            if cands:
                return f"「{device}」指代不清，可能是：{'、'.join(cands)}。你要哪个？"
            return f"没找到叫「{device}」的设备。"

        # 先看在线状态：墙上开关关着时米家实体是 unavailable，服务调用会"成功"返回但什么都不会发生
        current = (self.ha.state(dev.entity_id) or {}).get("state")
        if current == "unavailable":
            return f"{dev.label()}现在不在线（可能是墙上开关关着或者断电），我控制不了它。"

        action = (action or "").strip().lower()
        try:
            if action in ("on", "open", "turn_on", "打开", "开"):
                if dev.type == "light":
                    self.ha.call_service("light", "turn_on", {"entity_id": dev.entity_id})
                else:
                    self.ha.call_service("homeassistant", "turn_on", {"entity_id": dev.entity_id})
                return f"已打开{dev.label()}。"

            if action in ("off", "close", "turn_off", "关闭", "关"):
                if dev.type == "light":
                    self.ha.call_service("light", "turn_off", {"entity_id": dev.entity_id})
                else:
                    self.ha.call_service("homeassistant", "turn_off", {"entity_id": dev.entity_id})
                return f"已关闭{dev.label()}。"

            if action in ("toggle", "切换"):
                self.ha.call_service("homeassistant", "toggle", {"entity_id": dev.entity_id})
                return f"已切换{dev.label()}。"

            if action in ("brightness", "set_brightness", "亮度"):
                pct = max(0, min(100, int(value if value is not None else 100)))
                self.ha.call_service("light", "turn_on", {"entity_id": dev.entity_id, "brightness_pct": pct})
                return f"{dev.label()}已调到 {pct}%。"

            if action in ("brightness_delta", "dim", "brighten", "调暗", "调亮"):
                delta = int(value or 0)
                cur = self._brightness_pct(dev.entity_id)
                pct = max(1, min(100, cur + delta))
                self.ha.call_service("light", "turn_on", {"entity_id": dev.entity_id, "brightness_pct": pct})
                return f"{dev.label()}从 {cur}% 调到 {pct}%。"

            if action in ("color_temp", "色温"):
                kelvin = int(value or 4000)
                self.ha.call_service(
                    "light", "turn_on", {"entity_id": dev.entity_id, "color_temp_kelvin": kelvin}
                )
                return f"{dev.label()}色温已设为 {kelvin}K。"

            if action in ("temperature", "温度"):
                self.ha.call_service(
                    "climate", "set_temperature", {"entity_id": dev.entity_id, "temperature": float(value or 26)}
                )
                return f"{dev.label()}已设为 {value}度。"

            return f"不支持的动作：{action}。可用：on / off / toggle / brightness / brightness_delta / color_temp。"

        except HAError as e:
            return f"操作失败：{e}"

    def query(self, device: str) -> str:
        dev = self.resolve(device)
        if dev is None:
            return f"没找到叫「{device}」的设备。"
        st = self.ha.state(dev.entity_id)
        if st is None:
            return f"{dev.label()}在 Home Assistant 里不存在（可能被删了或改了名）。"
        state = st.get("state")
        attrs = st.get("attributes", {})
        if state == "unavailable":
            return f"{dev.label()}现在离线，可能是断电或者被墙上的开关关了。"
        if dev.type == "light":
            if state != "on":
                return f"{dev.label()}现在是关着的。"
            parts = ["开着"]
            if attrs.get("brightness") is not None:
                parts.append(f"亮度 {round(attrs['brightness'] / 255 * 100)}%")
            if attrs.get("color_temp_kelvin"):
                parts.append(f"{attrs['color_temp_kelvin']}K")
            return f"{dev.label()}现在{'，'.join(parts)}。"
        return f"{dev.label()}现在状态：{state}。"

    # ---------------------------------------------------------------- 播报

    @staticmethod
    def _device_frag(entity_id: str) -> str:
        """同一台设备的实体共享前缀：media_player.xiaomi_cn_100000006_oh2p → xiaomi_cn_100000006_oh2p。"""
        obj = entity_id.split(".", 1)[-1]
        return "_".join(obj.split("_")[:4])

    def _find_speakers(self) -> list[tuple[str, str]]:
        """所有能"念文本"的音箱：[(notify_entity_id, 显示名)]。"""
        out = []
        for st in self.ha.states():
            eid = st["entity_id"]
            if eid.startswith("notify.") and "play_text" in eid:
                out.append((eid, str(st["attributes"].get("friendly_name", eid))))
        return out

    def _speaker_notify(self, target: str = "") -> tuple[str, str]:
        """找可播报的 notify 实体（米家音箱的『播放文本』），返回 (entity_id, 显示名)。

        优先级：显式指定 > 配置 ANNOUNCE_ENTITY > 别名表里的音箱（小爱音箱）> 第一个能念文本的音箱
        """
        speakers = self._find_speakers()

        if target:
            dev = self.resolve(target)
            if dev is None:
                return "", f"没找到叫「{target}」的设备"
            frag = self._device_frag(dev.entity_id)
            for eid, name in speakers:
                if frag and frag in eid:
                    return eid, name
            return "", f"{dev.label()}不支持文字播报"

        if self.settings.announce_entity:
            return self.settings.announce_entity, "音箱"

        # 别名表里的音箱（media_player）优先——通常就是家里的主力小爱
        for dev in self.devices.values():
            if dev.type != "media_player":
                continue
            frag = self._device_frag(dev.entity_id)
            for eid, name in speakers:
                if frag and frag in eid:
                    return eid, name

        return speakers[0] if speakers else ("", "")

    def _dnd_hint(self) -> str:
        """音箱在勿扰状态时提醒一句，否则用户会以为播报坏了。"""
        for st in self.ha.states():
            eid = st["entity_id"]
            if eid.startswith("switch.") and "no_disturb" in eid and st["state"] == "on":
                return "（音箱正在勿扰状态，可能听不到）"
        return ""

    def announce(self, text: str, target: str = "") -> str:
        """把一段话播报到音箱上（走米家云 TTS，约 1 秒延迟）。"""
        text = (text or "").strip()
        if not text:
            return "没有要播报的内容。"

        entity_id, name = self._speaker_notify(target)
        if not entity_id:
            return f"没有可用的播报音箱{('：' + name) if name else '（米家设备里找不到『播放文本』）'}。"

        chunks = split_for_speech(text, max(20, self.settings.announce_max_chars))
        if self.dry_run:
            return f"（dry-run）会通过「{name}」播报 {len(chunks)} 条：{text[:30]}"

        try:
            for i, chunk in enumerate(chunks):
                self.ha.notify(entity_id, chunk)
                if i < len(chunks) - 1:
                    time.sleep(0.8)  # 别把几条 TTS 挤在一起
        except HAError as e:
            return f"播报失败：{e}"

        hint = self._dnd_hint()
        short = text if len(text) <= 30 else text[:30] + "…"
        return f"已在「{name}」播报：{short}{hint}"

    def scene(self, name: str) -> str:
        try:
            scenes = [s for s in self.ha.states() if s["entity_id"].startswith("scene.")]
        except HAError as e:
            return f"查场景失败：{e}"
        hits = [s for s in scenes
                if name in s["entity_id"] or name in str(s.get("attributes", {}).get("friendly_name", ""))]
        if not hits:
            avail = "、".join(str(s.get("attributes", {}).get("friendly_name", s["entity_id"])) for s in scenes) or "无"
            return f"没有叫「{name}」的场景。现有场景：{avail}。"
        if len(hits) > 1:
            return f"「{name}」匹配到多个场景，说不清是哪个。"
        self.ha.call_service("scene", "turn_on", {"entity_id": hits[0]["entity_id"]})
        return f"已执行场景：{hits[0].get('attributes', {}).get('friendly_name', name)}。"

    def timer(self, minutes: float, label: str = "") -> str:
        minutes = float(minutes or 0)
        if minutes <= 0 or minutes > 600:
            return "定时的时长必须大于 0 分钟、不超过 600 分钟。"
        # 记忆开启时走持久化闹钟（重启可恢复）
        mem = self.memory
        if mem is not None and getattr(mem, "enabled", False):
            out = mem.schedule_timer(minutes, label=label or "")
            if out:
                return out
        secs = minutes * 60
        # 小于一分钟说"秒"更像人话（"0.0167 分钟"没人这么讲）
        if minutes < 1:
            spoken = f"{round(secs):g} 秒"
        else:
            spoken = f"{minutes:g} 分钟"
        text = f"{spoken}到了" + (f"，{label}" if label else "") + "。"

        def _fire() -> None:
            if self._announce:
                try:
                    self._announce(text)
                except Exception:  # noqa: BLE001
                    pass
            else:
                print(f"⏰ {text}")

        t = threading.Timer(secs, _fire)
        t.daemon = True
        t.start()
        self._timers = [x for x in self._timers if x.is_alive()] + [t]
        return f"好，{spoken}后提醒你。"

    def memory_tool(
        self,
        action: str,
        content: str = "",
        memory_id: str = "",
        slot: str = "",
        target: str = "",
    ) -> str:
        mem = self.memory
        if mem is None or not getattr(mem, "enabled", False):
            return "记忆功能未启用。"
        return mem.tool_memory(
            action,
            content=content or "",
            memory_id=memory_id or "",
            slot=slot or "",
            target=target or "",
        )

    def cancel_timers(self) -> None:
        for t in self._timers:
            t.cancel()
        self._timers.clear()
        mem = self.memory
        if mem is not None and getattr(mem, "enabled", False) and getattr(mem, "alarms", None):
            try:
                mem.alarms.cancel_all_pending()
            except Exception:  # noqa: BLE001
                pass

    # ---------------------------------------------------------------- 内部

    def _brightness_pct(self, entity_id: str) -> int:
        st = self.ha.state(entity_id) or {}
        b = st.get("attributes", {}).get("brightness")
        if b is None:
            return 100 if st.get("state") == "on" else 0
        return round(b / 255 * 100)

    # ---------------------------------------------------------------- 网页搜索

    def web_search(self, query: str) -> str:
        """SearXNG 搜索，返回带 result_id 的摘要 JSON。"""
        from .web_search import search as local_search

        q = (query or "").strip()
        if not q:
            return "搜索词为空。"
        return local_search(
            q,
            count=self.settings.web_search_count,
            timeout=self.settings.web_search_timeout,
            searxng_base_url=self.settings.searxng_base_url,
            cache_path=self.settings.local_web_search_cache,
            fetch_timeout=self.settings.web_fetch_timeout,
            fetch_max_chars=self.settings.web_fetch_max_chars,
        )

    def web_fetch(
        self,
        result_id: str = "",
        url: str = "",
        start: int = 0,
        max_chars: int | None = None,
    ) -> str:
        """按 result_id（或 url）取页面全文切片。"""
        from .web_search import fetch as local_fetch

        rid = (result_id or "").strip() or None
        u = (url or "").strip() or None
        try:
            start_i = int(start or 0)
        except (TypeError, ValueError):
            start_i = 0
        try:
            chars = int(max_chars) if max_chars is not None else None
        except (TypeError, ValueError):
            chars = None
        return local_fetch(
            result_id=rid,
            url=u,
            start=start_i,
            max_chars=chars,
            searxng_base_url=self.settings.searxng_base_url,
            search_timeout=self.settings.web_search_timeout,
            fetch_timeout=self.settings.web_fetch_timeout,
            fetch_max_chars=self.settings.web_fetch_max_chars,
            cache_path=self.settings.local_web_search_cache,
        )

    # ---------------------------------------------------------------- 本地时间

    def get_time(self) -> str:
        """本机本地时间（日期+星期+时分），返回可朗读短句。"""
        now = datetime.now()
        wd = f"星期{_WEEKDAYS[now.weekday()]}"
        h = now.hour
        if h < 6:
            period, h12 = "凌晨", h if h else 12
        elif h < 12:
            period, h12 = "上午", h if h else 12
        elif h == 12:
            period, h12 = "中午", 12
        elif h < 18:
            period, h12 = "下午", h - 12
        else:
            period, h12 = "晚上", h - 12
        minute = f"{now.minute:02d}"
        return (
            f"现在是{now.year}年{now.month}月{now.day}日{wd}，"
            f"{period}{h12}点{minute}分。"
        )

    # ---------------------------------------------------------------- 天气

    def weather(self, city: str = "") -> str:
        """中国气象局实况+近几日预报，返回可朗读短句。"""
        from .weather import lookup

        c = (city or "").strip() or (self.settings.weather_default_city or "").strip()
        if not c:
            return "请告诉我要查哪个城市。"
        return lookup(c, timeout=self.settings.weather_timeout)

    def set_tts(self, backend: str, voice: str = "", sid: float | None = None) -> str:
        """运行中切换 TTS 后端 / 音色。"""
        if self._tts is None or not hasattr(self._tts, "switch"):
            return "TTS 切换未就绪。"
        kwargs: dict[str, Any] = {}
        if voice:
            kwargs["voice"] = voice
        if sid is not None:
            kwargs["sid"] = int(sid)
        return str(self._tts.switch(backend, **kwargs))

    # ---------------------------------------------------------------- 给 LLM 的 schema

    def schemas(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [
            {
                "type": "function",
                "function": {
                    "name": "control",
                    "description": "开关或调节家电。设备用中文名，例如「客厅灯」「卧室灯」。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "device": {"type": "string", "description": "设备中文名"},
                            "action": {
                                "type": "string",
                                "enum": ["on", "off", "toggle", "brightness", "brightness_delta", "color_temp", "temperature"],
                                "description": "动作；brightness_delta 用于「再亮一点/暗一点」这类相对调整",
                            },
                            "value": {
                                "type": "number",
                                "description": "brightness:0-100；brightness_delta:正负百分比；color_temp:开尔文；temperature:摄氏度",
                            },
                        },
                        "required": ["device", "action"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "query",
                    "description": "查询设备当前状态。用户问「灯开着吗」「空调多少度」时用。",
                    "parameters": {
                        "type": "object",
                        "properties": {"device": {"type": "string", "description": "设备中文名"}},
                        "required": ["device"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "announce",
                    "description": (
                        "让家里的音箱（小爱）念一段话，用于全屋广播、提醒别人、或用户明确要求"
                        "『让音箱说/广播/跟客厅说』。普通回答不要用它——你自己的声音用户已经听到了。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string", "description": "要念的内容，口语短句，不要超过 80 字"},
                            "target": {"type": "string", "description": "指定哪个音箱（设备中文名），一般留空"},
                        },
                        "required": ["text"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "scene",
                    "description": "执行预设场景，例如「回家」「睡觉」「观影」。",
                    "parameters": {
                        "type": "object",
                        "properties": {"name": {"type": "string", "description": "场景名"}},
                        "required": ["name"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "timer",
                    "description": "设置定时提醒。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "minutes": {"type": "number", "description": "多少分钟后提醒"},
                            "label": {"type": "string", "description": "提醒内容，例如「关火」「吃药」"},
                        },
                        "required": ["minutes"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_time",
                    "description": (
                        "查询本机本地时间：现在几点、今天几号、星期几。"
                        "用户问时间/日期/星期时必须调用；不要凭记忆猜，也不要用 web_search。"
                        "问商店营业时间、航班时刻等外部时间表请用 web_search，不要用本工具。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "set_tts",
                    "description": (
                        "切换自己的说话声音/TTS 后端。"
                        "用户说「换成云端音色」「切回本地」「用 nano」「换成 say」时必须调用；"
                        "不要只口头答应。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "backend": {
                                "type": "string",
                                "enum": ["neural", "nano", "say", "qwen"],
                                "description": "neural=本地VITS；nano=侧车；say=系统；qwen=千问云端",
                            },
                            "voice": {
                                "type": "string",
                                "description": "可选：say 音色名，或 qwen 音色如 longanyang",
                            },
                            "sid": {
                                "type": "number",
                                "description": "可选：neural 多说话人编号（0 起）",
                            },
                        },
                        "required": ["backend"],
                    },
                },
            },
        ]
        if getattr(self.settings, "memory_enabled", False):
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": "memory",
                        "description": (
                            "管理分层记忆：remember 记住事实/偏好/规则；forget 忘掉（用户说「忘掉…」时用）；"
                            "list 列出；correct 纠正冲突事实；recall_alarms 查看待触发提醒。"
                            "擦除多条或清空全部时，工具会要求用户口头确认。"
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["remember", "forget", "list", "correct", "recall_alarms"],
                                    "description": "动作",
                                },
                                "content": {"type": "string", "description": "要记住/纠正的内容，或确认语"},
                                "memory_id": {"type": "string", "description": "记忆 id，形如 m_xxxx"},
                                "slot": {"type": "string", "description": "槽位，如 food.苹果 / name"},
                                "target": {"type": "string", "description": "forget 时的自然语言目标"},
                            },
                            "required": ["action"],
                        },
                    },
                }
            )
        if self.settings.weather_enabled:
            default_city = (self.settings.weather_default_city or "成都").strip()
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": "weather",
                        "description": (
                            "查中国城市天气（中国气象局）：实况气温、风力湿度和近几日预报。"
                            f"用户没说城市时用「{default_city}」。不要用 web_search/web_fetch 查天气。"
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "city": {
                                    "type": "string",
                                    "description": f"城市或区县名，例如「成都」「温江」；默认{default_city}",
                                },
                            },
                            "required": [],
                        },
                    },
                }
            )
        if self.settings.web_search_enabled:
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "description": (
                            "联网搜索新闻、营业时间、旅游攻略、价格行情等需要实时信息的问题。"
                            "只返回摘要和 result_id；依赖页面细节前再调 web_fetch。"
                            "查天气请用 weather，不要用本工具。query 用完整中文问句。"
                            "家电控制请用 control/query。"
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {
                                    "type": "string",
                                    "description": "完整中文搜索问句，例如「青城山门票多少钱」",
                                },
                            },
                            "required": ["query"],
                        },
                    },
                }
            )
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": "web_fetch",
                        "description": (
                            "根据 web_search 返回的 result_id 抓取页面全文切片。"
                            "在依赖票价、时刻表、长文要点等摘要里没有的细节之前必须调用。"
                            "优先传 result_id；没有 id 时才直接传 url。"
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "result_id": {
                                    "type": "string",
                                    "description": "web_search 结果里的 result_id，例如 res_...",
                                },
                                "url": {
                                    "type": "string",
                                    "description": "无 result_id 时才用的页面 URL",
                                },
                                "start": {
                                    "type": "integer",
                                    "description": "正文起始字符偏移，默认 0",
                                },
                                "max_chars": {
                                    "type": "integer",
                                    "description": "本次最多返回多少字；默认用系统配置",
                                },
                            },
                            "required": [],
                        },
                    },
                }
            )
        return out

    def dispatch(self, name: str, args: dict[str, Any]) -> str:
        fn = {
            "control": self.control,
            "query": self.query,
            "announce": self.announce,
            "scene": self.scene,
            "timer": self.timer,
            "get_time": self.get_time,
            "memory": self.memory_tool,
            "web_search": self.web_search,
            "web_fetch": self.web_fetch,
            "weather": self.weather,
            "set_tts": self.set_tts,
        }.get(name)
        if fn is None:
            return f"没有这个工具：{name}"
        if name in ("web_search", "web_fetch") and not self.settings.web_search_enabled:
            return "网页搜索未启用。"
        if name == "weather" and not self.settings.weather_enabled:
            return "天气查询未启用。"
        if name == "memory" and not getattr(self.settings, "memory_enabled", False):
            return "记忆功能未启用。"
        try:
            return str(fn(**args))
        except TypeError as e:
            return f"工具参数不对：{e}"
        except Exception as e:  # noqa: BLE001
            return f"工具执行异常：{e}"
