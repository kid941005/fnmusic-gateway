# -*- coding: utf-8 -*-
"""按会话持久化播放进度，供 music_resume / music_stop 使用。

只保存很小的 JSON：单曲保存当前曲目与已播放秒数；歌单保存完整曲目、
当前游标和已播放秒数。进程/容器重启后仍能恢复。调用方传入的 key
来自小智桥接器生成的 X-GW-Session 或网关来源地址。
"""
import json
import os
import threading
import time


class ResumeStore:
    def __init__(self, path, ttl=7 * 24 * 3600, max_items=64):
        self.path = str(path or "")
        self.ttl = max(60, int(ttl or 7 * 24 * 3600))
        self.max_items = max(1, int(max_items or 64))
        self._lock = threading.RLock()

    def _load(self):
        if not self.path:
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save(self, data):
        if not self.path:
            return
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, self.path)

    def _prune(self, data, now):
        dead = [k for k, v in data.items() if not isinstance(v, dict) or
                float(v.get("updated_at") or 0) + self.ttl <= now]
        for key in dead:
            data.pop(key, None)
        if len(data) > self.max_items:
            ordered = sorted(data.items(), key=lambda kv: float(kv[1].get("updated_at") or 0))
            for key, _ in ordered[:len(data) - self.max_items]:
                data.pop(key, None)

    def set(self, key, value):
        key = str(key or "default")
        with self._lock:
            data = self._load()
            now = time.time()
            self._prune(data, now)
            # 调用方随后不会再改这个值；_save 同步序列化，浅拷贝即可避免整表深拷贝。
            item = dict(value or {})
            item["updated_at"] = now
            data[key] = item
            self._save(data)

    def get(self, key):
        key = str(key or "default")
        with self._lock:
            data = self._load()
            now = time.time()
            self._prune(data, now)
            item = data.get(key)
            return item if isinstance(item, dict) else None

    def clear(self, key):
        key = str(key or "default")
        with self._lock:
            data = self._load()
            if key in data:
                data.pop(key, None)
                self._save(data)
