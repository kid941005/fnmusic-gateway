# -*- coding: utf-8 -*-
"""在线曲目播放计数：同一首第二次真正开播后，自动补进本地曲库。"""
import json
import os
import threading
import time


class PlayStats:
    """按在线 guid 记录播放次数；5 秒内重复计入同一首只算一次。

    计数持久化到 data/play_stats.json，阈值默认 2；下载失败后 300 秒内不重复触发。
    """

    def __init__(self, path, threshold=2, dedupe_seconds=5.0,
                 failure_backoff=300.0, max_items=500, logger=None):
        self.path = str(path or "")
        try:
            self.threshold = max(1, int(threshold or 1))
        except (TypeError, ValueError):
            self.threshold = 2
        self.dedupe_seconds = max(0.0, float(dedupe_seconds or 0))
        self.failure_backoff = max(0.0, float(failure_backoff or 0))
        self.max_items = max(8, int(max_items or 8))
        self.log = logger
        self._lock = threading.Lock()
        self._items = {}
        self._failed = {}
        self._loaded = False

    def _load_locked(self):
        if self._loaded:
            return
        self._loaded = True
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._items = dict(data.get("items") or {})
            self._failed = dict(data.get("failed") or {})
        except Exception as exc:
            if self.log:
                self.log.warning("read play stats failed: %s", exc)
            self._items = {}
            self._failed = {}

    def _save_locked(self):
        if not self.path:
            return
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".part"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "updated_at": int(time.time()),
                           "items": self._items, "failed": self._failed},
                          fh, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception as exc:
            if self.log:
                self.log.warning("write play stats failed: %s", exc)
            try:
                os.remove(tmp)
            except OSError:
                pass

    @staticmethod
    def _key(guid):
        return str(guid or "").strip()

    def note(self, guid, title="", artist=""):
        """记录一次真正开播，返回该曲累计次数；5 秒内重复调用不累加。"""
        key = self._key(guid)
        if not key:
            return 0
        now = time.time()
        with self._lock:
            self._load_locked()
            item = self._items.get(key) or {"count": 0}
            last = float(item.get("last_play") or 0.0)
            if now - last >= self.dedupe_seconds:
                item["count"] = int(item.get("count") or 0) + 1
                item["last_play"] = now
            item["title"] = str(title or item.get("title") or "")
            item["artist"] = str(artist or item.get("artist") or "")
            item["updated_at"] = int(now)
            self._items[key] = item
            self._trim_locked()
            self._save_locked()
            return int(item.get("count") or 0)

    def count(self, guid):
        key = self._key(guid)
        if not key:
            return 0
        with self._lock:
            self._load_locked()
            return int((self._items.get(key) or {}).get("count") or 0)

    def should_download(self, guid, threshold=None):
        """是否达到自动下载阈值；失败退避期内返回 False。"""
        key = self._key(guid)
        if not key:
            return False
        need = max(1, int(threshold or self.threshold))
        now = time.time()
        with self._lock:
            self._load_locked()
            failed = float(self._failed.get(key) or 0.0)
            if failed and now - failed < self.failure_backoff:
                return False
            item = self._items.get(key) or {}
            return int(item.get("count") or 0) >= need

    def mark_downloaded(self, guid):
        key = self._key(guid)
        if not key:
            return
        with self._lock:
            self._load_locked()
            self._items.pop(key, None)
            self._failed.pop(key, None)
            self._save_locked()

    def mark_failed(self, guid):
        key = self._key(guid)
        if not key:
            return
        with self._lock:
            self._load_locked()
            self._failed[key] = time.time()
            self._save_locked()

    def _trim_locked(self):
        if len(self._items) <= self.max_items:
            return
        ordered = sorted(self._items.items(),
                         key=lambda kv: float((kv[1] or {}).get("updated_at") or 0.0))
        for key, _ in ordered[:len(self._items) - self.max_items]:
            self._items.pop(key, None)
            self._failed.pop(key, None)
