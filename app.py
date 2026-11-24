#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fnmusic-gateway —— 在线音源 → 小智 的一层网关。

单端口出口（管理、MCP、播放、WebUI 全部走 GW_PORT）
--------------------------------------------------
1) MCP（streamable-http）  POST /mcp
   给服务端/云端 LLM 用：查歌单、收藏、搜歌、拿直链。
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
GET /api/play?query=|playlist=[&index=]       取播放直链（会预热转码）
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
import itertools
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
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from gwlib import __version__ as VERSION
from gwlib.errors import BadRequest, GatewayError, as_payload
from gwlib.quality import DEFAULT_QUALITY, describe as describe_quality, get_profile, normalize_quality
from gwlib.storage import LibraryStore, MediaCache, TranscodeSlots, audio_seconds
from gwlib.sources import FallbackProvider, is_ext_guid
from gwlib.native_source import (NativeMusicDLProvider,
                                  deps_available as online_deps_available,
                                  deps_error as online_deps_error)
from gwlib.playback import PlaybackService
from gwlib.playlist import PlaylistRegistry, iter_clean_mp3, iter_clean_mp3_stream
from gwlib.resume import ResumeStore
from gwlib.mcp_tools import build_tools
from gwlib.mcp_server import McpHttpBridge, McpService
from gwlib.config import ConfigStore
from gwlib.settings_page import SETTINGS_HTML
from gwlib.home_page import HOME_HTML
from gwlib.login_page import login_html
from gwlib import netease_login
from fnmusic_client import normalize_base

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


def _truthy(v):
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _device_base(value):
    """校验并规范化返回给设备的地址前缀（只接受 http/https）。"""
    v = str(value or "").strip().rstrip("/")
    if not v:
        return ""
    try:
        parts = urllib.parse.urlsplit(v)
    except Exception:
        return ""
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    return v


def _safe_next_path(raw, default="/"):
    """登录后跳转只允许站内路径，防开放重定向。"""
    raw = str(raw or "").strip()
    if not raw:
        return default
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError:
        return default
    if parts.scheme or parts.netloc:
        return default
    path = parts.path or default
    if (not path.startswith("/") or path.startswith("//") or "\\" in path
            or any(ord(c) < 0x20 or ord(c) == 0x7f for c in path)):
        return default
    if path in ("/login", "/login/", "/logout", "/logout/"):
        return default
    if parts.query:
        if any(ord(c) < 0x20 or ord(c) == 0x7f for c in parts.query):
            return default
        path += "?" + parts.query
    return path


DATA_DIR = _env("GW_DATA", os.path.join(BASE_DIR, "data"))
os.makedirs(DATA_DIR, exist_ok=True)

HOST = _env("GW_HOST", "0.0.0.0")
PORT = _env_int("GW_PORT", 8766)
# 单端口：只监听 GW_PORT。PUBLIC_BASE 只影响返回给设备的直链前缀，不再额外开监听端口。
PUBLIC_BASE = _device_base(_env("GW_PUBLIC_BASE", "") or "")
URL_TTL = _env_int("GW_URL_TTL", 900)
CACHE_DIR = _env("GW_CACHE_DIR", os.path.join(DATA_DIR, "cache"))
CACHE_MAX = _env_int("GW_CACHE_MAX_BYTES", 8 * 1024 ** 3)
MOCK = _env_bool("GW_MOCK", False)
STATE_FILE = os.path.join(DATA_DIR, "state.json")
RESUME_FILE = os.path.join(DATA_DIR, "resume.json")
MAX_LIMIT = _env_int("GW_MAX_LIMIT", 50)
HTTP_BODY_CAP = 1024 * 1024
MCP_BODY_CAP = 4 * 1024 * 1024
MCP_TIMEOUT = max(30, _env_int("GW_MCP_TIMEOUT", 300))
# 管理令牌：为空时不鉴权（兼容旧部署）。单端口模式下强烈建议设置。
# 带令牌后，/play/ 与 /lyrics/ 之外的接口都必须带 X-Admin-Token 或 Authorization: Bearer。
ADMIN_TOKEN = (_env("GW_ADMIN_TOKEN", "") or "").strip()
ADMIN_COOKIE = "fnmusic_admin"
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

# 内置在线音源（musicdl）。数据保存在 data/config.json 的 online 段，网页设置可改；
# 环境变量只作为首次启动默认值。
ONLINE_ENABLED = _env_bool("GW_ONLINE_ENABLED", True)
ONLINE_PLATFORMS = [x.strip() for x in
                    (_env("GW_ONLINE_PLATFORMS", "KuwoMusicClient,MiguMusicClient") or "").split(",")
                    if x.strip()]
ONLINE_EXTRA_PLATFORMS = []
ONLINE_SEARCH_LIMIT = max(1, _env_int("GW_ONLINE_SEARCH_LIMIT", 10))
ONLINE_TIMEOUT = max(3, _env_int("GW_ONLINE_TIMEOUT", 25))
ONLINE_TOTAL_LIMIT = max(1, _env_int("GW_ONLINE_TOTAL_LIMIT", MAX_LIMIT))
ONLINE_TOTAL_TIMEOUT = max(1, _env_int("GW_ONLINE_TOTAL_TIMEOUT", 40))
DOWNLOAD_DIR = _env("GW_DOWNLOAD_DIR", "")
# 飞牛音乐（可选，优先级最高的音源）。地址/账号/密码优先读 data/config.json（网页「网关设置」写入），
# 环境变量只作为首次启动的默认值。
FNOS_TOKEN_FILE = os.path.join(DATA_DIR, "fnmusic_token.json")
FNOS_TIMEOUT = max(3, _env_int("GW_FNOS_TIMEOUT", 12))
FNOS_CONFIG = ConfigStore(os.path.join(DATA_DIR, "config.json"))
PREVIEW_MIN_SECONDS = max(1, _env_int("GW_PREVIEW_MIN_SECONDS", 35))

# 音质与存储策略
QUALITY_DEFAULT = normalize_quality(_env("GW_QUALITY_DEFAULT", DEFAULT_QUALITY))
TEE_SAVE = _env_bool("GW_TEE_SAVE", True)
LIBRARY_MAX_BYTES = _env_int("GW_LIBRARY_MAX_BYTES", 0)

# 搜索：别名纠错 + 相关度打分（中文 ASR 常把歌名/歌手听成同音字）
SMART_SEARCH = _env_bool("GW_SMART_SEARCH", True)
MCP_SEARCH_BUDGET = max(1.0, float(_env("GW_MCP_SEARCH_BUDGET", "8")))
FNOS_SEARCH_TIMEOUT = max(1, _env_int("GW_FNOS_SEARCH_TIMEOUT", 5))
ALIASES_FILE = _env("GW_ALIASES_FILE", os.path.join(DATA_DIR, "music_aliases.json"))

# ---------------------------------------------------------------- 高级设置
_MIB = 1024 * 1024
# 网页「高级设置」可在线调整的运行参数；环境变量只作为首次启动默认值。
# 注意：端口、路径、GW_ADMIN_TOKEN 等启动参数不在此列，仍需在 .env 里设置。
ADVANCED_SPEC = {
    "quality_default": {
        "label": "默认音质", "kind": "enum",
        "values": ["standard", "high", "exhigh", "lossless"],
        "default": QUALITY_DEFAULT, "help": "standard/high/exhigh/lossless",
    },
    "max_limit": {"label": "搜索返回上限", "kind": "int", "default": MAX_LIMIT,
                  "min": 1, "max": 500, "unit": "首"},
    "preview_min_seconds": {"label": "试听判定阈值", "kind": "int",
                            "default": PREVIEW_MIN_SECONDS, "min": 1, "max": 3600, "unit": "秒"},
    "url_ttl": {"label": "直链有效期", "kind": "int", "default": URL_TTL,
                "min": 60, "max": 86400, "unit": "秒"},
    "cache_max_mb": {"label": "播放缓存上限", "kind": "int", "default": CACHE_MAX // _MIB,
                     "min": 0, "max": 1048576, "unit": "MB", "help": "0 表示不限制"},
    "library_max_mb": {"label": "本地曲库上限", "kind": "int", "default": LIBRARY_MAX_BYTES // _MIB,
                       "min": 0, "max": 1048576, "unit": "MB", "help": "0 表示不限制"},
    "transcode_concurrency": {"label": "同时转码数", "kind": "int",
                              "default": TRANSCODE_CONCURRENCY, "min": 1, "max": 8, "unit": "个"},
    "smart_search": {"label": "智能搜索（别名纠错 + 相关度）", "kind": "bool", "default": SMART_SEARCH},
    "tee_save": {"label": "边播边存到本地曲库", "kind": "bool", "default": TEE_SAVE},
    "lyrics_enabled": {"label": "启用歌词", "kind": "bool", "default": LYRICS_ENABLED},
    "lrclib_url": {"label": "LRCLIB 地址", "kind": "str", "default": LRCLIB_URL,
                   "help": "留空或 off 表示关闭在线歌词检索"},
    "lyrics_retry": {"label": "歌词重试间隔", "kind": "int", "default": LYRICS_RETRY,
                     "min": 60, "max": 31536000, "unit": "秒"},
    "fnos_timeout": {"label": "飞牛音乐请求超时", "kind": "int",
                     "default": FNOS_TIMEOUT, "min": 3, "max": 120, "unit": "秒"},
    "mcp_timeout": {"label": "MCP 上游超时", "kind": "int",
                    "default": MCP_TIMEOUT, "min": 30, "max": 3600, "unit": "秒"},
}
_ADV_DEFAULTS = {k: v["default"] for k, v in ADVANCED_SPEC.items()}


def _adv_parse(key, value):
    """把网页/配置文件里的字符串值按类型校验，返回 Python 值。"""
    spec = ADVANCED_SPEC[key]
    kind = spec["kind"]
    if kind == "bool":
        return value if isinstance(value, bool) else _truthy(value)
    if kind == "int":
        if isinstance(value, bool):
            raise BadRequest("%s 必须是整数" % spec["label"])
        try:
            n = int(str(value).strip())
        except (TypeError, ValueError):
            raise BadRequest("%s 必须是整数" % spec["label"])
        if n < spec["min"] or n > spec["max"]:
            raise BadRequest("%s 需在 %d~%d 之间" % (spec["label"], spec["min"], spec["max"]))
        return n
    if kind == "enum":
        v = str(value or "").strip().lower()
        if v not in spec["values"]:
            raise BadRequest("%s 取值必须是 %s" % (spec["label"], "/".join(spec["values"])))
        return v
    v = str(value or "").strip()
    if key == "lrclib_url":
        if v.lower() in ("off", "none", "-"):
            return ""
        if v and not re.match(r"^https?://", v):
            raise BadRequest("LRCLIB 地址必须以 http:// 或 https:// 开头，留空或 off 表示关闭")
        return v.rstrip("/")
    return v


def _adv_store_value(key, value):
    kind = ADVANCED_SPEC[key]["kind"]
    if kind == "bool":
        return "1" if value else "0"
    if kind == "str" and not value:
        return "off"  # ConfigStore 用空串表示删除，这里用 off 表示显式关闭
    return str(value)


def advanced_settings():
    """高级设置：data/config.json 优先，环境变量默认值兜底。"""
    out = dict(_ADV_DEFAULTS)
    cfg = FNOS_CONFIG.section("advanced")
    for key in ADVANCED_SPEC:
        raw = cfg.get(key)
        if raw is None:
            continue
        try:
            out[key] = _adv_parse(key, raw)
        except BadRequest:
            pass
    return out


def apply_advanced_settings():
    """把高级设置同步到模块全局与已创建的服务对象，做到保存即生效。"""
    global URL_TTL, MAX_LIMIT, TRANSCODE_CONCURRENCY, LYRICS_ENABLED, LRCLIB_URL
    global LYRICS_RETRY, FNOS_TIMEOUT, PREVIEW_MIN_SECONDS, QUALITY_DEFAULT
    global TEE_SAVE, SMART_SEARCH
    vals = advanced_settings()
    URL_TTL = vals["url_ttl"]
    MAX_LIMIT = vals["max_limit"]
    TRANSCODE_CONCURRENCY = vals["transcode_concurrency"]
    LYRICS_ENABLED = vals["lyrics_enabled"]
    LRCLIB_URL = vals["lrclib_url"]
    LYRICS_RETRY = vals["lyrics_retry"]
    FNOS_TIMEOUT = vals["fnos_timeout"]
    PREVIEW_MIN_SECONDS = vals["preview_min_seconds"]
    QUALITY_DEFAULT = vals["quality_default"]
    TEE_SAVE = vals["tee_save"]
    SMART_SEARCH = vals["smart_search"]
    cache = globals().get("CACHE")
    if cache is not None:
        cache.reconfigure(max_bytes=vals["cache_max_mb"] * _MIB,
                          transcode_slots=TRANSCODE_CONCURRENCY)
    library = globals().get("LIBRARY")
    if library is not None:
        library.max_bytes = vals["library_max_mb"] * _MIB
    playback = globals().get("PLAYBACK")
    if playback is not None:
        playback.default_quality = QUALITY_DEFAULT
        playback.ttl = URL_TTL
        playback.preview_min_seconds = PREVIEW_MIN_SECONDS
        playback.tee_save = TEE_SAVE
    bridge = globals().get("MCP_BRIDGE")
    if bridge is not None:
        bridge.timeout = vals["mcp_timeout"]
    return vals


def save_advanced_settings(patch):
    """写 config.json 的 advanced 段并热应用；只处理提交上来的字段。"""
    if not isinstance(patch, dict):
        raise BadRequest("请求体必须是对象")
    store = {}
    for key, raw in patch.items():
        if key not in ADVANCED_SPEC:
            continue
        store[key] = _adv_store_value(key, _adv_parse(key, raw))
    if store:
        FNOS_CONFIG.update("advanced", store)
    apply_advanced_settings()
    reset_provider()  # 超时/条数变化后按新配置重建 provider
    return advanced_settings()


def _log_level(name):
    return getattr(logging, str(name or "INFO").strip().upper(), logging.INFO)


logging.basicConfig(level=_log_level(_env("GW_LOG_LEVEL", "INFO")),
                    format="%(asctime)s %(levelname)s [v" + VERSION + "] %(message)s")
LOG = logging.getLogger("fnmusic-gw")
FNOS_CONFIG.log = LOG

# 同时最多几个 ffmpeg：防止多台设备同时点播把宿主机 CPU 打满
_TRANSCODE_SLOTS = TranscodeSlots(TRANSCODE_CONCURRENCY)


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


def _sign_raw(guid, exp, start=0):
    msg = ("%s|%d" % (guid, exp)).encode("utf-8")
    if start:
        msg += ("|%d" % int(start)).encode("utf-8")
    return hmac.new(SECRET, msg, hashlib.sha256).hexdigest()[:40]


def sign(guid, exp, quality=None, start=0):
    """签名绑定 guid/过期时间/音质；有续播秒数时一并绑定。"""
    q = normalize_quality(quality or QUALITY_DEFAULT)
    msg = ("%s|%d|%s" % (guid, exp, q)).encode("utf-8")
    if start:
        msg += ("|%d" % int(start)).encode("utf-8")
    return hmac.new(SECRET, msg, hashlib.sha256).hexdigest()[:40]


def verify(guid, exp, sig, quality=None, start=0):
    try:
        exp = int(exp)
    except Exception:
        return False
    if exp < int(time.time()):
        return False
    given = str(sig or "")
    # 兼容旧版本已发出的、只签 guid+exp 的 URL（URL_TTL 内平滑升级）。
    return (hmac.compare_digest(sign(guid, exp, quality, start), given) or
            hmac.compare_digest(_sign_raw(guid, exp, start), given))


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
    out = {"guid": guid, "title": title, "artist": str(artist or ""), "album": str(album or "")}
    raw_duration = _first(t, "duration_s", "duration_seconds", "duration", "durationMs", "duration_ms")
    try:
        seconds = float(raw_duration)
        if seconds > 0:
            out["duration_s"] = seconds / 1000.0 if seconds > 10000 else seconds
    except (TypeError, ValueError):
        pass
    return out


def norm_playlist(p):
    if not isinstance(p, dict):
        return {"guid": str(p), "name": str(p), "count": 0}
    return {
        "guid": str(_first(p, "guid", "playlistGuid", "playlistGUID", "id")),
        "name": str(_first(p, "name", "title", "playlistName")),
        "count": _to_int(_first(p, "trackCount", "count", "songCount", default=0)),
    }


def norm_artist(p):
    if not isinstance(p, dict):
        return {"guid": str(p), "name": str(p), "count": 0}
    return {
        "guid": str(_first(p, "guid", "artistGuid", "artistGUID", "artistId", "id")),
        "name": str(_first(p, "name", "title", "artistName")),
        "count": _to_int(_first(p, "trackCount", "count", "songCount", default=0)),
    }


def norm_album(p):
    if not isinstance(p, dict):
        return {"guid": str(p), "name": str(p), "count": 0}
    return {
        "guid": str(_first(p, "guid", "albumGuid", "albumGUID", "albumId", "id")),
        "name": str(_first(p, "name", "title", "albumName")),
        "count": _to_int(_first(p, "trackCount", "count", "songCount", default=0)),
    }


def fmt_tracks(tracks, limit=10, show_guid=False):
    lines = []
    for i, t in enumerate(tracks[:limit], 1):
        a = (" - " + t["artist"]) if t.get("artist") else ""
        line = "%d. %s%s" % (i, t.get("title") or t.get("guid"), a)
        if show_guid and t.get("guid"):
            line += "（guid=%s）" % t["guid"]
        lines.append(line)
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


def resolve_download_track(p, guid, title="", artist=""):
    """下载用曲目元数据：显式标题 > 当前播放状态 > provider 缓存 > guid 兜底。

    前端/MCP 按 guid 下载时不再依赖「刚播放过」，避免保存成 guid - 未知。
    """
    guid = str(guid or "").strip()
    title = str(title or "").strip()
    artist = str(artist or "").strip()
    if title and title != guid:
        return {"guid": guid, "title": title, "artist": artist, "source": "ext"}
    state = load_state()
    if state.get("guid") == guid and state.get("title"):
        return {"guid": guid, "title": str(state["title"]),
                "artist": artist or str(state.get("artist") or ""), "source": "ext"}
    info_fn = getattr(p, "track_info", None)
    if callable(info_fn):
        try:
            info = info_fn(guid)
        except Exception as exc:
            LOG.debug("track_info(%s) failed: %s", guid, exc)
            info = None
        if isinstance(info, dict) and info.get("title"):
            out = dict(info)
            out["guid"] = guid
            out.setdefault("source", "ext")
            return out
    return {"guid": guid, "title": title or guid, "artist": artist, "source": "ext"}


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


def fast_search_tracks(p, query, limit=10, budget=None):
    """MCP 快速搜索：本地/在线边搜边返回，最多等 budget 秒，先到先播。"""
    q = (query or "").strip()
    if not q:
        return [], ""
    fixed, changed = apply_aliases(q)
    deadline = time.monotonic() + max(1.0, float(budget or MCP_SEARCH_BUDGET))
    raw = []
    stream = getattr(p, "search_stream", None)
    try:
        if callable(stream):
            try:
                iterator = stream(fixed, max(1, limit),
                                  deadline_s=max(0.2, deadline - time.monotonic()))
            except TypeError:
                iterator = stream(fixed, max(1, limit))
            for track in iterator:
                raw.append(track)
                if len(raw) >= limit or time.monotonic() >= deadline:
                    break
        else:
            raw.extend(p.search(fixed, max(limit * 3, 20)))
    except Exception as e:
        LOG.warning("fast search %r failed: %s", fixed, e)
    # 别名生效但结果太少时，用原词补一次，避免别名表写错导致搜不到。
    if changed and len(raw) < limit and time.monotonic() < deadline:
        try:
            raw.extend(p.search(q, max(limit, 10)))
        except Exception as e:
            LOG.warning("fast search fallback %r failed: %s", q, e)
    return rank_tracks(fixed, dedupe(raw), limit), fixed


# ---------------------------------------------------------------- 状态
_state_lock = threading.Lock()
# 最近一次通过 music_play 启动的连续流 token；music_next/previous/status 以它为游标。
class _PlaylistCursors:
    """按客户端/会话保存当前连续播放游标，避免多设备互相覆盖。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._items = {}

    def set(self, key, token, index=0, started_at=None):
        with self._lock:
            self._items[str(key or "default")] = (
                str(token or ""), max(0, int(index)),
                float(started_at if started_at is not None else time.time()))

    def get(self, key):
        with self._lock:
            item = self._items.get(str(key or "default"))
            if not item:
                return None
            return {"token": item[0], "index": item[1], "started_at": item[2]}

    def clear(self, key):
        with self._lock:
            self._items.pop(str(key or "default"), None)


CURRENT_PLAYLISTS = _PlaylistCursors()


def _session_key(args=None, fallback=""):
    raw = ""
    if isinstance(args, dict):
        raw = args.get("_session_id") or args.get("session_id") or ""
    if not raw:
        raw = fallback or ""
    return str(raw).strip() or "default"


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


# 续播记录只保留恢复播放必需的字段；music_status 每秒轮询时不再反复深拷贝整张歌单。
_RESUME_TRACK_FIELDS = ("guid", "title", "artist")
STATUS_RESUME_INTERVAL = 30.0
_STATUS_RESUME_LAST = {}
_STATUS_RESUME_LOCK = threading.Lock()


def _resume_track(track):
    if not isinstance(track, dict):
        return {}
    # title/artist 即使为空也保留键：PlaybackService 直接用 current["title"]/["artist"]。
    out = {"guid": str(track.get("guid") or ""),
           "title": str(track.get("title") or ""),
           "artist": str(track.get("artist") or "")}
    if track.get("duration_s") is not None:
        out["duration_s"] = track.get("duration_s")
    return out


def _resume_marker(session_key, kind, index):
    try:
        idx = max(0, int(index or 0))
    except (TypeError, ValueError):
        idx = 0
    return (str(session_key or "default"), str(kind or ""), idx)


def _resume_save(session_key, kind, track=None, tracks=None, label="", quality=None,
                 index=0, position=0.0, token="", loop=False):
    """保存本会话的可续播状态；只是元数据，不包含音频。"""
    try:
        position = max(0.0, float(position or 0))
    except (TypeError, ValueError):
        position = 0.0
    try:
        index = max(0, int(index or 0))
    except (TypeError, ValueError):
        index = 0
    data = {"kind": kind, "quality": quality or DEFAULT_QUALITY,
            "label": str(label or ""), "index": index, "position": position,
            "started_at": time.time(), "token": str(token or ""),
            "loop": bool(loop)}
    if isinstance(track, dict):
        compact = _resume_track(track)
        if compact:
            data["track"] = compact
    if tracks:
        data["tracks"] = [c for c in (_resume_track(t) for t in tracks) if c]
    try:
        RESUMES.set(session_key, data)
        with _STATUS_RESUME_LOCK:
            _STATUS_RESUME_LAST[_resume_marker(session_key, kind, index)] = time.time()
    except Exception as exc:
        LOG.warning("save resume state failed: %s", exc)


def _resume_save_status(session_key, kind, **kwargs):
    """music_status 高频调用时的去抖写盘；同一会话/曲目每 30 秒最多落一次。"""
    marker = _resume_marker(session_key, kind, kwargs.get("index", 0))
    now = time.time()
    with _STATUS_RESUME_LOCK:
        if now - _STATUS_RESUME_LAST.get(marker, 0.0) < STATUS_RESUME_INTERVAL:
            return False
        _STATUS_RESUME_LAST[marker] = now
    _resume_save(session_key, kind, **kwargs)
    return True


def _resume_get(session_key):
    try:
        return RESUMES.get(session_key)
    except Exception:
        return None


def _resume_clear(session_key):
    try:
        RESUMES.clear(session_key)
    except Exception:
        pass


def _current_playback_track(session_key):
    """当前会话正在播放的曲目；优先连续流游标，其次续播记录，最后全局上次播放。"""
    cursor = CURRENT_PLAYLISTS.get(session_key)
    item = PLAYLISTS.get(cursor.get("token")) if cursor else None
    if item and item.get("tracks"):
        idx = max(0, min(int(cursor.get("index", 0)), len(item["tracks"]) - 1))
        return dict(item["tracks"][idx])
    st = _resume_get(session_key) or {}
    if isinstance(st.get("track"), dict) and st["track"].get("guid"):
        return dict(st["track"])
    last = load_state()
    if last.get("guid"):
        return {"guid": last["guid"], "title": last.get("title") or last["guid"],
                "artist": last.get("artist") or "",
                "duration_s": last.get("duration")}
    return None


# ---------------------------------------------------------------- 转码缓存
_ADV_BOOT = advanced_settings()
CACHE = MediaCache(CACHE_DIR, _ADV_BOOT["cache_max_mb"] * _MIB, LOG,
                   transcode_slots=_TRANSCODE_SLOTS)
PLAYLISTS = PlaylistRegistry(ttl=URL_TTL, max_items=500)
RESUMES = ResumeStore(RESUME_FILE)
LIBRARY = LibraryStore(lambda: DOWNLOAD_DIR, LOG,
                       max_bytes=_ADV_BOOT["library_max_mb"] * _MIB)


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

    def track_info(self, guid):
        for track in self.TRACKS:
            if track["guid"] == guid:
                return dict(track)
        return None

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

    def artist_items(self, limit=200):
        return [
            {"guid": "artist-1", "name": "周杰伦", "count": 2},
            {"guid": "artist-2", "name": "陈奕迅", "count": 1},
            {"guid": "artist-3", "name": "朴树", "count": 1},
            {"guid": "artist-4", "name": "Yiruma", "count": 1},
        ][:limit]

    def album_items(self, limit=200):
        return [
            {"guid": "album-1", "name": "叶惠美", "count": 1},
            {"guid": "album-2", "name": "十一月的萧邦", "count": 1},
            {"guid": "album-3", "name": "猎户星座", "count": 1},
        ][:limit]

    def artist_tracks(self, guid, limit=500):
        wanted = {"artist-1": "周杰伦", "artist-2": "陈奕迅",
                  "artist-3": "朴树", "artist-4": "Yiruma"}.get(guid, "")
        return [t for t in self.TRACKS if t.get("artist") == wanted][:limit]

    def album_tracks(self, guid, limit=500):
        wanted = {"album-1": "叶惠美", "album-2": "十一月的萧邦",
                  "album-3": "猎户星座"}.get(guid, "")
        return [t for t in self.TRACKS if t.get("album") == wanted][:limit]

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

class FnProvider:
    """飞牛音乐后端：登录 NAS 上的音乐服务，读写曲库/歌单/收藏。"""

    def __init__(self, cfg=None):
        from fnmusic_client import FnMusicClient
        s = cfg or fnos_settings()
        if not s.get("base"):
            raise RuntimeError("未配置飞牛音乐地址")
        self.client = FnMusicClient(
            base=s["base"], username=s.get("username") or "",
            password=s.get("password") or None,
            device_id=s.get("device_id") or None,
            token_file=FNOS_TOKEN_FILE, timeout=FNOS_TIMEOUT,
            insecure_tls=bool(s.get("insecure_tls")))
        self.client.ensure_token()

    def search(self, q, limit=10):
        rows = dedupe(norm_track(t) for t in _rows(self.client.search_track(
            q, 1, limit, timeout=min(FNOS_SEARCH_TIMEOUT, FNOS_TIMEOUT))))
        for row in rows:
            row["source"] = "local"
        return rows

    def track_info(self, guid):
        """按 guid 取飞牛曲目元数据，下载时用来还原「标题 - 歌手」文件名。"""
        data = self.client.track_metadata(guid)
        track = data.get("track") if isinstance(data, dict) else None
        if not isinstance(track, dict):
            return None
        row = norm_track(track)
        row["source"] = "local"
        return row

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

    def artist_items(self, limit=200, query=""):
        if query:
            data = self.client.search_artist(query, 1, max(1, int(limit or 20)))
        else:
            data = self.client.artist_list(1, limit)
        return [norm_artist(r) for r in _rows(data)[:limit]
                if norm_artist(r).get("guid")]

    def album_items(self, limit=200, query=""):
        if query:
            data = self.client.search_album(query, 1, max(1, int(limit or 20)))
        else:
            data = self.client.album_list(1, limit)
        return [norm_album(r) for r in _rows(data)[:limit]
                if norm_album(r).get("guid")]

    def artist_tracks(self, guid, limit=500):
        rows = dedupe(norm_track(t) for t in _rows(self.client.artist_tracks(guid, 1, limit)))
        for row in rows:
            row["source"] = "local"
        return rows[:limit]

    def album_tracks(self, guid, limit=500):
        rows = dedupe(norm_track(t) for t in _rows(self.client.album_tracks(guid, 1, limit)))
        for row in rows:
            row["source"] = "local"
        return rows[:limit]

    def favorites(self, limit=100):
        rows = []
        page = 1
        while len(rows) < limit:
            batch = _rows(self.client.favorite_list(page, 50))
            if not batch:
                break
            rows.extend(norm_track(t) for t in batch)
            page += 1
            if len(batch) < 50:
                break
        return dedupe(rows)[:limit]

    def stream(self, guid):
        return self.client.stream_bytes(guid)

    def stream_to_path(self, guid, path, quality=None):
        return self.client.stream_to_path(guid, path)

    def iter_stream(self, guid, quality=None):
        return self.client.iter_stream(guid)

    def add_favorite(self, guid):
        self.client.favorite_add(guid)

    def remove_favorite(self, guid):
        self.client.favorite_remove(guid)


def fnos_settings():
    """飞牛音乐连接配置：data/config.json 优先，环境变量兜底。"""
    cfg = FNOS_CONFIG.section("fnos")
    base = normalize_base(cfg.get("base") or (_env("GW_FNOS_BASE", "") or ""))
    user = (cfg.get("username") or (_env("GW_FNOS_USER", "") or "")).strip()
    pwd = cfg.get("password") or (_env("GW_FNOS_PASS", "") or "")
    dev = (cfg.get("device_id") or (_env("GW_FNOS_DEVICE_ID", "") or "")).strip()
    tls_cfg = cfg.get("insecure_tls")
    insecure_tls = _env_bool("GW_FNOS_INSECURE_TLS", False) if tls_cfg in (None, "") \
        else _truthy(tls_cfg)
    if cfg:
        source = "config"
    elif base or user or pwd:
        source = "env"
    else:
        source = "none"
    return {"base": base, "username": user, "password": pwd, "device_id": dev,
            "insecure_tls": insecure_tls, "source": source,
            "token_cached": os.path.exists(FNOS_TOKEN_FILE)}


def apply_fnos_tls():
    """把配置里的自签证书开关同步给 fnmusic_client（读 FNMUSIC_INSECURE_TLS）。"""
    os.environ["FNMUSIC_INSECURE_TLS"] = "1" if fnos_settings()["insecure_tls"] else ""


def _split_csv(value):
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    return [x.strip() for x in str(value or "").split(",") if x.strip()]


def online_settings():
    """内置在线音源配置：data/config.json 的 online 段优先，环境变量兜底。"""
    cfg = FNOS_CONFIG.section("online")
    out = {
        "enabled": ONLINE_ENABLED,
        "platforms": list(ONLINE_PLATFORMS),
        "extra_platforms": list(ONLINE_EXTRA_PLATFORMS),
        "search_limit": ONLINE_SEARCH_LIMIT,
        "timeout": ONLINE_TIMEOUT,
        "total_limit": ONLINE_TOTAL_LIMIT,
        "total_timeout": ONLINE_TOTAL_TIMEOUT,
        "netease_cookie": "",
    }
    if "enabled" in cfg:
        out["enabled"] = _truthy(cfg["enabled"])
    if "platforms" in cfg:
        out["platforms"] = _split_csv(cfg["platforms"])
    if "extra_platforms" in cfg:
        out["extra_platforms"] = _split_csv(cfg["extra_platforms"])
    for key in ("search_limit", "timeout", "total_limit", "total_timeout"):
        if key in cfg:
            try:
                out[key] = int(cfg[key])
            except ValueError:
                pass
    out["netease_cookie"] = str(cfg.get("netease_cookie") or "")
    return out


def online_platforms(settings=None):
    s = settings or online_settings()
    return list(dict.fromkeys(s["platforms"] + s["extra_platforms"]))


def online_active(settings=None):
    s = settings or online_settings()
    return bool(s["enabled"] and online_platforms(s))


def new_native_provider(settings=None, platforms=None, timeout=None):
    s = settings or online_settings()
    return NativeMusicDLProvider(
        platforms=platforms or online_platforms(s),
        work_dir=os.path.join(DATA_DIR, "musicdl_outputs"),
        search_limit=s["search_limit"],
        timeout=timeout or s["timeout"],
        total_limit=s["total_limit"],
        total_timeout=s["total_timeout"],
        netease_cookie=s.get("netease_cookie") or "")


def _clean_netease_cookie(value):
    """校验并清理网易云 Cookie；空字符串表示清除。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > 4096:
        raise BadRequest("网易云 MUSIC_U Cookie 过长（最多 4096 字符）")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise BadRequest("网易云 MUSIC_U Cookie 含非法控制字符")
    return text


def save_netease_cookie(cookie):
    """保存扫码登录得到的 MUSIC_U，并重建在线音源。"""
    text = _clean_netease_cookie(cookie)
    if not text:
        raise BadRequest("扫码登录未返回可保存的 MUSIC_U")
    if "MUSIC_U=" not in text:
        raise BadRequest("扫码登录结果缺少 MUSIC_U")
    FNOS_CONFIG.update("online", {"netease_cookie": text})
    reset_provider()
    return True


def _online_int(patch, key, low, high, label):
    if key not in patch:
        return None
    try:
        value = int(str(patch.get(key)).strip())
    except (TypeError, ValueError):
        raise BadRequest("%s 必须是整数" % label)
    if value < low or value > high:
        raise BadRequest("%s 需在 %d~%d 之间" % (label, low, high))
    return value


def save_online_settings(patch):
    if not isinstance(patch, dict):
        raise BadRequest("请求体必须是对象")
    store = {}
    if "enabled" in patch:
        store["enabled"] = "1" if _truthy(patch.get("enabled")) else "0"
    for key, label in (("platforms", "平台列表"), ("extra_platforms", "扩展平台")):
        if key not in patch:
            continue
        rows = _split_csv(patch.get(key))
        for name in rows:
            if not re.match(r"^[A-Za-z][A-Za-z0-9_]{0,63}$", name):
                raise BadRequest("%s 含非法平台名：%s" % (label, name))
        store[key] = ",".join(dict.fromkeys(rows))
    if _truthy(patch.get("clear_netease_cookie")):
        store["netease_cookie"] = ""
    elif "netease_cookie" in patch:
        cookie = _clean_netease_cookie(patch.get("netease_cookie"))
        if cookie:
            store["netease_cookie"] = cookie
    for key, low, high, label in (
            ("search_limit", 1, 10, "单平台搜索条数"),
            ("timeout", 3, 120, "单平台超时"),
            ("total_limit", 1, 500, "聚合搜索总条数"),
            ("total_timeout", 1, 300, "聚合搜索总超时")):
        value = _online_int(patch, key, low, high, label)
        if value is not None:
            store[key] = str(value)
    settings = online_settings()
    if "enabled" in store:
        settings["enabled"] = store["enabled"] == "1"
    if "platforms" in store:
        settings["platforms"] = _split_csv(store["platforms"])
    if "extra_platforms" in store:
        settings["extra_platforms"] = _split_csv(store["extra_platforms"])
    for key in ("search_limit", "timeout", "total_limit", "total_timeout"):
        if key in store:
            settings[key] = int(store[key])
    if settings["enabled"] and not online_platforms(settings):
        raise BadRequest("启用在线音源时至少选择一个平台")
    FNOS_CONFIG.update("online", store)
    reset_provider()
    return settings


def online_status():
    settings = online_settings()
    if not online_active(settings):
        return []
    prov = new_native_provider(settings)
    try:
        return prov.status()
    finally:
        # 这个临时 provider 只用于查状态，必须释放它的线程池/worker 子进程。
        close_fn = getattr(prov, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:
                LOG.debug("关闭状态查询 provider 失败", exc_info=True)


def online_services():
    settings = online_settings()
    return online_platforms(settings) if settings["enabled"] else []


def fnos_configured():
    s = fnos_settings()
    return bool(s["base"] and s["username"] and (s["password"] or s["token_cached"]))


apply_fnos_tls()


def gateway_mode():
    if MOCK:
        return "mock"
    online = online_active()
    if fnos_configured():
        return "fnos+online" if online else "fnos"
    return "online" if online else "none"


def _close_provider_quietly(prov):
    close_fn = getattr(prov, "close", None)
    if callable(close_fn):
        try:
            close_fn()
        except Exception:
            LOG.debug("关闭 provider 失败", exc_info=True)


def reset_provider():
    """配置变更后丢弃 provider 单例，下一次请求按新配置重建。

    旧实例可能仍被正在播放/流式的请求持有，不能立刻 close（会中断音频）；
    先放入退休队列，等新实例稳定或队列过长时再关闭。
    """
    global _PROVIDER, _PROVIDER_ERR
    old = None
    with _PROVIDER_LOCK:
        old = _PROVIDER
        _PROVIDER = None
        _PROVIDER_ERR = None
    if old is None:
        return
    stale = []
    with _RETIRED_LOCK:
        _RETIRED_PROVIDERS.append(old)
        while len(_RETIRED_PROVIDERS) > _RETIRED_MAX:
            stale.append(_RETIRED_PROVIDERS.pop(0))
    for prov in stale:
        _close_provider_quietly(prov)


_PROVIDER = None
_PROVIDER_ERR = None
_PROVIDER_LOCK = threading.Lock()
_RETIRED_PROVIDERS = []
_RETIRED_LOCK = threading.Lock()
_RETIRED_MAX = 8


def provider():
    """惰性单例。飞牛音乐优先，内置在线音源（musicdl）补足。"""
    global _PROVIDER, _PROVIDER_ERR
    p = _PROVIDER
    if p is not None:
        return p
    with _PROVIDER_LOCK:
        if _PROVIDER is None:
            try:
                if MOCK:
                    _PROVIDER = MockProvider()
                else:
                    base = None
                    if fnos_configured():
                        base = FnProvider()
                        LOG.info("飞牛音乐已连接：%s", fnos_settings()["base"])
                    settings = online_settings()
                    exts = []
                    if online_active(settings):
                        exts = [new_native_provider(settings)]
                        LOG.info("内置在线音源：%s", ", ".join(online_platforms(settings)))
                        for ext in exts:
                            warm = getattr(ext, "prewarm", None)
                            if callable(warm):
                                try:
                                    warm()
                                except Exception:
                                    LOG.debug("在线音源预热失败", exc_info=True)
                    if exts:
                        _PROVIDER = FallbackProvider(
                            base, exts=exts, timeout=settings["timeout"],
                            search_limit=settings["search_limit"],
                            download_dir_getter=lambda: DOWNLOAD_DIR,
                            max_bytes=LIBRARY_MAX_BYTES,
                            total_limit=settings["total_limit"],
                            total_timeout=settings["total_timeout"])
                    elif base is not None:
                        _PROVIDER = base
                    else:
                        raise RuntimeError(
                            "未配置音源：请在网页「设置」填写飞牛音乐地址与账号，"
                            "或在「在线音源」中启用 musicdl 平台")
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
_PLAYLIST_NOISE = (
    "歌单", "列表", "播放", "随机", "随便", "随意", "音乐", "歌曲",
    "帮我", "请", "一下", "我想", "想听", "听听", "来点", "点播", "全部",
)


def _playlist_core(name):
    """把「播放儿歌歌单」「随机听睡前」这类口语化成可比较的核心名。"""
    out = _norm_text(name)
    for word in _PLAYLIST_NOISE:
        out = out.replace(word, "")
    return out


def playlist_match_score(query, name):
    """给多个歌单名打分，优先精确名/核心名，其次包含关系和相似度。"""
    q_raw, n_raw = _norm_text(query), _norm_text(name)
    if not q_raw or not n_raw:
        return 0
    if q_raw == n_raw:
        return 1000
    q, n = _playlist_core(query), _playlist_core(name)
    if q and q == n:
        return 950
    if q and n and (q in n or n in q):
        shorter, longer = sorted((len(q), len(n)))
        return 800 + int(100 * shorter / max(1, longer))
    for a, b in ((q, n), (q_raw, n_raw)):
        if not a or not b:
            continue
        ratio = difflib.SequenceMatcher(None, a, b).ratio()
        if ratio >= 0.6:
            return int(600 * ratio)
    return 0


def _match_named(items, name):
    key = (name or "").strip()
    if not key:
        return None
    best, best_score = None, 0
    for item in items or []:
        score = playlist_match_score(key, item.get("name") or "")
        if score > best_score or (
                score == best_score and best is not None and
                len(str(item.get("name") or "")) < len(str(best.get("name") or ""))):
            best, best_score = item, score
    return best if best_score >= 480 else None


def resolve_playlist(p, name):
    """在多歌单中选最匹配的一个，避免「儿歌」误中「儿歌大全」等。"""
    best = _match_named(p.playlists() or [], name)
    if best and _norm_text(name) != _norm_text(best.get("name") or ""):
        LOG.info("歌单匹配：%s -> %s", name, best.get("name"))
    return best


def resolve_artist(p, name):
    try:
        items = getattr(p, "artist_items", None)
        if callable(items):
            # 先用搜索接口精确定位，避免每个查询都拉 200 个歌手全表。
            try:
                candidates = items(query=name)
            except TypeError:
                candidates = items()
        else:
            candidates = [{"guid": str(x), "name": str(x)} for x in (p.artists(200) or [])]
    except Exception as exc:
        LOG.debug("artist list unavailable: %s", exc)
        return None
    hit = _match_named(candidates, name)
    if hit or not callable(items):
        return hit
    try:
        return _match_named(items(), name)
    except Exception as exc:
        LOG.debug("artist list fallback unavailable: %s", exc)
        return None


def resolve_album(p, name):
    try:
        items = getattr(p, "album_items", None)
        if callable(items):
            try:
                candidates = items(query=name)
            except TypeError:
                candidates = items()
        else:
            candidates = [{"guid": str(x), "name": str(x)} for x in (p.albums(200) or [])]
    except Exception as exc:
        LOG.debug("album list unavailable: %s", exc)
        return None
    hit = _match_named(candidates, name)
    if hit or not callable(items):
        return hit
    try:
        return _match_named(items(), name)
    except Exception as exc:
        LOG.debug("album list fallback unavailable: %s", exc)
        return None


def shuffle_tracks(tracks):
    out = [t for t in (tracks or []) if isinstance(t, dict)]
    if len(out) > 1:
        random.shuffle(out)
    return out


def pick_tracks(p, query="", playlist="", limit=10):
    if playlist:
        pl = resolve_playlist(p, playlist)
        if not pl:
            raise BadRequest("没有找到歌单「%s」" % playlist)
        return p.playlist_tracks(pl["guid"], 200)[:limit], "歌单「%s」" % pl["name"]
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
    raise BadRequest("需要 query 或 playlist 之一")


def make_url(base, guid, ttl=None, quality=None, start=0):
    exp = int(time.time()) + (ttl or URL_TTL)
    g = urllib.parse.quote(guid, safe="")
    q = normalize_quality(quality or QUALITY_DEFAULT)
    extra = "&start=%d" % int(start) if start else ""
    return "%s/play/%s.mp3?exp=%d&sig=%s&q=%s%s" % (
        base, g, exp, sign(guid, exp, q, start), urllib.parse.quote(q, safe=""), extra)


def make_playlist_url(base, token, ttl=None, quality=None, session="", start=0):
    exp = int(time.time()) + (ttl or URL_TTL)
    guid = "pl:" + str(token)
    q = normalize_quality(quality or QUALITY_DEFAULT)
    url = "%s/playlist/%s.mp3?exp=%d&sig=%s&q=%s" % (
        base, urllib.parse.quote(str(token), safe=""), exp,
        sign(guid, exp, q, int(start or 0)), urllib.parse.quote(q, safe=""))
    if start:
        url += "&start=%d" % int(start)
    if session:
        url += "&s=" + urllib.parse.quote(str(session), safe="")
    return url


def build_playlist_info(p, tracks, label, base, quality=None, action="连续播放",
                        start_index=0, session_key="", loop=False, start_position=0):
    """把多首曲目登记为一个连续播放流，返回给设备的直链信息。

    连续流仍然只有一个 URL，但设备屏显必须拿到「当前首」的歌名/歌手/歌词/单曲时长，
    不能把歌单名和整表总时长当成一首歌；后续曲目由 music_status 按播放游标回填。
    """
    rows = [t for t in (tracks or []) if isinstance(t, dict) and t.get("guid")]
    if not rows:
        raise BadRequest("没有可以连续播放的曲目")
    try:
        start_index = max(0, min(int(start_index or 0), len(rows) - 1))
    except (TypeError, ValueError):
        start_index = 0
    profile = get_profile(quality or QUALITY_DEFAULT)
    playlist_label = label or "连续播放"
    token = PLAYLISTS.put(rows, quality=profile["name"], ttl=URL_TTL, label=playlist_label, loop=loop)
    if not token:
        raise BadRequest("没有可以连续播放的曲目")
    stored = PLAYLISTS.get(token)
    if not stored or len(stored.get("tracks") or []) < len(rows):
        raise BadRequest("列表过长，最多支持 %d 首" % (len(stored.get("tracks") or []) if stored else 0))
    # 共享列表上的 index 仅作为无会话信息时的兜底；实际播放游标按 session 保存。
    PLAYLISTS.set_index(token, start_index, time.time())
    try:
        start_position = max(0, int(start_position or 0))
    except (TypeError, ValueError):
        start_position = 0
    url = make_playlist_url(base, token, ttl=URL_TTL, quality=profile["name"],
                            session=session_key, start=start_position)
    total_s = 0.0
    for track in rows:
        try:
            total_s += float(track.get("duration_s") or 0)
        except (TypeError, ValueError):
            pass
    first = rows[start_index]
    try:
        first_duration = float(first.get("duration_s") or 0)
    except (TypeError, ValueError):
        first_duration = 0.0
    first_title = str(first.get("title") or "").strip() or playlist_label
    first_artist = str(first.get("artist") or "").strip()
    try:
        first_lyric_url = lyric_url_for(base, first)
    except Exception:
        LOG.debug("playlist first lyric url failed", exc_info=True)
        first_lyric_url = ""
    try:
        if hasattr(CACHE, "prefetch"):
            CACHE.prefetch(first["guid"], p, quality=profile["name"])
    except Exception:
        LOG.debug("playlist first prefetch failed", exc_info=True)
    return {
        "guid": "pl:" + token, "title": first_title,
        "artist": first_artist, "album": "",
        "url": url, "lyric_url": first_lyric_url, "expires_in": URL_TTL,
        "quality": profile["name"], "quality_label": profile["label"],
        "bitrate": profile["bitrate"], "source": "local",
        "preview": False, "duration": first_duration or None, "saving": False,
        "preparing": True, "playlist_token": token, "count": len(rows),
        "start_index": start_index, "start_position": start_position,
        "loop": bool(loop),
        "playlist_label": playlist_label, "playlist_duration": total_s or None,
        "device_args": {"url": url, "title": first_title, "artist": first_artist,
                        "lyric_url": first_lyric_url,
                        "duration": int(round(first_duration)) if first_duration else 0},
        "device_tool": "self.music.play_url",
        "spoken": "正在%s%s，第 %d 首 %s" % (action, playlist_label, start_index + 1,
                                             ("，".join(x for x in (first_title, first_artist) if x))),
    }


def with_web_url(info):
    """给 WebUI 的同源播放地址，避免 PUBLIC_BASE 与当前访问主机不一致导致播放失败。"""
    out = dict(info)
    try:
        guid = str(out.get("guid") or "")
        if guid.startswith("pl:"):
            # 连续播放列表要用 /playlist 流，不能当成普通单曲 /play。
            out["web_url"] = make_playlist_url("", guid[3:],
                                               ttl=out.get("expires_in"),
                                               quality=out.get("quality"),
                                               start=out.get("start_position") or 0)
        else:
            out["web_url"] = make_url("", guid, ttl=out.get("expires_in"),
                                      quality=out.get("quality"))
    except Exception:
        out["web_url"] = out.get("url") or ""
    return out


def make_lyric_url(base, guid, ttl=None, title=None, artist=None):
    exp = int(time.time()) + (ttl or URL_TTL)
    g = urllib.parse.quote(guid, safe="")
    url = "%s/lyrics/%s.lrc?exp=%d&sig=%s" % (base, g, exp, sign(guid, exp))
    if title:
        url += "&title=" + urllib.parse.quote(str(title), safe="")
    if artist:
        url += "&artist=" + urllib.parse.quote(str(artist), safe="")
    return url


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


def lyrics_for(track, force=False):
    """取（必要时抓取并缓存）一首歌的 LRC 文件路径；空文件表示查不到。"""
    if not LYRICS_ENABLED:
        return ""
    guid = str(track.get("guid") or "")
    if not guid:
        return ""
    path = lyric_cache_path(guid)
    try:
        st = os.stat(path)
        if st.st_size > 0:
            return path
        if not force and (time.time() - st.st_mtime) < LYRICS_RETRY:
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


_LYRIC_FETCH_LOCK = threading.Lock()
_LYRIC_FETCHING = set()


def _kick_lyric_fetch(track):
    """后台预热歌词，绝不让 /api/play 阻塞在外部歌词站点的网络超时上。"""
    guid = str(track.get("guid") or "")
    if not guid:
        return
    with _LYRIC_FETCH_LOCK:
        if guid in _LYRIC_FETCHING:
            return
        _LYRIC_FETCHING.add(guid)

    def run():
        try:
            lyrics_for(dict(track))
        except Exception as exc:
            LOG.info("lyrics warmup %s failed: %s", guid, exc)
        finally:
            with _LYRIC_FETCH_LOCK:
                _LYRIC_FETCHING.discard(guid)

    threading.Thread(target=run, name="lyrics-%s" % guid[:16], daemon=True).start()


def lyric_url_for(base, track, ttl=None):
    """返回签名歌词直链；缓存未命中时后台预热，设备来取时再同步抓取，不阻塞播放。"""
    if not LYRICS_ENABLED:
        return ""
    guid = str(track.get("guid") or "")
    if not guid:
        return ""
    path = lyric_cache_path(guid)
    try:
        if os.path.getsize(path) > 0:
            return make_lyric_url(base, guid, ttl, track.get("title"), track.get("artist"))
    except OSError:
        pass
    # 先返回可用的签名歌词直链，同时后台预热；设备真正来取 /lyrics 时再同步抓取。
    _kick_lyric_fetch(track)
    return make_lyric_url(base, guid, ttl, track.get("title"), track.get("artist"))


def _lrc_line_at(track, elapsed_s):
    """从已缓存的 LRC 里取 elapsed_s 对应的当前歌词行；没有缓存或没有时间轴时返回空串。"""
    guid = str((track or {}).get("guid") or "")
    if not guid:
        return ""
    try:
        path = lyric_cache_path(guid)
        if os.path.getsize(path) <= 0:
            return ""
        with open(path, "r", encoding="utf-8", errors="replace") as fp:
            text = fp.read(LYRIC_MAX_BYTES)
    except OSError:
        return ""
    best, best_ts = "", -1.0
    for line in text.splitlines():
        m = re.match(r"^\s*\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\](.*)$", line)
        if not m:
            continue
        mm, ss, frac, body = int(m.group(1)), int(m.group(2)), m.group(3) or "0", m.group(4).strip()
        ts = mm * 60 + ss + int(frac.ljust(3, "0")[:3]) / 1000.0
        if ts <= elapsed_s + 0.05:
            best, best_ts = body, ts
        else:
            break
    return best if best_ts >= 0 else ""


PLAYBACK = PlaybackService(CACHE, LIBRARY, make_url, lyric_url_for, audio_seconds,
                           save_state, default_quality=_ADV_BOOT["quality_default"],
                           ttl=_ADV_BOOT["url_ttl"],
                           preview_min_seconds=_ADV_BOOT["preview_min_seconds"],
                           tee_save=_ADV_BOOT["tee_save"])
apply_advanced_settings()


# ---------------------------------------------------------------- MCP 工具
EXT_PREFIX = "ext:"

TOOLS = build_tools()


def _tool_music_search(args, p, base, limit, quality):
    q = str(args.get("query") or "").strip()
    if not q:
        raise BadRequest("query 不能为空")
    if SMART_SEARCH:
        rows, fixed = fast_search_tracks(p, q, limit)
    else:
        rows, fixed = p.search(q, limit), ""
    tail = ("（已按别名纠正为「%s」）" % fixed) if fixed and fixed != _norm_text(q) else ""
    local_count = sum(1 for t in rows if (t.get("source") or "") == "local")
    online_count = max(0, len(rows) - local_count)
    meta_fn = getattr(p, "last_search_meta", None)
    meta = meta_fn() if callable(meta_fn) else {}
    errs = []
    if isinstance(meta, dict):
        if meta.get("local_error"):
            errs.append("本地搜索失败：" + str(meta["local_error"]))
        if meta.get("online_error"):
            errs.append("在线搜索失败：" + str(meta["online_error"]))
    extra = ("（本地 %d · 在线 %d）" % (local_count, online_count))
    if errs:
        extra += "；" + "；".join(errs)
    return "搜索「%s」%s找到 %d 首%s：\n%s" % (
        q, tail, len(rows), extra, fmt_tracks(rows, limit, show_guid=True))


def _tool_music_browse(args, p, base, limit, quality):
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


def _tool_music_playlists(args, p, base, limit, quality):
    pls = p.playlists()
    return "我的歌单 %d 个：\n%s" % (len(pls), "\n".join(
        "%d. %s（%d 首）" % (i, x["name"], x["count"]) for i, x in enumerate(pls, 1)) or "（空）")


def _tool_music_playlist_tracks(args, p, base, limit, quality):
    pl = resolve_playlist(p, str(args.get("name") or ""))
    if not pl:
        raise BadRequest("没有找到歌单「%s」" % args.get("name"))
    rows = p.playlist_tracks(pl["guid"], 200)[:limit]
    return "歌单「%s」共 %d 首：\n%s" % (pl["name"], len(rows), fmt_tracks(rows, limit))


def _tool_music_favorites(args, p, base, limit, quality):
    rows = p.favorites(limit)
    return "收藏 %d 首：\n%s" % (len(rows), fmt_tracks(rows, limit))


_FAVORITE_PHRASES = ("收藏", "我的收藏", "收藏夹", "收藏列表", "收藏的歌单",
                    "收藏歌曲", "我收藏的歌单", "收藏音乐")


def _wants_favorites(text):
    n = _norm_text(text)
    if not n:
        return False
    if n in tuple(_norm_text(x) for x in _FAVORITE_PHRASES):
        return True
    return "收藏" in n and any(w in n for w in ("歌单", "列表", "歌曲", "音乐"))


def _wants_shuffle(text):
    n = _norm_text(text)
    return any(w in n for w in ("随机", "随便", "随意", "shuffle", "random"))


def build_list_playback(p, base, quality=None, playlist="", artist="", album="",
                       favorites=False, shuffle=False, index=1, session_key="",
                       library=False, loop=False):
    """把歌单/收藏/歌手/专辑解析成连续播放流，MCP 与 REST 共用。"""
    if library:
        rows = p.list_tracks(500)
        label = "音乐库"
    elif artist:
        item = resolve_artist(p, artist)
        if not item:
            names = "、".join(str(x.get("name") or "") for x in (getattr(p, "artist_items", lambda: [])() or [])[:8])
            raise BadRequest("没有找到歌手「%s」%s" % (artist, ("，现有：" + names) if names else ""))
        rows = p.artist_tracks(item["guid"], 500)
        label = "歌手「%s」" % item["name"]
    elif album:
        item = resolve_album(p, album)
        if not item:
            names = "、".join(str(x.get("name") or "") for x in (getattr(p, "album_items", lambda: [])() or [])[:8])
            raise BadRequest("没有找到专辑「%s」%s" % (album, ("，现有：" + names) if names else ""))
        rows = p.album_tracks(item["guid"], 500)
        label = "专辑「%s」" % item["name"]
    elif favorites:
        rows, label = p.favorites(500), "收藏"
    elif playlist:
        # 先找真实歌单；只有确实没有同名歌单时，才把「收藏」类短语当收藏。
        pl = resolve_playlist(p, playlist)
        if pl:
            rows = p.playlist_tracks(pl["guid"], 500)
            label = "歌单「%s」" % pl["name"]
        elif _wants_favorites(playlist):
            rows, label = p.favorites(500), "收藏"
        else:
            names = "、".join(str(x.get("name") or "") for x in (p.playlists() or [])[:8])
            raise BadRequest("没有找到歌单「%s」%s" % (playlist, ("，现有：" + names) if names else ""))
    else:
        raise BadRequest("需要歌单、收藏、歌手或专辑之一")
    if shuffle:
        rows = shuffle_tracks(rows)
        start = 0
        action = "随机播放"
    else:
        start = max(0, _to_int(index, 1) - 1)
        action = "连续播放"
    if not rows:
        raise BadRequest("列表里没有可播放的歌曲")
    if loop:
        action = "随机循环" if shuffle else "循环播放"
    # 保留完整列表，start 使用绝对索引；状态/前后曲/剩余数量都基于同一份列表。
    start = max(0, min(start, len(rows) - 1))
    info = build_playlist_info(p, rows, label, base, quality, action=action,
                               start_index=start, session_key=session_key, loop=loop)
    return info, label, start, bool(shuffle), action, rows


def _format_list_playback(info, label, total, start, shuffle, action):
    args_json = json.dumps(info.get("device_args") or {}, ensure_ascii=False)
    order_note = "，随机顺序" if shuffle else ""
    return ("准备%s：%s，共 %d 首%s，当前第 %d 首\n"
            "歌曲：%s%s\n"
            "直链（%d 分钟内有效）：%s\n"
            "歌词直链：%s\n"
            "单曲时长：%d 秒\n"
            "device_args：%s\n"
            "设备工具：self.music.play_url。参数取 device_args：url 填直链，title 填「%s」，"
            "artist 填「%s」，lyric_url 填歌词直链（没有就留空），duration 填 %d。"
            "歌词由设备每 10 秒通过 music_status 自动刷新；说「下一曲/上一曲」时调用 music_next/music_previous 后再用 self.music.play_url 播放新的 device_args。"
            % (action, label, total, order_note, start + 1,
               info.get("title") or "", (" - " + info.get("artist")) if info.get("artist") else "",
               max(1, (info.get("expires_in") or URL_TTL) // 60), info["url"],
               info.get("lyric_url") or "（暂未取到，留空）",
               int(info.get("duration") or 0), args_json,
               info.get("title") or "", info.get("artist") or "",
               int(info.get("duration") or 0)))


def _saved_list_response(st, p, base, quality=None, session_key="", action="继续播放",
                         loop=None, start=None):
    """从续播记录恢复一个连续播放列表，并登记新的游标。"""
    tracks = [dict(t) for t in (st.get("tracks") or [])
              if isinstance(t, dict) and t.get("guid")]
    if not tracks:
        raise BadRequest("没有可继续播放的歌曲列表")
    if start is None:
        start = _to_int(st.get("index"), 0)
    start = max(0, min(int(start), len(tracks) - 1))
    label = str(st.get("label") or "当前列表")
    q = quality or st.get("quality") or QUALITY_DEFAULT
    lp = bool(st.get("loop")) if loop is None else bool(loop)
    try:
        position = max(0, int(float(st.get("position") or 0)))
    except (TypeError, ValueError):
        position = 0
    info = build_playlist_info(p, tracks, label, base, q, action=action,
                               start_index=start, session_key=session_key, loop=lp,
                               start_position=position)
    token = info.get("playlist_token")
    if token:
        CURRENT_PLAYLISTS.set(session_key, token, start, time.time() - position)
    _resume_save(session_key, "playlist", tracks=tracks, label=label, quality=q,
                 index=start, position=position, token=token, loop=lp)
    return _format_list_playback(info, label, len(tracks), start, False, action)


def _tool_music_play(args, p, base, limit, quality):
    session_key = _session_key(args)
    guid = str(args.get("guid") or "").strip()
    playlist = str(args.get("playlist") or "").strip()
    query_text = str(args.get("query") or "").strip()
    artist = str(args.get("artist") or "").strip()
    album = str(args.get("album") or "").strip()
    library = _truthy(args.get("library")) or _truthy(args.get("all"))
    loop_mode = str(args.get("loop") or "").strip().lower()
    loop_one = loop_mode in ("one", "single", "single_loop", "单曲循环", "单曲")
    loop_all = loop_mode in ("all", "list", "循环", "列表循环")
    fav_raw = args.get("favorites")
    favorites = _truthy(fav_raw)
    shuffle = _truthy(args.get("shuffle")) or _truthy(args.get("random"))
    if not guid and not playlist and query_text and _wants_favorites(query_text) and _wants_shuffle(query_text):
        shuffle = True
        favorites, query_text = True, ""
    if _wants_shuffle(playlist) or _wants_shuffle(query_text):
        shuffle = True
    # 普通 query 只搜一次：搜索结果全部来自同一歌手时才升级为「播放歌手」，
    # 避免每个查询都去拉一遍歌手全表。
    pre_rows = None
    pre_label = ""
    if not guid and not playlist and not artist and not album and not favorites and query_text:
        pre_rows, pre_label = pick_tracks(p, query_text, "", 20)
        names = {_norm_text(t.get("artist")) for t in pre_rows if t.get("artist")}
        if len(pre_rows) >= 2 and len(names) == 1 and _norm_text(query_text) in names:
            artist = query_text
    if favorites or playlist or artist or album or library:
        info, label, start, shuffle, action, rows = build_list_playback(
            p, base, quality, playlist=playlist, artist=artist, album=album,
            favorites=favorites, shuffle=shuffle, index=args.get("index"),
            session_key=session_key, library=library, loop=loop_all)
        total = len(rows)
        token = info.get("playlist_token")
        if token:
            CURRENT_PLAYLISTS.set(session_key, token, start, time.time())
        _resume_save(session_key, "playlist", tracks=rows, label=label,
                     quality=quality or info.get("quality"), index=start,
                     token=token, loop=loop_all)
        args_json = json.dumps(info.get("device_args") or {}, ensure_ascii=False)
        order_note = "，随机顺序" if shuffle else ""
        return ("准备%s：%s，共 %d 首%s，当前第 %d 首\n"
                "歌曲：%s%s\n"
                "直链（%d 分钟内有效）：%s\n"
                "歌词直链：%s\n"
                "单曲时长：%d 秒\n"
                "device_args：%s\n"
                "设备工具：self.music.play_url。参数取 device_args：url 填直链，title 填「%s」，"
                "artist 填「%s」，lyric_url 填歌词直链（没有就留空），duration 填 %d。"
                "歌词由设备每 10 秒通过 music_status 自动刷新；说「下一曲/上一曲」时调用 music_next/music_previous 后再用 self.music.play_url 播放新的 device_args。"
                % (action, label, total, order_note, start + 1,
                   info.get("title") or "", (" - " + info.get("artist")) if info.get("artist") else "",
                   max(1, (info.get("expires_in") or URL_TTL) // 60), info["url"],
                   info.get("lyric_url") or "（暂未取到，留空）",
                   int(info.get("duration") or 0), args_json,
                   info.get("title") or "", info.get("artist") or "",
                   int(info.get("duration") or 0)))
    # 单曲播放明确结束上一段连续流，避免 music_status 继续沿用旧歌单状态。
    CURRENT_PLAYLISTS.clear(session_key)
    if guid:
        track = {"guid": guid, "title": str(args.get("title") or "").strip() or guid,
                 "artist": str(args.get("artist") or "").strip()}
        info_fn = getattr(p, "track_info", None)
        if callable(info_fn):
            try:
                found = info_fn(guid)
            except Exception:
                found = None
            if isinstance(found, dict):
                track.update({k: v for k, v in found.items() if v})
        label, idx, total = "按 guid", 1, 1
    else:
        if pre_rows is not None:
            rows, label = pre_rows, pre_label
        else:
            rows, label = pick_tracks(p, query_text, playlist, 20)
        idx = _to_int(args.get("index"), 1)
        if idx < 1 or idx > len(rows):
            raise BadRequest("序号 %d 超出范围（共 %d 首）" % (idx, len(rows)))
        track = rows[idx - 1]
        total = len(rows)
    if loop_one:
        only_label = "单曲循环"
        only_info = build_playlist_info(p, [track], only_label, base, quality,
                                        action="单曲循环", start_index=0,
                                        session_key=session_key, loop=True)
        only_token = only_info.get("playlist_token")
        if only_token:
            CURRENT_PLAYLISTS.set(session_key, only_token, 0, time.time())
        _resume_save(session_key, "playlist", tracks=[track], label=only_label,
                     quality=quality or only_info.get("quality"), index=0,
                     token=only_token, loop=True)
        return _format_list_playback(only_info, only_label, 1, 0, False, "单曲循环")
    info = PLAYBACK.prepare(track, p, base, quality=quality, wait=False)
    _resume_save(session_key, "single", track=track,
                 quality=quality or info.get("quality"), position=0.0)
    src_line = ("\n来源：在线音源 %s" % info["source"]) if info["source"] != "local" else ""
    prev_line = ""
    if info["preview"]:
        prev_line = ("\n注意：在线音源只给到试听片段（约 %d 秒）；说「下载」可保存完整版到本地曲库。" % int(info["duration"] or 0))
    save_line = "，正在后台保存到本地曲库" if info["saving"] else ""
    prep_line = "\n音频正在后台准备，设备可直接用直链播放。" if info.get("preparing") else ""
    return ("准备播放：%s%s\n%s 第 %d/%d 首，音质 %s（%s kbps）%s%s%s\n直链（%d 分钟内有效）：%s\n"
            "歌词直链：%s\n"
            "总时长：%d 秒\n"
            "设备工具：self.music.play_url，参数 url 填上面的直链，title 填「%s」，"
            "artist 填「%s」，lyric_url 填上面的歌词直链（没有歌词就留空），"
            "duration 填 %d（总秒数，整数）。" % (
                info["title"], (" - " + info["artist"]) if info["artist"] else "", label, idx, total,
                info["quality_label"], info["bitrate"], src_line, prev_line + save_line, prep_line,
                max(1, (info.get("expires_in") or URL_TTL) // 60), info["url"],
                info["lyric_url"] or "（这首歌查不到歌词，留空即可）",
                int(info.get("duration") or 0),
                info["title"], info["artist"] or "", int(info.get("duration") or 0)))


def _tool_music_skip(args, p, base, quality, delta):
    """在当前收藏/歌单连续流里前后跳一首，返回该单曲的完整 device_args。"""
    session_key = _session_key(args)
    cursor = CURRENT_PLAYLISTS.get(session_key)
    token = cursor.get("token") if cursor else None
    item = PLAYLISTS.get(token) if token else None
    if not item or not item.get("tracks"):
        return "当前没有正在播放的收藏或歌单。请先让我播放收藏/歌单，再说下一曲或上一曲。"
    tracks = item["tracks"]
    total = len(tracks)
    current = int(cursor.get("index", 0)) if cursor else 0
    target = current + int(delta)
    if target < 0:
        return "已经是第一首了，没有上一首（共 %d 首，当前第 %d 首）。" % (total, current + 1)
    if target >= total:
        return "已经是最后一首了（共 %d 首，当前第 %d 首）。" % (total, current + 1)
    track = tracks[target]
    info = PLAYBACK.prepare(dict(track), p, base, quality=quality, wait=False)
    CURRENT_PLAYLISTS.set(session_key, token, target, time.time())
    _prefetch_playlist_next(tracks, target, p, quality or item.get("quality"),
                            bool(item.get("loop")))
    _resume_save(session_key, "playlist", tracks=tracks, label=item.get("label") or "",
                 quality=quality or item.get("quality"), index=target, token=token,
                 loop=bool(item.get("loop")))
    args_json = json.dumps(info.get("device_args") or {}, ensure_ascii=False)
    what = "下一首" if delta > 0 else "上一首"
    return ("准备播放%s：%s%s\n"
            "来自：%s 第 %d/%d 首\n"
            "直链（%d 分钟内有效）：%s\n"
            "歌词直链：%s\n"
            "单曲时长：%d 秒\n"
            "device_args：%s\n"
            "设备工具：self.music.play_url。参数取 device_args：url 填直链，title 填「%s」，"
            "artist 填「%s」，lyric_url 填歌词直链（没有就留空），duration 填 %d。"
            % (what, info.get("title") or "", (" - " + info.get("artist")) if info.get("artist") else "",
               item.get("label") or "当前歌单", target + 1, total,
               max(1, (info.get("expires_in") or URL_TTL) // 60), info["url"],
               info.get("lyric_url") or "（这首歌查不到歌词，留空即可）",
               int(info.get("duration") or 0), args_json,
               info.get("title") or "", info.get("artist") or "",
               int(info.get("duration") or 0)))


def _tool_music_next(args, p, base, limit, quality):
    return _tool_music_skip(args, p, base, quality, +1)


def _tool_music_previous(args, p, base, limit, quality):
    return _tool_music_skip(args, p, base, quality, -1)


def _tool_music_favorite(args, p, base, limit, quality):
    action = str(args.get("action") or "").lower()
    q = str(args.get("query") or "").strip()
    guid = str(args.get("guid") or "").strip()
    title = str(args.get("title") or "").strip()
    artist = str(args.get("artist") or "").strip()
    current = _truthy(args.get("current"))
    if action not in ("add", "remove"):
        raise BadRequest("action 只能是 add 或 remove")
    if current:
        t = _current_playback_track(_session_key(args))
        if not t:
            raise BadRequest("当前没有正在播放的歌曲，无法按 current 收藏/取消收藏")
    elif guid:
        t = {"guid": guid, "title": title or guid, "artist": artist}
        info_fn = getattr(p, "track_info", None)
        if callable(info_fn):
            try:
                found = info_fn(guid)
            except Exception:
                found = None
            if isinstance(found, dict):
                t.update({k: v for k, v in found.items() if v})
    elif q:
        rows = p.search(q, 5)
        if not rows:
            raise BadRequest("没找到「%s」" % q)
        t = rows[0]
    else:
        raise BadRequest("需要 query、guid 或 current=true 之一")
    if action == "add":
        if is_ext_guid(t["guid"]) and hasattr(p, "download_ext"):
            fp, n = p.download_ext(t, quality or "lossless")
            return "已收藏并下载到本地曲库：%s（%s，%.1f MB）" % (
                t.get("title") or t["guid"], fp, n / 1048576.0)
        p.add_favorite(t["guid"])
        return "已收藏：%s" % (t.get("title") or t["guid"])
    p.remove_favorite(t["guid"])
    return "已取消收藏：%s" % (t.get("title") or t["guid"])


def _tool_music_download(args, p, base, limit, quality):
    guid = str(args.get("guid") or "").strip()
    q = str(args.get("query") or "").strip()
    if not LIBRARY.enabled():
        raise BadRequest("未配置 GW_DOWNLOAD_DIR，无法保存到本地")
    if guid:
        track = resolve_download_track(
            p, guid, args.get("title") or "", args.get("artist") or "")
    elif q:
        rows = p.search(q, 5)
        if not rows:
            raise BadRequest("没找到「%s」" % q)
        track = rows[0]
    else:
        raise BadRequest("guid 或 query 至少给一个")
    fp, n = LIBRARY.save_from_provider(p, track, quality or DEFAULT_QUALITY)
    return "已保存到本地曲库：%s（%s，%.1f MB）" % (track.get("title") or track["guid"], fp, n / 1048576.0)


def _tool_music_sources(args, p, base, limit, quality):
    settings = online_settings()
    if not online_active(settings):
        return "未启用在线音源：请在网页「设置 → 在线音源」选择平台并启用。"
    rows = online_status()
    lines = ["在线音源 %d 个：" % len(rows)]
    for item in rows:
        lines.append("%s / %s %s" % (item.get("source"), item.get("platform") or item.get("url"),
                                     " 正常" if item.get("healthy") else " 不可用"))
    return "\n".join(lines)


def _tool_music_storage(args, p, base, limit, quality):
    c = CACHE.stats()
    lib = LIBRARY.stats()
    return ("播放缓存：%d 个文件，%.1f MB（上限 %.1f MB）\n本地曲库：%s，%d 个文件，%.1f MB" % (
        c.get("files", 0), c.get("bytes", 0) / 1048576.0,
        c.get("limit_bytes", 0) / 1048576.0,
        lib.get("dir") or "未配置", lib.get("files", 0), lib.get("bytes", 0) / 1048576.0))


def _tool_music_status(args, p, base, limit, quality):
    # 连续流有游标时，返回“正在播放”的第 N 首，供设备在播放中刷新歌名/歌词/单曲时长。
    session_key = _session_key(args)
    cursor = CURRENT_PLAYLISTS.get(session_key)
    token = cursor.get("token") if cursor else None
    item = PLAYLISTS.get(token, copy_tracks=False) if token else None
    if item and item.get("tracks"):
        tracks = item["tracks"]
        idx = max(0, min(int(cursor.get("index", 0)), len(tracks) - 1))
        track = tracks[idx]
        _prefetch_playlist_next(tracks, idx, p, item.get("quality") or quality,
                                bool(item.get("loop")))
        started = float(cursor.get("started_at") or time.time())
        elapsed = max(0, int(time.time() - started))
        try:
            duration = int(float(track.get("duration_s") or 0))
        except (TypeError, ValueError):
            duration = 0
        lyric = _lrc_line_at(track, elapsed)
        _resume_save_status(session_key, "playlist", tracks=tracks, label=item.get("label") or "",
                            quality=item.get("quality"), index=idx, position=min(elapsed, duration or elapsed),
                            token=token, loop=bool(item.get("loop")))
        return ("正在播放：%s%s\n"
                "来自：%s 第 %d/%d 首\n"
                "时长：%d 秒\n"
                "已播放：%d 秒\n"
                "歌词：%s" % (
                    track.get("title") or "", (" - " + track["artist"]) if track.get("artist") else "",
                    item.get("label") or "当前歌单", idx + 1, len(tracks),
                    duration, elapsed, lyric))
    st = load_state()
    if not st.get("title"):
        return "还没播放过。"
    at = _to_int(st.get("at"))
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at)) if at > 0 else "时间未知"
    qn = ("（音质 %s）" % st.get("quality")) if st.get("quality") else ""
    if st.get("guid"):
        elapsed = max(0, int(time.time() - _to_int(st.get("at"), int(time.time()))))
        duration = _to_int(st.get("duration"), 0)
        if duration > 0:
            elapsed = min(elapsed, duration)
        _resume_save_status(session_key, "single",
                            track={"guid": st["guid"], "title": st.get("title") or st["guid"],
                                   "artist": st.get("artist") or "", "duration_s": st.get("duration")},
                            quality=st.get("quality"), position=elapsed)
    return "上一次播放：%s%s%s（%s）" % (
        st.get("title"), (" - " + st["artist"]) if st.get("artist") else "", qn, when)


def _format_resume_single(info, position):
    args_json = json.dumps(info.get("device_args") or {}, ensure_ascii=False)
    return ("准备继续播放：%s%s\n"
            "从约 %d 秒继续\n"
            "直链（%d 分钟内有效）：%s\n"
            "歌词直链：%s\n"
            "单曲时长：%d 秒\n"
            "device_args：%s\n"
            "设备工具：self.music.play_url。参数取 device_args：url 填直链，title 填「%s」，"
            "artist 填「%s」，lyric_url 填歌词直链（没有就留空），duration 填 %d。"
            % (info.get("title") or "", (" - " + info["artist"]) if info.get("artist") else "",
               int(position or 0), max(1, (info.get("expires_in") or URL_TTL) // 60),
               info["url"], info.get("lyric_url") or "（这首歌查不到歌词，留空即可）",
               int(info.get("duration") or 0), args_json, info.get("title") or "",
               info.get("artist") or "", int(info.get("duration") or 0)))


def _tool_music_resume(args, p, base, limit, quality):
    """从本会话保存的续播记录继续播放；单曲按秒数切片，列表按原游标恢复。"""
    session_key = _session_key(args)
    st = _resume_get(session_key) or {}
    kind = str(st.get("kind") or "")
    if kind == "playlist" and st.get("tracks"):
        return _saved_list_response(st, p, base, quality, session_key,
                                     action="继续播放", loop=bool(st.get("loop")))
    track = None
    position = 0
    if kind == "single" and isinstance(st.get("track"), dict) and st["track"].get("guid"):
        track = dict(st["track"])
        try:
            position = max(0, int(float(st.get("position") or 0)))
        except (TypeError, ValueError):
            position = 0
    if not track:
        track = _current_playback_track(session_key)
    if not track:
        return "没有找到可继续播放的记录。请先播放歌曲或歌单，再让我继续播放。"
    q = quality or st.get("quality")
    info = PLAYBACK.prepare(track, p, base, quality=q, wait=False, start=position)
    _resume_save(session_key, "single", track=track, quality=q, position=position)
    return _format_resume_single(info, position)


def _tool_music_stop(args, p, base, limit, quality):
    """停止当前播放，但保留续播位置，用户之后说继续播放可以接着放。"""
    session_key = _session_key(args)
    cursor = CURRENT_PLAYLISTS.get(session_key)
    token = cursor.get("token") if cursor else None
    item = PLAYLISTS.get(token) if token else None
    saved = False
    if item and item.get("tracks"):
        tracks = item["tracks"]
        idx = max(0, min(int(cursor.get("index", 0)), len(tracks) - 1))
        track = tracks[idx]
        started = float(cursor.get("started_at") or time.time())
        elapsed = max(0, int(time.time() - started))
        try:
            duration = int(float(track.get("duration_s") or 0))
        except (TypeError, ValueError):
            duration = 0
        _resume_save(session_key, "playlist", tracks=tracks, label=item.get("label") or "",
                     quality=item.get("quality"), index=idx,
                     position=min(elapsed, duration or elapsed), token=token,
                     loop=bool(item.get("loop")))
        saved = True
    else:
        st = load_state()
        if st.get("guid"):
            elapsed = max(0, int(time.time() - _to_int(st.get("at"), int(time.time()))))
            duration = _to_int(st.get("duration"), 0)
            if duration > 0:
                elapsed = min(elapsed, duration)
            _resume_save(session_key, "single",
                         track={"guid": st["guid"], "title": st.get("title") or st["guid"],
                                "artist": st.get("artist") or "",
                                "duration_s": st.get("duration")},
                         quality=st.get("quality"), position=elapsed)
            saved = True
    CURRENT_PLAYLISTS.clear(session_key)
    if saved:
        return "已停止当前播放，已记录播放进度。下次说「继续播放」可以从这里接着放。\n设备工具：self.music.stop"
    return "当前没有正在播放的音乐。\n设备工具：self.music.stop"


def _tool_music_loop(args, p, base, limit, quality):
    """切换当前播放的循环模式：one 单曲循环 / all 列表循环 / off 关闭循环。"""
    session_key = _session_key(args)
    mode = str(args.get("mode") or "").strip().lower()
    if mode in ("one", "single", "单曲循环", "单曲"):
        track = _current_playback_track(session_key)
        if not track:
            return "当前没有正在播放的歌曲，无法切换单曲循环。请先播放一首歌。"
        st = {"tracks": [track], "label": "单曲循环", "quality": quality,
              "loop": True, "index": 0}
        return _saved_list_response(st, p, base, quality, session_key,
                                    action="单曲循环", loop=True, start=0)
    if mode in ("all", "list", "列表循环", "循环"):
        st = _resume_get(session_key) or {}
        if not st.get("tracks"):
            track = _current_playback_track(session_key)
            if not track:
                return "当前没有正在播放的歌曲，无法切换列表循环。请先播放歌单或收藏。"
            st = {"tracks": [track], "label": "单曲循环", "quality": quality, "index": 0}
        return _saved_list_response(st, p, base, quality, session_key,
                                    action="循环播放", loop=True)
    if mode in ("off", "none", "顺序播放", "关闭循环", "normal"):
        st = _resume_get(session_key) or {}
        if st.get("tracks"):
            return _saved_list_response(st, p, base, quality, session_key,
                                        action="顺序播放", loop=False)
        track = _current_playback_track(session_key)
        if not track:
            return "当前没有正在播放的歌曲，无法关闭循环。"
        info = PLAYBACK.prepare(track, p, base, quality=quality, wait=False)
        _resume_save(session_key, "single", track=track,
                     quality=quality or info.get("quality"), position=0)
        return _format_resume_single(info, 0)
    raise BadRequest("mode 只能是 one / all / off")


_TOOL_HANDLERS = {
    "music_search": _tool_music_search,
    "music_browse": _tool_music_browse,
    "music_playlists": _tool_music_playlists,
    "music_playlist_tracks": _tool_music_playlist_tracks,
    "music_favorites": _tool_music_favorites,
    "music_play": _tool_music_play,
    "music_next": _tool_music_next,
    "music_previous": _tool_music_previous,
    "music_favorite": _tool_music_favorite,
    "music_loop": _tool_music_loop,
    "music_resume": _tool_music_resume,
    "music_stop": _tool_music_stop,
    "music_download": _tool_music_download,
    "music_sources": _tool_music_sources,
    "music_storage": _tool_music_storage,
    "music_status": _tool_music_status,
}


def call_tool(name, args, base, session_id=""):
    """按工具名分派到独立实现，便于阅读、单测和后续扩展。"""
    args = dict(args or {})
    if session_id and not args.get("_session_id"):
        args["_session_id"] = str(session_id)
    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        raise RuntimeError("未知工具：%s" % name)
    p = _LazyProvider()
    limit = clamp_limit(args.get("limit"), 10)
    quality = args.get("quality")
    return handler(args, p, base, limit, quality)


# 官方 MCPServer 2.3：工具注册/Schema/校验/错误包装由官方库负责；
# 对外仍走 8766 的 /mcp，官方 ASGI 应用只监听 127.0.0.1 随机端口。
MCP_SERVICE = McpService(call_tool, VERSION,
                         lambda: PUBLIC_BASE or ("http://127.0.0.1:%d" % PORT), logger=LOG)
MCP_BRIDGE = McpHttpBridge(MCP_SERVICE, timeout=advanced_settings()["mcp_timeout"], logger=LOG)


def _resume_audio_file(path, start):
    """从缓存 MP3 生成从 start 秒开始的切片，断点续播用；失败时退回原文件。"""
    try:
        start = max(0, int(start or 0))
    except (TypeError, ValueError):
        return path
    if start <= 0 or not path:
        return path
    src = os.path.abspath(str(path))
    dst = "%s.resume-%d.mp3" % (os.path.splitext(src)[0], start)
    try:
        if os.path.getsize(dst) > 1024:
            return dst
    except OSError:
        pass
    tmp = dst + ".part"
    try:
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-i", src, "-ss", str(start), "-vn", "-map_metadata", "-1",
                        "-c:a", "copy", "-f", "mp3", tmp],
                       check=True, timeout=180)
        if os.path.getsize(tmp) > 1024:
            os.replace(tmp, dst)
            return dst
    except Exception as exc:
        LOG.warning("resume slice %s from %ss failed: %s", src, start, exc)
    try:
        os.remove(tmp)
    except OSError:
        pass
    return path


def _prefetch_playlist_next(tracks, index, p, quality=None, loop=False):
    """后台预取当前曲目的下一首，降低连续播放/手动切歌的等待。"""
    if not tracks:
        return
    try:
        current = max(0, int(index or 0))
    except (TypeError, ValueError):
        return
    if current >= len(tracks):
        return
    nxt = current + 1
    if nxt >= len(tracks):
        if not loop:
            return
        nxt = 0
    if nxt == current:
        return
    track = tracks[nxt]
    if not isinstance(track, dict) or not track.get("guid"):
        return
    try:
        if hasattr(CACHE, "prefetch"):
            CACHE.prefetch(track["guid"], p, quality=quality or "standard")
    except Exception:
        LOG.debug("prefetch next playlist track failed", exc_info=True)


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

    def _sse_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self._sse_broken = False

    def _sse_send(self, obj):
        if getattr(self, "_sse_broken", False):
            return False
        data = ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")
        try:
            self.wfile.write(("%X\r\n" % len(data)).encode("ascii") + data + b"\r\n")
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            self._sse_broken = True
            return False

    def _sse_end(self):
        if getattr(self, "_sse_broken", False):
            return
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self._sse_broken = True

    def _handle_api_search_stream(self, qs):
        q = (qs.get("q") or [""])[0].strip()
        if not q:
            raise BadRequest("参数 q 不能为空")
        limit = clamp_limit(self._qs_int(qs, "limit", 10), 10)
        p = provider()
        fixed, changed = apply_aliases(q)
        self._sse_start()
        self._sse_send({"type": "start", "query": q, "corrected": fixed if changed else ""})
        count = 0
        seen = set()
        metas = []

        def _meta():
            meta_fn = getattr(p, "last_search_meta", None)
            if callable(meta_fn):
                try:
                    got = meta_fn() or {}
                except Exception:
                    got = {}
                if isinstance(got, dict):
                    metas.append(got)

        def _emit(track):
            key = (_norm_text(track.get("title")), _norm_text(track.get("artist")))
            if key in seen:
                return None  # 重复结果，不发送也不计数
            seen.add(key)
            return self._sse_send({"type": "track", "track": track})

        def _iter(term, want):
            stream = getattr(p, "search_stream", None)
            if callable(stream):
                return stream(term, want)
            return p.search(term, want)

        try:
            broken = False
            for track in _iter(fixed, limit):
                sent = _emit(track)
                if sent is False:
                    broken = True
                    break
                if sent:
                    count += 1
            _meta()
            if changed and not broken and count < limit:
                for track in _iter(q, limit - count):
                    sent = _emit(track)
                    if sent is False:
                        broken = True
                        break
                    if sent:
                        count += 1
                _meta()
            local_count = sum(int(m.get("local_count") or 0) for m in metas)
            online_count = sum(int(m.get("online_count") or 0) for m in metas)
            if not metas:
                online_count = max(0, count - local_count)
            local_error = "；".join(str(m.get("local_error")) for m in metas if m.get("local_error"))
            online_error = "；".join(str(m.get("online_error")) for m in metas if m.get("online_error"))
            self._sse_send({"type": "done", "query": q, "count": count,
                            "corrected": fixed if changed else "",
                            "local_count": local_count,
                            "online_count": online_count,
                            "local_error": local_error,
                            "online_error": online_error})
        except Exception as exc:
            LOG.exception("GET /api/search/stream failed")
            self._sse_send({"type": "error", "error": str(exc)})
        finally:
            self._sse_end()

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

    def _request_base(self):
        """本次请求返回给设备的直链前缀：显式 PUBLIC_BASE 优先；桥接器可注入合法 X-GW-Base。"""
        base = self._base()
        if not PUBLIC_BASE:
            incoming = _device_base(self.headers.get("X-GW-Base"))
            if incoming:
                base = incoming
        return base

    def _client_key(self):
        """同一来源地址视为同一设备/会话；MCP 侧还可显式传 session_id。"""
        try:
            host = (self.client_address or ("",))[0]
        except Exception:
            host = ""
        return str(host or "default")

    def _mcp_session_key(self):
        """MCP 会话键：优先客户端/桥接器显式注入的会话头，其次来源地址。"""
        for key in ("X-GW-Session", "x-gw-session", "Mcp-Session-Id", "mcp-session-id"):
            value = (self.headers.get(key) or "").strip()
            if value:
                return value
        return self._client_key()

    def _request_session_key(self, qs=None):
        """REST 会话键：优先 ?session=/?s=，其次 X-GW-Session，最后来源地址。"""
        if qs:
            for key in ("session", "s"):
                value = (qs.get(key) or [""])[0].strip()
                if value:
                    return value
        value = (self.headers.get("X-GW-Session") or "").strip()
        return value or self._client_key()

    def _admin_cookie_ok(self):
        raw = self.headers.get("Cookie") or ""
        if not raw:
            return False
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except Exception:
            return False
        morsel = jar.get(ADMIN_COOKIE)
        return bool(morsel) and hmac.compare_digest(morsel.value, ADMIN_TOKEN)

    def _admin_cookie_header(self):
        jar = SimpleCookie()
        jar[ADMIN_COOKIE] = ADMIN_TOKEN
        morsel = jar[ADMIN_COOKIE]
        morsel["path"] = "/"
        morsel["httponly"] = True
        morsel["samesite"] = "Lax"
        if (self.headers.get("X-Forwarded-Proto") or "").lower() == "https":
            morsel["secure"] = True
        return ("Set-Cookie", morsel.OutputString())

    def _admin_cookie_clear_header(self):
        jar = SimpleCookie()
        jar[ADMIN_COOKIE] = ""
        morsel = jar[ADMIN_COOKIE]
        morsel["path"] = "/"
        morsel["max-age"] = "0"
        morsel["expires"] = "Thu, 01 Jan 1970 00:00:00 GMT"
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
                or path == "/playlist" or path.startswith("/playlist/")
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

    def _login_url(self, next_path="/"):
        return "/login?next=" + urllib.parse.quote(next_path or "/", safe="")

    def _handle_login_get(self, u, qs):
        nxt = _safe_next_path((qs.get("next") or ["/"])[0])
        if not ADMIN_TOKEN or self._admin_credential_ok():
            return self._redirect(nxt)
        return self._raw(login_html(nxt).encode("utf-8"), 200,
                         "text/html; charset=utf-8", [("Cache-Control", "no-store")])

    def _handle_login_post(self):
        body = self._form_body()
        nxt = _safe_next_path(str(body.get("next") or "/"))
        if not ADMIN_TOKEN:
            return self._redirect(nxt)
        password = str(body.get("password") or "")
        if password and hmac.compare_digest(password, ADMIN_TOKEN):
            return self._redirect(nxt, [self._admin_cookie_header()])
        return self._raw(login_html(nxt, "密码错误").encode("utf-8"), 401,
                         "text/html; charset=utf-8", [("Cache-Control", "no-store")])

    def _handle_logout(self):
        return self._redirect("/login", [self._admin_cookie_clear_header()])

    def _handle_settings(self, u, qs):
        """网关设置页：支持 ?token= 引导 Cookie，其余走 Cookie 鉴权。"""
        token = (qs.get("token") or [""])[0]
        if token and ADMIN_TOKEN:
            if not hmac.compare_digest(token, ADMIN_TOKEN):
                return self._unauthorized()
            return self._redirect(self._strip_token(u) or "/settings", [self._admin_cookie_header()])
        if not self._admin_credential_ok():
            return self._redirect(self._login_url("/settings"))
        return self._raw(SETTINGS_HTML.encode("utf-8"), 200, "text/html; charset=utf-8")

    def _handle_settings_get(self):
        s = fnos_settings()
        return self._json({
            "base": s["base"], "username": s["username"],
            "password_set": bool(s["password"]) or s["token_cached"],
            "device_id": s["device_id"], "insecure_tls": s["insecure_tls"],
            "source": s["source"], "token_cached": s["token_cached"],
            "configured": fnos_configured(), "download_dir": DOWNLOAD_DIR,
        })

    def _handle_settings_put(self):
        body = self._body()
        patch = {}
        for key in ("base", "username", "password", "device_id"):
            if key not in body:
                continue
            if key == "password" and not str(body.get(key) or "").strip():
                continue  # 密码留空表示不修改
            patch[key] = body.get(key)
        if "base" in patch:
            patch["base"] = normalize_base(patch["base"])
        if "insecure_tls" in body:
            patch["insecure_tls"] = "1" if _truthy(body.get("insecure_tls")) else ""
        base = str(patch.get("base", fnos_settings()["base"]) or "").strip()
        if base and not re.match(r"^https?://[^\s]+$", base):
            raise BadRequest("地址必须以 http:// 或 https:// 开头")
        FNOS_CONFIG.update("fnos", patch)
        apply_fnos_tls()
        reset_provider()
        s = fnos_settings()
        LOG.info("飞牛音乐配置已更新：%s（用户 %s）", s["base"] or "-", s["username"] or "-")
        return self._handle_settings_get()

    def _handle_settings_test(self):
        body = self._body()
        s = fnos_settings()
        password = body.get("password")
        if password in (None, ""):
            password = s["password"]
        cfg = {"base": normalize_base(body.get("base") or s["base"]),
               "username": str(body.get("username") or s["username"]).strip(),
               "password": password or "",
               "device_id": str(body.get("device_id") or s["device_id"]).strip()}
        if not cfg["base"]:
            raise BadRequest("请先填写飞牛音乐地址")
        if not cfg["username"]:
            raise BadRequest("请先填写账号")
        from fnmusic_client import FnMusicClient
        if "insecure_tls" in body:
            insecure_tls = _truthy(body.get("insecure_tls"))
        else:
            insecure_tls = bool(s.get("insecure_tls"))
        started = time.time()
        try:
            client = FnMusicClient(base=cfg["base"], username=cfg["username"],
                                   password=cfg["password"] or None,
                                   device_id=cfg["device_id"] or None,
                                   token_file=None, timeout=min(FNOS_TIMEOUT, 10),
                                   insecure_tls=insecure_tls)
            client.ensure_token()
            tracks = _rows(client.track_list(1, 3))
            return self._json({"ok": True, "ms": int((time.time() - started) * 1000),
                               "tracks": len(tracks), "message": "连接成功"})
        except Exception as e:
            return self._json({"ok": False, "ms": int((time.time() - started) * 1000),
                               "error": str(e), "message": "连接失败"})

    def _handle_settings_logout(self):
        try:
            os.remove(FNOS_TOKEN_FILE)
        except FileNotFoundError:
            pass
        except OSError as e:
            LOG.warning("删除登录令牌失败：%s", e)
        reset_provider()
        return self._json({"ok": True, "token_cached": False})

    def _handle_advanced_get(self):
        return self._json(advanced_settings())

    def _handle_advanced_put(self):
        body = self._body()
        vals = save_advanced_settings(body)
        LOG.info("高级设置已更新：%s", json.dumps(vals, ensure_ascii=False))
        return self._json(vals)

    def _handle_online_get(self):
        s = online_settings()
        s["netease_cookie_set"] = bool(str(s.get("netease_cookie") or "").strip())
        s.pop("netease_cookie", None)
        s["services"] = [{"source": "musicdl", "platform": p, "url": "native"}
                         for p in online_platforms(s)]
        s["deps_available"] = online_deps_available()
        s["deps_error"] = online_deps_error()
        return self._json(s)

    def _handle_online_put(self):
        body = self._body()
        vals = save_online_settings(body)
        LOG.info("在线音源设置已更新：enabled=%s platforms=%s",
                 vals["enabled"], ",".join(online_platforms(vals)) or "-")
        return self._handle_online_get()

    def _handle_online_test(self):
        body = self._body()
        platform = str(body.get("platform") or "").strip()
        if platform and not re.match(r"^[A-Za-z][A-Za-z0-9_]{0,63}$", platform):
            raise BadRequest("平台名非法")
        settings = online_settings()
        platforms = [platform] if platform else online_platforms(settings)
        if not platforms:
            raise BadRequest("没有可测试的平台，请先选择平台")
        started = time.time()
        provider = new_native_provider(settings, platforms=platforms,
                                       timeout=min(15, settings["timeout"]))
        try:
            results = [provider.probe(name) for name in platforms]
        finally:
            provider.close()
        return self._json({
            "ok": any(r.get("ok") for r in results),
            "ms": int((time.time() - started) * 1000),
            "results": results})

    def _handle_online_qr_start(self):
        # 消费请求体，保持 HTTP keep-alive 正常；只返回 key/二维码，不返回或记录任何 Cookie。
        self._body()
        return self._json(netease_login.start_qr())

    def _handle_online_qr_status(self, qs):
        key = (qs.get("key") or [""])[0].strip()
        if not key:
            raise BadRequest("缺少二维码 key")
        result = netease_login.poll_qr(key)
        if result.get("status") == "success":
            cookie = result.pop("cookie", "")
            save_netease_cookie(cookie)
            result["netease_cookie_set"] = True
            LOG.info("网易云扫码登录成功，已保存 MUSIC_U")
        return self._json(result)

    def _handle_root(self, u, qs):
        token = (qs.get("token") or [""])[0]
        if token and ADMIN_TOKEN:
            if not hmac.compare_digest(token, ADMIN_TOKEN):
                return self._unauthorized()
            return self._redirect(self._strip_token(u) or "/", [self._admin_cookie_header()])
        if not self._admin_credential_ok():
            return self._redirect(self._login_url("/"))
        return self._raw(HOME_HTML.encode("utf-8"), 200, "text/html; charset=utf-8")

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

    def _raw_body(self, cap=HTTP_BODY_CAP):
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            chunks, total = [], 0
            while True:
                line = self.rfile.readline(1024)
                if not line:
                    break
                try:
                    size = int((line.split(b";", 1)[0] or b"0").strip() or b"0", 16)
                except ValueError:
                    raise BadRequest("chunked 长度非法")
                if size < 0:
                    raise BadRequest("chunked 长度非法")
                if size == 0:
                    while True:
                        trailer = self.rfile.readline(1024)
                        if not trailer or trailer in (b"\r\n", b"\n"):
                            break
                    break
                total += size
                if total > cap:
                    raise BadRequest("请求体过大（上限 %d 字节）" % cap)
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)
            return b"".join(chunks)
        n = _to_int(self.headers.get("Content-Length"), 0)
        if n < 0:
            raise BadRequest("Content-Length 非法")
        if n > cap:
            raise BadRequest("请求体过大（上限 %d 字节）" % cap)
        return self.rfile.read(n) if n else b""

    def _body(self, cap=HTTP_BODY_CAP):
        raw = self._raw_body(cap)
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            raise BadRequest("请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise BadRequest("请求体必须是 JSON 对象")
        return data

    def _form_body(self):
        raw = self._raw_body()
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype == "application/json":
            if not raw.strip():
                return {}
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                raise BadRequest("请求体不是合法 JSON")
            if not isinstance(data, dict):
                raise BadRequest("请求体必须是 JSON 对象")
            return data
        try:
            fields = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True)
        except Exception:
            raise BadRequest("请求体不是合法表单")
        return {k: (v[0] if v else "") for k, v in fields.items()}

    def do_GET(self):
        try:
            u, qs = self._qs()
            path = u.path
            if path in ("/login", "/login/"):
                return self._handle_login_get(u, qs)
            if path in ("/logout", "/logout/"):
                return self._handle_logout()
            if path == "/":
                return self._handle_root(u, qs)
            if path in ("/settings", "/settings/"):
                return self._handle_settings(u, qs)
            if not self._admin_ok(path):
                return self._unauthorized()
            if path == "/healthz":
                # 存活探针：只证明进程活着，不探音源（音源掉线时缓存仍能服务）
                return self._json({"ok": True, "version": VERSION,
                                   "mode": gateway_mode(),
                                   "mock": MOCK,
                                   "fnos": {"configured": fnos_configured(),
                                            "source": fnos_settings()["source"],
                                            "token_cached": fnos_settings()["token_cached"]},
                                   "provider_error": provider_error(),
                                   "online": {"enabled": online_active(),
                                              "platforms": online_services(),
                                              "deps": online_deps_available(),
                                              "error": online_deps_error()},
                                   "quality_default": QUALITY_DEFAULT,
                                   "quality_profiles": describe_quality(),
                                   "mcp_server": {"available": MCP_SERVICE.available,
                                                  "engine": "mcp>=2.3",
                                                  "error": str(MCP_SERVICE.import_error) if MCP_SERVICE.import_error else None},
                                   "auth_required": bool(ADMIN_TOKEN),
                                   "cache_mb": round(self._cache_mb(), 1)})
            if path == "/api/settings/fnos":
                return self._handle_settings_get()
            if path == "/api/settings/online":
                return self._handle_online_get()
            if path == "/api/settings/online/qr":
                return self._handle_online_qr_status(qs)
            if path == "/api/settings/advanced":
                return self._handle_advanced_get()
            if path == "/readyz":
                # 就绪探针：真探一次数据面，音源全挂时回 503
                return self._handle_readyz()
            if path == "/play" or path.startswith("/play/"):
                return self._handle_play(u, qs)
            if path == "/playlist" or path.startswith("/playlist/"):
                return self._handle_playlist(u, qs)
            if path == "/lyrics" or path.startswith("/lyrics/"):
                return self._handle_lyrics(u, qs)
            if path == "/api/search/stream":
                return self._handle_api_search_stream(qs)
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
                meta_fn = getattr(p, "last_search_meta", None)
                if callable(meta_fn):
                    try:
                        meta = meta_fn() or {}
                    except Exception:
                        meta = {}
                    if isinstance(meta, dict):
                        out.update(meta)
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
            if path == "/api/play":
                return self._handle_api_play(qs)
            if path == "/api/status":
                return self._json(load_state() or {"title": None})
            if path == "/api/sources":
                return self._json({"sources": online_status()})
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
            if u.path in ("/login", "/login/"):
                return self._handle_login_post()
            if not self._admin_ok(u.path):
                return self._unauthorized()
            if u.path == "/api/settings/fnos/test":
                return self._handle_settings_test()
            if u.path == "/api/settings/fnos/logout":
                return self._handle_settings_logout()
            if u.path == "/api/settings/online/test":
                return self._handle_online_test()
            if u.path == "/api/settings/online/qr":
                return self._handle_online_qr_start()
            if u.path == "/mcp":
                return self._handle_mcp()
            if u.path == "/api/favorite":
                body = self._body()
                p = provider()
                action = str(body.get("action") or "add").lower()
                if action not in ("add", "remove"):
                    raise BadRequest("action 只能是 add 或 remove")
                guid = str(body.get("guid") or "").strip()
                current = _truthy(body.get("current"))
                if current:
                    track = _current_playback_track(self._request_session_key())
                    if not track:
                        raise BadRequest("当前没有正在播放的歌曲，无法按 current 收藏/取消收藏")
                    guid = track["guid"]
                elif not guid:
                    rows = p.search(str(body.get("query") or ""), 1)
                    if not rows:
                        return self._json({"error": "track not found"}, 404)
                    guid = rows[0]["guid"]
                if action == "add":
                    if is_ext_guid(guid) and hasattr(p, "download_ext"):
                        track = resolve_download_track(p, guid)
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
                title = str(body.get("title") or "").strip()
                artist = str(body.get("artist") or "").strip()
                quality = body.get("quality")
                p = provider()
                if guid:
                    track = resolve_download_track(p, guid, title, artist)
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
        u, _ = self._qs()
        if u.path in ("/api/settings/fnos", "/api/settings/online", "/api/settings/advanced"):
            try:
                if not self._admin_ok(u.path):
                    return self._unauthorized()
                if u.path == "/api/settings/online":
                    return self._handle_online_put()
                if u.path == "/api/settings/advanced":
                    return self._handle_advanced_put()
                return self._handle_settings_put()
            except GatewayError as e:
                body, code = as_payload(e)
                return self._json(body, code)
            except Exception as e:
                LOG.exception("PUT %s failed", self.path)
                return self._json({"error": str(e)}, 500)
        return self._json({"error": "not found", "path": u.path}, 404)

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
        if u.path == "/mcp":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._json({"error": "not found", "path": u.path}, 404)

    def _handle_readyz(self):
        """就绪探针：mock 探本地曲目；配了飞牛先真连一次飞牛，再汇总在线音源。"""
        try:
            if MOCK:
                rows = provider().list_tracks(1)
                return self._json({"ok": True, "mode": "mock", "tracks_probe": len(rows)})
            fnos_cfg = fnos_configured()
            out = {"mode": gateway_mode(), "fnos": None, "sources": []}
            fnos_ok = False
            if fnos_cfg:
                started = time.time()
                try:
                    rows = FnProvider().list_tracks(1)
                    fnos_ok = True
                    out["fnos"] = {"ok": True, "ms": int((time.time() - started) * 1000),
                                   "tracks_probe": len(rows)}
                except Exception as e:
                    out["fnos"] = {"ok": False, "ms": int((time.time() - started) * 1000),
                                   "error": str(e)}
            if online_active():
                out["sources"] = online_status()
            sources_ok = any(r.get("healthy") for r in out["sources"])
            ok = bool(fnos_ok or sources_ok)
            if not fnos_cfg and not online_active():
                out["error"] = "未配置任何音源"
            return self._json(dict(ok=ok, **out), 200 if ok else 503)
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
        session_key = self._request_session_key(qs)
        guid = (qs.get("guid") or [""])[0].strip()
        query = (qs.get("query") or qs.get("q") or [""])[0]
        playlist = (qs.get("playlist") or [""])[0]
        index = self._qs_int(qs, "index", 1)
        quality = (qs.get("quality") or [""])[0] or None
        p = provider()
        if guid:
            # WebUI 直接按 guid 播放：不再按标题重搜，避免同名曲/排序变化点错歌。
            CURRENT_PLAYLISTS.clear(session_key)
            track = {"guid": guid,
                     "title": (qs.get("title") or [guid])[0],
                     "artist": (qs.get("artist") or [""])[0]}
            info = PLAYBACK.prepare(track, p, self._base(), quality=quality, wait=False)
            info.update({"label": "直链", "index": 1, "count": 1})
            return self._json(with_web_url(info))
        favorites = _truthy((qs.get("favorites") or [""])[0])
        artist = (qs.get("artist") or [""])[0].strip()
        album = (qs.get("album") or [""])[0].strip()
        shuffle = _truthy((qs.get("shuffle") or [""])[0])
        library = _truthy((qs.get("library") or [""])[0]) or _truthy((qs.get("all") or [""])[0])
        loop_mode = (qs.get("loop") or [""])[0].strip().lower()
        loop_all = loop_mode in ("all", "list", "循环", "列表循环")
        if favorites or artist or album or shuffle or library or loop_all:
            # 收藏/歌手/专辑/随机/整库/循环走整列表连续流；纯 playlist 保持旧的单曲行为。
            info, label, start, shuffled, _action, rows = build_list_playback(
                p, self._base(), quality, playlist=playlist, artist=artist, album=album,
                favorites=favorites, shuffle=shuffle, index=index, session_key=session_key,
                library=library, loop=loop_all)
            info.update({"label": label, "index": start + 1, "count": len(rows),
                         "shuffle": shuffled, "loop": loop_all})
            _resume_save(session_key, "playlist", tracks=rows, label=label,
                         quality=quality or info.get("quality"), index=start,
                         token=info.get("playlist_token"), loop=loop_all)
            if info.get("playlist_token"):
                CURRENT_PLAYLISTS.set(session_key, info["playlist_token"], start, time.time())
            return self._json(with_web_url(info))
        CURRENT_PLAYLISTS.clear(session_key)
        rows, label = pick_tracks(p, query, playlist, 20)
        if index < 1 or index > len(rows):
            return self._json({"error": "index out of range", "count": len(rows)}, 400)
        info = PLAYBACK.prepare(rows[index - 1], p, self._base(), quality=quality, wait=False)
        info.update({"label": label, "index": index, "count": len(rows)})
        return self._json(with_web_url(info))

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
            body = b""
        if not body:
            # 首次播放只返回签名歌词直链，真正来取时才查歌词，避免阻塞 /api/play。
            title = (qs.get("title") or [""])[0]
            artist = (qs.get("artist") or [""])[0]
            try:
                lyrics_for({"guid": guid, "title": title, "artist": artist}, force=True)
                with open(path, "rb") as fp:
                    body = fp.read(LYRIC_MAX_BYTES)
            except OSError:
                body = b""
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

    def _handle_playlist(self, u, qs):
        token = ""
        if u.path.startswith("/playlist/"):
            token = urllib.parse.unquote(u.path[len("/playlist/"):])
            if token.endswith(".mp3"):
                token = token[:-4]
        if not token:
            return self._json({"error": "missing playlist token"}, 400)
        exp = (qs.get("exp") or [""])[0]
        sig = (qs.get("sig") or [""])[0]
        quality = (qs.get("q") or [""])[0] or "standard"
        start_position = max(0, self._qs_int(qs, "start", 0))
        if not verify("pl:" + token, exp, sig, quality, start=start_position):
            return self._json({"error": "invalid or expired signature"}, 403)
        item = PLAYLISTS.get(token)
        if not item or not item.get("tracks"):
            return self._json({"error": "playlist expired"}, 404)
        quality = quality or item.get("quality") or "standard"
        p = provider()
        tracks = item["tracks"]
        session_key = self._request_session_key(qs)
        cursor = CURRENT_PLAYLISTS.get(session_key)
        if cursor and cursor.get("token") == token:
            start_hint = int(cursor.get("index", 0))
        else:
            start_hint = int(item.get("index", 0) or 0)
        start_index = max(0, min(start_hint, len(tracks) - 1))
        first_path = None
        first_stream = None
        first_chunk = b""
        can_stream = (start_position == 0 and self._allow_stream() and
                      callable(getattr(p, "iter_stream", None)))
        for i in range(start_index, len(tracks)):
            track = tracks[i]
            raw_gen = None
            stream = None
            try:
                if can_stream:
                    raw_gen = CACHE.stream(track["guid"], p, quality=quality)
                    first_raw = next(raw_gen)
                    stream = iter_clean_mp3_stream(itertools.chain((first_raw,), raw_gen))
                    first_chunk = next(stream)
                    first_stream = stream
                else:
                    first_path = CACHE.ensure(track["guid"], p, quality=quality)
                start_index = i
                break
            except Exception as exc:
                LOG.warning("playlist track %s prepare failed: %s", track.get("guid"), exc)
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
                if raw_gen is not None:
                    try:
                        raw_gen.close()
                    except Exception:
                        pass
                first_stream = None
                first_chunk = b""
        if not first_path and first_stream is None:
            return self._json({"error": "playlist tracks are unavailable"}, 502)
        CURRENT_PLAYLISTS.set(session_key, token, start_index,
                               time.time() - start_position)
        PLAYLISTS.set_index(token, start_index, time.time() - start_position)
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command == "HEAD":
            return
        loop = bool(item.get("loop"))
        first_cycle = True
        try:
            while True:
                for offset, track in enumerate(tracks[start_index:]):
                    _prefetch_playlist_next(tracks, start_index + offset, p, quality, loop)
                    path_start = start_position if (first_cycle and offset == 0) else 0
                    if offset == 0 and first_stream is not None:
                        CURRENT_PLAYLISTS.set(session_key, token, start_index + offset,
                                               time.time() - path_start)
                        _resume_save(session_key, "playlist", tracks=tracks,
                                     label=item.get("label") or "", quality=quality,
                                     index=start_index + offset, position=path_start,
                                     token=token, loop=loop)
                        stream = first_stream
                        first_stream = None
                        chunk = first_chunk
                        first_chunk = b""
                        try:
                            if chunk:
                                self.wfile.write(chunk)
                                self.wfile.flush()
                            for block in stream:
                                self.wfile.write(block)
                                self.wfile.flush()
                        finally:
                            close = getattr(stream, "close", None)
                            if callable(close):
                                try:
                                    close()
                                except Exception:
                                    pass
                        continue
                    if offset == 0:
                        path = first_path
                    else:
                        path = None
                    if path is None:
                        try:
                            path = CACHE.ensure(track["guid"], p, quality=quality)
                        except Exception as exc:
                            LOG.warning("playlist track %s skipped: %s", track.get("guid"), exc)
                            continue
                    if path_start:
                        path = _resume_audio_file(path, path_start)
                    CURRENT_PLAYLISTS.set(session_key, token, start_index + offset,
                                           time.time() - path_start)
                    _resume_save(session_key, "playlist", tracks=tracks,
                                 label=item.get("label") or "", quality=quality,
                                 index=start_index + offset, position=path_start,
                                 token=token, loop=loop)
                    for block in iter_clean_mp3(path):
                        self.wfile.write(block)
                        self.wfile.flush()
                first_cycle = False
                start_position = 0
                if not loop:
                    break
                start_index = 0
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

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
        quality = (qs.get("q") or [""])[0] or None
        start = max(0, self._qs_int(qs, "start", 0))
        if not verify(guid, exp, sig, quality, start=start):
            LOG.warning("bad signature for %s", guid)
            return self._json({"error": "invalid or expired signature"}, 403)
        cached = CACHE.path(guid, quality)
        try:
            cached_ok = os.path.exists(cached) and os.path.getsize(cached) > 1024
        except OSError:
            cached_ok = False
        if not start and not cached_ok and self._allow_stream():
            if self._serve_stream(guid, quality):
                return
        try:
            path = CACHE.ensure(guid, provider(), quality=quality)
        except Exception as e:
            LOG.exception("transcode %s failed", guid)
            return self._json({"error": "transcode failed: %s" % e}, 502)
        if start:
            path = _resume_audio_file(path, start)
        duration_ms = 0
        try:
            secs = PLAYBACK.audio_seconds(path)
            if secs:
                duration_ms = int(round(float(secs) * 1000))
        except Exception:
            duration_ms = 0
        lyric_url = ""
        try:
            lyric_path = lyric_cache_path(guid)
            if os.path.getsize(lyric_path) > 0:
                lyric_url = make_lyric_url(self._base(), guid)
        except OSError:
            lyric_url = ""
        return self._serve_file(path, duration_ms=duration_ms, lyric_url=lyric_url)

    def _allow_stream(self):
        """首播是否可走无 Content-Length 的边收边播：只处理 GET 且无 Range / bytes=0-。"""
        if self.command != "GET":
            return False
        rng = (self.headers.get("Range") or "").strip().lower()
        return not rng or rng == "bytes=0-"

    def _serve_stream(self, guid, quality=None):
        """未缓存曲目首块到达即回 200 并持续写入；已在发送响应后返回 True。

        返回 False 表示首块准备失败、尚未发送任何响应，调用方可回退完整缓存。
        """
        p = provider()
        if not callable(getattr(p, "iter_stream", None)):
            return False
        gen = CACHE.stream(guid, p, quality=quality)
        try:
            first = next(gen)
        except StopIteration:
            try:
                gen.close()
            except Exception:
                pass
            return False
        except Exception as exc:
            try:
                gen.close()
            except Exception:
                pass
            LOG.warning("stream prepare %s failed: %s", guid, exc)
            return False
        try:
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            if self.command == "HEAD":
                return True
            self.wfile.write(first)
            self.wfile.flush()
            for chunk in gen:
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return True
        finally:
            try:
                gen.close()
            except Exception:
                pass
        return True

    def _serve_file(self, path, duration_ms=0, lyric_url=""):
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
        if duration_ms > 0:
            self.send_header("X-Audio-Duration-Ms", str(int(duration_ms)))
        if lyric_url:
            self.send_header("X-Lyric-Url", lyric_url)
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
        try:
            raw = self._raw_body(MCP_BODY_CAP)
        except BadRequest:
            return self._json({"jsonrpc": "2.0", "id": None,
                               "error": {"code": -32600, "message": "payload too large"}}, 413)
        # 优先走官方 MCPServer 2.3 的 streamable-http；它只监听 127.0.0.1 随机端口。
        # 桥接器与网关分容器时，Host 是容器内地址；桥接器可显式注入公网前缀。
        base = self._request_base()
        headers = {
            "Content-Type": self.headers.get("Content-Type") or "application/json",
            "Accept": self.headers.get("Accept") or "application/json, text/event-stream",
            "X-GW-Base": base,
            "X-GW-Session": self._mcp_session_key(),
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
                    text = call_tool(name, args, self._request_base(), session_id=self._mcp_session_key())
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
    if not PUBLIC_BASE:
        LOG.warning("未设置 GW_PUBLIC_BASE：直连设备会按请求 Host 推导直链；使用 MCP 接入点(wss)桥接时设备会收到容器内地址，请设置 GW_PUBLIC_BASE 为设备可访问地址")
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
