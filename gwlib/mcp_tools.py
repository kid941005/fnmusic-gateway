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
            "description": "在本地曲库优先搜歌（自动纠正同音字/错字，按相关度排序）；本地没有时会并行到所有在线音源兜底。返回带序号的曲目列表。",
            "inputSchema": {"type": "object", "properties": {
                "query": {"type": "string", "description": "歌名或歌手"},
                "limit": {"type": "integer", "description": "最多返回几首，默认 10"}},
                "required": ["query"]},
        },
        {
            "name": "music_browse",
            "description": "浏览飞牛音乐库：category=track 全部歌曲 / album 专辑 / artist 歌手 / playlist 歌单。",
            "inputSchema": {"type": "object", "properties": {
                "category": {"type": "string", "enum": ["track", "album", "artist", "playlist"]},
                "limit": {"type": "integer"}}, "required": ["category"]},
        },
        {
            "name": "music_playlists",
            "description": "列出我的自建歌单。",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "music_playlist_tracks",
            "description": "按歌单名列出里面的曲目。",
            "inputSchema": {"type": "object", "properties": {
                "name": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["name"]},
        },
        {
            "name": "music_favorites",
            "description": "列出我收藏的歌曲。",
            "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}},
        },
        {
            "name": "music_mood",
            "description": "按心情/场景选曲，返回候选列表。mood 例如：安静、放松、睡前、开心、伤感、运动、工作、下雨。",
            "inputSchema": {"type": "object", "properties": {
                "mood": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["mood"]},
        },
        {
            "name": "music_play",
            "description": ("拿一首歌的可播放直链，同时给出歌名、歌手、歌词直链和音质（设备屏显要用）。"
                            "query/mood/playlist 三选一，index 从 1 开始。"
                            "拿到结果后必须调用设备工具 self.music.play_url，并把 url/title/artist/lyric_url 四个参数都填上。"),
            "inputSchema": {"type": "object", "properties": {
                "query": {"type": "string"}, "mood": {"type": "string"},
                "playlist": {"type": "string"}, "index": {"type": "integer"},
                "quality": QUALITY_PROP}, "required": []},
        },
        {
            "name": "music_favorite",
            "description": "收藏或取消收藏一首歌。action=add|remove，query 是歌名。在线歌曲 add 时会按最高音质下载到本地曲库（需配置 GW_DOWNLOAD_DIR）。",
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
            "description": "把在线歌曲下载/保存到本地曲库（GW_DOWNLOAD_DIR）。guid 或 query 二选一；quality=lossless 时保存原始文件。",
            "inputSchema": {"type": "object", "properties": {
                "guid": {"type": "string"}, "query": {"type": "string"},
                "quality": QUALITY_PROP}, "required": []},
        },
        {
            "name": "music_sources",
            "description": "查看已配置在线音源及健康状态。",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "music_storage",
            "description": "查看播放缓存与本地曲库存放情况。",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


def quality_options():
    return describe()
