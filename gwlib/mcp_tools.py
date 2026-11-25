# -*- coding: utf-8 -*-
"""MCP 工具清单：协议适配层只做 JSON-RPC，具体参数定义集中在这里。"""
from .quality import describe

QUALITY_ENUM = ["standard", "high", "exhigh", "lossless"]
QUALITY_PROP = {"type": "string", "enum": QUALITY_ENUM,
                "description": "音质：standard 128k / high 192k / exhigh 320k / lossless（下载保留原始文件）；下载类未指定时用设置里的下载音质"}


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
                            "artist=歌手名 会连续播放飞牛音乐库里这个歌手的全部歌曲；album=专辑名 会连续播放整张专辑。"
                            "library=true 会连续播放整个音乐库（全部歌曲）；loop=one 单曲循环，loop=all 列表循环，loop=off 顺序播放。"
                            "要随机顺序再加 shuffle=true：随机播放收藏用 favorites=true&shuffle=true，随机播放某歌单用 playlist=歌单名&shuffle=true，随机播放某歌手用 artist=歌手名&shuffle=true，随机播放整个音乐库用 library=true&shuffle=true。"
                            "歌单/歌手/专辑名支持模糊匹配：例如「播放儿歌歌单」可传 playlist=儿歌歌单，会匹配到「儿歌」。"
                            "如果用户是在切歌/下一首/换一首/上一首，不要调用本工具，改用 music_next 或 music_previous。"
                            "拿到结果后必须调用设备工具 self.music.play_url，并把 url/title/artist/lyric_url/duration 五个参数都填上。"),
            "inputSchema": {"type": "object", "properties": {
                "guid": {"type": "string", "description": "music_search 返回的曲目 guid，优先使用"},
                "query": {"type": "string"},
                "playlist": {"type": "string", "description": "歌单名；会连续播放整张歌单"},
                "artist": {"type": "string", "description": "歌手名；会连续播放飞牛音乐库里该歌手的全部歌曲"},
                "album": {"type": "string", "description": "专辑名；会连续播放整张专辑"},
                "favorites": {"type": "boolean", "description": "true=连续播放收藏列表"},
                "library": {"type": "boolean", "description": "true=连续播放整个音乐库（全部歌曲）"},
                "loop": {"type": "string", "enum": ["off", "one", "all"],
                         "description": "off=顺序播放（默认）/ one=单曲循环 / all=列表循环"},
                "shuffle": {"type": "boolean", "description": "true=随机顺序播放（配合 playlist/artist/album/favorites/library）"},
                "index": {"type": "integer", "description": "从列表第几首开始，默认 1"},
                "quality": QUALITY_PROP},
                "anyOf": [{"required": ["guid"]}, {"required": ["query"]}, {"required": ["playlist"]}, {"required": ["artist"]}, {"required": ["album"]}, {"required": ["favorites"]}, {"required": ["library"]}]},
        },
        {
            "name": "music_next",
            "description": ("播放当前收藏/歌单的下一首。用户说“切歌”“切割”“下一首”“换一首”“换首歌”“不要这首”时调用。必须在 music_play 以 playlist/favorites/artist/album 播放过之后使用；"
                            "返回该单曲的 device_args（url 与上一首不同，含 title/artist/lyric_url/duration），"
                            "拿到后调用设备工具 self.music.play_url 播放。已经是最后一首时不会从头开始。"),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "music_previous",
            "description": ("播放当前收藏/歌单的上一首。用户说“上一首”“前一首”“回到刚才那首”时调用。规则同 music_next；已经是第一首时不会循环到末尾。"),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "music_loop",
            "description": ("切换当前播放的循环模式。用户说“单曲循环”“循环这首歌”传 mode=one；说“列表循环”“循环播放”传 mode=all；说“关闭循环”“顺序播放”传 mode=off。"
                            "播放时也可以直接在 music_play 里传 loop=one/all/off。"),
            "inputSchema": {"type": "object", "properties": {
                "mode": {"type": "string", "enum": ["one", "all", "off"]}},
                "required": ["mode"]},
        },
        {
            "name": "music_resume",
            "description": ("从上次停止的位置继续播放。用户说“继续播放”“接着放”“续播”“刚才那首接着放”时调用。单曲会从记录的秒数继续，歌单/收藏会恢复到原列表的对应曲目。"),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "music_stop",
            "description": ("停止当前播放并记录进度。用户说“停止播放”“别放了”“暂停一下”时调用；之后说“继续播放”会调用 music_resume 接着放。"),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "music_favorite",
            "description": ("收藏或取消收藏一首歌。action=add|remove；可以传 query=歌名，或 guid=精确曲目，或 current=true 表示当前正在播放的歌。"
                            "配了飞牛音乐时直接写飞牛收藏；在线音源曲目 add 会按下载音质下载到本地下载目录（需配置 GW_DOWNLOAD_DIR）。"),
            "inputSchema": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["add", "remove"]},
                "query": {"type": "string", "description": "歌名（与 guid/current 三选一）"},
                "guid": {"type": "string", "description": "精确曲目 guid（与 query/current 三选一）"},
                "current": {"type": "boolean", "description": "true=收藏/取消当前正在播放的歌曲"},
                "title": {"type": "string", "description": "可选：配合 guid 指定标题"},
                "artist": {"type": "string", "description": "可选：配合 guid 指定歌手"},
                "quality": QUALITY_PROP}, "required": ["action"],
                "anyOf": [{"required": ["query"]}, {"required": ["guid"]}, {"required": ["current"]}]},
        },
        {
            "name": "music_status",
            "description": "查询当前播放状态。连续流播放时返回当前第几首、歌名/歌手、单曲时长、已播放秒数和当前歌词行；没有连续流时返回上一次播放信息。",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "music_download",
            "description": "把在线歌曲下载/保存到本地下载目录（GW_DOWNLOAD_DIR）。guid 或 query 二选一；未指定 quality 时使用下载音质（默认 lossless，保存原始文件）。",
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
