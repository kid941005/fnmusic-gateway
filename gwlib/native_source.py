# -*- coding: utf-8 -*-
"""网关内置在线音源（musicdl）。

不依赖 fnmusic-ext 容器：每个平台一个 musicdl worker 子进程，一次搜索一个任务，
进程隔离防止第三方客户端卡死或超时。曲目解析成 ext:musicdl:<平台>:<id>，
播放/下载/歌词在同进程缓存（TTL 1800s）内直接取真实 download_url。
"""
import importlib.util
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed

LOG = logging.getLogger("fnmusic-gw.native")

DEFAULT_PLATFORMS = ["KuwoMusicClient", "MiguMusicClient"]
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def deps_available():
    for name in ("musicdl", "httpx"):
        try:
            if importlib.util.find_spec(name) is None:
                return False
        except Exception:
            return False
    return True


def deps_error():
    missing = []
    for name in ("musicdl", "httpx"):
        try:
            if importlib.util.find_spec(name) is None:
                missing.append(name)
        except Exception:
            missing.append(name)
    return "缺少依赖：" + ", ".join(missing) if missing else ""


class _Worker:
    """单个平台的 musicdl 子进程，JSON 行协议。"""

    def __init__(self, platform, work_dir, timeout, netease_cookie=""):
        self.platform = platform
        self.work_dir = work_dir
        self.timeout = max(3.0, float(timeout))
        self.netease_cookie = str(netease_cookie or "").strip()
        self.proc = None
        self._q = queue.Queue()
        self._stderr = []
        self._lock = threading.Lock()
        self._start()

    def _env(self):
        env = dict(os.environ)
        env["MUSICDL_WORK_DIR"] = self.work_dir
        env["MUSICDL_REQUEST_TIMEOUT"] = str(max(1.0, min(5.0, self.timeout)))
        env["MUSICDL_SEARCH_PAGE_CAP"] = "10"
        env["MUSICDL_PROBE_TIMEOUT"] = str(max(1.0, min(3.0, self.timeout / 4)))
        env["PYTHONUNBUFFERED"] = "1"
        root = ROOT_DIR
        old = env.get("PYTHONPATH") or ""
        env["PYTHONPATH"] = root + (os.pathsep + old if old else "")
        return env

    def _start(self):
        os.makedirs(self.work_dir, exist_ok=True)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "gwlib.musicdl_worker"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=ROOT_DIR, env=self._env(), bufsize=0,
        )
        threading.Thread(target=self._pump_stdout, args=(self.proc,), daemon=True).start()
        threading.Thread(target=self._pump_stderr, args=(self.proc,), daemon=True).start()
        msg = self._next(max(10.0, min(30.0, self.timeout + 8)))
        if not msg or msg.get("type") != "ready":
            self.close()
            raise RuntimeError("musicdl worker 启动失败：%s" % (self._stderr_tail() or "无输出"))

    def _pump_stdout(self, proc):
        try:
            for line in proc.stdout:
                line = line.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    self._q.put(json.loads(line))
                except ValueError:
                    LOG.warning("online worker bad protocol line: %.160s", line)
        finally:
            self._q.put(None)

    def _pump_stderr(self, proc):
        try:
            for line in proc.stderr:
                text = line.decode("utf-8", "replace").rstrip()
                if not text:
                    continue
                self._stderr.append(text)
                if len(self._stderr) > 30:
                    del self._stderr[:10]
                LOG.debug("online %s: %s", self.platform, text)
        except Exception:
            pass

    def _stderr_tail(self):
        return " | ".join(self._stderr[-5:])

    def _next(self, timeout):
        try:
            return self._q.get(timeout=max(0.05, float(timeout)))
        except queue.Empty:
            return None

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def iter_search(self, keyword, limit, timeout, stop_event=None):
        """逐条产出 entry；调用方提前结束时关闭 worker，避免残留任务污染下一次搜索。"""
        with self._lock:
            if not self.alive():
                self.close()
                self._q = queue.Queue()
                self._stderr = []
                self._start()
            task = {"op": "search", "source": self.platform, "keyword": keyword,
                    "fetch_size": max(1, min(10, int(limit))), "limit": max(1, int(limit)),
                    "budget_s": max(1.0, float(timeout)),
                    "netease_cookie": self.netease_cookie if self.platform == "NeteaseMusicClient" else ""}
            try:
                self.proc.stdin.write((json.dumps(task, ensure_ascii=False) + "\n").encode("utf-8"))
                self.proc.stdin.flush()
            except Exception as exc:
                self.close()
                raise RuntimeError("musicdl worker 写入失败：%s" % exc)
            deadline = time.monotonic() + max(1.0, float(timeout))
            completed = False
            try:
                while True:
                    if stop_event is not None and stop_event.is_set():
                        return
                    remain = deadline - time.monotonic()
                    if remain <= 0:
                        raise RuntimeError("搜索超时（%.1fs）" % timeout)
                    wait = min(remain, 0.5) if stop_event is not None else remain
                    msg = self._next(wait)
                    if msg is None:
                        # 可中断模式下只等 0.5s，回到循环顶部检查停止标志/超时
                        continue
                    kind = msg.get("type")
                    if kind == "entry":
                        entry = msg.get("entry")
                        if isinstance(entry, list) and len(entry) >= 3:
                            yield entry
                    elif kind == "result":
                        completed = True
                        if msg.get("partial"):
                            self.close()
                        return
                    elif kind == "error":
                        raise RuntimeError(str(msg.get("error") or "musicdl 搜索失败"))
                    else:
                        LOG.debug("online worker ignored message: %s", kind)
            finally:
                if not completed:
                    self.close()

    def search(self, keyword, limit, timeout):
        return list(self.iter_search(keyword, limit, timeout))

    def close(self):
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass


class NativeMusicDLProvider:
    """内置 musicdl 聚合音源：多平台并行，缓存真实播放地址。"""

    svc = "musicdl"

    def __init__(self, platforms=None, work_dir="", search_limit=10, timeout=25,
                 total_limit=50, total_timeout=40, ttl=1800, netease_cookie=""):
        self.platforms = [p for p in (platforms or DEFAULT_PLATFORMS) if p]
        self.work_dir = work_dir or os.path.join(ROOT_DIR, "data", "musicdl_outputs")
        self.netease_cookie = str(netease_cookie or "").strip()
        self.search_limit = max(1, int(search_limit or 10))
        self.timeout = max(3.0, float(timeout or 25))
        self.total_limit = max(1, int(total_limit or 50))
        self.total_timeout = max(1.0, float(total_timeout or 40))
        self.ttl = max(60, int(ttl or 1800))
        self._workers = {}
        self._worker_locks = {}
        self._cache = {}
        self._cache_order = []
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=min(4, len(self.platforms)) or 1,
                                        thread_name_prefix="musicdl-search")

    # ---- worker / cache ----
    def _worker(self, platform):
        # 每个平台独立锁：启动 worker 最多等 30s，不能阻塞其他平台/缓存/status。
        with self._lock:
            lock = self._worker_locks.setdefault(platform, threading.Lock())
        with lock:
            with self._lock:
                worker = self._workers.get(platform)
            if worker is not None and worker.alive():
                return worker
            if worker is not None:
                worker.close()
            worker = _Worker(platform, self.work_dir, self.timeout,
                             netease_cookie=self.netease_cookie)
            with self._lock:
                self._workers[platform] = worker
            return worker

    def prewarm(self):
        """后台预热各平台 worker，缩短首次搜索等待。"""
        if not deps_available():
            return
        for platform in self.platforms:
            threading.Thread(target=self._warm_one, args=(platform,),
                             daemon=True, name="musicdl-warm-" + str(platform)).start()

    def _warm_one(self, platform):
        try:
            self._worker(platform)
        except Exception as exc:
            LOG.info("musicdl prewarm %s failed: %s", platform, exc)

    def _remember(self, item, headers, lyric):
        eid = str(item.get("id") or "")
        if not eid:
            return
        now = time.time()
        with self._lock:
            self._cache[eid] = (item, headers, lyric, now + self.ttl)
            # 同一曲目重复搜索时先移除旧位置，避免重复 eid 在淘汰时误删仍有效的缓存。
            try:
                self._cache_order.remove(eid)
            except ValueError:
                pass
            self._cache_order.append(eid)
            while len(self._cache_order) > 1000:
                old = self._cache_order.pop(0)
                self._cache.pop(old, None)

    def _lookup(self, eid):
        with self._lock:
            row = self._cache.get(str(eid))
        if not row:
            return None
        item, headers, lyric, expires = row
        if time.time() > expires:
            return None
        return item, headers, lyric

    def _track(self, item):
        eid = str(item.get("id") or "")
        return {
            "guid": "ext:musicdl:" + eid,
            "title": item.get("title") or "",
            "artist": item.get("artist") or "",
            "album": item.get("album") or "",
            "duration_s": item.get("duration_s"),
            "ext": item.get("ext") or "mp3",
            "bitrate": item.get("bitrate"),
            "samplerate": item.get("samplerate"),
            "codec": item.get("codec"),
            "channels": item.get("channels"),
            "quality": item.get("quality") or "unknown",
            "cover_url": item.get("cover_url") or "",
            "source": "musicdl",
            "platform": item.get("source") or "",
            "ext_id": eid,
        }

    def _entry_track(self, entry):
        """把 worker 的一条 entry 转成前端曲目并写入播放缓存。"""
        try:
            item, headers, lyric = entry[0], entry[1] or {}, str(entry[2] or "")
        except (IndexError, TypeError):
            return None
        if not isinstance(item, dict) or not item.get("download_url"):
            return None
        self._remember(item, headers, lyric)
        return self._track(item)

    def _search_platform(self, platform, keyword, limit):
        deadline = time.monotonic() + self.timeout
        worker = self._worker(platform)
        tracks = []
        seen = set()
        for entry in worker.iter_search(keyword, limit,
                                        max(1.0, deadline - time.monotonic())):
            track = self._entry_track(entry)
            if not track:
                continue
            key = (track["title"].casefold(), track["artist"].casefold())
            if key in seen:
                continue
            seen.add(key)
            tracks.append(track)
            if len(tracks) >= limit:
                break
        return tracks

    # ---- provider contract ----
    def search(self, q, limit=10):
        q = str(q or "").strip()
        if not q or not self.platforms:
            return []
        per = max(1, min(10, int(limit or 10)))
        cap = min(max(1, int(limit or 1)), self.total_limit)
        deadline = time.monotonic() + self.total_timeout
        pool = self._pool
        futures = [pool.submit(self._search_platform, p, q, per) for p in self.platforms]
        batches = []
        try:
            try:
                for future in as_completed(futures, timeout=max(0.05, deadline - time.monotonic())):
                    try:
                        batches.append(future.result() or [])
                    except Exception as exc:
                        LOG.warning("online platform search failed: %s", exc)
            except FuturesTimeoutError:
                LOG.warning("online search exceeded %.1fs total budget", self.total_timeout)
        finally:
            for future in futures:
                future.cancel()
        rows = []
        seen = set()
        for batch in batches:
            for track in batch:
                if track["guid"] in seen:
                    continue
                seen.add(track["guid"])
                rows.append(track)
                if len(rows) >= cap:
                    return rows[:cap]
        return rows[:cap]

    def search_stream(self, q, limit=10, deadline_s=None):
        """多平台并行搜索，哪个平台先探活成功就先产出哪首，不等全部完成。"""
        q = str(q or "").strip()
        if not q or not self.platforms:
            return
        per = max(1, min(10, int(limit or 10)))
        cap = min(max(1, int(limit or 1)), self.total_limit)
        budget = min(self.total_timeout, float(deadline_s)) if deadline_s else self.total_timeout
        deadline = time.monotonic() + max(0.1, budget)
        out = queue.Queue()
        stop = threading.Event()

        def run(platform):
            try:
                worker = self._worker(platform)
                seen = set()
                remain = max(1.0, deadline - time.monotonic())
                for entry in worker.iter_search(
                        q, per, min(self.timeout, remain), stop_event=stop):
                    if stop.is_set():
                        break
                    track = self._entry_track(entry)
                    if not track:
                        continue
                    key = (track["title"].casefold(), track["artist"].casefold())
                    if key in seen:
                        continue
                    seen.add(key)
                    out.put(("track", track))
            except Exception as exc:
                out.put(("error", (platform, str(exc))))
            finally:
                out.put(("done", platform))

        threads = [threading.Thread(target=run, args=(p,), daemon=True,
                                    name="musicdl-stream-" + str(p))
                   for p in self.platforms]
        for thread in threads:
            thread.start()
        active = len(threads)
        seen_global = set()
        count = 0
        try:
            while active > 0:
                remain = deadline - time.monotonic()
                if remain <= 0:
                    LOG.warning("online stream exceeded %.1fs total budget",
                                self.total_timeout)
                    break
                try:
                    kind, payload = out.get(timeout=min(0.25, remain))
                except queue.Empty:
                    continue
                if kind == "track":
                    key = (payload["title"].casefold(), payload["artist"].casefold())
                    if key in seen_global:
                        continue
                    seen_global.add(key)
                    count += 1
                    yield payload
                    if count >= cap:
                        stop.set()
                        return
                elif kind == "error":
                    platform, error = payload
                    LOG.warning("online platform %s streaming search failed: %s",
                                platform, error)
                else:
                    active -= 1
        finally:
            stop.set()

    @staticmethod
    def _guid_eid(guid):
        # guid 形如 ext:musicdl:<platform>:<id>，eid 与缓存键都是 <platform>:<id>
        parts = str(guid).split(":", 2)
        return parts[2] if len(parts) == 3 else ""

    def _open(self, guid):
        eid = self._guid_eid(guid)
        row = self._lookup(eid)
        if not row:
            raise RuntimeError("在线曲目已过期，请重新搜索")
        item, headers, _lyric = row
        url = str(item.get("download_url") or "")
        if not url:
            raise RuntimeError("在线音源未返回可播放地址")
        hdrs = dict(headers or {})
        hdrs.setdefault("User-Agent", "fnmusic-gateway/native")
        req = urllib.request.Request(url, headers=hdrs)
        return urllib.request.urlopen(req, timeout=max(20, self.timeout * 3))

    def stream(self, guid, quality=None):
        with self._open(guid) as resp:
            ctype = resp.headers.get("Content-Type") or "application/octet-stream"
            return ctype, resp.read()

    def stream_to_path(self, guid, path, quality=None):
        with self._open(guid) as resp, open(path, "wb") as fh:
            ctype = resp.headers.get("Content-Type") or "application/octet-stream"
            total = 0
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                total += len(chunk)
            return ctype, total

    def track_info(self, guid):
        """按 guid 从搜索缓存还原曲目元数据，供按 guid 下载时命名。"""
        row = self._lookup(self._guid_eid(guid))
        return self._track(row[0]) if row else None

    def lyrics(self, guid):
        row = self._lookup(self._guid_eid(guid))
        return str(row[2] or "") if row else ""

    def health(self):
        return deps_available()

    def status(self):
        ok = deps_available()
        reason = "" if ok else deps_error()
        out = []
        for platform in self.platforms:
            with self._lock:
                worker = self._workers.get(platform)
            out.append({"source": "musicdl", "platform": platform, "url": "native",
                        "healthy": bool(ok and (worker is None or worker.alive())),
                        "error": reason})
        return out

    def probe(self, platform, keyword="周杰伦", limit=1):
        started = time.monotonic()
        try:
            if not deps_available():
                return {"platform": platform, "ok": False, "count": 0,
                        "ms": 0, "error": deps_error()}
            rows = self._search_platform(platform, keyword, limit)
            return {"platform": platform, "ok": True, "count": len(rows),
                    "ms": int((time.monotonic() - started) * 1000), "error": ""}
        except Exception as exc:
            return {"platform": platform, "ok": False, "count": 0,
                    "ms": int((time.monotonic() - started) * 1000), "error": str(exc)}

    def close(self):
        pool = self._pool
        self._pool = None
        if pool is not None:
            try:
                pool.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for worker in workers:
            worker.close()
