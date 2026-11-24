#!/usr/bin/env python3
"""桥接器自测：起一个假接入点，按接入点真实行为给桥接器发请求，校验回包。

    python tests/bridge_selftest.py            # 需要 fnmusic-gateway 已在本机 8799 跑起来
"""
import asyncio, json, os, subprocess, sys

PORT = int(os.environ.get("SELFTEST_PORT", "8798"))
GW = os.environ.get("GW_MCP_URL", "http://127.0.0.1:8799/mcp")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

async def main():
    import websockets

    got = {}

    async def handler(ws):
        reqs = [
            {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"sampling": {}, "roots": {"listChanged": False}},
                "clientInfo": {"name": "xz-mcp-broker", "version": "0.0.1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}},
            {"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {
                "name": "music_status", "arguments": {}, "serialNumber": None}},
        ]
        for r in reqs:
            await ws.send(json.dumps(r))
            if r.get("id") is None:
                await asyncio.sleep(0.3)
                continue
            raw = await asyncio.wait_for(ws.recv(), timeout=60)
            got[r["id"]] = json.loads(raw)
        await ws.close()

    async with websockets.serve(handler, "127.0.0.1", PORT):
        env = dict(os.environ, MCP_ENDPOINT="ws://127.0.0.1:%d/" % PORT, GW_MCP_URL=GW,
                   GW_PUBLIC_BASE=os.environ.get("GW_PUBLIC_BASE", "http://127.0.0.1:8799"))
        proc = subprocess.Popen([sys.executable, "-u", "bridge/mcp_pipe_http.py"],
                                cwd=HERE, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        await asyncio.sleep(6)
        proc.terminate()
        try:
            out = proc.communicate(timeout=5)[0]
        except Exception:
            out = ""

    ok = True
    def check(name, cond, extra=""):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
        ok = ok and cond

    init = got.get(0, {})
    check("initialize 有 serverInfo", "serverInfo" in (init.get("result") or {}),
          json.dumps((init.get("result") or {}).get("serverInfo") or {}, ensure_ascii=False))
    tl = (got.get(1, {}).get("result") or {}).get("tools") or []
    check("tools/list 返回 16 个工具", len(tl) == 16, "got %d" % len(tl))
    check("ping 回空 result", got.get(2, {}).get("result") == {})
    call = got.get(10, {})
    text = ((call.get("result") or {}).get("content") or [{}])[0].get("text", "")
    check("tools/call music_status 有文本", bool(text), text[:80].replace("\n", " "))
    print("---- 桥接器日志 ----")
    print(out[-1200:])
    sys.exit(0 if ok else 1)

asyncio.run(main())
