# -*- coding: utf-8 -*-
"""小智 xiaozhi-server 插件：fnmusic-gateway 直链播放。

放到 main/xiaozhi-server/plugins_func/functions/ 下，重启服务端生效。
依赖网关：GET {GW}/api/play -> {url}，然后把 url 交给设备工具 self.music.play_url。

注意（实测自固件 v2.6.0）：
  - 设备开始拉流后会主动断开 WS 语音通道，所以"好，播放 X"这句提示音可能听不到；
    这不是 bug，是固件为了省带宽关掉了语音。
  - 播歌期间 ASR 关闭，说"停止"无效，只能唤醒词打断，或让服务端调 self.music.stop。
  - 所以本插件的定位是"音质优先"，要定时停/语音打断请继续用 fnmusic.py 的 TTS FILE 通道。
"""
import json
import os
import urllib.parse
import urllib.request

from plugins_func.register import Action, ActionResponse, ToolType, register_function

GW_BASE = os.environ.get("FNMUSIC_GW_BASE", "http://192.168.50.100:8766").rstrip("/")
DEVICE_PLAY = os.environ.get("FNMUSIC_GW_DEVICE_PLAY", "self.music.play_url")
DEVICE_STOP = os.environ.get("FNMUSIC_GW_DEVICE_STOP", "self.music.stop")


def _reg(name, desc, parameters):
    """注册成 hass_state 模块，绕开智控台下发的插件勾选覆盖。"""
    def deco(fn):
        register_function(name, desc, ToolType.SYSTEM_CTL, parameters)(fn)
        fn.__module__ = "hass_state"
        return fn
    return deco


def _gw(path, timeout=20):
    url = GW_BASE + path
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _device(conn, tool, args, timeout=15):
    """同步插件跑在 plugin 线程池里，可以直接把协程丢回 conn.loop。"""
    import asyncio
    from core.providers.tools.device_mcp.mcp_handler import call_mcp_tool
    payload = json.dumps(args, ensure_ascii=False)
    fut = asyncio.run_coroutine_threadsafe(
        call_mcp_tool(conn, getattr(conn, "mcp_client", None), tool, payload), conn.loop)
    return fut.result(timeout=timeout)


@_reg("fnmusic_gw_play", "从飞牛音乐库播放歌曲。mode=search 按歌名（value 是歌名），mode=mood 按心情，mode=playlist 按歌单名，mode=stop 停止播放。", {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": ["search", "mood", "playlist", "stop"]},
        "value": {"type": "string", "description": "歌名 / 心情 / 歌单名"},
    },
    "required": ["mode"],
})
def fnmusic_gw_play(conn, mode="search", value=""):
    if mode == "stop":
        try:
            _device(conn, DEVICE_STOP, {})
            return ActionResponse(action=Action.RESPONSE, result="stopped", response="好，停了。")
        except Exception as e:
            return ActionResponse(action=Action.RESPONSE, result=str(e), response="停不下来，可能没在放歌。")
    key = {"search": "query", "mood": "mood", "playlist": "playlist"}.get(mode)
    if not key or not value:
        return ActionResponse(action=Action.RESPONSE, result="bad args", response="要听什么？")
    try:
        data = _gw("/api/play?" + urllib.parse.urlencode({key: value}))
    except Exception as e:
        return ActionResponse(action=Action.RESPONSE, result=str(e), response="音乐服务连不上。")
    url = data.get("url")
    if not url:
        return ActionResponse(action=Action.RESPONSE, result="no url", response="没找到这首歌。")
    if len(url) > 2048:
        return ActionResponse(action=Action.RESPONSE, result="url too long", response="直链太长了，换一首吧。")
    try:
        _device(conn, DEVICE_PLAY, {"url": url})
    except Exception as e:
        return ActionResponse(action=Action.RESPONSE, result="device failed: %s" % e,
                              response="设备没能播放这首歌。")
    return ActionResponse(action=Action.RESPONSE, result="playing %s" % data.get("title"),
                          response="好，播放%s。" % (data.get("spoken") or data.get("title")))


@_reg("fnmusic_gw_search", "在飞牛音乐库里搜歌，只列出结果不播放。", {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
})
def fnmusic_gw_search(conn, query=""):
    try:
        data = _gw("/api/search?" + urllib.parse.urlencode({"q": query, "limit": 5}))
    except Exception as e:
        return ActionResponse(action=Action.RESPONSE, result=str(e), response="音乐服务连不上。")
    rows = data.get("tracks") or []
    if not rows:
        return ActionResponse(action=Action.RESPONSE, result="empty", response="没搜到。")
    names = "、".join(t.get("title", "") for t in rows[:5])
    return ActionResponse(action=Action.RESPONSE, result=names, response="找到%s，要听哪首？" % names)


@_reg("fnmusic_gw_mood", "按心情挑歌并播放。", {
    "type": "object",
    "properties": {"mood": {"type": "string"}},
    "required": ["mood"],
})
def fnmusic_gw_mood(conn, mood=""):
    return fnmusic_gw_play(conn, "mood", mood)
