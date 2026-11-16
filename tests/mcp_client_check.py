# -*- coding: utf-8 -*-
"""用官方 MCP Python SDK 走一遍 streamable-http，验证与 xiaozhi-server 同款客户端能握手。"""
import asyncio
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main(url):
    async with streamable_http_client(url) as (read, write):
        async with ClientSession(read, write) as s:
            init = await s.initialize()
            print("SERVER", init.server_info.name, init.server_info.version, init.protocol_version)
            tools = await s.list_tools()
            print("TOOLS", len(tools.tools), [t.name for t in tools.tools])
            r = await s.call_tool("music_search", {"query": "晴天"})
            print("SEARCH", r.content[0].text.replace("\n", " | ")[:160])
            r = await s.call_tool("music_play", {"query": "晴天"})
            print("PLAY", r.content[0].text.replace("\n", " | ")[:220])
            r = await s.call_tool("music_mood", {"mood": "睡前"})
            print("MOOD", r.content[0].text.replace("\n", " | ")[:160])


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8799/mcp"))
