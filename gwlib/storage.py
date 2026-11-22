# -*- coding: utf-8 -*-
"""存储层：质量感知的 MP3 转码缓存 + 本地曲库保存（边播边存 / 收藏下载）。"""
import hashlib
import inspect
import logging
import os
import re
import subprocess
import tempfile
import threading
import weakref

from .errors import BadRequest, StorageError
from .quality import get_profile

LOG = logging.getLogger("fnmusic-gw.storage")


def _accepts_quality(fn):
    """按函数签名判断是否接受 quality 关键字，避免用 TypeError 探测。"""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return True
    return "quality" in params or any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())


def _call_stream(provider, guid, quality=None):
    fn = provider.stream
    return fn(guid, quality=quality) if _accepts_quality(fn) else fn(guid)


def _call_stream_to_path(provider, guid, path, quality=None):
    """优先用 provider.stream_to_path 流式落盘，老 provider 回退到 stream 字节。"""
    fn = getattr(provider, "stream_to_path", None)
    if fn is None:
        ctype, raw = _call_stream(provider, guid, quality)
        with open(path, "wb") as fh:
            fh.write(raw)
        return ctype, len(raw)
    return fn(guid, path, quality=quality) if _accepts_quality(fn) else fn(guid, path)


def _looks_like_mp3(path, content_type=""):
    """标准音质下，源本身就是 MP3 时直接复用，省掉一次 ffmpeg 转码。"""
    ctype = (content_type or "").lower()
    if "audio/mpeg" in ctype or "audio/mp3" in ctype or "mpeg" in ctype:
        return True
    try:
        with open(path, "rb") as fh:
            head = fh.read(4)
    except OSError:
        return False
    return head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and head[1] in (0xFB, 0xF3, 0xF2))


def audio_seconds(path):
    """ffprobe 读时长（秒，float）；失败返回 None。"""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15).stdout
        return float(out.decode("utf-8", "replace").strip())
    except Exception:
        return None


class TranscodeSlots:
    """可在线调整上限的转码闸门；替代直接替换 BoundedSemaphore 的竞态做法。"""

    def __init__(self, limit=2):
        self._limit = max(1, int(limit or 1))
        self._active = 0
        self._cond = threading.Condition()

    @property
    def limit(self):
        with self._cond:
            return self._limit

    def resize(self, limit):
        with self._cond:
            self._limit = max(1, int(limit or 1))
            self._cond.notify_all()

    def __enter__(self):
        with self._cond:
            while self._active >= self._limit:
                self._cond.wait()
            self._active += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        with self._cond:
            self._active = max(0, self._active - 1)
            self._cond.notify()
        return False


class MediaCache:
    """按 guid + 音质档位缓存 MP3；大小按 atime LRU 淘汰。"""

    def __init__(self, directory, max_bytes, logger=None, transcode_slots=None,
                 bitrate=None, sample_rate=None, channels=None):
        self.dir = directory
        self.max = max_bytes
        self.log = logger or LOG
        self._legacy_bitrate = bitrate
        self._legacy_sample_rate = sample_rate
        self._legacy_channels = channels
        os.makedirs(directory, exist_ok=True)
        # 弱引用：锁只在有线程持有/等待时存活，避免 guid 越多锁越多
        self._locks = weakref.WeakValueDictionary()
        self._guard = threading.Lock()
        self._slots = transcode_slots or TranscodeSlots(2)
        self._prefetching = set()
        self._prefetch_guard = threading.Lock()
        self._cleanup_stale()

    def reconfigure(self, max_bytes=None, transcode_slots=None):
        """在线调整缓存上限/转码并发，不替换正在使用的闸门对象。"""
        if max_bytes is not None:
            self.max = max(0, int(max_bytes))
        if transcode_slots is not None:
            self._slots.resize(transcode_slots)

    def _cleanup_stale(self):
        try:
            for name in os.listdir(self.dir):
                if name.endswith(".part") or ".src" in name:
                    try:
                        os.remove(os.path.join(self.dir, name))
                    except OSError:
                        pass
        except Exception as exc:
            self.log.warning("cache cleanup failed: %s", exc)

    def key(self, guid, quality=None):
        profile = get_profile(quality)
        raw = "%s|%s" % (guid, profile["name"])
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def path(self, guid, quality=None):
        return os.path.join(self.dir, self.key(guid, quality) + ".mp3")

    def prefetch(self, guid, provider, quality=None):
        """后台准备缓存，不阻塞 MCP/WebUI 的播放请求。"""
        profile = get_profile(quality)
        dst = self.path(guid, profile["name"])
        if os.path.exists(dst) and os.path.getsize(dst) > 1024:
            return dst
        key = os.path.basename(dst)
        with self._prefetch_guard:
            if key in self._prefetching:
                return dst
            self._prefetching.add(key)

        def run():
            try:
                self.ensure(guid, provider, quality=profile["name"])
            except Exception as exc:
                self.log.info("cache prefetch failed for %s: %s", guid, exc)
            finally:
                with self._prefetch_guard:
                    self._prefetching.discard(key)

        threading.Thread(target=run, daemon=True, name="cache-prefetch").start()
        return dst

    def _lock(self, key):
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    def ensure(self, guid, provider, timeout=180, quality=None):
        profile = get_profile(quality)
        dst = self.path(guid, profile["name"])
        if os.path.exists(dst) and os.path.getsize(dst) > 1024:
            return dst
        with self._lock(os.path.basename(dst)):
            if os.path.exists(dst) and os.path.getsize(dst) > 1024:
                return dst
            src = dst + ".src"
            tmp = dst + ".part"
            try:
                # 直接流式落盘到临时源文件，避免整首 FLAC/MP3 读进内存
                ctype, _ = _call_stream_to_path(provider, guid, src, profile["name"])
                if not os.path.exists(src) or os.path.getsize(src) < 1:
                    raise StorageError("empty audio for %s" % guid)
                # 标准音质且源本身是 MP3：直接改名复用，跳过 ffmpeg。
                if profile["name"] == "standard" and _looks_like_mp3(src, ctype):
                    os.replace(src, dst)
                    self._check_single_file(dst)
                    self.evict(protect=dst)
                    return dst
                bitrate = profile["bitrate"] or self._legacy_bitrate or "128k"
                sample_rate = profile["sample_rate"] or self._legacy_sample_rate or 44100
                channels = profile["channels"] or self._legacy_channels or 2
                cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-i", src,
                       "-vn", "-map_metadata", "-1",
                       "-ac", str(channels), "-ar", str(sample_rate),
                       "-c:a", "libmp3lame", "-b:a", bitrate, "-f", "mp3", tmp]
                with self._slots:
                    try:
                        subprocess.run(cmd, check=True, timeout=timeout,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                    except subprocess.TimeoutExpired:
                        raise StorageError("ffmpeg 转码超时: %s" % guid)
                    except subprocess.CalledProcessError as exc:
                        detail = (exc.stderr or b"").decode("utf-8", "replace").strip()[:200]
                        raise StorageError("ffmpeg 转码失败: %s%s" % (
                            guid, ("（%s）" % detail) if detail else ""))
                if not os.path.exists(tmp) or os.path.getsize(tmp) < 1024:
                    raise StorageError("ffmpeg 转码失败: %s" % guid)
                os.replace(tmp, dst)
            finally:
                for leftover in (src, tmp):
                    try:
                        os.remove(leftover)
                    except Exception:
                        pass
            self._check_single_file(dst)
            self.evict(protect=dst)
            return dst

    def _check_single_file(self, path):
        """单曲超过缓存上限时明确报错，避免刚写入就被 evict 掉。"""
        if not self.max:
            return
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        if size > self.max:
            try:
                os.remove(path)
            except OSError:
                pass
            raise StorageError("缓存上限 %.1f MiB 小于单曲大小 %.1f MiB" % (
                self.max / 1048576.0, size / 1048576.0))

    def evict(self, protect=None):
        try:
            protected = os.path.abspath(protect) if protect else None
            items = []
            for name in os.listdir(self.dir):
                if not name.endswith(".mp3"):
                    continue
                fp = os.path.join(self.dir, name)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                items.append((st.st_atime, st.st_size, fp))
            if self.max <= 0:
                return
            total = sum(x[1] for x in items)
            if total <= self.max:
                return
            items.sort()
            for _, size, fp in items:
                if total <= self.max * 0.9:
                    break
                if protected and os.path.abspath(fp) == protected:
                    continue
                try:
                    os.remove(fp)
                    total -= size
                except Exception:
                    pass
            self.log.info("cache evicted to %.1f MiB", total / 1048576.0)
        except Exception as exc:
            self.log.warning("cache evict failed: %s", exc)

    def stats(self):
        files = 0
        total = 0
        try:
            for name in os.listdir(self.dir):
                if not name.endswith(".mp3"):
                    continue
                try:
                    size = os.path.getsize(os.path.join(self.dir, name))
                except OSError:
                    continue
                files += 1
                total += size
        except Exception:
            pass
        return {"files": files, "bytes": total, "limit_bytes": self.max,
                "used_percent": round((total / self.max) * 100, 1) if self.max else 0}


class LibraryStore:
    """把在线音源原始文件落到本地下载目录。保存原始文件而不是转码 MP3。"""

    def __init__(self, directory_getter, logger=None, max_bytes=0):
        self._dir_getter = directory_getter or (lambda: "")
        self.max_bytes = max(0, int(max_bytes or 0))
        self.log = logger or LOG
        self._jobs = {}
        self._lock = threading.Lock()

    def enabled(self):
        return bool(str(self._dir_getter() or "").strip())

    def target_path(self, track, ext):
        directory = str(self._dir_getter() or "").strip()
        if not directory:
            raise BadRequest("保存到本地曲库需要先配置 GW_DOWNLOAD_DIR")
        name = "%s - %s" % (track.get("title") or track.get("guid") or "unknown",
                            track.get("artist") or "未知")
        name = re.sub(r'[\\/:*?"<>|]+', "_", name).strip().lstrip(".")[:150] or "unknown"
        os.makedirs(directory, exist_ok=True)
        return os.path.join(directory, name + ext)

    @staticmethod
    def _ext_for(ctype):
        low = (ctype or "").lower()
        if "flac" in low:
            return ".flac"
        if "mpeg" in low or "mp3" in low:
            return ".mp3"
        if "wav" in low:
            return ".wav"
        return ".bin"

    def _check_capacity(self, size):
        if not self.max_bytes:
            return
        current = self.stats().get("bytes", 0)
        if current + size > self.max_bytes:
            raise StorageError("本地曲库已达容量上限（%.0f MB）" % (self.max_bytes / 1048576.0))

    def save_bytes(self, track, ctype, raw):
        if not raw:
            raise BadRequest("在线音源返回空音频")
        self._check_capacity(len(raw))
        fp = self.target_path(track, self._ext_for(ctype))
        tmp = fp + ".part"
        try:
            with open(tmp, "wb") as fh:
                fh.write(raw)
            os.replace(tmp, fp)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        return fp, len(raw)

    def save_from_provider(self, provider, track, quality=None):
        directory = str(self._dir_getter() or "").strip()
        if not directory:
            raise BadRequest("保存到本地曲库需要先配置 GW_DOWNLOAD_DIR")
        os.makedirs(directory, exist_ok=True)
        # 下载前先确认还有空间，避免边下边写把磁盘写满后才报错。
        self._check_capacity(1)
        fd, tmp = tempfile.mkstemp(prefix=".fnmusic-", suffix=".part", dir=directory)
        os.close(fd)
        try:
            # 边下边写临时文件；拿到 Content-Type 后再决定扩展名
            ctype, size = _call_stream_to_path(provider, track["guid"], tmp, quality)
            if not size:
                raise BadRequest("在线音源返回空音频")
            self._check_capacity(size)
            fp = self.target_path(track, self._ext_for(ctype))
            os.replace(tmp, fp)
            tmp = ""
        finally:
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        return fp, size

    def save_async(self, provider, track, quality=None):
        if not self.enabled():
            return False
        guid = str(track.get("guid") or "")
        if not guid:
            return False
        with self._lock:
            if guid in self._jobs and self._jobs[guid].is_alive():
                return False
            job = threading.Thread(target=self._run_job, args=(provider, track, quality),
                                   name="library-save", daemon=True)
            self._jobs[guid] = job
            job.start()
            return True

    def _run_job(self, provider, track, quality):
        try:
            self.save_from_provider(provider, track, quality)
        except Exception as exc:
            self.log.info("library save %s failed: %s", track.get("guid"), exc)
        finally:
            with self._lock:
                self._jobs.pop(track.get("guid"), None)

    def stats(self):
        directory = str(self._dir_getter() or "").strip()
        files = 0
        total = 0
        if directory and os.path.isdir(directory):
            try:
                for name in os.listdir(directory):
                    if os.path.splitext(name)[1].lower() not in (".mp3", ".flac", ".wav", ".m4a", ".ogg", ".ape"):
                        continue
                    fp = os.path.join(directory, name)
                    if os.path.isfile(fp):
                        files += 1
                        total += os.path.getsize(fp)
            except Exception:
                pass
        return {"dir": directory, "enabled": bool(directory), "files": files, "bytes": total,
                "limit_bytes": self.max_bytes}
