"""OpenAI Agents SDK 运行时：可切换 OpenAI 兼容 LLM + 本地 tools + 可选 MCP。

两个必须知道的运行方式约束（本机实测踩过）：

1. **MCP 与 Runner 必须跑在同一个事件循环里**
   `MCPServerStdio.connect()` 会在当前 task 里进入 anyio cancel scope；
   而 `Runner.run_sync` 每次都用 `create_task` 在新 task 里跑。
   两者混用会在第二句就炸：
   `RuntimeError: Attempted to exit cancel scope in a different task`。
   所以这里用一个常驻后台事件循环线程，connect 和每次 run 都在它上面执行。

2. **异常要归一化回 app 认识的那几种**
   app.handle_text 只识别 `httpx.TimeoutException` / `httpx.TransportError` /
   `RuntimeError("... HTTP 5xx ...")`。SDK 抛的是 `agents.*` / `openai.*`，
   直接冒出去会把语音主循环打死。这里统一翻译成上面的类型。
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Callable

import httpx
from agents import (
    Agent,
    ModelSettings,
    OpenAIChatCompletionsModel,
    RunHooks,
    Runner,
    set_tracing_disabled,
)
from agents.exceptions import (
    AgentsException,
    MaxTurnsExceeded,
    ModelBehaviorError,
    ModelTimeoutError,
)
from agents.items import ToolCallItem, ToolCallOutputItem
from agents.mcp import MCPServerStdio, MCPServerStreamableHttp
from agents.tool_context import ToolContext
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from .agent_tools import ToolBag, build_function_tools
from .config import Settings
from .llm import LLMResult, ToolCall
from .session import Session
from .tools import Tools

set_tracing_disabled(True)

DEFAULT_INSTRUCTIONS = "你叫贾维斯，是家里的语音管家，回答简短口语化。"
MCP_CONNECT_TIMEOUT = 15.0

# 失败信号：假模型/工具返回这些时不能回「好，打开了。」
_FAIL_MARKS = ("没找到", "指代不清", "不在线", "参数不对", "失败", "未启用")


def _warn(msg: str) -> None:
    print(f"  ⚠️  {msg}", file=sys.stderr)


class _LoopThread:
    """常驻后台事件循环：MCP 连接与所有 Runner.run 都在同一个 loop 上。"""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="jarvis-agent-loop", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def submit(self, coro: Any, timeout: float | None = None) -> Any:
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout)
        except FutureTimeout:
            fut.cancel()
            raise
        except BaseException:
            fut.cancel()
            raise

    def stop(self) -> None:
        if self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=3)


def _parse_mcp_servers(raw: str) -> list[dict[str, Any]]:
    text = (raw or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        _warn(f"MCP_SERVERS 不是合法 JSON，已忽略：{e}")
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        _warn("MCP_SERVERS 必须是 JSON 数组或对象，已忽略")
        return []
    out: list[dict[str, Any]] = []
    for i, x in enumerate(data):
        if not isinstance(x, dict):
            _warn(f"MCP_SERVERS 第 {i + 1} 项不是对象，已跳过")
            continue
        out.append(x)
    return out


def _build_mcp_servers(cfgs: list[dict[str, Any]]) -> list[Any]:
    servers: list[Any] = []
    for i, cfg in enumerate(cfgs):
        kind = str(cfg.get("type") or cfg.get("transport") or "stdio").lower()
        name = cfg.get("name")
        if kind in ("stdio", "std"):
            command = cfg.get("command")
            if not command:
                _warn(f"MCP 第 {i + 1} 项是 stdio 但缺 command，已跳过")
                continue
            params: dict[str, Any] = {
                "command": str(command),
                "args": list(cfg.get("args") or []),
            }
            if cfg.get("env"):
                params["env"] = dict(cfg["env"])
            if cfg.get("cwd"):
                params["cwd"] = cfg["cwd"]
            servers.append(MCPServerStdio(params=params, name=name, cache_tools_list=True))
        elif kind in ("http", "streamable_http", "streamable-http"):
            url = cfg.get("url")
            if not url:
                _warn(f"MCP 第 {i + 1} 项是 http 但缺 url，已跳过")
                continue
            params = {"url": str(url)}
            if cfg.get("headers"):
                params["headers"] = dict(cfg["headers"])
            servers.append(MCPServerStreamableHttp(params=params, name=name, cache_tools_list=True))
        else:
            _warn(f"MCP 第 {i + 1} 项 type={kind!r} 不支持（只认 stdio / http），已跳过")
    return servers


class _EarlyAckHooks(RunHooks[Any]):
    """工具刚决定时触发首句先出声；并收集 ToolCall 列表。"""

    def __init__(
        self,
        on_tool_calls: Callable[[list[ToolCall]], None] | None,
        collected: list[ToolCall],
    ) -> None:
        self._on = on_tool_calls
        self.collected = collected
        self._acked = False

    async def on_tool_start(self, context: Any, agent: Agent[Any], tool: Any) -> None:
        name = getattr(tool, "name", "") or ""
        call_id = ""
        args: dict[str, Any] = {}
        if isinstance(context, ToolContext):
            name = context.tool_name or name
            call_id = context.tool_call_id or ""
            raw = context.tool_arguments or "{}"
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else dict(raw)
                if isinstance(parsed, dict):
                    args = parsed
            except (json.JSONDecodeError, TypeError):
                args = {}
        tc = ToolCall(id=call_id or f"call_{len(self.collected)}", name=name, arguments=args)
        self.collected.append(tc)
        if self._acked or self._on is None:
            return
        self._acked = True
        try:
            self._on([tc])
        except Exception:  # noqa: BLE001
            pass


class JarvisAgent:
    """Agents SDK 封装，对外保持近似原 DeepSeek.chat 契约。"""

    def __init__(
        self,
        settings: Settings | None = None,
        tools: Tools | None = None,
        session: Session | None = None,
        timeout: float | None = None,
    ) -> None:
        st = settings or Settings.load()
        api_key = (st.llm_api_key or st.deepseek_key or "").strip()
        if not api_key:
            raise SystemExit("缺少 LLM_API_KEY（或 DEEPSEEK_KEY），填到 .env 里")
        if tools is None:
            raise SystemExit("JarvisAgent 需要 Tools 实例")

        self.settings = st
        self.model_name = st.llm_model
        self.base_url = st.llm_base_url.rstrip("/")
        self.timeout = float(st.llm_timeout if timeout is None else timeout)
        self._key = api_key
        self._provider = (st.llm_provider or "").strip().lower()
        if not self._provider:
            self._provider = "deepseek" if "deepseek" in self.base_url.lower() else "openai_compatible"

        self._tool_search = bool(getattr(st, "tool_search_enabled", False))
        self._bag = ToolBag(
            tools=tools,
            session=session,
            tool_search_limit=int(getattr(st, "tool_search_limit", 5) or 5),
        )
        self._refresh_local_catalog()
        self._function_tools = build_function_tools(self._bag)
        self._mcp_servers = _build_mcp_servers(_parse_mcp_servers(st.mcp_servers))
        self._active_mcp: list[Any] = []
        self._mcp_connected = False

        self._client = AsyncOpenAI(
            base_url=self.base_url,
            api_key=self._key,
            timeout=self.timeout,
        )
        self._model = OpenAIChatCompletionsModel(
            model=self.model_name,
            openai_client=self._client,
        )
        self._disable_thinking = self._provider == "deepseek" or st.llm_disable_thinking
        self._agent = Agent(
            name="Jarvis",
            instructions=DEFAULT_INSTRUCTIONS,
            model=self._model,
            tools=list(self._function_tools),
            mcp_servers=[],
            model_settings=self._model_settings(),
        )
        self._loop = _LoopThread()

    def _refresh_local_catalog(self) -> None:
        """把 defer 本地工具写入目录（MCP 条目在 connect 后追加）。"""
        if not self._tool_search:
            return
        self._bag.catalog.remove_source("local")
        self._bag.catalog.add_local_schemas(self._bag.tools.deferred_local_schemas(), defer=True)

    def bind_session(self, session: Session) -> None:
        self._bag.session = session

    def key_hint(self) -> str:
        k = self._key or ""
        if len(k) <= 8:
            return "***"
        return f"{k[:4]}…{k[-4:]}（len={len(k)}）"

    def call_hint(self) -> str:
        return (
            f"model={self.model_name} base={self.base_url} provider={self._provider} "
            f"timeout={self.timeout:g}s key={self.key_hint()} "
            f"mcp={len(self._active_mcp)}/{len(self._mcp_servers)}"
        )

    def _model_settings(
        self,
        *,
        temperature: float = 0.3,
        force_tool: str | None = None,
    ) -> ModelSettings:
        kwargs: dict[str, Any] = {
            "temperature": temperature,
            "tool_choice": force_tool or "auto",
            "timeout": self.timeout,
        }
        if self._disable_thinking:
            # DeepSeek 默认开 thinking，思考模式下 tool_choice 不可用
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        return ModelSettings(**kwargs)

    # ------------------------------------------------------------------ MCP

    def _ensure_mcp(self) -> None:
        """连接 MCP；单个失败只降级，不让整个管家起不来。

        tool_search 开启时：MCP 只作上游执行后端，工具进目录不挂到 Agent。
        关闭时：仍把 MCP 全量挂到 Agent（旧行为）。
        """
        if self._mcp_connected or not self._mcp_servers:
            return
        self._mcp_connected = True

        async def _connect_all() -> list[Any]:
            active: list[Any] = []
            for s in self._mcp_servers:
                try:
                    await s.connect()
                    active.append(s)
                except Exception as e:  # noqa: BLE001
                    _warn(f"MCP「{s.name}」连接失败，已跳过：{e}")
            return active

        try:
            self._active_mcp = self._loop.submit(_connect_all(), timeout=MCP_CONNECT_TIMEOUT)
        except FutureTimeout:
            _warn(f"MCP 连接超时（>{MCP_CONNECT_TIMEOUT:g}s），本轮不挂 MCP")
            self._active_mcp = []
        except Exception as e:  # noqa: BLE001
            _warn(f"MCP 连接异常，本轮不挂 MCP：{e}")
            self._active_mcp = []
        self._bag.mcp_servers = list(self._active_mcp)
        if self._tool_search and self._active_mcp:
            try:
                self._loop.submit(self._index_mcp_tools(), timeout=MCP_CONNECT_TIMEOUT)
            except Exception as e:  # noqa: BLE001
                _warn(f"MCP 工具索引入目录失败：{e}")
        mode = "目录检索" if self._tool_search else "全量挂载"
        names = "、".join(str(s.name) for s in self._active_mcp) or "无"
        print(
            f"  🔌 MCP：{len(self._active_mcp)}/{len(self._mcp_servers)} 已连接（{names}，{mode}）",
            file=sys.stderr,
        )

    async def _index_mcp_tools(self) -> None:
        for s in self._active_mcp:
            try:
                tools = await s.list_tools()
                self._bag.catalog.add_mcp_tools(str(s.name), tools)
            except Exception as e:  # noqa: BLE001
                _warn(f"MCP「{s.name}」list_tools 失败：{e}")

    async def _cleanup_mcp(self) -> None:
        for s in self._active_mcp:
            try:
                await s.cleanup()
            except Exception:  # noqa: BLE001
                pass
        self._active_mcp = []

    # ------------------------------------------------------------------ 结果组装

    @staticmethod
    def _messages_to_input(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
        """拆出 system → instructions；其余转为 Runner input。"""
        instructions = DEFAULT_INSTRUCTIONS
        items: list[dict[str, Any]] = []
        for m in messages:
            role = m.get("role")
            content = m.get("content") or ""
            if role == "system":
                instructions = str(content)
                continue
            if role in ("user", "assistant") and content is not None:
                items.append({"role": role, "content": str(content)})
        if not items:
            items = [{"role": "user", "content": ""}]
        return instructions, items

    def _fallback_result(self, t0: float, raw_error: str) -> LLMResult:
        """打满轮次 / 模型行为异常时，用已有工具结果兜底，别让整句没声。"""
        results = list(self._bag.last_results)
        text = results[-1][1] if results else ""
        return LLMResult(
            text=text,
            tool_calls=list(self._bag.collected_calls),
            tool_results=results,
            latency_ms=(time.perf_counter() - t0) * 1000,
            rounds=0,
            raw_error=raw_error,
        )

    @staticmethod
    def _result_from_run(
        run_result: Any,
        collected_calls: list[ToolCall],
        bag_results: list[tuple[str, str]],
        t0: float,
    ) -> LLMResult:
        text = ""
        final = run_result.final_output
        if isinstance(final, str):
            text = final.strip()
        elif final is not None:
            text = str(final).strip()

        # 若 hook 未收到调用（例如 MCP），从 new_items 补齐
        calls = list(collected_calls)
        if not calls:
            for item in run_result.new_items or []:
                if isinstance(item, ToolCallItem):
                    raw = item.raw_item
                    name = getattr(raw, "name", None) or getattr(item, "name", "") or ""
                    call_id = getattr(raw, "call_id", None) or getattr(raw, "id", None) or ""
                    args_raw = getattr(raw, "arguments", None) or "{}"
                    try:
                        args = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    if not isinstance(args, dict):
                        args = {}
                    calls.append(ToolCall(id=str(call_id) or f"call_{len(calls)}", name=str(name), arguments=args))

        results = list(bag_results)
        if not results:
            for item in run_result.new_items or []:
                if isinstance(item, ToolCallOutputItem):
                    out = item.output
                    results.append(("tool", str(out)))

        # 对齐名字：按顺序把空名填上
        if results and calls and all(n == "tool" or not n for n, _ in results):
            fixed: list[tuple[str, str]] = []
            for i, (_, out) in enumerate(results):
                n = calls[i].name if i < len(calls) else "tool"
                fixed.append((n, out))
            results = fixed

        rounds = len(getattr(run_result, "raw_responses", []) or []) or 1
        return LLMResult(
            text=text,
            tool_calls=calls,
            tool_results=results,
            latency_ms=(time.perf_counter() - t0) * 1000,
            rounds=int(rounds),
        )

    # ------------------------------------------------------------------ 调用

    def _total_timeout(self, max_turns: int) -> float:
        # 每轮一次请求，外加工具执行与重问余量
        return max(20.0, self.timeout * (max_turns + 1) + 5.0)

    async def _run_async(
        self,
        input_items: list[dict[str, Any]],
        instructions: str,
        temperature: float,
        force_tool: str | None,
        max_turns: int,
        hooks: _EarlyAckHooks,
        t0: float,
    ) -> LLMResult:
        # tool_search 开启时 MCP 不进 Agent 前缀；关闭时保持全量挂载
        mcp_for_agent = [] if self._tool_search else list(self._active_mcp)
        agent = self._agent.clone(
            instructions=instructions,
            model_settings=self._model_settings(temperature=temperature, force_tool=force_tool),
            mcp_servers=mcp_for_agent,
        )
        try:
            run_result = await Runner.run(
                agent,
                input_items,
                max_turns=max_turns,
                hooks=hooks,
            )
        except MaxTurnsExceeded as e:
            _warn(f"工具轮次打满（{max_turns} 轮），用已有结果兜底")
            return self._fallback_result(t0, str(e))
        except ModelBehaviorError as e:
            # 模型编了个不存在的工具名 / 参数 JSON 坏掉：说句兜底话，别让进程死
            _warn(f"模型输出不合法：{e}")
            return self._fallback_result(t0, str(e))
        except (APITimeoutError, ModelTimeoutError) as e:
            raise httpx.ReadTimeout(f"LLM 超时：{e}") from e
        except APIConnectionError as e:
            raise httpx.ConnectError(f"LLM 连接失败：{e}") from e
        except APIStatusError as e:
            raise RuntimeError(f"LLM HTTP {e.status_code}: {str(e)[:300]}") from e
        except AgentsException as e:
            raise RuntimeError(f"LLM 运行失败：{e}") from e
        return self._result_from_run(run_result, hooks.collected, self._bag.last_results, t0)

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,  # noqa: ARG002 — 兼容旧调用方
        dispatch=None,  # noqa: ARG002 — 真实路径走 FunctionTool；FakeLLM 仍用 dispatch
        max_rounds: int = 4,
        temperature: float = 0.3,
        force_tool: str | None = None,
        on_tool_calls: Callable[[list[ToolCall]], None] | None = None,
    ) -> LLMResult:
        t0 = time.perf_counter()
        self._bag.reset_turn()
        self._ensure_mcp()
        # 意图强制到已 defer 的本地工具时，改为强制 tool_search
        if self._tool_search and force_tool and force_tool in self._bag.tools.defer_local_names():
            force_tool = "tool_search"

        instructions, input_items = self._messages_to_input(messages)
        hooks = _EarlyAckHooks(on_tool_calls, self._bag.collected_calls)
        timeout = self._total_timeout(max_rounds)

        try:
            return self._loop.submit(
                self._run_async(input_items, instructions, temperature, force_tool, max_rounds, hooks, t0),
                timeout=timeout,
            )
        except FutureTimeout as e:
            raise httpx.ReadTimeout(f"LLM 整体超时（>{timeout:g}s）") from e
        except RuntimeError as e:
            has_side_effects = bool(hooks.collected or self._bag.last_results)
            # 只有「还没跑过任何工具」时才允许降级重试，否则会重复执行设备动作
            if self._disable_thinking and not has_side_effects and self._thinking_conflict(str(e)):
                _warn(f"网关不接受 thinking 字段，关掉后重试一次：{str(e)[:120]}")
                self._disable_thinking = False
                return self._loop.submit(
                    self._run_async(input_items, instructions, temperature, force_tool, max_rounds, hooks, t0),
                    timeout=timeout,
                )
            if has_side_effects and self._thinking_conflict(str(e)):
                # 已经动过设备了，重试等于再动一次：用已有结果收尾
                _warn(f"动作已执行，后续请求被网关拒绝（{str(e)[:80]}），用已有结果收尾")
                return self._fallback_result(t0, str(e))
            raise

    @staticmethod
    def _thinking_conflict(msg: str) -> bool:
        low = msg.lower()
        return "http 400" in low and ("thinking" in low or "tool_choice" in low)

    def close(self) -> None:
        try:
            if self._mcp_connected and self._active_mcp:
                self._loop.submit(self._cleanup_mcp(), timeout=10)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._loop.submit(self._client.close(), timeout=5)
        except Exception:  # noqa: BLE001
            pass
        self._loop.stop()
