# -*- coding: utf-8 -*-
"""播放编排层：选质量、预热缓存、生成签名直链/歌词直链、触发边播边存。"""
import logging
import os
import threading
import time

from .quality import get_profile, normalize_quality
from .sources import is_ext_guid

LOG = logging.getLogger("fnmusic-gw.playback")


class PlaybackService:
    def __init__(self, cache, library, make_url, lyric_url_for, audio_seconds,
                 save_state, default_quality="standard", ttl=900,
                 preview_min_seconds=35, tee_save=True):
        self.cache = cache
        self.library = library
        self.make_url = make_url
        self.lyric_url_for = lyric_url_for
        self.audio_seconds = audio_seconds
        self.save_state = save_state
        self.default_quality = normalize_quality(default_quality)
        self.ttl = ttl
        self.preview_min_seconds = preview_min_seconds
        self.tee_save = tee_save

    def _alternate(self, provider, track):
        """当前在线源流失败时，用同歌名/歌手再搜一次，换一个源。"""
        if not is_ext_guid(track.get("guid")):
            return None
        query = "%s %s" % (track.get("title") or "", track.get("artist") or "")
        try:
            rows = provider.search(query.strip(), 8)
        except Exception:
            return None
        for row in rows:
            if row.get("guid") and row.get("guid") != track.get("guid"):
                return row
        return None

    def _duration_from_track(self, track):
        """没有缓存文件时，用搜索结果里的时长字段估算，避免为 play 阻塞 ffprobe。"""
        for key in ("duration_s", "duration_seconds", "duration"):
            try:
                seconds = float(track.get(key))
            except (TypeError, ValueError):
                continue
            if seconds > 0:
                return seconds / 1000.0 if seconds > 10000 else seconds
        return None

    def _ensure_with_failover(self, track, provider, profile):
        current = dict(track)
        last_error = None
        for attempt in range(2):
            try:
                path = self.cache.ensure(current["guid"], provider, quality=profile["name"])
                return path, current
            except Exception as exc:
                last_error = exc
                alt = self._alternate(provider, current) if attempt == 0 else None
                if alt is None:
                    raise
                LOG.info("playback failover %s -> %s (%s)", current.get("guid"), alt.get("guid"), exc)
                current = alt
        raise last_error or RuntimeError("playback failed")

    def _prefetch_quiet(self, guid, provider, quality):
        try:
            self.cache.ensure(guid, provider, quality=quality)
        except Exception as exc:
            LOG.info("playback prefetch failed for %s: %s", guid, exc)

    def prepare(self, track, provider, base, quality=None, save=True, ttl=None, wait=True):
        profile = get_profile(quality or self.default_quality)
        current = dict(track)
        path = None
        if wait:
            path, current = self._ensure_with_failover(current, provider, profile)
        else:
            # 快速路径：已经有缓存就直接返回；否则后台准备，先给设备可播放 URL。
            try:
                cached = self.cache.path(current["guid"], profile["name"])
            except Exception:
                cached = ""
            if cached and os.path.exists(cached) and os.path.getsize(cached) > 1024:
                path = cached
            elif hasattr(self.cache, "prefetch"):
                self.cache.prefetch(current["guid"], provider, quality=profile["name"])
            else:
                threading.Thread(target=self._prefetch_quiet,
                                 args=(current["guid"], provider, profile["name"]),
                                 daemon=True).start()

        if path:
            try:
                duration = self.audio_seconds(path)
            except Exception:
                duration = None
        else:
            duration = self._duration_from_track(current)
        preview = bool(duration and duration < self.preview_min_seconds)

        url = self.make_url(base, current["guid"], ttl=ttl, quality=profile["name"])
        try:
            lyric_url = self.lyric_url_for(base, current, ttl=ttl)
        except Exception as exc:
            LOG.info("lyric lookup failed for %s: %s", current.get("guid"), exc)
            lyric_url = ""
        saving = False
        if path and save and self.tee_save and is_ext_guid(current.get("guid")) and self.library.enabled():
            saving = self.library.save_async(provider, current, profile["name"])
        state = {"guid": current["guid"], "title": current["title"], "artist": current["artist"],
                 "source": current.get("source") or "local", "preview": preview,
                 "duration": duration, "quality": profile["name"], "at": int(time.time()),
                 "url": url}
        try:
            self.save_state(state)
        except Exception as exc:
            LOG.info("save play state failed: %s", exc)
        return {
            "guid": current["guid"], "title": current["title"], "artist": current["artist"],
            "album": current.get("album", ""), "url": url, "lyric_url": lyric_url,
            "expires_in": ttl or self.ttl, "quality": profile["name"],
            "quality_label": profile["label"], "bitrate": profile["bitrate"],
            "source": current.get("source") or "local", "preview": preview,
            "duration": duration, "saving": saving, "preparing": bool(not path and not wait),
            "device_args": {"url": url, "title": current["title"],
                            "artist": current.get("artist") or "", "lyric_url": lyric_url,
                            "duration": int(round(duration)) if duration else 0},
            "device_tool": "self.music.play_url",
            "spoken": "%s%s" % (current["title"],
                                ("，" + current["artist"]) if current.get("artist") else ""),
        }
