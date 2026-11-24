# -*- coding: utf-8 -*-
"""多曲目连续播放：把歌单/收藏拼成一个可顺序读取的 MP3 流。

设备固件 self.music.play_url 只接受一个 URL，但 notify_player 会持续读取 HTTP
直到 EOF，因此这里把多首 MP3 顺序写到同一个响应里，设备就会连续播放。
"""
import os
import secrets
import threading
import time


class PlaylistRegistry:
    """短时保存「要连续播放哪些曲目」，token 只用于服务端查找。"""

    def __init__(self, ttl=900, max_items=200):
        self.ttl = max(60, int(ttl or 900))
        self.max_items = max(1, int(max_items or 200))
        self._items = {}
        self._lock = threading.Lock()

    def _prune(self):
        now = time.time()
        dead = [k for k, v in self._items.items() if v["expires"] <= now]
        for key in dead:
            self._items.pop(key, None)

    def put(self, tracks, quality="standard", ttl=None, label="", loop=False):
        clean = []
        for track in tracks or []:
            if not isinstance(track, dict) or not track.get("guid"):
                continue
            clean.append({
                "guid": str(track["guid"]),
                "title": str(track.get("title") or ""),
                "artist": str(track.get("artist") or ""),
                "duration_s": track.get("duration_s"),
            })
            if len(clean) >= self.max_items:
                break
        if not clean:
            return None
        token = secrets.token_urlsafe(18)
        with self._lock:
            self._prune()
            self._items[token] = {
                "tracks": clean,
                "quality": str(quality or "standard"),
                "expires": time.time() + (self.ttl if ttl is None else max(1, int(ttl))),
                "label": str(label or ""),
                "index": 0,
                "started_at": time.time(),
                "loop": bool(loop),
            }
        return token

    def get(self, token):
        with self._lock:
            self._prune()
            item = self._items.get(str(token or ""))
            if not item:
                return None
            return {
                "tracks": [dict(t) for t in item["tracks"]],
                "quality": item["quality"],
                "expires": item["expires"],
                "label": item.get("label", ""),
                "index": int(item.get("index", 0)),
                "started_at": float(item.get("started_at", time.time())),
                "loop": bool(item.get("loop", False)),
            }

    def set_index(self, token, index, started_at=None):
        """记录连续流当前播到第几首（用于 music_next/music_previous/music_status）。"""
        with self._lock:
            item = self._items.get(str(token or ""))
            if not item:
                return False
            try:
                idx = int(index)
            except (TypeError, ValueError):
                return False
            item["index"] = max(0, min(idx, len(item["tracks"]) - 1))
            item["started_at"] = float(started_at if started_at is not None else time.time())
            return True

    def pop(self, token):
        with self._lock:
            return self._items.pop(str(token or ""), None)


def iter_clean_mp3(path, chunk_size=64 * 1024):
    """逐块读 MP3，跳过每首开头的 ID3v2 和结尾的 ID3v1，避免拼流中间出现标签。"""
    with open(path, "rb") as fp:
        head = fp.read(10)
        if len(head) >= 10 and head[:3] == b"ID3":
            size = ((head[6] & 0x7F) << 21) | ((head[7] & 0x7F) << 14) | \
                   ((head[8] & 0x7F) << 7) | (head[9] & 0x7F)
            fp.seek(10 + size)
        else:
            fp.seek(0)
        tail = b""
        while True:
            block = fp.read(chunk_size)
            if not block:
                break
            data = tail + block
            if len(data) > 128:
                yield data[:-128]
                tail = data[-128:]
            else:
                tail = data
        if tail and not tail.startswith(b"TAG"):
            yield tail
