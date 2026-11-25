# -*- coding: utf-8 -*-
"""音源层：外部音源客户端 + 本地优先聚合器。

对外契约：
  search(q, limit) -> [track]，track 至少含 guid/title/artist，外部曲目 guid=ext:<svc>:<id>
  stream(guid, quality) -> (content_type, bytes)
  lyrics(guid) -> str
"""
import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed

from . import __version__ as VERSION
from .errors import BadRequest
from .quality import get_profile
from .storage import LibraryStore, _call_stream, _call_stream_to_path

LOG = logging.getLogger("fnmusic-gw.sources")
EXT_PREFIX = "ext:"


def _stream_ext(ext, q, per, deadline_s=None):
    """兼容老外部音源 search_stream(q, limit) 与新签名 deadline_s。"""
    fn = getattr(ext, "search_stream", None)
    if not callable(fn):
        return list(ext.search(q, per) or [])
    if deadline_s is None:
        return fn(q, per)
    try:
        return fn(q, per, deadline_s=deadline_s)
    except TypeError:
        return fn(q, per)
SERVICE_PORTS = {"8768": "musicdl", "8770": "musicbox", "8772": "lxmusic", "8776": "neteasefree"}
SERVICE_ALIASES = {"musicdl": "musicdl", "lx": "lxmusic", "lxmusic": "lxmusic",
                   "musicbox": "musicbox", "netease": "musicbox", "neteasefree": "neteasefree"}


def _first(d, *keys, default=""):
    if not isinstance(d, dict):
        return default
    for key in keys:
        val = d.get(key)
        if val not in (None, "", [], {}):
            return val
    return default


def _rows(data):
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("list", "records", "items", "rows", "tracks", "data", "content", "songs"):
            val = data.get(key)
            if isinstance(val, list):
                return val
            if isinstance(val, dict):
                got = _rows(val)
                if got:
                    return got
    return []


def _norm_track(t):
    if not isinstance(t, dict):
        return {"guid": str(t), "title": str(t), "artist": "", "album": ""}
    guid = str(_first(t, "guid", "trackGuid", "trackGUID", "id"))
    title = str(_first(t, "title", "name", "trackName", "trackTitle", "fileName", "songName"))
    artist = _first(t, "artist", "artistName", "artists", "singer", "singers")
    if isinstance(artist, list):
        artist = "/".join(str(x.get("name")) if isinstance(x, dict) else str(x) for x in artist)
    album = _first(t, "album", "albumName", "albumTitle")
    if isinstance(album, dict):
        album = album.get("name", "")
    out = {"guid": guid, "title": title, "artist": str(artist or ""), "album": str(album or "")}
    for key in ("duration_s", "ext", "bitrate", "samplerate", "codec", "channels", "quality", "cover_url"):
        if key in t:
            out[key] = t[key]
    return out


def _norm_text(s):
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(s or "").casefold())


def is_ext_guid(guid):
    return str(guid or "").startswith(EXT_PREFIX)


def ext_split(guid):
    svc, _, eid = str(guid)[len(EXT_PREFIX):].partition(":")
    return svc, eid


def guess_service(base):
    try:
        port = urllib.parse.urlparse(base).port
    except Exception:
        port = None
    return SERVICE_PORTS.get(str(port), "")


def parse_spec(spec):
    text = str(spec or "").strip()
    # URL 本身可能含 @（如 http://user@host:8001），先按 URL 处理
    if re.match(r"^https?://", text):
        return guess_service(text) or "musicdl", text
    svc, sep, url = text.partition("@")
    if not sep or not url:
        svc, url = "", text
    svc = svc.strip().lower()
    svc = SERVICE_ALIASES.get(svc, svc) if svc else (guess_service(url) or "musicdl")
    return svc, str(url or "").strip()


def _http_json(url, timeout):
    req = urllib.request.Request(url, headers={
        "User-Agent": "fnmusic-gateway/" + VERSION,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read(2 * 1024 * 1024).decode("utf-8", "replace"))


class ExtProvider:
    """fnos_music_ext 音源容器的只读客户端。

    musicdl      GET /search?keyword=&limit=        GET /stream?id=&proxy=true[&quality=mp3]  GET /info?id=
    lxmusic      GET /api/v1/search?keyword=        GET /api/v1/track/url?id=&quality=          GET /api/v1/track/lyric?id=
    musicbox     GET /api/v1/search?keyword=        GET /api/v1/song/{id}/url?quality=          GET /api/v1/song/{id}/lyric
    neteasefree  同 musicbox
    """

    def __init__(self, base, svc="", timeout=8, version=None):
        self.base = (base or "").rstrip("/")
        self.svc = svc or guess_service(self.base) or "musicdl"
        self.timeout = timeout
        self.version = version or VERSION

    def health(self):
        try:
            data = _http_json(self.base + "/healthz", min(3, self.timeout))
            return bool(data.get("ok", True)) if isinstance(data, dict) else bool(data)
        except Exception:
            return False

    def _get(self, url):
        return _http_json(url, self.timeout)

    def search(self, q, limit=10):
        quoted = urllib.parse.quote(str(q or ""))
        # 上游 musicdl 的 /search 限制 limit<=10，统一夹取，避免 HTTP 422。
        limit = min(max(1, int(limit)), 10)
        try:
            if self.svc == "musicdl":
                data = self._get("%s/search?keyword=%s&limit=%d" % (self.base, quoted, limit))
            else:
                data = self._get("%s/api/v1/search?keyword=%s&limit=%d" % (self.base, quoted, limit))
        except Exception as exc:
            LOG.warning("ext %s search failed: %s", self.svc, exc)
            return []
        return self._norm_rows(data, limit)

    def _norm_rows(self, data, limit):
        out = []
        for row in _rows(data)[: max(limit, 1) * 2]:
            if not isinstance(row, dict):
                continue
            eid = _first(row, "id", "songId", "song_id", "songmid", "songMid", "mid", "hash")
            if not eid:
                continue
            track = _norm_track(row)
            track["guid"] = "%s%s:%s" % (EXT_PREFIX, self.svc, eid)
            track["source"] = self.svc
            track["ext_id"] = str(eid)
            if not track.get("title"):
                track["title"] = str(_first(row, "name", "songName", default=str(eid)))
            out.append(track)
        seen = set()
        uniq = []
        for track in out:
            if track["guid"] in seen:
                continue
            seen.add(track["guid"])
            uniq.append(track)
        return uniq[:limit]

    def _resolve_url(self, eid, quality=None):
        profile = get_profile(quality)
        eid_q = urllib.parse.quote(str(eid), safe="")
        ext_quality = str(profile.get("ext_quality") or "mp3")
        if self.svc == "musicdl":
            # musicdl 的 quality=mp3 强制官方有损档；高层档位仍由网关转码提升码率。
            q = "&quality=mp3" if ext_quality == "mp3" else ""
            return "%s/stream?id=%s&proxy=true%s" % (self.base, eid_q, q)
        if self.svc == "lxmusic":
            data = self._get("%s/api/v1/track/url?id=%s&quality=%s" % (
                self.base, eid_q, "lossless" if ext_quality == "lossless" else "exhigh"))
        else:
            data = self._get("%s/api/v1/song/%s/url?quality=%s" % (
                self.base, eid_q, "lossless" if ext_quality == "lossless" else "exhigh"))
        holder = data.get("data") if isinstance(data, dict) else None
        url = (holder or {}).get("url") if isinstance(holder, dict) else None
        if not url and isinstance(data, dict):
            url = data.get("url")
        return str(url or "")

    def _open(self, guid, quality=None):
        _svc, eid = ext_split(guid)
        url = self._resolve_url(eid, quality)
        if not url:
            raise RuntimeError("在线音源未返回可播放地址（%s）" % self.svc)
        req = urllib.request.Request(url, headers={"User-Agent": "fnmusic-gateway/" + self.version})
        return urllib.request.urlopen(req, timeout=max(20, self.timeout * 3))

    def stream(self, guid, quality=None):
        with self._open(guid, quality) as resp:
            ctype = resp.headers.get("Content-Type") or "application/octet-stream"
            return ctype, resp.read()

    def iter_stream(self, guid, quality=None):
        """打开上游音频，按 64 KiB 分块产出，供网关边收边播。"""
        resp = self._open(guid, quality)
        ctype = resp.headers.get("Content-Type") or "application/octet-stream"

        def _iter():
            try:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk
            finally:
                try:
                    resp.close()
                except Exception:
                    pass

        return ctype, _iter()

    def stream_to_path(self, guid, path, quality=None):
        """流式写入文件，避免把整首无损音频读进内存。返回 (content_type, 字节数)。"""
        with self._open(guid, quality) as resp, open(path, "wb") as fh:
            ctype = resp.headers.get("Content-Type") or "application/octet-stream"
            total = 0
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                total += len(chunk)
            return ctype, total

    def lyrics(self, guid):
        _svc, eid = ext_split(guid)
        eid_q = urllib.parse.quote(str(eid), safe="")
        try:
            if self.svc == "musicdl":
                data = self._get("%s/info?id=%s" % (self.base, eid_q))
            elif self.svc == "lxmusic":
                data = self._get("%s/api/v1/track/lyric?id=%s" % (self.base, eid_q))
            else:
                data = self._get("%s/api/v1/song/%s/lyric" % (self.base, eid_q))
        except Exception:
            return ""
        for holder in (data, (data or {}).get("data") if isinstance(data, dict) else None):
            if isinstance(holder, dict):
                for key in ("lyric", "lrc", "lyrics", "text"):
                    value = holder.get(key)
                    if isinstance(value, str) and value.strip():
                        return value
        return ""


class FallbackProvider:
    """在线音源聚合器。

    base=None 表示没配飞牛音乐：没有本地曲库、歌单和收藏，
    相关方法直接给出明确提示；搜索、播放、下载全部走在线音源。
    传入 base 时保持旧行为：本地曲库优先，本地不足再并行问在线音源补齐。
    """

    def __init__(self, base, urls=None, timeout=8, search_limit=10,
                 download_dir_getter=None, parallel=True,
                 max_bytes=0, total_limit=50, total_timeout=15, exts=None,
                 scan_cmd_getter=None, scan_url_getter=None,
                 scan_callback_getter=None):
        self.base = base
        if exts is not None:
            self.exts = list(exts)
        else:
            self.exts = [ExtProvider(url, svc, timeout=timeout) for svc, url in
                         (parse_spec(spec) for spec in (urls or []))]
        self.search_limit = max(1, int(search_limit or 10))
        self.parallel = parallel
        self.total_limit = max(1, int(total_limit or 50))
        self.total_timeout = max(1.0, float(total_timeout or 15))
        # 常驻线程池：避免每次 search 新建/销毁线程；并行与否在构造时固定。
        workers = min(4, len(self.exts)) if (self.parallel and len(self.exts) > 1) else 1
        self._pool = ThreadPoolExecutor(max_workers=max(1, workers),
                                        thread_name_prefix="gw-search")
        # 每个请求线程独立的搜索诊断，供 /api/search 告诉前端本地/在线各命中多少。
        self._search_meta = threading.local()
        self.library = LibraryStore(
            download_dir_getter or (lambda: os.environ.get("GW_DOWNLOAD_DIR", "")),
            LOG,
            max_bytes=max_bytes,
            scan_cmd_getter=scan_cmd_getter,
            scan_url_getter=scan_url_getter,
            scan_callback_getter=scan_callback_getter,
        )

    def _local(self):
        """取本地后端；未配置飞牛音乐时抛 BadRequest 而不是 AttributeError。"""
        if self.base is None:
            raise BadRequest("未配置飞牛音乐：没有本地曲库/歌单/收藏，直接按歌名搜索即可")
        return self.base

    def list_tracks(self, limit=20):
        return self._local().list_tracks(limit)

    def playlists(self):
        return self._local().playlists()

    def playlist_tracks(self, guid, limit=200):
        return self._local().playlist_tracks(guid, limit)

    def albums(self, limit=20):
        return self._local().albums(limit)

    def artists(self, limit=20):
        return self._local().artists(limit)

    def artist_items(self, limit=200, query=""):
        fn = getattr(self._local(), "artist_items", None)
        if callable(fn):
            try:
                return fn(limit, query=query)
            except TypeError:
                return fn(limit)
        return [{"guid": str(x), "name": str(x)} for x in (self._local().artists(limit) or [])]

    def album_items(self, limit=200, query=""):
        fn = getattr(self._local(), "album_items", None)
        if callable(fn):
            try:
                return fn(limit, query=query)
            except TypeError:
                return fn(limit)
        return [{"guid": str(x), "name": str(x)} for x in (self._local().albums(limit) or [])]

    def artist_tracks(self, guid, limit=500):
        fn = getattr(self._local(), "artist_tracks", None)
        if not callable(fn):
            raise BadRequest("当前本地后端不支持按歌手播放")
        return fn(guid, limit)

    def album_tracks(self, guid, limit=500):
        fn = getattr(self._local(), "album_tracks", None)
        if not callable(fn):
            raise BadRequest("当前本地后端不支持按专辑播放")
        return fn(guid, limit)

    def favorites(self, limit=100):
        return self._local().favorites(limit)

    def add_favorite(self, guid):
        return self._local().add_favorite(guid)

    def remove_favorite(self, guid):
        return self._local().remove_favorite(guid)

    @staticmethod
    def _is_local_playable(track):
        if not track:
            return False
        if track.get("playable") is False:
            return False
        src = str(track.get("source") or "").strip().lower()
        return src in ("local", "download")

    def local_search(self, q, limit=10):
        """检索飞牛音乐本地曲库；结果统一标记为可播放的 local。"""
        try:
            cap = max(1, int(limit or 1))
        except (TypeError, ValueError):
            cap = 10
        if self.base is None:
            return []
        out = []
        seen = set()
        try:
            for track in (self.base.search(q, cap) or []):
                item = dict(track)
                item.setdefault("source", "local")
                item["playable"] = True
                key = (_norm_text(item.get("title")), _norm_text(item.get("artist")))
                if key in seen:
                    continue
                seen.add(key)
                out.append(item)
                if len(out) >= cap:
                    break
        except Exception as exc:
            LOG.warning("local search failed: %s", exc)
            try:
                self._search_meta.value["local_error"] = str(exc)
            except Exception:
                pass
        return out

    def search(self, q, limit=10):
        # 总结果上限：所有源加起来最多 total_limit 条
        cap = min(max(1, int(limit or 1)), self.total_limit)
        meta = {"local_count": 0, "online_count": 0,
                "local_error": "", "online_error": ""}
        self._search_meta.value = meta
        rows = []
        if self.base is not None:
            rows = self.local_search(q, cap)
        meta["local_count"] = len(rows)
        # 本地有任何可播放结果就不再访问在线源：本地起播远快于在线。
        if rows and any(self._is_local_playable(t) for t in rows):
            return rows[:cap]
        if len(rows) >= cap or not self.exts:
            return rows
        have = set((_norm_text(t.get("title")), _norm_text(t.get("artist"))) for t in rows)
        need = cap - len(rows)
        per_source = max(need, self.search_limit)
        deadline = time.monotonic() + self.total_timeout
        batches = []
        workers = min(4, len(self.exts)) if (self.parallel and len(self.exts) > 1) else 1
        pool = self._pool
        futures = {}
        try:
            futures = {pool.submit(ext.search, q, per_source): ext for ext in self.exts}
            if workers == 1:
                # 串行：逐个等，但每个都受剩余总预算限制
                for future in futures:
                    remaining = max(0.0, deadline - time.monotonic())
                    try:
                        batches.append(future.result(timeout=remaining))
                    except FuturesTimeoutError:
                        LOG.warning("online search exceeded %.1fs total budget", self.total_timeout)
                        break
                    except Exception as exc:
                        meta["online_error"] = str(exc)
                        batches.append([])
            else:
                # 并行：到点就停止等待，已返回的结果照常合并
                try:
                    for future in as_completed(futures, timeout=max(0.0, deadline - time.monotonic())):
                        try:
                            batches.append(future.result())
                        except Exception as exc:
                            meta["online_error"] = str(exc)
                            batches.append([])
                except FuturesTimeoutError:
                    LOG.warning("online search exceeded %.1fs total budget", self.total_timeout)
        finally:
            for future in futures:
                future.cancel()
        for batch in batches:
            for track in batch:
                key = (_norm_text(track.get("title")), _norm_text(track.get("artist")))
                if key in have:
                    continue
                have.add(key)
                rows.append(track)
                if len(rows) >= cap:
                    meta["online_count"] = max(0, len(rows) - meta["local_count"])
                    return rows[:cap]
        meta["online_count"] = max(0, len(rows) - meta["local_count"])
        return rows[:cap]

    def search_stream(self, q, limit=10, deadline_s=None):
        """本地优先，在线音源边搜边出；每拿到一首就 yield，不等全部完成。"""
        cap = min(max(1, int(limit or 1)), self.total_limit)
        meta = {"local_count": 0, "online_count": 0,
                "local_error": "", "online_error": ""}
        self._search_meta.value = meta
        rows = []
        have = set()

        def add(track):
            key = (_norm_text(track.get("title")), _norm_text(track.get("artist")))
            if key in have:
                return False
            have.add(key)
            rows.append(track)
            return True

        local_playable = False
        if self.base is not None:
            try:
                local = self.local_search(q, cap)
            except Exception as exc:
                LOG.warning("local search failed: %s", exc)
                meta["local_error"] = str(exc)
                local = []
            for track in local:
                if self._is_local_playable(track):
                    local_playable = True
                if add(track):
                    yield track
            meta["local_count"] = len(rows)

        # 本地已有可播放结果时直接结束，不等待在线源。
        if local_playable or len(rows) >= cap or not self.exts:
            meta["online_count"] = 0
            return
        budget = min(self.total_timeout, float(deadline_s)) if deadline_s else self.total_timeout
        deadline = time.monotonic() + max(0.1, budget)
        per_source = max(1, max(cap - len(rows), self.search_limit))
        for ext in self.exts:
            if time.monotonic() >= deadline:
                break
            try:
                batch = _stream_ext(ext, q, per_source, deadline_s)
                for track in batch:
                    if add(track):
                        yield track
                    if len(rows) >= cap:
                        meta["online_count"] = max(0, len(rows) - meta["local_count"])
                        return
            except Exception as exc:
                LOG.warning("online search failed: %s", exc)
                meta["online_error"] = str(exc)
        meta["online_count"] = max(0, len(rows) - meta["local_count"])

    def last_search_meta(self):
        """最近一次 search 的诊断信息（同请求线程）。"""
        return dict(getattr(self._search_meta, "value", {}) or {})

    def track_info(self, guid):
        """按 guid 取曲目元数据；在线曲目尽量从本地缓存还原标题/歌手。"""
        if is_ext_guid(guid):
            ext = self._ext(guid)
            fn = getattr(ext, "track_info", None)
            return fn(guid) if callable(fn) else None
        if self.base is None:
            return None
        fn = getattr(self.base, "track_info", None)
        return fn(guid) if callable(fn) else None

    def _ext(self, guid):
        svc, _ = ext_split(guid)
        for ext in self.exts:
            if ext.svc == svc:
                return ext
        raise RuntimeError("未知在线音源: %s" % svc)

    def stream(self, guid, quality=None):
        if is_ext_guid(guid):
            ext = self._ext(guid)
            if not ext:
                raise RuntimeError("在线音源未配置")
            return _call_stream(ext, guid, quality)
        return _call_stream(self._local(), guid, quality)

    def iter_stream(self, guid, quality=None):
        target = self._ext(guid) if is_ext_guid(guid) else self._local()
        if not target:
            raise RuntimeError("在线音源未配置")
        fn = getattr(target, "iter_stream", None)
        if callable(fn):
            return fn(guid, quality=quality)
        ctype, raw = _call_stream(target, guid, quality)
        return ctype, iter((raw,))

    def scan_library(self):
        """触发飞牛官方扫库；未配置飞牛音乐时返回 False。"""
        if self.base is None:
            return False
        fn = getattr(self.base, "scan_library", None)
        if not callable(fn):
            return False
        fn()
        return True

    def stream_to_path(self, guid, path, quality=None):
        if is_ext_guid(guid):
            ext = self._ext(guid)
            if not ext:
                raise RuntimeError("在线音源未配置")
            return _call_stream_to_path(ext, guid, path, quality)
        return _call_stream_to_path(self._local(), guid, path, quality)

    def lyrics_text(self, guid):
        if not is_ext_guid(guid):
            return ""
        ext = self._ext(guid)
        return ext.lyrics(guid) if ext else ""

    def download_ext(self, track, quality=None):
        return self.library.save_from_provider(self, track, quality=quality)

    def close(self):
        """释放内置音源的 worker 子进程与检索线程池。"""
        pool = self._pool
        self._pool = None
        if pool is not None:
            try:
                pool.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
        for ext in self.exts:
            close_fn = getattr(ext, "close", None)
            if callable(close_fn):
                try:
                    close_fn()
                except Exception:
                    pass

    def status(self):
        out = []
        for ext in self.exts:
            status_fn = getattr(ext, "status", None)
            if callable(status_fn):
                try:
                    out.extend(status_fn() or [])
                    continue
                except Exception:
                    pass
            out.append({"source": getattr(ext, "svc", "online"),
                        "url": getattr(ext, "base", ""),
                        "healthy": bool(ext.health())})
        return out
