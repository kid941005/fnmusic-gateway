# -*- coding: utf-8 -*-
"""MCP 服务层：基于官方 mcp>=2.3 的 MCPServer（原 FastMCP）。

设计目标：
- 工具注册、参数 Schema、参数校验、错误包装都交给官方 MCPServer；
- 对外仍然复用网关 8766 的 /mcp 路径，兼容现有小智接入点与桥接器；
- 官方 streamable-http 应用只监听 127.0.0.1 的随机端口，由 app.py 反向代理，
  因此不需要新增对外端口，也不破坏「只开 8766」的部署约束。
"""
from __future__ import annotations

import asyncio
import atexit
import http.client
import inspect
import logging
import threading
import time
from typing import Any, Callable

try:  # mcp>=2.3；缺失时网关仍能用旧 JSON-RPC 兜底，但 /healthz 会标记
    from mcp.server.mcpserver import Context, MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.server.mcpserver.tools.base import Tool

    MCP_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - 仅在旧环境/缺依赖时触发
    Context = None  # type: ignore[assignment]
    MCPServer = None  # type: ignore[assignment]
    Tool = None  # type: ignore[assignment]

    class ToolError(Exception):  # type: ignore[no-redef]
        """官方 ToolError 的占位实现，保证模块可导入。"""

    MCP_IMPORT_ERROR = exc

from .mcp_tools import build_tools

LOG = logging.getLogger("fnmusic-gateway.mcp")

_JSON_TYPES = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
}

DEFAULT_INSTRUCTIONS = (
    "这是音乐播放网关（飞牛音乐优先，在线音源兜底）。先用 music_search 找到歌，"
    "music_play 会返回签名直链；拿到直链后必须调用设备工具 self.music.play_url，"
    "并传入 url、title、artist、lyric_url、duration（秒，整数）五个参数。"
)


class McpService:
    """把网关的 13 个工具注册进官方 MCPServer 2.3。"""

    def __init__(self, call_tool: Callable[[str, dict, str], str], version: str,
                 default_base: str | Callable[[], str],
                 name: str = "fnmusic-gateway",
                 title: str = "音乐播放网关",
                 instructions: str = DEFAULT_INSTRUCTIONS,
                 logger: logging.Logger | None = None) -> None:
        self._call_tool = call_tool
        self._default_base = default_base
        self._log = logger or LOG
        self._schemas = build_tools()
        self.available = MCPServer is not None
        self.import_error = MCP_IMPORT_ERROR
        self.server = None
        if not self.available:
            self._log.warning("官方 mcp>=2.3 不可用，/mcp 将回退到内置 JSON-RPC：%s", MCP_IMPORT_ERROR)
            return
        try:
            tools = [self._make_tool(spec) for spec in self._schemas]
            self.server = MCPServer(name, title=title, instructions=instructions,
                                    version=version, tools=tools)
        except Exception as exc:  # 配置/版本问题不应让整个网关起不来
            self.available = False
            self.import_error = exc
            self.server = None
            self._log.exception("MCPServer 初始化失败，/mcp 将回退到内置 JSON-RPC")

    def _base_from_ctx(self, ctx: Any) -> str:
        if ctx is not None:
            try:
                headers = ctx.headers or {}
                # Starlette Headers 与普通 dict 都做大小写兼容
                for key in ("x-gw-base", "X-GW-Base"):
                    base = headers.get(key)
                    if base:
                        return str(base).rstrip("/")
            except Exception:
                pass
        base = self._default_base() if callable(self._default_base) else self._default_base
        return (base or "").rstrip("/")

    def _session_from_ctx(self, ctx: Any) -> str:
        """客户端/会话标识：优先显式 X-GW-Session，其次 MCP 会话 id。"""
        if ctx is not None:
            try:
                headers = ctx.headers or {}
                for key in ("x-gw-session", "X-GW-Session",
                            "mcp-session-id", "Mcp-Session-Id"):
                    value = headers.get(key)
                    if value:
                        return str(value).strip()
            except Exception:
                pass
        return ""

    def _make_tool(self, spec: dict) -> "Tool":
        name = spec["name"]
        description = spec.get("description") or ""
        schema = spec.get("inputSchema") or {"type": "object", "properties": {}}
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])

        params: list[inspect.Parameter] = []
        for prop_name, prop in props.items():
            py_type = _JSON_TYPES.get((prop or {}).get("type"), str)
            default = inspect.Parameter.empty if prop_name in required else None
            params.append(inspect.Parameter(prop_name, inspect.Parameter.KEYWORD_ONLY,
                                            annotation=py_type, default=default))
        # Context 由官方 Tool.run 直接注入，不属于工具入参。
        params.append(inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY,
                                        annotation=Context, default=None))

        async def _fn(**kwargs: Any) -> str:
            ctx = kwargs.pop("ctx", None)
            base = self._base_from_ctx(ctx)
            session = self._session_from_ctx(ctx)
            try:
                # call_tool 是同步实现（含 ffmpeg/HTTP），放到线程池避免阻塞事件循环。
                if session:
                    return await asyncio.to_thread(self._call_tool, name, dict(kwargs),
                                                   base, session)
                return await asyncio.to_thread(self._call_tool, name, dict(kwargs), base)
            except ToolError:
                raise
            except Exception as exc:
                # 保留中文错误给模型；官方 Tool.run 会包装成 isError=true 的文本结果。
                raise ToolError(str(exc)) from exc

        _fn.__name__ = name
        _fn.__doc__ = description
        _fn.__signature__ = inspect.Signature(parameters=params)
        tool = Tool.from_function(_fn, name=name, description=description)
        # 用原始 schema 覆盖自动生成的 schema，保留 enum / anyOf / 中文描述。
        tool.parameters = schema
        return tool

    def streamable_app(self):
        if not self.available or self.server is None:
            raise RuntimeError("MCPServer 不可用：%s" % self.import_error)
        return self.server.streamable_http_app(
            streamable_http_path="/mcp",
            json_response=True,
            stateless_http=True,
            host="127.0.0.1",
        )

    def tool_schemas(self) -> list[dict]:
        """原始工具定义，供旧 JSON-RPC 兜底使用。"""
        return list(self._schemas)


class McpHttpBridge:
    """在后台线程运行官方 streamable-http ASGI 应用，并提供回环 HTTP 转发。"""

    def __init__(self, service: McpService, timeout: int = 300,
                 logger: logging.Logger | None = None) -> None:
        self.service = service
        self.timeout = timeout
        self._log = logger or LOG
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._server = None
        self._port = 0

    @property
    def available(self) -> bool:
        return self.service.available

    def ensure_started(self) -> int:
        if not self.available:
            raise RuntimeError("MCPServer 不可用：%s" % self.service.import_error)
        with self._lock:
            if self._port:
                return self._port
            import uvicorn

            app = self.service.streamable_app()
            config = uvicorn.Config(
                app,
                host="127.0.0.1",
                port=0,
                log_level="warning",
                access_log=False,
                lifespan="on",
            )
            server = uvicorn.Server(config)
            thread = threading.Thread(target=server.run, name="gw-mcp-official", daemon=True)
            thread.start()
            deadline = time.time() + 20
            while time.time() < deadline:
                if server.started:
                    break
                if not thread.is_alive():
                    break
                time.sleep(0.05)
            if not server.started:
                raise RuntimeError("MCP 内部服务启动超时")
            try:
                port = server.servers[0].sockets[0].getsockname()[1]
            except Exception as exc:
                raise RuntimeError("无法获取 MCP 内部服务端口：%s" % exc) from exc
            self._server = server
            self._thread = thread
            self._port = int(port)
            atexit.register(self.close)
            self._log.info("官方 MCPServer 2.3 已启动：POST http://127.0.0.1:%d/mcp", self._port)
            return self._port

    def request(self, method: str, path: str, body: bytes,
                headers: dict[str, str]) -> tuple[int, str, list[tuple[str, str]], bytes]:
        port = self.ensure_started()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=self.timeout)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, resp.reason, list(resp.getheaders()), data
        finally:
            conn.close()

    def close(self) -> None:
        server = self._server
        if server is None:
            return
        self._server = None
        try:
            server.should_exit = True
        except Exception:
            pass
        thread = self._thread
        self._thread = None
        self._port = None
        if thread is not None:
            thread.join(timeout=5)
