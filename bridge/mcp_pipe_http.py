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
    GW_TIMEOUT     单次工具调用超时秒数，默认 120（搜索+预热转码可能较慢）
    LOG_LEVEL      DEBUG/INFO/WARNING，默认 INFO
"""

import asyncio
import json
import logging
import os
import sys
import urllib.error
import urllib.request

ENDPOINT = (os.environ.get("MCP_ENDPOINT") or "").strip()
GW_MCP_URL = (os.environ.get("GW_MCP_URL") or "http://127.0.0.1:8766/mcp").strip()
GW_TIMEOUT = float(os.environ.get("GW_TIMEOUT") or "120")
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
    head, _, tail = url.partition("token=")
    keep = tail[:4]
    return head + "token=" + keep + "***"


def _post_sync(payload: bytes):
    """同步 POST 到网关；返回 (响应 JSON 或 None, 是否通知)。"""
    req = urllib.request.Request(
        GW_MCP_URL,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=GW_TIMEOUT) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            body = resp.read()
    except urllib.error.HTTPError as e:          # 4xx/5xx 也要把 body 读出来
        ctype = (e.headers.get("Content-Type") or "").lower() if e.headers else ""
        body = e.read() or b""
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


async def forward(raw: str):
    """把一帧 WS 文本转发给网关，返回要回写给接入点的 JSON 字符串。"""
    try:
        msg = json.loads(raw)
    except Exception:
        LOG.warning("ignore non-json frame: %s", raw[:120])
        return None

    batch = msg if isinstance(msg, list) else [msg]
    requests = [m for m in batch if isinstance(m, dict) and m.get("id") is not None]
    if not requests:
        # 全是通知（notifications/initialized 等）：转发后无需回包
        await asyncio.to_thread(_post_sync, raw.encode("utf-8"))
        return None

    for m in requests:
        method = m.get("method") or ""
        if method == "tools/call":
            LOG.info("tools/call %s %s", (m.get("params") or {}).get("name"),
                     json.dumps((m.get("params") or {}).get("arguments") or {}, ensure_ascii=False)[:160])
        elif method:
            LOG.info("<- %s id=%s", method, m.get("id"))

    resp = await asyncio.to_thread(_post_sync, raw.encode("utf-8"))
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
        LOG.error("MCP_ENDPOINT 未设置，例如 wss://host:59826/mcp_endpoint/mcp/?token=xxx")
        sys.exit(2)
    LOG.info("gateway = %s", GW_MCP_URL)
    LOG.info("endpoint = %s", mask_url(ENDPOINT))

    backoff = INITIAL_BACKOFF
    while True:
        try:
            async with websockets.connect(
                ENDPOINT, ping_interval=20, ping_timeout=20, max_size=None
            ) as ws:
                LOG.info("接入点已连接")
                backoff = INITIAL_BACKOFF
                async for raw in ws:
                    if isinstance(raw, (bytes, bytearray)):
                        raw = raw.decode("utf-8", "replace")
                    reply = await forward(raw)
                    if reply:
                        await ws.send(reply)
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
