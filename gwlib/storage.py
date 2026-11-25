# -*- coding: utf-8 -*-
"""存储层：质量感知的 MP3 转码缓存 + 本地曲库保存（边播边存 / 收藏下载）。"""
import hashlib
import inspect
import logging
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
import weakref

from .errors import BadRequest, StorageError
from .quality import get_profile

LOG = logging.getLogger("fnmusic-gw.storage")


# 预取调度：固定 worker 数，失败短 TTL 内不再重试；预取转码只占用剩余槽。
PREFETCH_WORKERS = 2
PREFETCH_FAILURE_TTL = 30.0
PREFETCH_BUSY_TTL = 5.0


class PrefetchBusy(StorageError):
    """预取遇到转码槽位忙：不算永久失败，短 TTL 后可重试。"""


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


def _looks_like_mp3_bytes(data, content_type=""):
    """流式首块嗅探：Content-Type 或前几个字节是 MP3 时可直接透传。"""
    ctype = (content_type or "").lower()
    if "audio/mpeg" in ctype or "audio/mp3" in ctype or "mpeg" in ctype:
        return True
    head = bytes(data or b"")[:4]
    return head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and head[1] in (0xFB, 0xF3, 0xF2))


def _call_iter_stream(provider, guid, quality=None):
    """取 provider.iter_stream 的分块迭代器；老 provider 返回 None 由调用方回退。"""
    fn = getattr(provider, "iter_stream", None)
    if not callable(fn):
        return None
    return fn(guid, quality=quality) if _accepts_quality(fn) else fn(guid)


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

    def try_enter(self, reserve=0):
        """非阻塞获取槽位；reserve>0 时给前台保留至少这么多个空槽。"""
        with self._cond:
            if self._active + max(0, int(reserve)) >= self._limit:
                return False
            self._active += 1
            return True

    def release(self):
        with self._cond:
            self._active = max(0, self._active - 1)
            self._cond.notify()

    def __exit__(self, exc_type, exc, tb):
        self.release()
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
        self._prefetch_queue = queue.Queue(maxsize=64)
        self._prefetch_failed = {}
        self._prefetch_workers = []
        self._prefetch_failure_ttl = PREFETCH_FAILURE_TTL
        self._prefetch_busy_ttl = PREFETCH_BUSY_TTL
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
        """把下一首放入固定 worker 队列，阻塞播放线程的只是入队，不是下载/转码。"""
        profile = get_profile(quality)
        dst = self.path(guid, profile["name"])
        try:
            if os.path.exists(dst) and os.path.getsize(dst) > 1024:
                return dst
        except OSError:
            pass
        key = os.path.basename(dst)
        now = time.time()
        with self._prefetch_guard:
            failed = self._prefetch_failed.get(key)
            if failed and now - failed[0] < failed[1]:
                return dst
            if key in self._prefetching:
                return dst
            self._prefetching.add(key)
            self._start_prefetch_workers_locked()
            try:
                self._prefetch_queue.put_nowait((guid, provider, profile["name"], key))
            except queue.Full:
                self._prefetching.discard(key)
                self.log.info("cache prefetch queue full, skip %s", guid)
        return dst

    def _start_prefetch_workers_locked(self):
        if self._prefetch_workers:
            return
        for i in range(PREFETCH_WORKERS):
            worker = threading.Thread(target=self._prefetch_worker, daemon=True,
                                      name="cache-prefetch-%d" % (i + 1))
            worker.start()
            self._prefetch_workers.append(worker)

    def _prefetch_worker(self):
        while True:
            item = self._prefetch_queue.get()
            if item is None:
                self._prefetch_queue.task_done()
                return
            guid, provider, qname, key = item
            try:
                self.ensure(guid, provider, quality=qname, prefetch=True)
            except PrefetchBusy as exc:
                with self._prefetch_guard:
                    self._prefetch_failed[key] = (time.time(), self._prefetch_busy_ttl)
                self.log.debug("cache prefetch busy for %s: %s", guid, exc)
            except Exception as exc:
                with self._prefetch_guard:
                    self._prefetch_failed[key] = (time.time(), self._prefetch_failure_ttl)
                self.log.info("cache prefetch failed for %s: %s", guid, exc)
            else:
                with self._prefetch_guard:
                    self._prefetch_failed.pop(key, None)
            finally:
                with self._prefetch_guard:
                    self._prefetching.discard(key)
                self._prefetch_queue.task_done()

    def _lock(self, key):
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    def ensure(self, guid, provider, timeout=180, quality=None, prefetch=False):
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
                if prefetch:
                    # 预取永远给前台播放留至少一个槽；槽不够就直接跳过，下轮再预取。
                    if not self._slots.try_enter(reserve=1):
                        raise PrefetchBusy("转码槽位忙，跳过预取: %s" % guid)
                    try:
                        self._run_ffmpeg(cmd, timeout, guid)
                    finally:
                        self._slots.release()
                else:
                    with self._slots:
                        self._run_ffmpeg(cmd, timeout, guid)
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

    def stream(self, guid, provider, quality=None, chunk_size=65536, timeout=180):
        """边收边播：首块到达即 yield，同时把完整音频写入缓存。

        已缓存的曲目直接按块读文件；未缓存的曲目优先用 provider.iter_stream，
        标准音质且源为 MP3 时原样透传，其余音质走 ffmpeg stdin/stdout 管道转码。
        播放器断开时生成器被 close，临时文件会清理，不缓存半成品。
        """
        profile = get_profile(quality)
        dst = self.path(guid, profile["name"])
        if os.path.exists(dst) and os.path.getsize(dst) > 1024:
            with open(dst, "rb") as fh:
                while True:
                    block = fh.read(chunk_size)
                    if not block:
                        break
                    yield block
            return
        if not callable(getattr(provider, "iter_stream", None)):
            path = self.ensure(guid, provider, quality=quality, timeout=timeout)
            with open(path, "rb") as fh:
                while True:
                    block = fh.read(chunk_size)
                    if not block:
                        break
                    yield block
            return
        tmp = dst + ".stream-%s.part" % uuid.uuid4().hex
        iterator = None
        proc = None
        errf = None
        writer = None
        with self._lock(os.path.basename(dst)):
            if os.path.exists(dst) and os.path.getsize(dst) > 1024:
                with open(dst, "rb") as fh:
                    while True:
                        block = fh.read(chunk_size)
                        if not block:
                            break
                        yield block
                return
            try:
                got = _call_iter_stream(provider, guid, profile["name"])
                if not got:
                    raise StorageError("provider does not support streaming: %s" % guid)
                ctype, iterator = got
                first = next(iterator, b"")
                if not first:
                    raise StorageError("empty audio for %s" % guid)
                if profile["name"] == "standard" and _looks_like_mp3_bytes(first, ctype):
                    writer = open(tmp, "wb")
                    writer.write(first)
                    yield first
                    for chunk in iterator:
                        if not chunk:
                            continue
                        writer.write(chunk)
                        yield chunk
                    writer.close()
                    writer = None
                else:
                    bitrate = profile["bitrate"] or self._legacy_bitrate or "128k"
                    sample_rate = profile["sample_rate"] or self._legacy_sample_rate or 44100
                    channels = profile["channels"] or self._legacy_channels or 2
                    cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-i", "pipe:0",
                           "-vn", "-map_metadata", "-1",
                           "-ac", str(channels), "-ar", str(sample_rate),
                           "-c:a", "libmp3lame", "-b:a", bitrate,
                           "-f", "mp3", "pipe:1"]
                    errf = tempfile.TemporaryFile()
                    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=errf)
                    feed_errors = []
                    def _feed():
                        try:
                            proc.stdin.write(first)
                            for chunk in iterator:
                                if chunk:
                                    proc.stdin.write(chunk)
                        except (BrokenPipeError, OSError) as exc:
                            feed_errors.append(exc)
                        except Exception as exc:
                            feed_errors.append(exc)
                        finally:
                            try:
                                proc.stdin.close()
                            except Exception:
                                pass
                    feeder = threading.Thread(target=_feed, daemon=True,
                                              name="cache-stream-feed")
                    feeder.start()
                    with self._slots:
                        writer = open(tmp, "wb")
                        while True:
                            out = proc.stdout.read(chunk_size)
                            if not out:
                                break
                            writer.write(out)
                            yield out
                        writer.close()
                        writer = None
                        rc = proc.wait(timeout=timeout)
                    if rc != 0:
                        try:
                            errf.seek(0)
                            detail = errf.read().decode("utf-8", "replace").strip()[:200]
                        except Exception:
                            detail = ""
                        raise StorageError("ffmpeg 流转码失败: %s%s" % (
                            guid, ("（%s）" % detail) if detail else ""))
                    if feed_errors:
                        raise StorageError("音频源读取失败: %s（%s）" % (guid, feed_errors[0]))
                if os.path.exists(tmp) and os.path.getsize(tmp) >= 1024:
                    os.replace(tmp, dst)
                    try:
                        self._check_single_file(dst)
                        self.evict(protect=dst)
                    except StorageError as exc:
                        self.log.warning("cache stream commit failed: %s", exc)
            except StorageError:
                raise
            except Exception as exc:
                raise StorageError("音频源读取失败: %s（%s）" % (guid, exc))
            finally:
                if writer is not None:
                    try:
                        writer.close()
                    except Exception:
                        pass
                if iterator is not None:
                    close = getattr(iterator, "close", None)
                    if callable(close):
                        try:
                            close()
                        except Exception:
                            pass
                if proc is not None and proc.poll() is None:
                    try:
                        proc.kill()
                        proc.wait(timeout=5)
                    except Exception:
                        pass
                if errf is not None:
                    try:
                        errf.close()
                    except Exception:
                        pass
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def _run_ffmpeg(self, cmd, timeout, guid):
        try:
            subprocess.run(cmd, check=True, timeout=timeout,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except subprocess.TimeoutExpired:
            raise StorageError("ffmpeg 转码超时: %s" % guid)
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or b"").decode("utf-8", "replace").strip()[:200]
            raise StorageError("ffmpeg 转码失败: %s%s" % (
                guid, ("（%s）" % detail) if detail else ""))

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

    AUDIO_EXTS = (".mp3", ".flac", ".wav", ".m4a", ".ogg", ".ape")

    def __init__(self, directory_getter, logger=None, max_bytes=0,
                 scan_cmd_getter=None, scan_url_getter=None,
                 scan_callback_getter=None,
                 scan_debounce=5.0, scan_timeout=60.0):
        self._dir_getter = directory_getter or (lambda: "")
        self.max_bytes = max(0, int(max_bytes or 0))
        self.log = logger or LOG
        self._jobs = {}
        self._lock = threading.RLock()
        # 下载完成后的飞牛扫描是可选功能：默认走已配置的飞牛音乐接口，
        # 也保留命令/HTTP 回调作为其他部署方式的高级覆盖。
        self._scan_cmd_getter = scan_cmd_getter or (lambda: "")
        self._scan_url_getter = scan_url_getter or (lambda: "")
        self._scan_callback_getter = scan_callback_getter or (lambda: None)
        self.scan_debounce = max(0.0, float(scan_debounce or 0))
        self.scan_timeout = max(1.0, float(scan_timeout or 60))
        self._scan_job = None
        self._scan_last = 0.0
        self._scan_lock = threading.Lock()

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
        self.trigger_scan()
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
        self.trigger_scan()
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

    def scan_configured(self):
        try:
            callback = self._scan_callback_getter()
        except Exception:
            callback = None
        return bool(callable(callback)
                    or str(self._scan_cmd_getter() or "").strip()
                    or str(self._scan_url_getter() or "").strip())

    def trigger_scan(self):
        """下载完成后异步触发飞牛扫描，失败只记日志；同一时间只跑一个。"""
        if not self.scan_configured():
            return False
        with self._scan_lock:
            now = time.monotonic()
            if self._scan_job is not None and self._scan_job.is_alive():
                return False
            if self.scan_debounce and now - self._scan_last < self.scan_debounce:
                return False
            self._scan_last = now
            job = threading.Thread(target=self._run_scan, name="library-scan", daemon=True)
            self._scan_job = job
            job.start()
            return True

    def _run_scan(self):
        try:
            self.run_scan()
        finally:
            with self._scan_lock:
                self._scan_job = None

    def run_scan(self):
        try:
            callback = self._scan_callback_getter()
        except Exception:
            callback = None
        if callable(callback):
            try:
                if callback():
                    self.log.info("library scan via configured music service ok")
                    return True
            except Exception as exc:
                self.log.warning("library scan via configured music service failed: %s", exc)
        cmd = str(self._scan_cmd_getter() or "").strip()
        url = str(self._scan_url_getter() or "").strip()
        if not cmd and not url:
            return False
        self.log.info("library scan triggered (cmd=%s, url=%s)", bool(cmd), bool(url))
        if cmd:
            try:
                subprocess.run(cmd, shell=True, check=False, timeout=self.scan_timeout,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as exc:
                self.log.warning("library scan command failed: %s", exc)
        if url:
            try:
                req = urllib.request.Request(url, data=b"", method="POST")
                with urllib.request.urlopen(req, timeout=min(self.scan_timeout, 15)) as resp:
                    resp.read(1024)
            except Exception as exc:
                self.log.warning("library scan url failed: %s", exc)
        return True

    def stats(self):
        directory = str(self._dir_getter() or "").strip()
        files = 0
        total = 0
        if directory and os.path.isdir(directory):
            try:
                for name in os.listdir(directory):
                    if os.path.splitext(name)[1].lower() not in self.AUDIO_EXTS:
                        continue
                    fp = os.path.join(directory, name)
                    if os.path.isfile(fp):
                        files += 1
                        total += os.path.getsize(fp)
            except Exception:
                pass
        return {"dir": directory, "enabled": bool(directory), "files": files, "bytes": total,
                "limit_bytes": self.max_bytes, "scan_configured": self.scan_configured()}
