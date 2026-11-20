# -*- coding: utf-8 -*-
"""小智 xiaozhi-server 插件：fnmusic-gateway 直链播放（示例）。

放到 main/xiaozhi-server/plugins_func/functions/ 下，重启服务端生效。
依赖网关：GET {GW}/api/play -> {url}，然后把 url 交给设备工具 self.music.play_url。

注册要点（踩过的坑，别改回去）：
  1. register_function 只有 3 个参数 (name, desc, type)，多传参数直接 TypeError；
  2. desc 必须是完整的 OpenAI 工具字典 {"type":"function","function":{...}}，
     否则 plugin_executor 取不到 description["function"]，LLM 看不到参数；
  3. 必须先把 fn.__module__ 改成智控台已勾选的模块名（这里用 hass_state）再注册——
     module_func_map 的 key 在 register_function 内部按 __module__ 计算，
     顺序反了智控台就不会下发这几个工具。

注意（实测自固件 v2.6.0）：
  - 设备开始拉流后会主动断开 WS 语音通道，所以"好，播放 X"这句提示音可能听不到；
    这不是 bug，是固件为了省带宽关掉了语音。
  - 播歌期间 ASR 关闭，说"停止"无效，只能唤醒词打断，或让服务端调 self.music.stop。
  - 所以本插件的定位是"音质优先"，要定时停/语音打断请继续用 fnmusic.py 的 TTS FILE 通道。
"""
import asyncio
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from plugins_func.register import Action, ActionResponse, ToolType, register_function

GW_BASE = os.environ.get("FNMUSIC_GW_BASE", "http://192.168.50.100:8766").rstrip("/")
DEVICE_PLAY = os.environ.get("FNMUSIC_GW_DEVICE_PLAY", "self.music.play_url")
DEVICE_STOP = os.environ.get("FNMUSIC_GW_DEVICE_STOP", "self.music.stop")
# 换成你在智控台勾选过的任意插件模块名，否则函数不会被下发
ENABLE_MODULE = os.environ.get("FNMUSIC_GW_MODULE", "hass_state")
MAX_URL = 2048  # 固件 self.music.play_url 的长度上限


def _reg(name, description, parameters):
    """注册一个工具：先改 __module__，再用完整 OpenAI 工具字典注册。"""
    desc = {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }

    def deco(fn):
        fn.__module__ = ENABLE_MODULE  # 必须早于 register_function
        return register_function(name, desc, ToolType.SYSTEM_CTL)(fn)

    return deco


def _gw(path, timeout=20):
    """GET 网关接口；把网关的 {"error": ...} 转成可读异常。"""
    req = urllib.request.Request(GW_BASE + path, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error") or str(e)
        except Exception:
            msg = str(e)
        raise RuntimeError(msg)


def _device(conn, tool, args, timeout=15):
    """同步插件跑在 plugin 线程池里，可以直接把协程丢回 conn.loop。"""
    from core.providers.tools.device_mcp.mcp_handler import call_mcp_tool

    payload = json.dumps(args, ensure_ascii=False)
    fut = asyncio.run_coroutine_threadsafe(
        call_mcp_tool(conn, getattr(conn, "mcp_client", None), tool, payload), conn.loop
    )
    return fut.result(timeout=timeout)


@_reg(
    "fnmusic_gw_play",
    "从在线音源播放歌曲。mode=search 按歌名（value 是歌名），"
    "mode=playlist 按歌单名（需本地曲库），mode=stop 停止播放。",
    {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["search", "playlist", "stop"],
                "description": "播放方式",
            },
            "value": {"type": "string", "description": "歌名 / 心情 / 歌单名"},
            "quality": {"type": "string", "enum": ["standard", "high", "exhigh", "lossless"],
                        "description": "音质，默认 standard；lossless 仅影响下载，播放仍为 320k MP3"},
        },
        "required": ["mode"],
    },
)
def fnmusic_gw_play(conn, mode="search", value="", quality=""):
    if mode == "stop":
        try:
            _device(conn, DEVICE_STOP, {})
            return ActionResponse(action=Action.RESPONSE, result="stopped", response="好，停了。")
        except Exception as e:
            return ActionResponse(
                action=Action.RESPONSE, result=str(e), response="停不下来，可能没在放歌。"
            )
    key = {"search": "query", "playlist": "playlist"}.get(mode)
    if not key or not value:
        return ActionResponse(action=Action.RESPONSE, result="bad args", response="要听什么？")
    try:
        params = {key: value}
        if quality:
            params["quality"] = quality
        data = _gw("/api/play?" + urllib.parse.urlencode(params))
    except Exception as e:
        return ActionResponse(action=Action.RESPONSE, result=str(e), response="没找到这首歌。")
    url = data.get("url")
    if not url:
        return ActionResponse(action=Action.RESPONSE, result="no url", response="没找到这首歌。")
    if len(url) > MAX_URL:
        return ActionResponse(action=Action.RESPONSE, result="url too long", response="直链太长了，换一首吧。")
    # 设备屏显要歌名/歌手/歌词：固件 play_url 支持 title/artist/lyric_url
    args = {
        "url": url,
        "title": str(data.get("title") or "") or "unknown",
        "artist": str(data.get("artist") or "") or "unknown",
    }
    lyric_url = str(data.get("lyric_url") or "")
    if lyric_url and len(lyric_url) <= MAX_URL:
        args["lyric_url"] = lyric_url
    try:
        _device(conn, DEVICE_PLAY, args)
    except Exception as e:
        return ActionResponse(
            action=Action.RESPONSE, result="device failed: %s" % e, response="设备没能播放这首歌。"
        )
    return ActionResponse(
        action=Action.RESPONSE,
        result="playing %s" % data.get("title"),
        response="好，播放%s。" % (data.get("spoken") or data.get("title")),
    )


@_reg(
    "fnmusic_gw_search",
    "在在线音源里搜歌，只列出结果不播放。",
    {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "歌名或歌手"}},
        "required": ["query"],
    },
)
def fnmusic_gw_search(conn, query=""):
    try:
        data = _gw("/api/search?" + urllib.parse.urlencode({"q": query, "limit": 5}))
    except Exception as e:
        return ActionResponse(action=Action.RESPONSE, result=str(e), response="没搜到。")
    rows = data.get("tracks") or []
    if not rows:
        return ActionResponse(action=Action.RESPONSE, result="empty", response="没搜到。")
    names = "、".join(t.get("title", "") for t in rows[:5])
    return ActionResponse(action=Action.RESPONSE, result=names, response="找到%s，要听哪首？" % names)
