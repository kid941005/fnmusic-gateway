# -*- coding: utf-8 -*-
"""用官方 MCP Python SDK 走一遍 streamable-http，验证与 xiaozhi-server 同款客户端能握手。"""
import asyncio
import os
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def _run(url, http_client):
    async with streamable_http_client(url, http_client=http_client) as (read, write):
        async with ClientSession(read, write) as s:
            init = await s.initialize()
            print("SERVER", init.server_info.name, init.server_info.version, init.protocol_version)
            tools = await s.list_tools()
            print("TOOLS", len(tools.tools), [t.name for t in tools.tools])
            r = await s.call_tool("music_search", {"query": "晴天"})
            print("SEARCH", r.content[0].text.replace("\n", " | ")[:160])
            r = await s.call_tool("music_play", {"query": "晴天"})
            print("PLAY", r.content[0].text.replace("\n", " | ")[:220])


async def main(url, token=""):
    token = (token or os.environ.get("GW_ADMIN_TOKEN", "")).strip()
    if token:
        # mcp 2.3 依赖 httpx2；老版本仍用 httpx。优先匹配 SDK 期望的类型。
        try:
            import httpx2 as _http
        except ImportError:
            import httpx as _http
        async with _http.AsyncClient(headers={"X-Admin-Token": token}) as hc:
            await _run(url, hc)
    else:
        await _run(url, None)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8799/mcp",
                   sys.argv[2] if len(sys.argv) > 2 else ""))
