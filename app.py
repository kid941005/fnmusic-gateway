#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fnmusic-gateway —— 飞牛音乐(fnOS) → 小智 的一层网关。

双栈出口
--------
1) MCP（streamable-http）  POST /mcp
   给服务端/云端 LLM 用：查歌单、收藏、按心情选曲、拿直链。
2) 签名直链               GET /play/<guid>.mp3?exp=<ts>&sig=<hmac>
   给设备 self.music.play_url 用：设备直拉，不经过小智服务端。

辅助 REST
--------
GET /healthz                                  健康检查
GET /api/search?q=&limit=                     搜歌
GET /api/playlists                            歌单列表
GET /api/playlist?name=&limit=                歌单曲目
GET /api/favorites                            收藏
GET /api/mood?mood=                           按心情选曲
GET /api/play?query=|mood=|playlist=[&index=] 取播放直链（会预热转码）
GET /api/status                               上一次播放

设计约束（实测自固件 v2.6.0）
---------------------------
- 设备只解 MP3；self.music.play_url 的 URL <= 2048 字节、仅 http/https、禁空白与 '@'
- 飞牛 token 绝不进 URL：直链用 HMAC 短时签名（默认 15 分钟）
- 飞牛曲库多为 FLAC：首次点播用 ffmpeg 转 128k/44.1k 立体声 MP3 并落盘缓存（LRU）

依赖：Python 3.9+ 标准库 + ffmpeg 可执行文件。无第三方包。
"""
import hashlib
import hmac
import json
import logging
import os
import random
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.1.0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)


def _env(k, d=None):
    v = os.environ.get(k)
    return d if v is None or v == "" else v


def _env_int(k, d):
    try:
        return int(_env(k, "") or d)
    except Exception:
        return d


def _env_bool(k, d=False):
    v = os.environ.get(k)
    if v is None:
        return d
    return v.strip().lower() in ("1", "true", "yes", "on")


DATA_DIR = _env("GW_DATA", os.path.join(BASE_DIR, "data"))
os.makedirs(DATA_DIR, exist_ok=True)

HOST = _env("GW_HOST", "0.0.0.0")
PORT = _env_int("GW_PORT", 8766)
PUBLIC_BASE = (_env("GW_PUBLIC_BASE", "") or "").rstrip("/")
URL_TTL = _env_int("GW_URL_TTL", 900)
CACHE_DIR = _env("GW_CACHE_DIR", os.path.join(DATA_DIR, "cache"))
CACHE_MAX = _env_int("GW_CACHE_MAX_BYTES", 8 * 1024 ** 3)
MP3_BITRATE = _env("GW_MP3_BITRATE", "128k")
MP3_SR = _env_int("GW_MP3_SR", 44100)
MP3_CHANNELS = _env_int("GW_MP3_CHANNELS", 2)
PREWARM = _env_bool("GW_PREWARM", True)
MOCK = _env_bool("FNMUSIC_MOCK", False)
MOODS_FILE = _env("GW_MOODS_FILE", os.path.join(DATA_DIR, "moods.json"))
STATE_FILE = os.path.join(DATA_DIR, "state.json")
MAX_LIMIT = _env_int("GW_MAX_LIMIT", 50)
HTTP_BODY_CAP = 1024 * 1024
MCP_BODY_CAP = 4 * 1024 * 1024
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+(:\d{1,5})?$")
TRANSCODE_CONCURRENCY = max(1, _env_int("GW_TRANSCODE_CONCURRENCY", 2))


def _log_level(name):
    return getattr(logging, str(name or "INFO").strip().upper(), logging.INFO)


logging.basicConfig(level=_log_level(_env("GW_LOG_LEVEL", "INFO")),
                    format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("fnmusic-gw")

# 同时最多几个 ffmpeg：防止多台设备同时点播把 NAS 的 CPU 打满
_TRANSCODE_SLOTS = threading.BoundedSemaphore(TRANSCODE_CONCURRENCY)


class BadRequest(Exception):
    """调用方参数错误：HTTP 映射为 400，MCP 侧原样回文本。"""


# ---------------------------------------------------------------- 签名
def _load_secret():
    s = _env("GW_SIGN_SECRET", "")
    if s:
        return s.encode("utf-8")
    p = os.path.join(DATA_DIR, "sign.key")
    if os.path.exists(p):
        try:
            with open(p, "rb") as f:
                v = f.read().strip()
            if v:
                return v
        except Exception:
            pass
    v = secrets.token_hex(32)
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(v)
        os.chmod(p, 0o600)
    except Exception:
        pass
    return v.encode("utf-8")


SECRET = _load_secret()


def sign(guid, exp):
    msg = ("%s|%d" % (guid, exp)).encode("utf-8")
    return hmac.new(SECRET, msg, hashlib.sha256).hexdigest()[:40]


def verify(guid, exp, sig):
    try:
        exp = int(exp)
    except Exception:
        return False
    if exp < int(time.time()):
        return False
    return hmac.compare_digest(sign(guid, exp), str(sig or ""))


# ---------------------------------------------------------------- 心情表
DEFAULT_MOODS = {
    "安静": ["轻音乐", "钢琴", "纯音乐"],
    "放松": ["轻音乐", "民谣", "chill"],
    "睡前": ["助眠", "轻音乐", "钢琴"],
    "开心": ["流行", "欢快", "周杰伦"],
    "伤感": ["情歌", "慢歌", "陈奕迅"],
    "运动": ["摇滚", "电子", "节奏"],
    "工作": ["纯音乐", "钢琴", "lofi"],
    "下雨": ["雨", "轻音乐", "民谣"],
}


def load_moods():
    table = dict(DEFAULT_MOODS)
    try:
        if os.path.exists(MOODS_FILE):
            with open(MOODS_FILE, "r", encoding="utf-8") as f:
                custom = json.load(f)
            if isinstance(custom, dict):
                for k, v in custom.items():
                    if isinstance(v, list):
                        table[str(k)] = [str(x) for x in v]
                    elif isinstance(v, str):
                        table[str(k)] = [v]
    except Exception as e:
        LOG.warning("load moods failed: %s", e)
    return table


MOODS = load_moods()


# ---------------------------------------------------------------- 工具函数
def _first(d, *keys, default=""):
    if not isinstance(d, dict):
        return default
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return default


def _to_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def clamp_limit(v, default=10, top=None):
    """把外部传进来的 limit 收敛到 [1, top]，非法值退回默认。"""
    return max(1, min(_to_int(v, default) or default, top or MAX_LIMIT))


def _rows(data):
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("list", "records", "items", "rows", "tracks", "data", "content"):
            v = data.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                r = _rows(v)
                if r:
                    return r
    return []


def norm_track(t):
    if not isinstance(t, dict):
        return {"guid": str(t), "title": str(t), "artist": "", "album": ""}
    guid = str(_first(t, "guid", "trackGuid", "trackGUID", "id"))
    title = str(_first(t, "title", "name", "trackName", "trackTitle", "fileName"))
    artist = _first(t, "artist", "artistName", "artists", "singer")
    if isinstance(artist, list):
        parts = []
        for a in artist:
            parts.append(str(a.get("name")) if isinstance(a, dict) else str(a))
        artist = "/".join(p for p in parts if p)
    album = _first(t, "album", "albumName", "albumTitle")
    if isinstance(album, dict):
        album = album.get("name", "")
    return {"guid": guid, "title": title, "artist": str(artist or ""), "album": str(album or "")}


def norm_playlist(p):
    if not isinstance(p, dict):
        return {"guid": str(p), "name": str(p), "count": 0}
    return {
        "guid": str(_first(p, "guid", "playlistGuid", "playlistGUID", "id")),
        "name": str(_first(p, "name", "title", "playlistName")),
        "count": _to_int(_first(p, "trackCount", "count", "songCount", default=0)),
    }


def fmt_tracks(tracks, limit=10):
    lines = []
    for i, t in enumerate(tracks[:limit], 1):
        a = (" - " + t["artist"]) if t.get("artist") else ""
        lines.append("%d. %s%s" % (i, t.get("title") or t.get("guid"), a))
    return "\n".join(lines) if lines else "（没有结果）"


def dedupe(tracks):
    seen = set()
    out = []
    for t in tracks:
        g = t.get("guid")
        if not g or g in seen:
            continue
        seen.add(g)
        out.append(t)
    return out


# ---------------------------------------------------------------- 状态
_state_lock = threading.Lock()


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(d):
    with _state_lock:
        try:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            LOG.warning("save state failed: %s", e)


# ---------------------------------------------------------------- 转码缓存
class Mp3Cache:
    def __init__(self, d, max_bytes):
        self.dir = d
        self.max = max_bytes
        os.makedirs(d, exist_ok=True)
        self._locks = {}
        self._guard = threading.Lock()
        self._cleanup_stale()

    def _cleanup_stale(self):
        """清掉上次进程被 kill 时留下的 .part / .src 半成品。"""
        try:
            for n in os.listdir(self.dir):
                if n.endswith(".part") or ".src" in n:
                    try:
                        os.remove(os.path.join(self.dir, n))
                    except OSError:
                        pass
        except Exception as e:
            LOG.warning("cache cleanup failed: %s", e)

    def path(self, guid):
        return os.path.join(self.dir, hashlib.sha1(guid.encode("utf-8")).hexdigest() + ".mp3")

    def _lock(self, guid):
        with self._guard:
            l = self._locks.get(guid)
            if l is None:
                l = threading.Lock()
                self._locks[guid] = l
            return l

    def ensure(self, guid, prov, timeout=180):
        dst = self.path(guid)
        if os.path.exists(dst) and os.path.getsize(dst) > 1024:
            return dst
        with self._lock(guid):
            if os.path.exists(dst) and os.path.getsize(dst) > 1024:
                return dst
            ctype, raw = prov.stream(guid)
            if not raw:
                raise RuntimeError("empty audio for %s" % guid)
            ctype = (ctype or "").lower()
            ext = ".flac" if "flac" in ctype else (".mp3" if "mpeg" in ctype else ".wav" if "wav" in ctype else ".bin")
            src = dst + ".src" + ext
            tmp = dst + ".part"     # 不以 .mp3 结尾：不会被当成缓存条目统计或淘汰
            try:
                with open(src, "wb") as f:
                    f.write(raw)
                cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-i", src,
                       "-vn", "-map_metadata", "-1",
                       "-ac", str(MP3_CHANNELS), "-ar", str(MP3_SR),
                       "-c:a", "libmp3lame", "-b:a", MP3_BITRATE, "-f", "mp3", tmp]
                with _TRANSCODE_SLOTS:
                    subprocess.run(cmd, check=True, timeout=timeout,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                if not os.path.exists(tmp) or os.path.getsize(tmp) < 1024:
                    raise RuntimeError("ffmpeg 转码失败: %s" % guid)
                os.replace(tmp, dst)
            finally:
                # 失败路径不留垃圾：.src 与 .part 一律清掉（成功时已被 replace/删除）
                for leftover in (src, tmp):
                    try:
                        os.remove(leftover)
                    except Exception:
                        pass
            self.evict()
            return dst

    def evict(self):
        try:
            items = []
            for n in os.listdir(self.dir):
                if not n.endswith(".mp3"):
                    continue
                fp = os.path.join(self.dir, n)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                items.append((st.st_atime, st.st_size, fp))
            total = sum(i[1] for i in items)
            if total <= self.max:
                return
            items.sort()
            for _, sz, fp in items:
                if total <= self.max * 0.9:
                    break
                try:
                    os.remove(fp)
                    total -= sz
                except Exception:
                    pass
            LOG.info("cache evicted to %.1f MiB", total / 1048576.0)
        except Exception as e:
            LOG.warning("cache evict failed: %s", e)


CACHE = Mp3Cache(CACHE_DIR, CACHE_MAX)


# ---------------------------------------------------------------- provider
class FnProvider:
    """飞牛音乐真实后端。"""

    def __init__(self):
        from fnmusic_client import FnMusicClient
        self.client = FnMusicClient(
            base=_env("FNMUSIC_BASE", _env("FNOS_BASE", "http://192.168.50.100:59820")),
            username=_env("FNMUSIC_USER", _env("FNOS_USER", "KID")),
            password=_env("FNMUSIC_PASS", _env("FNOS_PASS", "")) or None,
            device_id=_env("FNMUSIC_DEVICE_ID", _env("FNOS_DEVICE_ID", "")) or None,
            token_file=_env("FNMUSIC_TOKEN_FILE", os.path.join(DATA_DIR, "fnmusic_token.json")),
        )
        self.client.ensure_token()

    def search(self, q, limit=10):
        return dedupe(norm_track(t) for t in _rows(self.client.search_track(q, 1, limit)))

    def list_tracks(self, limit=20):
        return dedupe(norm_track(t) for t in _rows(self.client.track_list(1, limit)))

    def playlists(self):
        return [norm_playlist(p) for p in _rows(self.client.playlist_list(1, 200))]

    def playlist_tracks(self, guid, limit=200):
        return dedupe(norm_track(t) for t in _rows(self.client.playlist_tracks(guid, 1, limit)))

    def albums(self, limit=20):
        rows = _rows(self.client.album_list(1, limit))
        return [str(_first(r, "name", "title", default="")) for r in rows][:limit]

    def artists(self, limit=20):
        rows = _rows(self.client.artist_list(1, limit))
        return [str(_first(r, "name", "title", default="")) for r in rows][:limit]

    def favorites(self, limit=100):
        return dedupe(norm_track(t) for t in _rows(self.client.favorite_list(1, limit)))

    def stream(self, guid):
        return self.client.stream_bytes(guid)

    def add_favorite(self, guid):
        self.client.favorite_add(guid)

    def remove_favorite(self, guid):
        self.client.favorite_remove(guid)


class MockProvider:
    """离线自测用：不需要 NAS，音频是本地生成的 4 秒正弦波。"""

    TRACKS = [
        {"guid": "mock-1", "title": "晴天", "artist": "周杰伦", "album": "叶惠美"},
        {"guid": "mock-2", "title": "夜曲", "artist": "周杰伦", "album": "十一月的萧邦"},
        {"guid": "mock-3", "title": "富士山下", "artist": "陈奕迅", "album": "What's Going On"},
        {"guid": "mock-4", "title": "平凡之路", "artist": "朴树", "album": "猎户星座"},
        {"guid": "mock-5", "title": "River Flows in You", "artist": "Yiruma", "album": "First Love"},
        {"guid": "mock-6", "title": "晴天（钢琴版）", "artist": "纯音乐", "album": "钢琴曲集"},
    ]
    PLAYLISTS = [{"guid": "pl-1", "name": "睡前", "count": 2}, {"guid": "pl-2", "name": "通勤", "count": 2}]

    def _match(self, q, limit):
        q = (q or "").lower()
        return [t for t in self.TRACKS if q in t["title"].lower() or q in t["artist"].lower()][:limit]

    def search(self, q, limit=10):
        return self._match(q, limit)

    def list_tracks(self, limit=20):
        return self.TRACKS[:limit]

    def playlists(self):
        return list(self.PLAYLISTS)

    def playlist_tracks(self, guid, limit=200):
        if guid == "pl-1":
            return [self.TRACKS[4], self.TRACKS[5]]
        if guid == "pl-2":
            return [self.TRACKS[0], self.TRACKS[3]]
        return []

    def albums(self, limit=20):
        return ["叶惠美", "十一月的萧邦", "What's Going On", "猎户星座", "First Love"][:limit]

    def artists(self, limit=20):
        return ["周杰伦", "陈奕迅", "朴树", "Yiruma"][:limit]

    def favorites(self, limit=100):
        return [self.TRACKS[0], self.TRACKS[4]]

    def stream(self, guid):
        p = os.path.join(DATA_DIR, "mock", "tone.wav")
        if not os.path.exists(p):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-y", "-f", "lavfi",
                            "-i", "sine=frequency=440:duration=4", "-ar", "44100", p], check=True)
        with open(p, "rb") as f:
            return "audio/wav", f.read()

    def add_favorite(self, guid):
        return None

    def remove_favorite(self, guid):
        return None


_PROVIDER = None
_PROVIDER_ERR = None
_PROVIDER_LOCK = threading.Lock()


def provider():
    """惰性单例。并发首请求只登录一次（否则会重复登录/重复建连接）。"""
    global _PROVIDER, _PROVIDER_ERR
    p = _PROVIDER
    if p is not None:
        return p
    with _PROVIDER_LOCK:
        if _PROVIDER is None:
            try:
                _PROVIDER = MockProvider() if MOCK else FnProvider()
                _PROVIDER_ERR = None
            except Exception as e:
                _PROVIDER_ERR = str(e)
                raise
        return _PROVIDER


def provider_error():
    return _PROVIDER_ERR


# ---------------------------------------------------------------- 选曲
def resolve_playlist(p, name):
    key = (name or "").strip()
    if not key:
        return None
    pls = p.playlists()
    for pl in pls:
        if pl["name"] == key:
            return pl
    for pl in pls:
        if key in pl["name"] or pl["name"] in key:
            return pl
    return None


def mood_tracks(p, mood, limit=10):
    key = (mood or "").strip()
    kws = MOODS.get(key) or [key]
    out = []
    for kw in kws:
        try:
            out.extend(p.search(kw, limit))
        except Exception as e:
            LOG.warning("mood search %r failed: %s", kw, e)
    out = dedupe(out)
    random.shuffle(out)
    if not out:
        try:
            out = p.favorites(limit) or p.list_tracks(limit)
        except Exception:
            out = []
    return out[:limit]


def pick_tracks(p, query="", mood="", playlist="", limit=10):
    if playlist:
        pl = resolve_playlist(p, playlist)
        if not pl:
            raise BadRequest("没有找到歌单「%s」" % playlist)
        return p.playlist_tracks(pl["guid"], 200)[:limit], "歌单「%s」" % pl["name"]
    if mood:
        return mood_tracks(p, mood, limit), "心情「%s」" % mood
    if query:
        rows = p.search(query, limit)
        if not rows:
            raise BadRequest("没有搜到「%s」" % query)
        return rows, "搜索「%s」" % query
    raise BadRequest("需要 query / mood / playlist 之一")


def make_url(base, guid, ttl=None):
    exp = int(time.time()) + (ttl or URL_TTL)
    g = urllib.parse.quote(guid, safe="")
    return "%s/play/%s.mp3?exp=%d&sig=%s" % (base, g, exp, sign(guid, exp))


# ---------------------------------------------------------------- MCP 工具
TOOLS = [
    {
        "name": "music_search",
        "description": "在飞牛音乐库里搜歌。返回带序号的曲目列表。",
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
        "description": "拿一首歌的可播放直链（给设备 self.music.play_url 用）。query/mood/playlist 三选一，index 从 1 开始。",
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string"}, "mood": {"type": "string"},
            "playlist": {"type": "string"}, "index": {"type": "integer"}}, "required": []},
    },
    {
        "name": "music_favorite",
        "description": "收藏或取消收藏一首歌。action=add|remove，query 是歌名。",
        "inputSchema": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["add", "remove"]},
            "query": {"type": "string"}}, "required": ["action", "query"]},
    },
    {
        "name": "music_status",
        "description": "上一次播放的是什么。",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def call_tool(name, args, base):
    args = args or {}
    p = provider()
    limit = clamp_limit(args.get("limit"), 10)

    if name == "music_search":
        q = str(args.get("query") or "").strip()
        if not q:
            raise BadRequest("query 不能为空")
        rows = p.search(q, limit)
        return "搜索「%s」找到 %d 首：\n%s" % (q, len(rows), fmt_tracks(rows, limit))

    if name == "music_browse":
        cat = str(args.get("category") or "track")
        if cat not in ("track", "album", "artist", "playlist"):
            raise BadRequest("category 只能是 track / album / artist / playlist")
        if cat == "playlist":
            pls = p.playlists()
            return "歌单 %d 个：\n%s" % (len(pls), "\n".join(
                "%d. %s（%d 首）" % (i, x["name"], x["count"]) for i, x in enumerate(pls, 1)) or "（空）")
        if cat in ("album", "artist"):
            names = p.albums(limit) if cat == "album" else p.artists(limit)
            label = "专辑" if cat == "album" else "歌手"
            return "%s %d 个：\n%s" % (label, len(names), "\n".join(
                "%d. %s" % (i, n) for i, n in enumerate(names, 1)) or "（空）")
        rows = p.list_tracks(limit)
        return "全部歌曲（前 %d 首）：\n%s" % (len(rows), fmt_tracks(rows, limit))

    if name == "music_playlists":
        pls = p.playlists()
        return "我的歌单 %d 个：\n%s" % (len(pls), "\n".join(
            "%d. %s（%d 首）" % (i, x["name"], x["count"]) for i, x in enumerate(pls, 1)) or "（空）")

    if name == "music_playlist_tracks":
        pl = resolve_playlist(p, str(args.get("name") or ""))
        if not pl:
            raise BadRequest("没有找到歌单「%s」" % args.get("name"))
        rows = p.playlist_tracks(pl["guid"], 200)[:limit]
        return "歌单「%s」共 %d 首：\n%s" % (pl["name"], len(rows), fmt_tracks(rows, limit))

    if name == "music_favorites":
        rows = p.favorites(limit)
        return "收藏 %d 首：\n%s" % (len(rows), fmt_tracks(rows, limit))

    if name == "music_mood":
        mood = str(args.get("mood") or "").strip()
        if not mood:
            raise BadRequest("mood 不能为空")
        rows = mood_tracks(p, mood, limit)
        return "「%s」为你挑了 %d 首：\n%s" % (mood, len(rows), fmt_tracks(rows, limit))

    if name == "music_play":
        rows, label = pick_tracks(p, str(args.get("query") or ""), str(args.get("mood") or ""),
                                  str(args.get("playlist") or ""), 20)
        idx = _to_int(args.get("index"), 1)
        if idx < 1 or idx > len(rows):
            raise BadRequest("序号 %d 超出范围（共 %d 首）" % (idx, len(rows)))
        t = rows[idx - 1]
        if PREWARM:
            try:
                CACHE.ensure(t["guid"], p)
            except Exception as e:
                LOG.warning("prewarm %s failed: %s", t["guid"], e)
        url = make_url(base, t["guid"])
        save_state({"guid": t["guid"], "title": t["title"], "artist": t["artist"],
                    "at": int(time.time()), "url": url})
        return ("准备播放：%s%s\n%s 第 %d 首\n直链（%d 分钟内有效）：%s\n"
                "设备工具：self.music.play_url，参数 url 填上面的直链。" % (
                    t["title"], (" - " + t["artist"]) if t["artist"] else "", label, idx,
                    max(1, URL_TTL // 60), url))

    if name == "music_favorite":
        action = str(args.get("action") or "").lower()
        q = str(args.get("query") or "").strip()
        if not q:
            raise BadRequest("query 不能为空")
        rows = p.search(q, 5)
        if not rows:
            raise BadRequest("没找到「%s」" % q)
        t = rows[0]
        if action == "add":
            p.add_favorite(t["guid"])
            return "已收藏：%s" % t["title"]
        if action == "remove":
            p.remove_favorite(t["guid"])
            return "已取消收藏：%s" % t["title"]
        raise BadRequest("action 只能是 add 或 remove")

    if name == "music_status":
        st = load_state()
        if not st.get("title"):
            return "还没播放过。"
        at = _to_int(st.get("at"))
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at)) if at > 0 else "时间未知"
        return "上一次播放：%s%s（%s）" % (
            st.get("title"), (" - " + st["artist"]) if st.get("artist") else "", when)

    raise RuntimeError("未知工具：%s" % name)


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "fnmusic-gateway/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        LOG.info("%s %s", self.address_string(), fmt % args)

    def _json(self, obj, code=200, headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or []):
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _empty(self, code=202):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _base(self):
        if PUBLIC_BASE:
            return PUBLIC_BASE
        host = (self.headers.get("Host") or "").strip()
        # Host 会原样拼进返回给设备的直链，只放行 host[:port]
        if not _HOST_RE.match(host):
            host = "127.0.0.1:%d" % PORT
        return "http://" + host

    def _qs(self):
        u = urllib.parse.urlparse(self.path)
        return u, urllib.parse.parse_qs(u.query)

    def _qs_int(self, qs, key, default):
        raw = (qs.get(key) or [""])[0]
        if raw == "":
            return default
        try:
            return int(raw)
        except ValueError:
            raise BadRequest("参数 %s 必须是整数" % key)

    def _body(self, cap=HTTP_BODY_CAP):
        n = _to_int(self.headers.get("Content-Length"), 0)
        if n < 0:
            raise BadRequest("Content-Length 非法")
        if n > cap:
            raise BadRequest("请求体过大（上限 %d 字节）" % cap)
        raw = self.rfile.read(n) if n else b""
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            raise BadRequest("请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise BadRequest("请求体必须是 JSON 对象")
        return data

    def do_GET(self):
        try:
            u, qs = self._qs()
            path = u.path
            if path == "/healthz":
                # 存活探针：只证明进程活着，不碰 NAS（NAS 掉线时缓存仍能服务）
                return self._json({"ok": True, "version": VERSION, "mock": MOCK,
                                   "provider_error": provider_error(),
                                   "cache_mb": round(self._cache_mb(), 1),
                                   "moods": sorted(MOODS.keys())})
            if path == "/readyz":
                # 就绪探针：真访问一次曲库，NAS/账号异常回 503
                return self._handle_readyz()
            if path == "/play" or path.startswith("/play/"):
                return self._handle_play(u, qs)
            if path == "/api/search":
                q = (qs.get("q") or [""])[0].strip()
                if not q:
                    raise BadRequest("参数 q 不能为空")
                limit = clamp_limit(self._qs_int(qs, "limit", 10), 10)
                rows = provider().search(q, limit)
                return self._json({"query": q, "count": len(rows), "tracks": rows})
            if path == "/api/playlists":
                return self._json({"playlists": provider().playlists()})
            if path == "/api/playlist":
                name = (qs.get("name") or [""])[0]
                limit = clamp_limit(self._qs_int(qs, "limit", 200), 200, 500)
                p = provider()
                pl = resolve_playlist(p, name)
                if not pl:
                    return self._json({"error": "playlist not found: " + name}, 404)
                return self._json({"playlist": pl, "tracks": p.playlist_tracks(pl["guid"], limit)[:limit]})
            if path == "/api/favorites":
                limit = clamp_limit(self._qs_int(qs, "limit", 100), 100)
                return self._json({"tracks": provider().favorites(limit)})
            if path == "/api/mood":
                mood = (qs.get("mood") or [""])[0]
                limit = clamp_limit(self._qs_int(qs, "limit", 10), 10)
                rows = mood_tracks(provider(), mood, limit)
                return self._json({"mood": mood, "count": len(rows), "tracks": rows})
            if path == "/api/play":
                return self._handle_api_play(qs)
            if path == "/api/status":
                return self._json(load_state() or {"title": None})
            if path == "/mcp":
                return self._json({"error": "use POST /mcp (streamable-http)"}, 405,
                                  [("Allow", "POST, OPTIONS")])
            return self._json({"error": "not found", "path": path}, 404)
        except BadRequest as e:
            self._json({"error": str(e)}, 400)
        except BrokenPipeError:
            pass
        except Exception as e:
            LOG.exception("GET %s failed", self.path)
            try:
                self._json({"error": str(e)}, 500)
            except Exception:
                pass

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        try:
            u, qs = self._qs()
            if u.path == "/mcp":
                return self._handle_mcp()
            if u.path == "/api/favorite":
                body = self._body()
                p = provider()
                action = str(body.get("action") or "add").lower()
                if action not in ("add", "remove"):
                    raise BadRequest("action 只能是 add 或 remove")
                guid = str(body.get("guid") or "").strip()
                if not guid:
                    rows = p.search(str(body.get("query") or ""), 1)
                    if not rows:
                        return self._json({"error": "track not found"}, 404)
                    guid = rows[0]["guid"]
                if action == "add":
                    p.add_favorite(guid)
                else:
                    p.remove_favorite(guid)
                return self._json({"ok": True, "guid": guid, "action": action})
            if u.path == "/api/warm":
                body = self._body()
                guid = str(body.get("guid") or "").strip()
                if not guid:
                    raise BadRequest("guid 不能为空")
                path = CACHE.ensure(guid, provider())
                return self._json({"ok": True, "guid": guid, "bytes": os.path.getsize(path)})
            return self._json({"error": "not found", "path": u.path}, 404)
        except BadRequest as e:
            self._json({"error": str(e)}, 400)
        except BrokenPipeError:
            pass
        except Exception as e:
            LOG.exception("POST %s failed", self.path)
            try:
                self._json({"error": str(e)}, 500)
            except Exception:
                pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Allow", "GET, POST, HEAD, DELETE, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_DELETE(self):
        """MCP 客户端关闭会话时发 DELETE /mcp，回 204 即可（无状态，无需清理）。"""
        u, _ = self._qs()
        if u.path == "/mcp":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._json({"error": "not found", "path": u.path}, 404)

    def _handle_readyz(self):
        try:
            p = provider()
            rows = p.list_tracks(1)
            return self._json({"ok": True, "provider": "mock" if MOCK else "fnmusic",
                               "tracks_probe": len(rows)})
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 503)

    def _cache_mb(self):
        try:
            total = 0
            for n in os.listdir(CACHE_DIR):
                if n.endswith(".mp3"):
                    total += os.path.getsize(os.path.join(CACHE_DIR, n))
            return total / 1048576.0
        except Exception:
            return 0.0

    def _handle_api_play(self, qs):
        query = (qs.get("query") or qs.get("q") or [""])[0]
        mood = (qs.get("mood") or [""])[0]
        playlist = (qs.get("playlist") or [""])[0]
        index = self._qs_int(qs, "index", 1)
        p = provider()
        rows, label = pick_tracks(p, query, mood, playlist, 20)
        if index < 1 or index > len(rows):
            return self._json({"error": "index out of range", "count": len(rows)}, 400)
        t = rows[index - 1]
        warm = None
        if PREWARM:
            try:
                path = CACHE.ensure(t["guid"], p)
                warm = os.path.getsize(path)
            except Exception as e:
                LOG.warning("prewarm %s failed: %s", t["guid"], e)
                warm = None
        url = make_url(self._base(), t["guid"])
        save_state({"guid": t["guid"], "title": t["title"], "artist": t["artist"],
                    "at": int(time.time()), "url": url})
        return self._json({
            "guid": t["guid"], "title": t["title"], "artist": t["artist"], "album": t["album"],
            "url": url, "expires_in": URL_TTL, "label": label, "index": index,
            "count": len(rows), "warmed_bytes": warm,
            "device_tool": "self.music.play_url",
            "spoken": "%s%s" % (t["title"], ("，" + t["artist"]) if t["artist"] else ""),
        })

    def _handle_play(self, u, qs):
        guid = (qs.get("guid") or [""])[0]
        if not guid:
            path = u.path
            if path.startswith("/play/"):
                name = path[len("/play/"):]
                if name.endswith(".mp3"):
                    name = name[:-4]
                guid = urllib.parse.unquote(name)
        if not guid:
            return self._json({"error": "missing guid"}, 400)
        exp = (qs.get("exp") or [""])[0]
        sig = (qs.get("sig") or [""])[0]
        if not verify(guid, exp, sig):
            LOG.warning("bad signature for %s", guid)
            return self._json({"error": "invalid or expired signature"}, 403)
        try:
            path = CACHE.ensure(guid, provider())
        except Exception as e:
            LOG.exception("transcode %s failed", guid)
            return self._json({"error": "transcode failed: %s" % e}, 502)
        return self._serve_file(path)

    def _serve_file(self, path):
        try:
            size = os.path.getsize(path)
        except OSError:
            return self._json({"error": "not found"}, 404)
        if size <= 0:
            return self._json({"error": "empty audio file"}, 502)
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range") or ""
        if rng.startswith("bytes="):
            spec = rng[6:].split(",")[0].strip()
            a, _, b = spec.partition("-")
            try:
                if a == "" and b:
                    start = max(0, size - int(b))
                else:
                    start = int(a or 0)
                    if b:
                        end = min(size - 1, int(b))
            except ValueError:
                start, end = 0, size - 1
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            LOG.info("client aborted while streaming %s", path)

    def _handle_mcp(self):
        n = _to_int(self.headers.get("Content-Length"), 0)
        if n < 0 or n > MCP_BODY_CAP:
            return self._json({"jsonrpc": "2.0", "id": None,
                               "error": {"code": -32600, "message": "payload too large"}}, 413)
        raw = self.rfile.read(n) if n else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except Exception:
            return self._json({"jsonrpc": "2.0", "id": None,
                               "error": {"code": -32700, "message": "parse error"}}, 400)
        self._mcp_headers = []
        if isinstance(payload, list):
            out = []
            for m in payload:
                r = self._mcp_one(m)
                if r is not None:
                    out.append(r)
            if not out:
                return self._empty(202)
            return self._json(out, 200, self._mcp_headers)
        r = self._mcp_one(payload)
        if r is None:
            return self._empty(202)
        return self._json(r, 200, self._mcp_headers)

    def _mcp_one(self, msg):
        if not isinstance(msg, dict):
            return {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32600, "message": "invalid request"}}
        mid = msg.get("id")
        method = msg.get("method") or ""
        params = msg.get("params") or {}
        if mid is None and method:
            return None
        try:
            if method == "initialize":
                self._mcp_headers.append(("Mcp-Session-Id", uuid.uuid4().hex))
                return {"jsonrpc": "2.0", "id": mid, "result": {
                    "protocolVersion": params.get("protocolVersion") or "2025-03-26",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "fnmusic-gateway", "version": VERSION}}}
            if method == "ping":
                return {"jsonrpc": "2.0", "id": mid, "result": {}}
            if method == "tools/list":
                return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
            if method == "tools/call":
                name = params.get("name") or ""
                args = params.get("arguments") or {}
                try:
                    text = call_tool(name, args, self._base())
                    return {"jsonrpc": "2.0", "id": mid, "result": {
                        "content": [{"type": "text", "text": text}], "isError": False}}
                except Exception as e:
                    LOG.warning("tool %s failed: %s", name, e)
                    return {"jsonrpc": "2.0", "id": mid, "result": {
                        "content": [{"type": "text", "text": "调用失败：%s" % e}], "isError": True}}
            if method == "resources/list":
                return {"jsonrpc": "2.0", "id": mid, "result": {"resources": []}}
            if method == "prompts/list":
                return {"jsonrpc": "2.0", "id": mid, "result": {"prompts": []}}
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32601, "message": "method not found: %s" % method}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}


class GatewayServer(ThreadingHTTPServer):
    """默认 request_queue_size=5，多台设备同时拉流容易被拒；这里放宽到 64。"""

    daemon_threads = True
    request_queue_size = 64
    allow_reuse_address = True


def main():
    srv = GatewayServer((HOST, PORT), Handler)
    LOG.info("fnmusic-gateway %s on %s:%d (mock=%s, public_base=%s, ttl=%ds, cache=%dMiB)",
             VERSION, HOST, PORT, MOCK, PUBLIC_BASE or "auto", URL_TTL, CACHE_MAX // 1048576)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        LOG.info("bye")


if __name__ == "__main__":
    main()
