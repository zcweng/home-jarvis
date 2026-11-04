#!/usr/bin/env python3
"""Agents SDK 运行时离线回归：不需要真 key、不需要 HA、不需要音频设备。

覆盖 SDK 迁移里最容易回归的几条：

  ① 异常归一化：上游 5xx / 超时 / 工具名不存在 / 打满轮次 都不能把主循环打死
  ② 不重复执行：thinking 降级重试只在「还没跑过工具」时发生
  ③ 指代不丢：工具参数不合法时 session.last 不能被清空
  ④ FakeLLM 选设备：按真实设备名最长匹配，失败时不能回「好，打开了。」
  ⑤ MCP 多轮：同一个 MCP server 连续多句都能调到（需本机有 python，无需网络）

    .venv/bin/python scripts/selftest_agent_runtime.py
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from jarvis.agent_runtime import JarvisAgent  # noqa: E402
from jarvis.config import Settings  # noqa: E402
from jarvis.llm_fake import FakeLLM  # noqa: E402
from jarvis.session import Session  # noqa: E402
from jarvis.tools import Device, Tools  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  —— {detail}" if detail else ""))


class FakeHA:
    """不碰真 HA：状态固定、服务调用记账。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def state(self, entity_id: str) -> dict[str, Any]:
        return {"state": "off", "attributes": {"brightness": 128}}

    def call_service(self, domain: str, service: str, data: dict[str, Any] | None = None) -> None:
        self.calls.append((domain, service))

    def states(self) -> list[dict[str, Any]]:
        return []


class MockProvider:
    """可编排的 OpenAI 兼容 /chat/completions 假服务。"""

    def __init__(self, handler) -> None:
        self.requests: list[dict[str, Any]] = []
        self._handler = handler
        outer = self

        class _H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: ANN002
                pass

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                outer.requests.append(body)
                status, msg = outer._handler(body, len(outer.requests))
                if status != 200:
                    data = json.dumps({"error": {"message": msg, "type": "invalid_request_error"}}).encode()
                    self.send_response(status)
                else:
                    data = json.dumps(
                        {
                            "id": "x",
                            "object": "chat.completion",
                            "created": 0,
                            "model": body.get("model"),
                            "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                        },
                        ensure_ascii=False,
                    ).encode()
                    self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._srv = HTTPServer(("127.0.0.1", 0), _H)
        self.url = f"http://127.0.0.1:{self._srv.server_port}/v1"
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._srv.shutdown()


def _call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            }
        ],
    }


def _has_tool_result(body: dict[str, Any]) -> bool:
    return any(m.get("role") == "tool" for m in body.get("messages") or [])


def make_agent(url: str, *, mcp: str = "", provider: str = "openai_compatible", timeout: float = 10.0):
    st = Settings.load()
    st = Settings(
        **{
            **st.__dict__,
            "llm_base_url": url,
            "llm_api_key": "test-key-0123456789",
            "llm_model": "mock",
            "llm_timeout": timeout,
            "llm_provider": provider,
            "mcp_servers": mcp,
            "web_search_enabled": False,
            "weather_enabled": False,
        }
    )
    ha = FakeHA()
    tools = Tools(ha, settings=st, announce=lambda _t: None)
    tools.devices["客厅灯"] = Device(entity_id="light.keting", name="客厅灯", room="客厅", aliases=[], type="light")
    tools.devices["吸顶灯2"] = Device(entity_id="light.ceil2", name="吸顶灯2", room="客厅", aliases=[], type="light")
    session = Session.load(tools.catalog())
    return JarvisAgent(st, tools, session=session), tools, session, ha


# --------------------------------------------------------------------------- ① 异常归一化


def test_error_mapping() -> None:
    print("\n① 异常归一化")

    # 上游 502 → RuntimeError，且文案能被 app 识别为「服务异常」
    def five_xx(_body, _n):
        return 502, "upstream exploded"

    p = MockProvider(five_xx)
    agent, *_ = make_agent(p.url)
    try:
        agent.chat([{"role": "user", "content": "hi"}])
        check("5xx 抛出异常", False, "没抛")
    except RuntimeError as e:
        from jarvis.app import _is_transient_llm_error

        msg = str(e)
        check("5xx 归一化成 RuntimeError", "HTTP 502" in msg, msg[:60])
        check("5xx 被识别为可播报的网络故障", _is_transient_llm_error(e, msg))
    except BaseException as e:  # noqa: BLE001
        check("5xx 归一化成 RuntimeError", False, f"{type(e).__name__}: {e}")
    finally:
        agent.close()
        p.close()

    # 连不上 → httpx.TransportError（app 的既有分支能接住）
    agent, *_ = make_agent("http://127.0.0.1:1/v1")
    try:
        agent.chat([{"role": "user", "content": "hi"}])
        check("连接失败抛异常", False, "没抛")
    except httpx.TransportError:
        check("连接失败归一化成 TransportError", True)
    except BaseException as e:  # noqa: BLE001
        check("连接失败归一化成 TransportError", False, f"{type(e).__name__}: {e}")
    finally:
        agent.close()

    # 模型编造工具名 → 不炸，且不留空回复
    def bogus(_body, _n):
        return 200, _call("light_control", {"device": "客厅灯"})

    p = MockProvider(bogus)
    agent, *_ = make_agent(p.url)
    try:
        res = agent.chat([{"role": "user", "content": "开灯"}])
        check("编造工具名不抛异常", True, f"text={res.text[:40]!r}")
    except BaseException as e:  # noqa: BLE001
        check("编造工具名不抛异常", False, f"{type(e).__name__}: {e}")
    finally:
        agent.close()
        p.close()

    # 打满轮次 → 用已有工具结果兜底，而不是抛 MaxTurnsExceeded
    def always_tool(_body, _n):
        return 200, _call("query", {"device": "客厅灯"})

    p = MockProvider(always_tool)
    agent, *_ = make_agent(p.url)
    try:
        res = agent.chat([{"role": "user", "content": "查"}], max_rounds=2)
        check("打满轮次不抛异常", True, f"text={res.text[:40]!r}")
        check("打满轮次有工具结果", bool(res.tool_results), str(res.tool_results[:1]))
    except BaseException as e:  # noqa: BLE001
        check("打满轮次不抛异常", False, f"{type(e).__name__}: {e}")
    finally:
        agent.close()
        p.close()


# --------------------------------------------------------------------------- ② 不重复执行


def test_no_duplicate_tool_exec() -> None:
    print("\n② thinking 降级不重复执行工具")

    def handler(body, n):
        if n == 2:
            return 400, "thinking is not supported"
        if n == 1:
            return 200, _call("timer", {"minutes": 1, "label": "关火"})
        return 200, {"role": "assistant", "content": "好的。"}

    p = MockProvider(handler)
    agent, tools, *_ = make_agent(p.url, provider="deepseek")
    fired: list[tuple] = []
    tools.timer = lambda minutes, label="": (fired.append((minutes, label)), "已设定")[1]
    try:
        res = agent.chat([{"role": "user", "content": "一分钟提醒我关火"}])
        check("已经跑过工具后不再降级重试", len(fired) == 1, f"timer 执行 {len(fired)} 次")
        check("重试不产生重复工具调用", len(res.tool_calls) == 1, str([c.name for c in res.tool_calls]))
        check("已执行后用已有结果收尾", bool(res.text) and "running the tool" not in res.text, res.text[:40])
    except BaseException as e:  # noqa: BLE001
        check("已经跑过工具后不再降级重试", False, f"{type(e).__name__}: {e}")
    finally:
        agent.close()
        p.close()

    # 一轮都没跑过工具时，降级重试应当发生
    def handler2(body, n):
        if n == 1:
            return 400, "thinking is not supported"
        return 200, {"role": "assistant", "content": "在。"}

    p = MockProvider(handler2)
    agent, *_ = make_agent(p.url, provider="deepseek")
    try:
        res = agent.chat([{"role": "user", "content": "hi"}])
        check(
            "未跑工具时允许降级重试",
            res.text == "在。" and len(p.requests) == 2,
            f"requests={len(p.requests)} text={res.text!r}",
        )
        check(
            "重试后不再带 thinking 字段", "thinking" not in (p.requests[-1] or {}), str(sorted(p.requests[-1].keys()))
        )
    except BaseException as e:  # noqa: BLE001
        check("未跑工具时允许降级重试", False, f"{type(e).__name__}: {e}")
    finally:
        agent.close()
        p.close()


# --------------------------------------------------------------------------- ③ 指代不丢


def test_reference_not_clobbered() -> None:
    print("\n③ 工具失败不覆盖「最近一次操作」")

    def handler(body, n):
        if _has_tool_result(body):
            return 200, {"role": "assistant", "content": "好了"}
        args = '{"device": "客厅灯", "action": "on"}' if n == 1 else '{"device": "客厅灯", "action": }'
        return 200, {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": f"c{n}", "type": "function", "function": {"name": "control", "arguments": args}}],
        }

    p = MockProvider(handler)
    agent, tools, session, ha = make_agent(p.url)
    try:
        agent.chat(session.messages("把客厅灯打开"))
        first = session.last.device
        agent.chat(session.messages("再开一次"))
        check("首次操作被记住", first == "客厅灯", first)
        check("坏参数不覆盖上一次操作", session.last.device == first, f"device={session.last.device!r}")
        check("坏参数时也不真的调用 HA", len(ha.calls) == 1, str(ha.calls))
    except BaseException as e:  # noqa: BLE001
        check("坏参数不覆盖上一次操作", False, f"{type(e).__name__}: {e}")
    finally:
        agent.close()
        p.close()


# --------------------------------------------------------------------------- ④ FakeLLM 选设备


def test_fake_llm_device_pick() -> None:
    print("\n④ FakeLLM 设备名匹配")

    st = Settings.load()
    tools = Tools(FakeHA(), settings=st, announce=lambda _t: None)
    for name in ("吸顶灯", "吸顶灯2", "晾衣架灯", "客厅灯"):
        tools.devices[name] = Device(entity_id=f"light.{name}", name=name, room="客厅", aliases=[], type="light")
    names = list(tools.devices)

    def run(user: str) -> tuple[str, str, list[tuple[str, str]]]:
        ha = FakeHA()
        tools2 = Tools(ha, settings=st, announce=lambda _t: None)
        for name in names:
            tools2.devices[name] = Device(entity_id=f"light.{name}", name=name, room="客厅", aliases=[], type="light")
        llm = FakeLLM(devices=list(tools2.devices))
        session = Session.load(tools2.catalog())
        llm.chat(session.messages(user), dispatch=tools2.dispatch)
        call_dev = ""
        llm2 = FakeLLM(devices=list(tools2.devices))
        res = llm2.chat(session.messages(user), dispatch=tools2.dispatch)
        if res.tool_calls:
            call_dev = str(res.tool_calls[0].arguments.get("device", ""))
        return call_dev, res.text, ha.calls

    for user, expect in (
        ("把吸顶灯2打开", "吸顶灯2"),
        ("把晾衣架灯打开", "晾衣架灯"),
        ("把客厅灯打开", "客厅灯"),
    ):
        dev, text, calls = run(user)
        check(f"选设备：{user}", dev == expect, f"picked={dev!r}")
        check(f"真调了设备：{user}", bool(calls), str(calls))
        check(f"回复不是硬编码成功：{user}", "好，打开了" not in text or bool(calls), text[:40])

    dev, text, calls = run("把火星灯打开")
    check(
        "不存在的设备如实回答",
        not calls and any(m in text for m in ("没找到", "指代不清")),
        f"text={text[:40]!r} calls={calls}",
    )


# --------------------------------------------------------------------------- ⑤ MCP 多轮


def test_mcp_multi_turn() -> None:
    print("\n⑤ MCP 连续多轮可用")

    server_py = ROOT / "debug" / "_mcp_echo_probe.py"
    server_py.parent.mkdir(parents=True, exist_ok=True)
    server_py.write_text(
        "from mcp.server.mcpserver import MCPServer\n"
        "import anyio\n"
        "s = MCPServer('echo')\n"
        "@s.tool()\n"
        "def echo(text: str) -> str:\n"
        "    '''回显。'''\n"
        "    return f'ECHO:{text}'\n"
        "anyio.run(s.run_stdio_async)\n",
        encoding="utf-8",
    )
    cfg = json.dumps(
        [{"type": "stdio", "name": "echo", "command": sys.executable, "args": [str(server_py)]}],
        ensure_ascii=False,
    )

    def handler(body, _n):
        if not _has_tool_result(body):
            return 200, _call("echo", {"text": "嗨"})
        last = [m for m in body.get("messages") or [] if m.get("role") == "tool"][-1]
        return 200, {"role": "assistant", "content": f"工具说：{last.get('content')}"}

    p = MockProvider(handler)
    agent, *_ = make_agent(p.url, mcp=cfg, timeout=20.0)
    try:
        outs = []
        for i in range(3):
            res = agent.chat([{"role": "user", "content": f"第 {i + 1} 次回显"}])
            outs.append(res.text)
        ok = all("ECHO:嗨" in o for o in outs)
        check("MCP 连续 3 轮都能调到", ok, " | ".join(o[:36] for o in outs))
        check("MCP 失败时不吞成「运行出错」", all("running the tool" not in o for o in outs), str(outs))
    except BaseException as e:  # noqa: BLE001
        check("MCP 连续 3 轮都能调到", False, f"{type(e).__name__}: {e}")
    finally:
        agent.close()
        p.close()
        server_py.unlink(missing_ok=True)


def main() -> int:
    test_error_mapping()
    test_no_duplicate_tool_exec()
    test_reference_not_clobbered()
    test_fake_llm_device_pick()
    test_mcp_multi_turn()

    print("-" * 60)
    bad = [n for n, ok, _ in results if not ok]
    if bad:
        print(f"失败 {len(bad)} 项：{'、'.join(bad)}")
        return 1
    print(f"全部通过（{len(results)} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
