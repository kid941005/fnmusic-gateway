#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对比 CACHE.ensure 全量等待与 CACHE.stream 首块等待（本机模拟慢源）。

用法：
    python scripts/bench_streaming.py [--chunks 40] [--delay 0.02]

不依赖真实音源，用逐块 sleep 的假 provider 模拟网络慢速下载；
standard 音质 + MP3 源时 ensure 与 stream 都不会触发 ffmpeg，比较的是
「等全量下载完」与「首块到了就开播」的差距。
"""
import argparse
import concurrent.futures
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gwlib import storage as gw_storage  # noqa: E402


CHUNK = b"\xff\xfb" + b"x" * (64 * 1024 - 2)


class SlowProvider:
    def __init__(self, chunks, delay):
        self.chunks = chunks
        self.delay = delay

    def stream_to_path(self, guid, path, quality=None):
        with open(path, "wb") as fh:
            for _ in range(self.chunks):
                time.sleep(self.delay)
                fh.write(CHUNK)
        return "audio/mpeg", self.chunks * len(CHUNK)

    def iter_stream(self, guid, quality=None):
        def gen():
            for _ in range(self.chunks):
                time.sleep(self.delay)
                yield CHUNK
        return "audio/mpeg", gen()


def one_run(chunks, delay):
    root = tempfile.mkdtemp(prefix="gw-bench-")
    cache = gw_storage.MediaCache(root, max_bytes=chunks * len(CHUNK) * 4)
    provider = SlowProvider(chunks, delay)

    t0 = time.monotonic()
    path = cache.ensure("bench:ensure", provider, quality="standard")
    ensure_full_ms = (time.monotonic() - t0) * 1000.0
    ensure_bytes = os.path.getsize(path)

    t0 = time.monotonic()
    gen = cache.stream("bench:stream", provider, quality="standard")
    first = next(gen)
    first_chunk_ms = (time.monotonic() - t0) * 1000.0
    streamed = len(first)
    for block in gen:
        streamed += len(block)
    stream_full_ms = (time.monotonic() - t0) * 1000.0
    cached_bytes = os.path.getsize(cache.path("bench:stream", "standard"))
    shutil.rmtree(root, ignore_errors=True)
    return {
        "chunks": chunks,
        "chunk_kb": len(CHUNK) // 1024,
        "delay_ms": delay * 1000.0,
        "ensure_full_ms": ensure_full_ms,
        "ensure_bytes": ensure_bytes,
        "stream_first_chunk_ms": first_chunk_ms,
        "stream_full_ms": stream_full_ms,
        "stream_bytes": streamed,
        "stream_cached_bytes": cached_bytes,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks", type=int, default=40)
    ap.add_argument("--delay", type=float, default=0.02, help="每块之间的模拟网络延迟（秒）")
    ap.add_argument("--concurrency", type=int, default=1, help="并发播放的客户端数")
    args = ap.parse_args()
    chunks = max(2, args.chunks)
    delay = max(0.0, args.delay)
    workers = max(1, args.concurrency)
    if workers > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            rows = list(ex.map(lambda _: one_run(chunks, delay), range(workers)))
        firsts = [x["stream_first_chunk_ms"] for x in rows]
        fulls = [x["stream_full_ms"] for x in rows]
        base = rows[0]
        print("== fnmusic-gateway streaming benchmark (concurrent) ==")
        print("simulated source: %d chunks x %d KB, %.0f ms/chunk, %d clients" % (
            base["chunks"], base["chunk_kb"], base["delay_ms"], workers))
        print("stream first byte     : avg %.1f ms, max %.1f ms" % (
            sum(firsts) / len(firsts), max(firsts)))
        print("stream full download  : avg %.1f ms, max %.1f ms" % (
            sum(fulls) / len(fulls), max(fulls)))
        print("ensure full wait      : avg %.1f ms" % (
            sum(x["ensure_full_ms"] for x in rows) / len(rows)))
        return
    r = one_run(chunks, delay)
    speed = r["ensure_full_ms"] / r["stream_first_chunk_ms"] if r["stream_first_chunk_ms"] else float("inf")
    print("== fnmusic-gateway streaming benchmark ==")
    print("simulated source: %(chunks)d chunks x %(chunk_kb)d KB, %(delay_ms).0f ms/chunk" % r)
    print("ensure full wait      : %8.1f ms, %d bytes" % (r["ensure_full_ms"], r["ensure_bytes"]))
    print("stream first byte     : %8.1f ms" % r["stream_first_chunk_ms"])
    print("stream full download  : %8.1f ms, %d bytes" % (r["stream_full_ms"], r["stream_bytes"]))
    print("stream cached result  : %d bytes" % r["stream_cached_bytes"])
    if speed == float("inf"):
        print("first-byte speedup    : instant")
    else:
        print("first-byte speedup    : %.1fx" % speed)


if __name__ == "__main__":
    main()
