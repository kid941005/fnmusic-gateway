# -*- coding: utf-8 -*-
"""MCP 工具清单：协议适配层只做 JSON-RPC，具体参数定义集中在这里。"""
from .quality import describe

QUALITY_ENUM = ["standard", "high", "exhigh", "lossless"]
QUALITY_PROP = {"type": "string", "enum": QUALITY_ENUM,
                "description": "音质：standard 128k / high 192k / exhigh 320k / lossless 下载保原文件"}


def build_tools():
    return [
        {
            "name": "music_search",
            "description": "搜歌：飞牛音乐优先，本地不足再并行问在线音源（自动纠正同音字/错字，按相关度排序）。返回带序号和 guid 的曲目列表；播放时优先把 guid 原样传给 music_play。",
            "inputSchema": {"type": "object", "properties": {
                "query": {"type": "string", "description": "歌名或歌手"},
                "limit": {"type": "integer", "description": "最多返回几首，默认 10"}},
                "required": ["query"]},
        },
        {
            "name": "music_browse",
            "description": "浏览飞牛音乐曲库：category=track 全部歌曲 / album 专辑 / artist 歌手 / playlist 歌单。未配置飞牛音乐时返回提示。",
            "inputSchema": {"type": "object", "properties": {
                "category": {"type": "string", "enum": ["track", "album", "artist", "playlist"]},
                "limit": {"type": "integer"}}, "required": ["category"]},
        },
        {
            "name": "music_playlists",
            "description": "列出飞牛音乐里的歌单（未配置飞牛音乐时不可用）。",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "music_playlist_tracks",
            "description": "按歌单名列出飞牛音乐里的曲目（未配置飞牛音乐时不可用）。要连续播放整张歌单，直接调用 music_play 并传 playlist=歌单名。",
            "inputSchema": {"type": "object", "properties": {
                "name": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["name"]},
        },
        {
            "name": "music_favorites",
            "description": "列出飞牛音乐的收藏（未配置飞牛音乐时不可用）。要连续播放收藏，调用 music_play 并传 favorites=true。",
            "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}},
        },
        {
            "name": "music_play",
            "description": ("拿一首歌的可播放直链，同时给出歌名、歌手、歌词直链、总时长（秒）和音质（设备屏显要用）。"
                            "优先传 music_search 返回的 guid（最稳，不会同名播错）；没有 guid 时 query/playlist 二选一，index 从 1 开始。"
                            "playlist 会连续播放整张歌单，favorites=true 会连续播放收藏列表（播完自动结束），不用逐首调用。"
                            "拿到结果后必须调用设备工具 self.music.play_url，并把 url/title/artist/lyric_url/duration 五个参数都填上。"),
            "inputSchema": {"type": "object", "properties": {
                "guid": {"type": "string", "description": "music_search 返回的曲目 guid，优先使用"},
                "query": {"type": "string"},
                "playlist": {"type": "string", "description": "歌单名；会连续播放整张歌单"},
                "favorites": {"type": "boolean", "description": "true=连续播放收藏列表"},
                "index": {"type": "integer", "description": "从列表第几首开始，默认 1"},
                "quality": QUALITY_PROP},
                "anyOf": [{"required": ["guid"]}, {"required": ["query"]}, {"required": ["playlist"]}, {"required": ["favorites"]}]},
        },
        {
            "name": "music_favorite",
            "description": "收藏或取消收藏一首歌。action=add|remove，query 是歌名。配了飞牛音乐时直接写飞牛收藏；在线音源曲目 add 会按最高音质下载到本地下载目录（需配置 GW_DOWNLOAD_DIR）。",
            "inputSchema": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["add", "remove"]},
                "query": {"type": "string"}, "quality": QUALITY_PROP}, "required": ["action", "query"]},
        },
        {
            "name": "music_status",
            "description": "上一次播放的是什么（含音质）。",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "music_download",
            "description": "把在线歌曲下载/保存到本地下载目录（GW_DOWNLOAD_DIR）。guid 或 query 二选一；quality=lossless 时保存原始文件。",
            "inputSchema": {"type": "object", "properties": {
                "guid": {"type": "string"}, "query": {"type": "string"},
                "title": {"type": "string", "description": "可选：显式指定保存文件名里的歌名"},
                "artist": {"type": "string", "description": "可选：显式指定保存文件名里的歌手"},
                "quality": QUALITY_PROP},
                "anyOf": [{"required": ["guid"]}, {"required": ["query"]}]},
        },
        {
            "name": "music_sources",
            "description": "查看内置在线音源平台及健康状态（飞牛音乐的连接状态见网关设置页）。",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "music_storage",
            "description": "查看播放缓存与本地下载目录占用。",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


def quality_options():
    return describe()
