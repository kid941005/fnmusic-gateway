#!/usr/bin/env python3
"""小智 MCP 接入点 (WebSocket) <-> fnmusic-gateway (HTTP) 桥接器。

小智的「MCP 接入点」(mcp-endpoint-server) 是一个纯消息转发的注册中心：

    小智端 xiaozhi-server  --WS-->  接入点  --WS-->  本进程  --HTTP-->  fnmusic-gateway /mcp

接入点把 xiaozhi-server 发来的 JSON-RPC 请求（initialize / tools/list / tools/call / ping）
转发给工具端；工具端必须原样回一条 JSON-RPC 响应（id 与请求一致，接入点会做 id 改写与聚合）。
官方示例 mcp_pipe.py 是「WS <-> 子进程 stdio」，本脚本是它的 HTTP 版：把请求转给已经在跑的
fnmusic-gateway，不需要再拉起一个 stdio MCP 进程。

用法：
    export MCP_ENDPOINT='wss://xxx:59826/mcp_endpoint/mcp/?token=...'
    export GW_MCP_URL='http://127.0.0.1:8766/mcp'      # 默认值就是这个
    python bridge/mcp_pipe_http.py

环境变量：
    MCP_ENDPOINT   必填，接入点地址（ws:// 或 wss://）
    GW_MCP_URL     fnmusic-gateway 的 MCP 端点，默认 http://127.0.0.1:8766/mcp
    GW_PUBLIC_BASE 设备可访问的网关地址前缀；桥接模式必填，用于生成签名直链
    GW_ADMIN_TOKEN 网关管理令牌（设置 GW_ADMIN_TOKEN 时必填）
    GW_TIMEOUT     单次工具调用超时秒数，默认 120（搜索+预热转码可能较慢）
    GW_CONCURRENCY 并发转发的工具调用上限，默认 4（避免单个慢工具卡住后续请求）
    LOG_LEVEL      DEBUG/INFO/WARNING，默认 INFO
"""

import asyncio
import json
import logging
import os
import sys
import uuid
import urllib.error
import urllib.request

ENDPOINT = (os.environ.get("MCP_ENDPOINT") or "").strip()
GW_MCP_URL = (os.environ.get("GW_MCP_URL") or "http://127.0.0.1:8766/mcp").strip()
GW_PUBLIC_BASE = (os.environ.get("GW_PUBLIC_BASE") or "").strip().rstrip("/")


def _env_float(name, default):
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return float(default)


def _env_int(name, default):
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return int(default)


GW_TIMEOUT = max(1.0, _env_float("GW_TIMEOUT", 120))
GW_CONCURRENCY = max(1, _env_int("GW_CONCURRENCY", 4))
GW_ADMIN_TOKEN = (os.environ.get("GW_ADMIN_TOKEN") or "").strip()
INITIAL_BACKOFF = 1.0
MAX_BACKOFF = 600.0

logging.basicConfig(
    level=getattr(logging, (os.environ.get("LOG_LEVEL") or "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOG = logging.getLogger("mcp-pipe")


def mask_url(url: str) -> str:
    """日志里隐藏 token。"""
    if "token=" not in url:
        return url
    head, _, _tail = url.partition("token=")
    return head + "token=***"


def _headers(session=""):
    """构造转发头；session 用于网关按设备隔离歌单游标。"""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if GW_ADMIN_TOKEN:
        headers["X-Admin-Token"] = GW_ADMIN_TOKEN
    if GW_PUBLIC_BASE:
        headers["X-GW-Base"] = GW_PUBLIC_BASE
    if str(session or "").strip():
        headers["X-GW-Session"] = str(session).strip()
    return headers


def _session_hint(msg):
    """从 JSON-RPC 帧里提取可用的设备/会话标识。"""
    items = msg if isinstance(msg, list) else [msg]
    for item in items:
        if not isinstance(item, dict):
            continue
        params = item.get("params") if isinstance(item.get("params"), dict) else {}
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        sources = (item, meta, params)
        # 设备序列号优先于共享 session_id（跨 item/meta/params 全局优先），避免多设备串游标。
        for key in ("serialNumber", "serial_number", "serial"):
            for source in sources:
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:128]
        for key in ("device_id", "deviceId", "session_id", "sessionId"):
            for source in sources:
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:128]
    return ""


def device_session(base, msg):
    """把连接级会话与设备序列号拼成命名空间，避免同一 WS 下多设备串游标。"""
    hint = _session_hint(msg)
    return base + (":" + hint if hint else "")


def _jsonrpc_error_for(payload, message):
    """HTTP 4xx/5xx 不是 JSON-RPC 响应：按原请求 id 包装成标准错误，避免接入点等不到回包。"""
    try:
        msg = json.loads(payload.decode("utf-8", "replace"))
    except Exception:
        msg = None
    items = msg if isinstance(msg, list) else [msg]
    errs = [{"jsonrpc": "2.0", "id": item.get("id") if isinstance(item, dict) else None,
             "error": {"code": -32603, "message": message}} for item in items]
    return errs if isinstance(msg, list) else errs[0]


def _post_sync(payload: bytes, session=""):
    """同步 POST 到网关；返回响应 JSON 或 None。"""
    headers = _headers(session)
    req = urllib.request.Request(
        GW_MCP_URL,
        data=payload,
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=GW_TIMEOUT) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            body = resp.read()
    except urllib.error.HTTPError as e:          # 4xx/5xx 也要把 body 读出来
        ctype = (e.headers.get("Content-Type") or "").lower() if e.headers else ""
        body = e.read() or b""
        if e.code >= 400:
            detail = body.decode("utf-8", "replace")[:160]
            LOG.warning("gateway HTTP %s: %s", e.code, detail)
            return _jsonrpc_error_for(payload, "fnmusic-gateway HTTP %s" % e.code)
    except Exception as e:                        # 连不上 / 超时
        LOG.warning("gateway request failed: %s", e)
        return None

    if not body.strip():
        return None                               # 202，通知类消息
    text = body.decode("utf-8", "replace")
    if "event-stream" in ctype or text.lstrip().startswith("data:"):
        # SSE：把所有 data: 行拼起来再解析
        chunks = []
        for line in text.splitlines():
            if line.startswith("data:"):
                chunks.append(line[5:].strip())
        text = "".join(chunks) or text
    try:
        return json.loads(text)
    except Exception:
        LOG.warning("gateway returned non-json: %s", text[:200])
        return None


async def forward(raw: str, session: str = ""):
    """把一帧 WS 文本转发给网关，返回要回写给接入点的 JSON 字符串。"""
    try:
        msg = json.loads(raw)
    except Exception:
        LOG.warning("ignore non-json frame: %s", raw[:120])
        return json.dumps({"jsonrpc": "2.0", "id": None,
                           "error": {"code": -32700, "message": "invalid JSON"}},
                          ensure_ascii=False)

    if not str(session or "").strip():
        session = _session_hint(msg)
    batch = msg if isinstance(msg, list) else [msg]
    requests = [m for m in batch if isinstance(m, dict) and m.get("id") is not None]
    if not requests:
        # 全是通知（notifications/initialized 等）：转发后无需回包
        await asyncio.to_thread(_post_sync, raw.encode("utf-8"), session)
        return None

    for m in requests:
        method = m.get("method") or ""
        if method == "tools/call":
            LOG.info("tools/call %s %s", (m.get("params") or {}).get("name"),
                     json.dumps((m.get("params") or {}).get("arguments") or {}, ensure_ascii=False)[:160])
        elif method:
            LOG.info("<- %s id=%s", method, m.get("id"))

    resp = await asyncio.to_thread(_post_sync, raw.encode("utf-8"), session)
    if resp is None:
        # 网关没回包（超时 / 202 空体）：给每个请求回一条 JSON-RPC 错误。
        # 不回包的话接入点会一直等，用户侧表现为"工具无响应"。
        errs = [
            {"jsonrpc": "2.0", "id": m.get("id"),
             "error": {"code": -32603, "message": "fnmusic-gateway 无响应"}}
            for m in requests
        ]
        return json.dumps(errs if isinstance(msg, list) else errs[0], ensure_ascii=False)

    for item in (resp if isinstance(resp, list) else [resp]):
        if isinstance(item, dict) and isinstance(item.get("result"), dict):
            tools = item["result"].get("tools")
            if isinstance(tools, list):
                LOG.info("tools/list -> %d 个工具: %s", len(tools),
                         ", ".join(t.get("name", "?") for t in tools))
    return json.dumps(resp, ensure_ascii=False)


async def serve():
    import websockets

    if not ENDPOINT:
        # 没有配置接入点时保持容器存活，而不是 sys.exit(2) 让 Docker 无限重启。
        LOG.warning("MCP_ENDPOINT 未设置，桥接器空闲；配置后重启即可连接")
        while True:
            await asyncio.sleep(3600)
    if not GW_PUBLIC_BASE:
        LOG.error("GW_PUBLIC_BASE 未设置：桥接模式下设备直链会指向容器内地址，请设置为设备可访问的公网/局域网地址后重启")
        sys.exit(2)
    LOG.info("gateway = %s (public_base=%s)", GW_MCP_URL, GW_PUBLIC_BASE)
    LOG.info("endpoint = %s", mask_url(ENDPOINT))

    backoff = INITIAL_BACKOFF
    while True:
        try:
            async with websockets.connect(
                ENDPOINT, ping_interval=20, ping_timeout=20,
                max_size=4 * 1024 * 1024,
            ) as ws:
                conn_session = "bridge-" + uuid.uuid4().hex
                LOG.info("接入点已连接（并发上限 %d，会话 %s）", GW_CONCURRENCY, conn_session)
                backoff = INITIAL_BACKOFF
                slots = asyncio.Semaphore(GW_CONCURRENCY)
                send_lock = asyncio.Lock()
                pending = set()

                async def handle(raw_text):
                    try:
                        parsed = json.loads(raw_text) if isinstance(raw_text, str) else raw_text
                    except Exception:
                        parsed = {}
                    session = device_session(conn_session, parsed if isinstance(parsed, (dict, list)) else {})
                    async with slots:
                        reply = await forward(raw_text, session=session)
                    if reply:
                        async with send_lock:
                            await ws.send(reply)

                def _done(task):
                    pending.discard(task)
                    if not task.cancelled() and task.exception() is not None:
                        LOG.warning("转发任务失败: %s", task.exception())

                try:
                    max_pending = max(8, GW_CONCURRENCY * 4)
                    async for raw in ws:
                        if isinstance(raw, (bytes, bytearray)):
                            raw = raw.decode("utf-8", "replace")
                        # 帧潮涌时给 WS 读取施加背压，避免 create_task 无界堆积。
                        while len(pending) >= max_pending:
                            await asyncio.sleep(0.01)
                        task = asyncio.create_task(handle(raw))
                        pending.add(task)
                        task.add_done_callback(_done)
                finally:
                    for task in list(pending):
                        task.cancel()
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            LOG.warning("连接断开: %s", e)
        LOG.info(" %.0f 秒后重连", backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF)


if __name__ == "__main__":
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
