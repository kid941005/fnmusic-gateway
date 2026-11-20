#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fnmusic-gateway —— 在线音源 → 小智 的一层网关。

单端口出口（管理、MCP、播放、WebUI 全部走 GW_PORT）
--------------------------------------------------
1) MCP（streamable-http）  POST /mcp
   给服务端/云端 LLM 用：查歌单、收藏、按心情选曲、拿直链。
2) 签名直链               GET /play/<guid>.mp3?exp=<ts>&sig=<hmac>
   给设备 self.music.play_url 用：设备直拉，不经过小智服务端。

辅助 REST
--------
GET /healthz                                  健康检查
GET /api/search?q=&limit=                     搜歌（在线音源）
GET /api/sources                              在线音源健康状态
GET /api/playlists                            歌单列表（需本地曲库）
GET /api/playlist?name=&limit=                歌单曲目（需本地曲库）
GET /api/favorites                            收藏（需本地曲库）
GET /api/mood?mood=                           按心情选曲
GET /api/play?query=|mood=|playlist=[&index=] 取播放直链（会预热转码）
GET /api/status                               上一次播放

设计约束（实测自固件 v2.6.0）
---------------------------
- 设备只解 MP3；self.music.play_url 的 URL <= 2048 字节、仅 http/https、禁空白与 '@'
- 音源地址与密钥绝不进 URL：直链用 HMAC 短时签名（默认 15 分钟）
- 在线音源多为 FLAC/无损：首次点播用 ffmpeg 转 128k/44.1k 立体声 MP3 并落盘缓存（LRU）

依赖：Python 3.9+ 标准库 + ffmpeg 可执行文件；MCP 用官方 SDK 时需 mcp>=2.3。
"""
import difflib
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
import unicodedata
import urllib.parse
import urllib.request
import uuid
import http.client
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from gwlib import __version__ as VERSION
from gwlib.errors import BadRequest, GatewayError, as_payload
from gwlib.quality import DEFAULT_QUALITY, describe as describe_quality, normalize_quality
from gwlib.storage import LibraryStore, MediaCache, audio_seconds
from gwlib.sources import (FallbackProvider, is_ext_guid,
                           parse_spec as _parse_spec, fallback_services as _fallback_services,
                           source_status)
from gwlib.playback import PlaybackService
from gwlib.mcp_tools import build_tools
from gwlib.mcp_server import McpHttpBridge, McpService

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
    if v is None or not v.strip():
        return d
    return v.strip().lower() in ("1", "true", "yes", "on")


DATA_DIR = _env("GW_DATA", os.path.join(BASE_DIR, "data"))
os.makedirs(DATA_DIR, exist_ok=True)

HOST = _env("GW_HOST", "0.0.0.0")
PORT = _env_int("GW_PORT", 8766)
# 单端口：只监听 GW_PORT。PUBLIC_BASE 只影响返回给设备的直链前缀，不再额外开监听端口。
PUBLIC_BASE = (_env("GW_PUBLIC_BASE", "") or "").rstrip("/")
# WebUI 反向代理：留空则关闭；compose 内填 http://fnmusic-sources:8004
WEBUI_URL = (_env("GW_WEBUI_URL", "") or "").rstrip("/")
WEBUI_ENABLED = bool(WEBUI_URL)
WEBUI_PREFIX = "/app/fnmusic-ext"
WEBUI_TIMEOUT = max(3, _env_int("GW_WEBUI_TIMEOUT", 30))
WEBUI_BODY_CAP = max(1024 * 1024, _env_int("GW_WEBUI_BODY_CAP", 32 * 1024 * 1024))
WEBUI_COOKIE = "fnmusic_admin"
URL_TTL = _env_int("GW_URL_TTL", 900)
CACHE_DIR = _env("GW_CACHE_DIR", os.path.join(DATA_DIR, "cache"))
CACHE_MAX = _env_int("GW_CACHE_MAX_BYTES", 8 * 1024 ** 3)
MP3_BITRATE = _env("GW_MP3_BITRATE", "128k")
MP3_SR = _env_int("GW_MP3_SR", 44100)
MP3_CHANNELS = _env_int("GW_MP3_CHANNELS", 2)
PREWARM = _env_bool("GW_PREWARM", True)
MOCK = _env_bool("GW_MOCK", False)
MOODS_FILE = _env("GW_MOODS_FILE", os.path.join(DATA_DIR, "moods.json"))
STATE_FILE = os.path.join(DATA_DIR, "state.json")
MAX_LIMIT = _env_int("GW_MAX_LIMIT", 50)
HTTP_BODY_CAP = 1024 * 1024
MCP_BODY_CAP = 4 * 1024 * 1024
MCP_TIMEOUT = max(30, _env_int("GW_MCP_TIMEOUT", 300))
# 管理令牌：为空时不鉴权（兼容旧部署）。单端口模式下强烈建议设置。
# 带令牌后，/play/ 与 /lyrics/ 之外的接口都必须带 X-Admin-Token 或 Authorization: Bearer。
ADMIN_TOKEN = (_env("GW_ADMIN_TOKEN", "") or "").strip()
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+(:\d{1,5})?$")
TRANSCODE_CONCURRENCY = max(1, _env_int("GW_TRANSCODE_CONCURRENCY", 2))

# 歌词（设备端 self.music.play_url 的 lyric_url 会自己去拉 LRC）
LYRICS_ENABLED = _env_bool("GW_LYRICS", True)
LYRICS_DIR = _env("GW_LYRICS_DIR", os.path.join(DATA_DIR, "lyrics"))
LRCLIB_URL = (_env("GW_LRCLIB_URL", "https://lrclib.net/api/get") or "").rstrip("/")
LRCLIB_TIMEOUT = max(1, _env_int("GW_LRCLIB_TIMEOUT", 6))
LYRICS_RETRY = max(60, _env_int("GW_LYRICS_RETRY", 86400))
LYRIC_MAX_BYTES = 64 * 1024
os.makedirs(LYRICS_DIR, exist_ok=True)

# 在线音源（fnos_music_ext 音源容器）。格式：逗号分隔，每项 "type@http://host:port"
# 或只写 URL（按端口猜类型：8768 musicdl / 8770 musicbox / 8772 lxmusic / 8776 neteasefree）。
# 网关不登录任何音乐账号，只通过这些音源搜索/播放/下载；为空则启动后请求会明确报错。
FALLBACK_URLS = [x.strip() for x in (_env("GW_FALLBACK_URLS", "") or "").split(",") if x.strip()]
FALLBACK_TIMEOUT = max(1, _env_int("GW_FALLBACK_TIMEOUT", 8))
FALLBACK_SEARCH_LIMIT = max(1, _env_int("GW_FALLBACK_SEARCH_LIMIT", 10))
FALLBACK_TOTAL_LIMIT = max(1, _env_int("GW_FALLBACK_TOTAL_LIMIT", MAX_LIMIT))
FALLBACK_TOTAL_TIMEOUT = max(1, _env_int("GW_FALLBACK_TOTAL_TIMEOUT", 15))
DOWNLOAD_DIR = _env("GW_DOWNLOAD_DIR", "")
PREVIEW_MIN_SECONDS = max(1, _env_int("GW_PREVIEW_MIN_SECONDS", 35))

# 音质与存储策略
QUALITY_DEFAULT = normalize_quality(_env("GW_QUALITY_DEFAULT", DEFAULT_QUALITY))
TEE_SAVE = _env_bool("GW_TEE_SAVE", True)
SOURCE_SEARCH_PARALLEL = _env_bool("GW_SOURCES_SEARCH_PARALLEL", True)
LIBRARY_MAX_BYTES = _env_int("GW_LIBRARY_MAX_BYTES", 0)

# 搜索：别名纠错 + 相关度打分（中文 ASR 常把歌名/歌手听成同音字）
SMART_SEARCH = _env_bool("GW_SMART_SEARCH", True)
ALIASES_FILE = _env("GW_ALIASES_FILE", os.path.join(DATA_DIR, "music_aliases.json"))


def _log_level(name):
    return getattr(logging, str(name or "INFO").strip().upper(), logging.INFO)


logging.basicConfig(level=_log_level(_env("GW_LOG_LEVEL", "INFO")),
                    format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("fnmusic-gw")

# 同时最多几个 ffmpeg：防止多台设备同时点播把宿主机 CPU 打满
_TRANSCODE_SLOTS = threading.BoundedSemaphore(TRANSCODE_CONCURRENCY)


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


# ---------------------------------------------------------------- 搜索排序
# 中文 ASR 常把歌名/歌手听成同音字：别名表把「青花磁」纠正成「青花瓷」，再搜索并打分排序。
# 文件格式 {"青花磁": "青花瓷", "周杰轮": "周杰伦"}；键值都按 _norm_text 归一化后匹配。
_PUNCT_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")
_ALIAS_CACHE = {"stamp": None, "table": {}}


def _norm_text(s):
    """归一化：全角转半角、大小写折叠、去掉空格/标点/书名号等。"""
    if not s:
        return ""
    try:
        s = unicodedata.normalize("NFKC", str(s))
    except Exception:
        s = str(s)
    return _PUNCT_RE.sub("", s.casefold())


def aliases():
    """读取别名表；文件 mtime/size 变化时自动重载，坏文件退回空表。"""
    if not ALIASES_FILE:
        return {}
    try:
        st = os.stat(ALIASES_FILE)
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        _ALIAS_CACHE.update(stamp=None, table={})
        return {}
    if _ALIAS_CACHE["stamp"] == stamp:
        return _ALIAS_CACHE["table"]
    table = {}
    try:
        with open(ALIASES_FILE, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            src = raw.get("aliases") if isinstance(raw.get("aliases"), dict) else raw
            for k, v in src.items():
                nk, nv = _norm_text(k), _norm_text(v)
                if nk and nv and not nk.startswith("_"):
                    table[nk] = nv
        LOG.info("loaded %d alias(es) from %s", len(table), ALIASES_FILE)
    except Exception as e:
        LOG.warning("load aliases failed: %s", e)
    _ALIAS_CACHE.update(stamp=stamp, table=table)
    return table


def apply_aliases(text):
    """最长匹配替换别名，返回 (纠正后的文本, 是否改动)。"""
    t = _norm_text(text)
    table = aliases()
    if not t or not table:
        return t, False
    changed = False
    for key in sorted(table, key=len, reverse=True):
        if key in t:
            t = t.replace(key, table[key])
            changed = True
    return t, changed


# 版本词：用户没提这些词时，带这些词的候选要降权（翻唱/伴奏/钢琴版…）
_VERSION_WORDS = ("伴奏", "纯音乐", "钢琴版", "吉他版", "翻唱", "dj版", "remix",
                  "instrumental", "karaoke", "live", "现场", "女声版", "男声版",
                  "加速版", "慢速版", "八音盒", "cover", "片段")


def score_track(query, track):
    """给候选打分：精确 > 前缀 > 包含 > 相似度；歌手命中加分，多版本降权。"""
    q, _ = apply_aliases(query)
    if not q:
        return 0.0
    title, _ = apply_aliases(track.get("title") or "")
    artist, _ = apply_aliases(track.get("artist") or "")
    fields = [x for x in (title,
                          " ".join(y for y in (artist, title) if y),
                          " ".join(y for y in (title, artist) if y)) if x]
    best = 0.0
    for cand in fields:
        if cand == q:
            best = max(best, 100.0)
        elif cand.startswith(q):
            best = max(best, 88.0)
        elif q in cand:
            best = max(best, 78.0 - min(12.0, (len(cand) - len(q)) * 1.5))
        else:
            best = max(best, difflib.SequenceMatcher(None, q, cand).ratio() * 60.0)
    if artist and (artist == q or q in artist):
        best += 12.0
    if not any(w in q for w in _VERSION_WORDS):
        for w in _VERSION_WORDS:
            if w in title:
                best -= 15.0
                break
    if title and len(title) <= len(q) + 4:
        best += 3.0
    return round(best, 3)


def rank_tracks(query, rows, limit=10):
    """按打分降序；同分时短标题、稳定 guid 优先。"""
    scored = [(score_track(query, t), t) for t in rows]
    scored.sort(key=lambda x: (-x[0], len(x[1].get("title") or ""), str(x[1].get("guid") or "")))
    return [t for _, t in scored][:limit]


def search_tracks(p, query, limit=10):
    """搜索 + 别名纠正 + 打分排序；别名生效时同时用原词兜底（防别名表写错）。"""
    q = (query or "").strip()
    if not q:
        return [], ""
    fixed, changed = apply_aliases(q)
    raw = []
    try:
        raw.extend(p.search(fixed, max(limit * 3, 20)))
    except Exception as e:
        LOG.warning("search %r failed: %s", fixed, e)
    if changed:
        try:
            raw.extend(p.search(q, max(limit, 10)))
        except Exception as e:
            LOG.warning("search %r failed: %s", q, e)
    return rank_tracks(fixed, dedupe(raw), limit), fixed


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
CACHE = MediaCache(CACHE_DIR, CACHE_MAX, LOG, transcode_slots=_TRANSCODE_SLOTS)
LIBRARY = LibraryStore(lambda: DOWNLOAD_DIR, LOG, max_bytes=LIBRARY_MAX_BYTES)


# ---------------------------------------------------------------- provider
class MockProvider:
    """离线自测用：不需要任何音源，音频是本地生成的 4 秒正弦波。"""

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
    """惰性单例。纯在线音源模式：不登录任何音乐账号，只连 GW_FALLBACK_URLS。"""
    global _PROVIDER, _PROVIDER_ERR
    p = _PROVIDER
    if p is not None:
        return p
    with _PROVIDER_LOCK:
        if _PROVIDER is None:
            try:
                if MOCK:
                    _PROVIDER = MockProvider()
                elif FALLBACK_URLS:
                    _PROVIDER = FallbackProvider(
                        None, FALLBACK_URLS, timeout=FALLBACK_TIMEOUT,
                        search_limit=FALLBACK_SEARCH_LIMIT,
                        download_dir_getter=lambda: DOWNLOAD_DIR,
                        max_bytes=LIBRARY_MAX_BYTES,
                        parallel=SOURCE_SEARCH_PARALLEL,
                        total_limit=FALLBACK_TOTAL_LIMIT,
                        total_timeout=FALLBACK_TOTAL_TIMEOUT)
                    LOG.info("online sources enabled: %s", ", ".join(fallback_services()))
                else:
                    raise RuntimeError(
                        "未配置在线音源：请设置 GW_FALLBACK_URLS"
                        "（如 musicdl@http://fnmusic-sources:8001）")
                _PROVIDER_ERR = None
            except Exception as e:
                _PROVIDER_ERR = str(e)
                raise
        return _PROVIDER


def provider_error():
    return _PROVIDER_ERR


class _LazyProvider:
    """按需构造 provider，避免 music_sources/music_storage/music_status 依赖音源可用性。"""

    def __init__(self):
        self._p = None

    def _get(self):
        if self._p is None:
            self._p = provider()
        return self._p

    def __getattr__(self, name):
        return getattr(self._get(), name)


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
        if SMART_SEARCH:
            rows, fixed = search_tracks(p, query, limit)
        else:
            rows, fixed = p.search(query, limit), ""
        if not rows:
            raise BadRequest("没有搜到「%s」" % query)
        if fixed and fixed != _norm_text(query):
            return rows, "搜索「%s」（已纠正为「%s」）" % (query, fixed)
        return rows, "搜索「%s」" % query
    raise BadRequest("需要 query / mood / playlist 之一")


def make_url(base, guid, ttl=None, quality=None):
    exp = int(time.time()) + (ttl or URL_TTL)
    g = urllib.parse.quote(guid, safe="")
    q = normalize_quality(quality or QUALITY_DEFAULT)
    return "%s/play/%s.mp3?exp=%d&sig=%s&q=%s" % (
        base, g, exp, sign(guid, exp), urllib.parse.quote(q, safe=""))


def make_lyric_url(base, guid, ttl=None):
    exp = int(time.time()) + (ttl or URL_TTL)
    g = urllib.parse.quote(guid, safe="")
    return "%s/lyrics/%s.lrc?exp=%d&sig=%s" % (base, g, exp, sign(guid, exp))


def lyric_cache_path(guid):
    return os.path.join(LYRICS_DIR, urllib.parse.quote(guid, safe="") + ".lrc")


def _http_json(url, timeout):
    req = urllib.request.Request(url, headers={
        "User-Agent": "fnmusic-gateway/" + VERSION,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read(2 * 1024 * 1024).decode("utf-8", "replace"))


def plain_to_lrc(text, step=4):
    """把无时间轴的纯文本歌词按固定间隔补上 LRC 时间戳（固件只认 [mm:ss.xx] 行）。"""
    out, ts = [], 0
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        mm, ss = divmod(ts, 60)
        out.append("[%02d:%02d.00]%s" % (mm, ss, line))
        ts += step
    return "\n".join(out)


def fetch_lyrics(title, artist):
    """LRCLIB 查歌词：优先 syncedLyrics，退化为 plainLyrics 补时间戳；查不到返回空串。"""
    if not LYRICS_ENABLED or not title or not LRCLIB_URL:
        return ""
    title = str(title).strip()
    artist = str(artist or "").strip()
    data = None
    try:
        q = {"track_name": title, "artist_name": artist}
        data = _http_json(LRCLIB_URL + "?" + urllib.parse.urlencode(q), LRCLIB_TIMEOUT)
    except Exception as e:
        LOG.info("lyrics get %s / %s failed: %s", title, artist, e)
        data = None
    if not isinstance(data, dict):
        try:
            q = {"track_name": title}
            if artist:
                q["artist_name"] = artist
            rows = _http_json(LRCLIB_URL.replace("/get", "/search") + "?" +
                              urllib.parse.urlencode(q), LRCLIB_TIMEOUT)
            data = rows[0] if isinstance(rows, list) and rows else None
        except Exception as e:
            LOG.info("lyrics search %s / %s failed: %s", title, artist, e)
            return ""
    if not isinstance(data, dict):
        return ""
    synced = str(data.get("syncedLyrics") or "").strip()
    if synced:
        return synced[:LYRIC_MAX_BYTES]
    plain = str(data.get("plainLyrics") or "").strip()
    return plain_to_lrc(plain)[:LYRIC_MAX_BYTES] if plain else ""


def _as_lrc(text):
    """统一成带时间戳的 LRC：已有 [mm:ss] 时间标签直接用，否则按固定间隔补。"""
    t = (text or "").strip()
    if not t:
        return ""
    for line in t.splitlines():
        s = line.strip()
        if s.startswith("[") and "]" in s and ":" in s[1:10]:
            return t
    return plain_to_lrc(t)


def lyrics_for(track):
    """取（必要时抓取并缓存）一首歌的 LRC 文件路径；空文件表示查不到。"""
    if not LYRICS_ENABLED:
        return ""
    guid = str(track.get("guid") or "")
    if not guid:
        return ""
    path = lyric_cache_path(guid)
    try:
        st = os.stat(path)
        if st.st_size > 0 or (time.time() - st.st_mtime) < LYRICS_RETRY:
            return path
    except OSError:
        pass
    text = ""
    if is_ext_guid(guid):
        try:
            p = provider()
            text = p.lyrics_text(guid) if hasattr(p, "lyrics_text") else ""
        except Exception:
            text = ""
        text = _as_lrc(text)
    if not text.strip():
        text = fetch_lyrics(track.get("title"), track.get("artist"))
    tmp = path + ".part"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fp:
            fp.write(text)
        os.replace(tmp, path)
    except OSError as e:
        LOG.warning("write lyrics %s failed: %s", guid, e)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return ""
    LOG.info("lyrics for %s: %d bytes", guid, len(text))
    return path


def lyric_url_for(base, track, ttl=None):
    """有歌词才返回签名直链；没有则返回空串（设备页面只显示歌名/歌手）。"""
    path = lyrics_for(track)
    if not path:
        return ""
    try:
        if os.path.getsize(path) <= 0:
            return ""
    except OSError:
        return ""
    return make_lyric_url(base, track["guid"], ttl)


PLAYBACK = PlaybackService(CACHE, LIBRARY, make_url, lyric_url_for, audio_seconds,
                           save_state, default_quality=QUALITY_DEFAULT, ttl=URL_TTL,
                           prewarm=PREWARM, preview_min_seconds=PREVIEW_MIN_SECONDS,
                           tee_save=TEE_SAVE)


# ---------------------------------------------------------------- MCP 工具
# 在线音源兼容别名（历史测试/脚本仍可使用这些名字）
EXT_PREFIX = "ext:"


def _spec_svc(spec):
    return _parse_spec(spec)[0]


def fallback_services():
    return _fallback_services(FALLBACK_URLS)


TOOLS = build_tools()


def call_tool(name, args, base):
    args = args or {}
    p = _LazyProvider()
    limit = clamp_limit(args.get("limit"), 10)
    quality = args.get("quality")

    if name == "music_search":
        q = str(args.get("query") or "").strip()
        if not q:
            raise BadRequest("query 不能为空")
        if SMART_SEARCH:
            rows, fixed = search_tracks(p, q, limit)
        else:
            rows, fixed = p.search(q, limit), ""
        tail = ("（已按别名纠正为「%s」）" % fixed) if fixed and fixed != _norm_text(q) else ""
        return "搜索「%s」%s找到 %d 首：\n%s" % (q, tail, len(rows), fmt_tracks(rows, limit))

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
        info = PLAYBACK.prepare(rows[idx - 1], p, base, quality=quality)
        src_line = ("\n来源：在线音源 %s" % info["source"]) if info["source"] != "local" else ""
        prev_line = ""
        if info["preview"]:
            prev_line = ("\n注意：在线音源只给到试听片段（约 %d 秒）；说「下载」可保存完整版到本地曲库。" % int(info["duration"] or 0))
        save_line = "，正在后台保存到本地曲库" if info["saving"] else ""
        return ("准备播放：%s%s\n%s 第 %d 首，音质 %s（%s kbps）%s%s\n直链（%d 分钟内有效）：%s\n"
                "歌词直链：%s\n"
                "设备工具：self.music.play_url，参数 url 填上面的直链，title 填「%s」，"
                "artist 填「%s」，lyric_url 填上面的歌词直链（没有歌词就留空）。" % (
                    info["title"], (" - " + info["artist"]) if info["artist"] else "", label, idx,
                    info["quality_label"], info["bitrate"], src_line, prev_line + save_line,
                    max(1, (info.get("expires_in") or URL_TTL) // 60), info["url"],
                    info["lyric_url"] or "（这首歌查不到歌词，留空即可）",
                    info["title"], info["artist"] or ""))

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
            if is_ext_guid(t["guid"]) and hasattr(p, "download_ext"):
                fp, n = p.download_ext(t, quality or "lossless")
                return "已收藏并下载到本地曲库：%s（%s，%.1f MB）" % (
                    t["title"], fp, n / 1048576.0)
            p.add_favorite(t["guid"])
            return "已收藏：%s" % t["title"]
        if action == "remove":
            p.remove_favorite(t["guid"])
            return "已取消收藏：%s" % t["title"]
        raise BadRequest("action 只能是 add 或 remove")

    if name == "music_download":
        guid = str(args.get("guid") or "").strip()
        q = str(args.get("query") or "").strip()
        if not LIBRARY.enabled():
            raise BadRequest("未配置 GW_DOWNLOAD_DIR，无法保存到本地")
        if guid:
            st = load_state()
            track = {"guid": guid, "title": (st.get("title") if st.get("guid") == guid else "") or guid,
                     "artist": (st.get("artist") if st.get("guid") == guid else "") or "",
                     "source": "ext"}
        elif q:
            rows = p.search(q, 5)
            if not rows:
                raise BadRequest("没找到「%s」" % q)
            track = rows[0]
        else:
            raise BadRequest("guid 或 query 至少给一个")
        fp, n = LIBRARY.save_from_provider(p, track, quality or DEFAULT_QUALITY)
        return "已保存到本地曲库：%s（%s，%.1f MB）" % (track.get("title") or track["guid"], fp, n / 1048576.0)

    if name == "music_sources":
        if not FALLBACK_URLS:
            return "没有配置在线音源（GW_FALLBACK_URLS 为空）。网关不登录音乐账号，请先配置音源地址。"
        rows = source_status(FALLBACK_URLS)
        lines = ["在线音源 %d 个：" % len(rows)]
        for item in rows:
            lines.append("%s（%s）%s" % (item.get("source"), item.get("url"),
                                        " 正常" if item.get("healthy") else " 不可用"))
        return "\n".join(lines)

    if name == "music_storage":
        c = CACHE.stats()
        lib = LIBRARY.stats()
        return ("播放缓存：%d 个文件，%.1f MB（上限 %.1f MB）\n本地曲库：%s，%d 个文件，%.1f MB" % (
            c.get("files", 0), c.get("bytes", 0) / 1048576.0,
            c.get("limit_bytes", 0) / 1048576.0,
            lib.get("dir") or "未配置", lib.get("files", 0), lib.get("bytes", 0) / 1048576.0))

    if name == "music_status":
        st = load_state()
        if not st.get("title"):
            return "还没播放过。"
        at = _to_int(st.get("at"))
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at)) if at > 0 else "时间未知"
        qn = ("（音质 %s）" % st.get("quality")) if st.get("quality") else ""
        return "上一次播放：%s%s%s（%s）" % (
            st.get("title"), (" - " + st["artist"]) if st.get("artist") else "", qn, when)

    raise RuntimeError("未知工具：%s" % name)


# 官方 MCPServer 2.3：工具注册/Schema/校验/错误包装由官方库负责；
# 对外仍走 8766 的 /mcp，官方 ASGI 应用只监听 127.0.0.1 随机端口。
MCP_SERVICE = McpService(call_tool, VERSION,
                         lambda: PUBLIC_BASE or ("http://127.0.0.1:%d" % PORT), logger=LOG)
MCP_BRIDGE = McpHttpBridge(MCP_SERVICE, timeout=MCP_TIMEOUT, logger=LOG)


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

    def _raw(self, body, code=200, content_type="application/octet-stream", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
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

    def _admin_cookie_ok(self):
        raw = self.headers.get("Cookie") or ""
        if not raw:
            return False
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except Exception:
            return False
        morsel = jar.get(WEBUI_COOKIE)
        return bool(morsel) and hmac.compare_digest(morsel.value, ADMIN_TOKEN)

    def _admin_cookie_header(self):
        jar = SimpleCookie()
        jar[WEBUI_COOKIE] = ADMIN_TOKEN
        morsel = jar[WEBUI_COOKIE]
        morsel["path"] = "/"
        morsel["httponly"] = True
        morsel["samesite"] = "Lax"
        if (self.headers.get("X-Forwarded-Proto") or "").lower() == "https":
            morsel["secure"] = True
        return ("Set-Cookie", morsel.OutputString())

    def _admin_ok(self, path):
        """管理面鉴权：未配置令牌时保持旧行为；/play/ 与 /lyrics/ 永远放行。

        浏览器访问 WebUI 时无法带自定义 Header，因此额外接受登录 Cookie；
        Cookie 由首次访问 ?token=<GW_ADMIN_TOKEN> 时下发。
        """
        if not ADMIN_TOKEN:
            return True
        if (path in ("/", "/healthz") or path == "/play" or path.startswith("/play/")
                or path == "/lyrics" or path.startswith("/lyrics/")):
            return True
        return self._admin_credential_ok()

    def _admin_credential_ok(self):
        """只校验凭据本身（Header / Bearer / Cookie），不看路径是否公开。"""
        if not ADMIN_TOKEN:
            return True
        supplied = (self.headers.get("X-Admin-Token") or "").strip()
        auth = (self.headers.get("Authorization") or "").strip()
        if not supplied and auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        if supplied and hmac.compare_digest(supplied, ADMIN_TOKEN):
            return True
        return self._admin_cookie_ok()

    def _unauthorized(self):
        return self._json(
            {"error": {"code": "unauthorized", "message": "需要管理令牌"}},
            401, [("WWW-Authenticate", 'Bearer realm="fnmusic-gateway"')])

    def _redirect(self, location, headers=None):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for k, v in (headers or []):
            self.send_header(k, v)
        self.end_headers()

    @staticmethod
    def _strip_token(u):
        pairs = [(k, v) for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True)
                 if k != "token"]
        q = urllib.parse.urlencode(pairs)
        return u.path + (("?" + q) if q else "")

    def _handle_root(self, u, qs):
        if not WEBUI_ENABLED:
            return self._json({"service": "fnmusic-gateway", "version": VERSION,
                               "webui": False, "mcp": "/mcp"})
        token = (qs.get("token") or [""])[0]
        if token and ADMIN_TOKEN:
            if not hmac.compare_digest(token, ADMIN_TOKEN):
                return self._unauthorized()
            return self._redirect("/", [self._admin_cookie_header()])
        if not self._admin_credential_ok():
            return self._unauthorized()
        return self._proxy_webui("/")

    def _handle_webui_get(self, u, qs):
        if u.path == WEBUI_PREFIX:
            q = ("?" + u.query) if u.query else ""
            return self._redirect(WEBUI_PREFIX + "/" + q)
        token = (qs.get("token") or [""])[0]
        if token and ADMIN_TOKEN:
            if not hmac.compare_digest(token, ADMIN_TOKEN):
                return self._unauthorized()
            return self._redirect(self._strip_token(u), [self._admin_cookie_header()])
        if not self._admin_ok(u.path):
            return self._unauthorized()
        return self._proxy_webui()

    def _proxy_webui(self, upstream_path=None):
        if not WEBUI_ENABLED:
            return self._json({"error": {"code": "webui_disabled", "message": "未配置 WebUI"}}, 404)
        target = urllib.parse.urlparse(WEBUI_URL)
        if target.scheme not in ("http", "https") or not target.hostname:
            return self._json({"error": {"code": "webui_bad_url", "message": "GW_WEBUI_URL 非法"}}, 503)
        n = _to_int(self.headers.get("Content-Length"), 0)
        if n < 0 or n > WEBUI_BODY_CAP:
            return self._json({"error": {"code": "payload_too_large", "message": "请求体过大"}}, 413)
        body = self.rfile.read(n) if n else b""
        req_path = upstream_path if upstream_path is not None else self.path
        upath = (target.path.rstrip("/") + req_path) or "/"
        conn = None
        try:
            port = target.port or (443 if target.scheme == "https" else 80)
            cls = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
            conn = cls(target.hostname, port, timeout=WEBUI_TIMEOUT)
            hdrs = {}
            skip = {"host", "content-length", "x-trim-isadmin", "connection",
                    "proxy-connection", "transfer-encoding", "upgrade"}
            for key, value in self.headers.items():
                if key.lower() not in skip:
                    hdrs[key] = value
            hdrs["X-Trim-IsAdmin"] = "true"
            fwd_proto = (self.headers.get("X-Forwarded-Proto") or "http").strip()
            hdrs["X-Forwarded-Proto"] = fwd_proto
            if self.headers.get("Host"):
                hdrs["X-Forwarded-Host"] = self.headers.get("Host")
            if self.headers.get("X-Forwarded-For"):
                hdrs["X-Forwarded-For"] = self.headers.get("X-Forwarded-For") + ", " + self.client_address[0]
            else:
                hdrs["X-Forwarded-For"] = self.client_address[0]
            if self.command not in ("GET", "HEAD"):
                hdrs["Content-Length"] = str(len(body))
            conn.request(self.command, upath, body=body if self.command not in ("GET", "HEAD") else None,
                         headers=hdrs)
            resp = conn.getresponse()
            data = resp.read()
            ctype = resp.getheader("Content-Type") or "application/octet-stream"
            out = []
            for key, value in resp.getheaders():
                lower = key.lower()
                if lower in ("content-type", "content-length", "transfer-encoding",
                             "connection", "date", "server"):
                    continue
                if lower == "location" and WEBUI_URL and value.startswith(WEBUI_URL):
                    value = WEBUI_PREFIX + value[len(WEBUI_URL):]
                out.append((key, value))
            return self._raw(data, resp.status, ctype, out)
        except Exception as exc:
            LOG.warning("WebUI 反代失败：%s", exc)
            return self._json({"error": {"code": "webui_unavailable", "message": "WebUI 不可用"}}, 502)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

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
            if path == "/":
                return self._handle_root(u, qs)
            if path == WEBUI_PREFIX or path.startswith(WEBUI_PREFIX + "/"):
                return self._handle_webui_get(u, qs)
            if not self._admin_ok(path):
                return self._unauthorized()
            if path == "/healthz":
                # 存活探针：只证明进程活着，不探音源（音源掉线时缓存仍能服务）
                return self._json({"ok": True, "version": VERSION,
                                   "mode": "mock" if MOCK else "online",
                                   "mock": MOCK,
                                   "provider_error": provider_error(),
                                   "fallback": fallback_services(),
                                   "quality_default": QUALITY_DEFAULT,
                                   "quality_profiles": describe_quality(),
                                   "mcp_server": {"available": MCP_SERVICE.available,
                                                  "engine": "mcp>=2.3",
                                                  "error": str(MCP_SERVICE.import_error) if MCP_SERVICE.import_error else None},
                                   "auth_required": bool(ADMIN_TOKEN),
                                   "webui": {"enabled": WEBUI_ENABLED},
                                   "cache_mb": round(self._cache_mb(), 1),
                                   "moods": sorted(MOODS.keys())})
            if path == "/readyz":
                # 就绪探针：真探一次数据面，音源全挂时回 503
                return self._handle_readyz()
            if path == "/play" or path.startswith("/play/"):
                return self._handle_play(u, qs)
            if path == "/lyrics" or path.startswith("/lyrics/"):
                return self._handle_lyrics(u, qs)
            if path == "/api/search":
                q = (qs.get("q") or [""])[0].strip()
                if not q:
                    raise BadRequest("参数 q 不能为空")
                limit = clamp_limit(self._qs_int(qs, "limit", 10), 10)
                p = provider()
                if SMART_SEARCH:
                    rows, fixed = search_tracks(p, q, limit)
                else:
                    rows, fixed = p.search(q, limit), ""
                out = {"query": q, "count": len(rows), "tracks": rows}
                if fixed and fixed != _norm_text(q):
                    out["corrected"] = fixed
                return self._json(out)
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
            if path == "/api/sources":
                return self._json({"sources": source_status(FALLBACK_URLS) if FALLBACK_URLS else []})
            if path == "/api/storage":
                return self._json({"cache": CACHE.stats(), "library": LIBRARY.stats()})
            if path == "/api/quality":
                return self._json({"default": QUALITY_DEFAULT, "profiles": describe_quality()})
            if path == "/mcp":
                return self._json({"error": "use POST /mcp (streamable-http)"}, 405,
                                  [("Allow", "POST, OPTIONS")])
            return self._json({"error": "not found", "path": path}, 404)
        except GatewayError as e:
            body, code = as_payload(e)
            self._json(body, code)
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
            if u.path == WEBUI_PREFIX or u.path.startswith(WEBUI_PREFIX + "/"):
                if not self._admin_ok(u.path):
                    return self._unauthorized()
                return self._proxy_webui()
            if not self._admin_ok(u.path):
                return self._unauthorized()
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
                    if is_ext_guid(guid) and hasattr(p, "download_ext"):
                        st = load_state()
                        track = {"guid": guid,
                                 "title": (st.get("title") if st.get("guid") == guid else "") or guid,
                                 "artist": (st.get("artist") if st.get("guid") == guid else "") or "",
                                 "source": "ext"}
                        fp, n = p.download_ext(track, DEFAULT_QUALITY)
                        return self._json({"ok": True, "guid": guid, "action": action,
                                           "saved": fp, "bytes": n})
                    p.add_favorite(guid)
                else:
                    p.remove_favorite(guid)
                return self._json({"ok": True, "guid": guid, "action": action})
            if u.path == "/api/warm":
                body = self._body()
                guid = str(body.get("guid") or "").strip()
                if not guid:
                    raise BadRequest("guid 不能为空")
                path = CACHE.ensure(guid, provider(), quality=body.get("quality"))
                return self._json({"ok": True, "guid": guid,
                                   "bytes": os.path.getsize(path)})
            if u.path == "/api/download":
                body = self._body()
                if not LIBRARY.enabled():
                    raise BadRequest("未配置 GW_DOWNLOAD_DIR，无法保存到本地")
                guid = str(body.get("guid") or "").strip()
                query = str(body.get("query") or "").strip()
                quality = body.get("quality")
                p = provider()
                if guid:
                    st = load_state()
                    track = {"guid": guid,
                             "title": (st.get("title") if st.get("guid") == guid else "") or guid,
                             "artist": (st.get("artist") if st.get("guid") == guid else "") or "",
                             "source": "ext"}
                elif query:
                    rows = p.search(query, 5)
                    if not rows:
                        return self._json({"error": "track not found"}, 404)
                    track = rows[0]
                else:
                    raise BadRequest("guid 或 query 至少给一个")
                fp, n = LIBRARY.save_from_provider(p, track, quality or DEFAULT_QUALITY)
                return self._json({"ok": True, "guid": track["guid"], "path": fp, "bytes": n})
            return self._json({"error": "not found", "path": u.path}, 404)
        except GatewayError as e:
            body, code = as_payload(e)
            self._json(body, code)
        except BrokenPipeError:
            pass
        except Exception as e:
            LOG.exception("POST %s failed", self.path)
            try:
                self._json({"error": str(e)}, 500)
            except Exception:
                pass

    def do_PUT(self):
        self._do_webui_write()

    def do_PATCH(self):
        self._do_webui_write()

    def _do_webui_write(self):
        u, _ = self._qs()
        if not (u.path == WEBUI_PREFIX or u.path.startswith(WEBUI_PREFIX + "/")):
            return self._json({"error": "not found", "path": u.path}, 404)
        if not self._admin_ok(u.path):
            return self._unauthorized()
        return self._proxy_webui()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Allow", "GET, POST, PUT, PATCH, HEAD, DELETE, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_DELETE(self):
        """MCP 客户端关闭会话时发 DELETE /mcp，回 204 即可（无状态，无需清理）。"""
        u, _ = self._qs()
        if not self._admin_ok(u.path):
            return self._unauthorized()
        if u.path == WEBUI_PREFIX or u.path.startswith(WEBUI_PREFIX + "/"):
            return self._proxy_webui()
        if u.path == "/mcp":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._json({"error": "not found", "path": u.path}, 404)

    def _handle_readyz(self):
        """就绪探针：mock 探本地曲目，在线模式探所有音源健康状态。"""
        try:
            if MOCK:
                rows = provider().list_tracks(1)
                return self._json({"ok": True, "mode": "mock", "tracks_probe": len(rows)})
            rows = source_status(FALLBACK_URLS, timeout=3)
            healthy = [r for r in rows if r.get("healthy")]
            return self._json({"ok": bool(healthy), "mode": "online", "sources": rows},
                              200 if healthy else 503)
        except Exception as e:
            return self._json({"ok": False, "mode": "mock" if MOCK else "online",
                               "error": str(e)}, 503)

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
        quality = (qs.get("quality") or [""])[0] or None
        p = provider()
        rows, label = pick_tracks(p, query, mood, playlist, 20)
        if index < 1 or index > len(rows):
            return self._json({"error": "index out of range", "count": len(rows)}, 400)
        info = PLAYBACK.prepare(rows[index - 1], p, self._base(), quality=quality)
        info.update({"label": label, "index": index, "count": len(rows)})
        return self._json(info)

    def _handle_lyrics(self, u, qs):
        """签名歌词直链：设备 self.music.play_url 的 lyric_url 会自己来拉 LRC。"""
        guid = (qs.get("guid") or [""])[0]
        if not guid and u.path.startswith("/lyrics/"):
            name = u.path[len("/lyrics/"):]
            if name.endswith(".lrc"):
                name = name[:-4]
            guid = urllib.parse.unquote(name)
        if not guid:
            return self._json({"error": "missing guid"}, 400)
        exp = (qs.get("exp") or [""])[0]
        sig = (qs.get("sig") or [""])[0]
        if not verify(guid, exp, sig):
            return self._json({"error": "invalid or expired signature"}, 403)
        path = lyric_cache_path(guid)
        try:
            with open(path, "rb") as fp:
                body = fp.read(LYRIC_MAX_BYTES)
        except OSError:
            return self._json({"error": "no lyrics"}, 404)
        if not body:
            return self._json({"error": "no lyrics"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=300")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

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
        quality = (qs.get("q") or [""])[0] or None
        try:
            path = CACHE.ensure(guid, provider(), quality=quality)
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
        # 优先走官方 MCPServer 2.3 的 streamable-http；它只监听 127.0.0.1 随机端口。
        headers = {
            "Content-Type": self.headers.get("Content-Type") or "application/json",
            "Accept": self.headers.get("Accept") or "application/json, text/event-stream",
            "X-GW-Base": self._base(),
        }
        for key in ("Mcp-Session-Id", "MCP-Protocol-Version", "Last-Event-ID"):
            value = self.headers.get(key)
            if value:
                headers[key] = value
        try:
            status, _reason, rhdrs, data = MCP_BRIDGE.request("POST", "/mcp", raw, headers)
        except Exception as exc:
            LOG.warning("官方 MCPServer 不可用，回退内置 JSON-RPC：%s", exc)
            return self._handle_mcp_legacy(raw)

        out_headers = []
        has_session = False
        for key, value in rhdrs:
            lower = key.lower()
            if lower in ("content-length", "transfer-encoding", "connection", "date", "server", "content-type"):
                continue
            if lower == "mcp-session-id":
                has_session = True
            out_headers.append((key, value))
        # 官方 stateless 模式不返回会话 id；为了兼容既有客户端，initialize 补一个。
        if not has_session:
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else None
            except Exception:
                payload = None
            if isinstance(payload, dict) and payload.get("method") == "initialize":
                out_headers.append(("Mcp-Session-Id", uuid.uuid4().hex))
        if not data.strip():
            return self._empty(status)
        ctype = "application/json"
        for key, value in rhdrs:
            if key.lower() == "content-type":
                ctype = value
                break
        if "application/json" in ctype.lower():
            try:
                return self._json(json.loads(data.decode("utf-8")), status, out_headers)
            except Exception:
                pass
        return self._raw(data, status, ctype, out_headers)

    def _handle_mcp_legacy(self, raw):
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
    LOG.info("fnmusic-gateway %s on %s:%d (mock=%s, public_base=%s, ttl=%ds, cache=%dMiB, webui=%s)",
             VERSION, HOST, PORT, MOCK, PUBLIC_BASE or "auto", URL_TTL, CACHE_MAX // 1048576,
             WEBUI_URL or "off")
    if not ADMIN_TOKEN:
        LOG.warning("未设置 GW_ADMIN_TOKEN：管理接口、MCP 与 WebUI 均无鉴权，只应内网访问")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        LOG.info("bye")


if __name__ == "__main__":
    main()
