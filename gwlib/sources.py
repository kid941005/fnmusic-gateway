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
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from .quality import get_profile
from .storage import LibraryStore, _call_stream

LOG = logging.getLogger("fnmusic-gw.sources")
VERSION = "2.0.0"
EXT_PREFIX = "ext:"
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


def fallback_services(urls):
    return [parse_spec(spec)[0] for spec in urls]


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
        limit = max(1, int(limit))
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

    def stream(self, guid, quality=None):
        _svc, eid = ext_split(guid)
        url = self._resolve_url(eid, quality)
        if not url:
            raise RuntimeError("在线音源未返回可播放地址（%s）" % self.svc)
        req = urllib.request.Request(url, headers={"User-Agent": "fnmusic-gateway/" + self.version})
        with urllib.request.urlopen(req, timeout=max(20, self.timeout * 3)) as resp:
            ctype = resp.headers.get("Content-Type") or "application/octet-stream"
            return ctype, resp.read()

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
    """本地曲库优先；本地不足时并行问所有在线音源并按标题/歌手去重补齐。"""

    def __init__(self, base, urls, timeout=8, search_limit=10,
                 download_dir_getter=None, scan_cmd_getter=None, parallel=True,
                 max_bytes=0):
        self.base = base
        self.exts = [ExtProvider(url, svc, timeout=timeout) for svc, url in
                     (parse_spec(spec) for spec in urls)]
        self.search_limit = max(1, int(search_limit or 10))
        self.parallel = parallel
        self.library = LibraryStore(
            download_dir_getter or (lambda: os.environ.get("GW_DOWNLOAD_DIR", "")),
            scan_cmd_getter or (lambda: os.environ.get("GW_LIBRARY_SCAN_CMD", "")),
            LOG,
            max_bytes=max_bytes,
        )

    def list_tracks(self, limit=20):
        return self.base.list_tracks(limit)

    def playlists(self):
        return self.base.playlists()

    def playlist_tracks(self, guid, limit=200):
        return self.base.playlist_tracks(guid, limit)

    def albums(self, limit=20):
        return self.base.albums(limit)

    def artists(self, limit=20):
        return self.base.artists(limit)

    def favorites(self, limit=100):
        return self.base.favorites(limit)

    def add_favorite(self, guid):
        return self.base.add_favorite(guid)

    def remove_favorite(self, guid):
        return self.base.remove_favorite(guid)

    def search(self, q, limit=10):
        try:
            rows = self.base.search(q, limit)
        except Exception as exc:
            LOG.warning("local search failed: %s", exc)
            rows = []
        if len(rows) >= limit or not self.exts:
            return rows
        have = set((_norm_text(t.get("title")), _norm_text(t.get("artist"))) for t in rows)
        need = limit - len(rows)
        per_source = max(need, self.search_limit)
        batches = []
        if self.parallel and len(self.exts) > 1:
            with ThreadPoolExecutor(max_workers=min(4, len(self.exts))) as pool:
                futures = {pool.submit(ext.search, q, per_source): ext for ext in self.exts}
                for future in as_completed(futures):
                    try:
                        batches.append(future.result())
                    except Exception:
                        batches.append([])
        else:
            for ext in self.exts:
                try:
                    batches.append(ext.search(q, per_source))
                except Exception:
                    batches.append([])
        for batch in batches:
            for track in batch:
                key = (_norm_text(track.get("title")), _norm_text(track.get("artist")))
                if key in have:
                    continue
                have.add(key)
                rows.append(track)
                need -= 1
                if need <= 0:
                    return rows[:limit]
        return rows[:limit]

    def _ext(self, guid):
        svc, _ = ext_split(guid)
        for ext in self.exts:
            if ext.svc == svc:
                return ext
        return self.exts[0] if self.exts else None

    def stream(self, guid, quality=None):
        if is_ext_guid(guid):
            ext = self._ext(guid)
            if not ext:
                raise RuntimeError("在线音源未配置")
            return _call_stream(ext, guid, quality)
        return _call_stream(self.base, guid, quality)

    def lyrics_text(self, guid):
        if not is_ext_guid(guid):
            return ""
        ext = self._ext(guid)
        return ext.lyrics(guid) if ext else ""

    def download_ext(self, track, quality=None):
        return self.library.save_from_provider(self, track, quality=quality)

    def status(self):
        out = []
        for ext in self.exts:
            out.append({"source": ext.svc, "url": ext.base, "healthy": ext.health()})
        return out


def source_status(urls, timeout=3):
    out = []
    for spec in urls:
        svc, url = parse_spec(spec)
        ext = ExtProvider(url, svc, timeout=timeout)
        out.append({"source": svc, "url": url, "healthy": ext.health()})
    return out
