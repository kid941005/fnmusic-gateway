# -*- coding: utf-8 -*-
"""fnmusic-gateway 回归测试（mock 模式，进程内起服务，不依赖 NAS）。

    python tests/test_gateway.py -v

覆盖：签名/直链、纯函数、MCP JSON-RPC、REST、Range 下载、参数校验。
"""
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.parse

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

DATA = tempfile.mkdtemp(prefix="gw-test-")
os.environ.setdefault("GW_DATA", DATA)
os.environ["FNMUSIC_MOCK"] = "1"
os.environ.pop("GW_PUBLIC_BASE", None)

import app  # noqa: E402


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 前一个测试类可能清理过临时目录，这里补回来（app.CACHE 是模块级单例）
        os.makedirs(app.CACHE_DIR, exist_ok=True)
        cls.srv = app.GatewayServer(("127.0.0.1", 0), app.Handler)
        cls.port = cls.srv.server_address[1]
        cls.t = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.t.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def req(self, method, path, body=None, headers=None, raw=False):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = dict(headers or {})
        payload = None
        if body is not None:
            payload = body if isinstance(body, bytes) else json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
        c.request(method, path, body=payload, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        if raw:
            return r, data
        try:
            return r, json.loads(data.decode("utf-8")) if data else {}
        except Exception:
            return r, data


class TestPure(unittest.TestCase):
    def test_sign_verify(self):
        exp = int(app.time.time()) + 60
        sig = app.sign("g1", exp)
        self.assertTrue(app.verify("g1", str(exp), sig))
        self.assertFalse(app.verify("g2", str(exp), sig))
        self.assertFalse(app.verify("g1", str(exp), sig[:-1] + ("0" if sig[-1] != "0" else "1")))
        self.assertFalse(app.verify("g1", str(exp), ""))

    def test_expired(self):
        exp = int(app.time.time()) - 5
        self.assertFalse(app.verify("g1", str(exp), app.sign("g1", exp)))

    def test_make_url(self):
        url = app.make_url("http://h:1", "a/b c", 60)
        self.assertTrue(url.startswith("http://h:1/play/"))
        self.assertIn("exp=", url)
        self.assertIn("sig=", url)
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        guid = urllib.parse.unquote(url.split("/play/")[1].split(".mp3")[0])
        self.assertEqual(guid, "a/b c")
        self.assertTrue(app.verify(guid, qs["exp"][0], qs["sig"][0]))

    def test_norm(self):
        t = app.norm_track({"guid": "g", "title": "T", "artistName": "A", "albumName": "B"})
        self.assertEqual((t["title"], t["artist"], t["album"]), ("T", "A", "B"))
        self.assertEqual(app.norm_track({"id": "x", "name": "n"})["guid"], "x")
        pl = app.norm_playlist({"guid": "p", "name": "n", "trackCount": "abc"})
        self.assertEqual(pl["count"], 0)

    def test_clamp(self):
        self.assertEqual(app.clamp_limit(0, 10), 10)   # 0 视为未传
        self.assertEqual(app.clamp_limit(-5, 10), 1)
        self.assertEqual(app.clamp_limit(10 ** 6, 10), app.MAX_LIMIT)
        self.assertEqual(app.clamp_limit(None, 10), 10)


class TestTools(unittest.TestCase):
    def setUp(self):
        self.p = app.MockProvider()

    def test_search_and_mood(self):
        self.assertIn("晴天", app.call_tool("music_search", {"query": "晴天"}, "http://h"))
        self.assertIn("睡前", app.call_tool("music_mood", {"mood": "睡前"}, "http://h"))

    def test_browse_categories(self):
        for cat in ("track", "album", "artist", "playlist"):
            self.assertTrue(app.call_tool("music_browse", {"category": cat}, "http://h"))
        with self.assertRaises(app.BadRequest):
            app.call_tool("music_browse", {"category": "bogus"}, "http://h")

    def test_play_url(self):
        out = app.call_tool("music_play", {"query": "晴天"}, "http://h")
        self.assertIn("self.music.play_url", out)
        url = [ln for ln in out.splitlines() if "http" in ln][0].split("：")[-1]
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        guid = urllib.parse.unquote(url.split("/play/")[1].split(".mp3")[0])
        self.assertTrue(app.verify(guid, qs["exp"][0], qs["sig"][0]))

    def test_play_bad_index(self):
        with self.assertRaises(app.BadRequest):
            app.call_tool("music_play", {"query": "晴天", "index": 99}, "http://h")

    def test_status_and_favorite(self):
        self.assertTrue(app.call_tool("music_status", {}, "http://h"))
        self.assertIn("已收藏", app.call_tool("music_favorite", {"action": "add", "query": "晴天"}, "http://h"))
        with self.assertRaises(app.BadRequest):
            app.call_tool("music_favorite", {"action": "wat", "query": "晴天"}, "http://h")


class TestHttp(Base):
    def test_healthz(self):
        r, d = self.req("GET", "/healthz")
        self.assertEqual(r.status, 200)
        self.assertTrue(d["ok"])
        self.assertTrue(d["mock"])

    def test_readyz(self):
        r, d = self.req("GET", "/readyz")
        self.assertEqual(r.status, 200)
        self.assertTrue(d["ok"])

    def test_search(self):
        r, d = self.req("GET", "/api/search?q=" + urllib.parse.quote("晴天"))
        self.assertEqual(r.status, 200)
        self.assertGreaterEqual(d["count"], 1)
        r, d = self.req("GET", "/api/search?q=")
        self.assertEqual(r.status, 400)
        r, d = self.req("GET", "/api/search?q=x&limit=abc")
        self.assertEqual(r.status, 400)

    def test_play_and_download(self):
        r, d = self.req("GET", "/api/play?query=" + urllib.parse.quote("晴天"))
        self.assertEqual(r.status, 200)
        url = d["url"]
        path = url.split("://", 1)[1].split("/", 1)[1]
        r2, body = self.req("GET", "/" + path, raw=True)
        self.assertEqual(r2.status, 200)
        self.assertEqual(r2.getheader("Content-Type"), "audio/mpeg")
        self.assertGreater(len(body), 1024)
        r3, part = self.req("GET", "/" + path, headers={"Range": "bytes=0-99"}, raw=True)
        self.assertEqual(r3.status, 206)
        self.assertEqual(len(part), 100)

    def test_bad_signature(self):
        r, d = self.req("GET", "/api/play?query=" + urllib.parse.quote("晴天"))
        path = d["url"].split("://", 1)[1].split("/", 1)[1]
        bad = path.replace("sig=", "sig=0")
        r2, _ = self.req("GET", "/" + bad, raw=True)
        self.assertEqual(r2.status, 403)

    def test_warm_and_favorite(self):
        r, d = self.req("POST", "/api/warm", {"guid": ""})
        self.assertEqual(r.status, 400)
        r, d = self.req("POST", "/api/warm", {"guid": "mock-1"})
        self.assertEqual(r.status, 200)
        self.assertGreater(d["bytes"], 1024)
        r, d = self.req("POST", "/api/favorite", {"action": "nope", "guid": "mock-1"})
        self.assertEqual(r.status, 400)
        r, d = self.req("POST", "/api/favorite", {"action": "add", "guid": "mock-1"})
        self.assertEqual(r.status, 200)

    def test_unknown_route(self):
        r, _ = self.req("GET", "/playlists", raw=True)
        self.assertEqual(r.status, 404)

    def test_mcp(self):
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "t", "version": "0"}}}
        r, d = self.req("POST", "/mcp", init)
        self.assertEqual(r.status, 200)
        self.assertTrue(r.getheader("Mcp-Session-Id"))
        self.assertEqual(d["result"]["serverInfo"]["name"], "fnmusic-gateway")
        r, d = self.req("POST", "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(len(d["result"]["tools"]), 9)
        r, d = self.req("POST", "/mcp", {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                         "params": {"name": "music_status", "arguments": {}}})
        self.assertIn("content", d["result"])
        r, d = self.req("POST", "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(r.status, 202)
        r, _ = self.req("POST", "/mcp", b"not json", raw=True)
        self.assertEqual(r.status, 400)
        r, _ = self.req("DELETE", "/mcp", raw=True)
        self.assertEqual(r.status, 204)


class TestCache(Base):
    @unittest.skipUnless(shutil.which("ffmpeg"), "需要 ffmpeg")
    def test_ensure_and_no_part_left(self):
        p = app.MockProvider()
        path = app.CACHE.ensure("mock-1", p)
        self.assertTrue(path.endswith(".mp3"))
        self.assertGreater(os.path.getsize(path), 1024)
        left = [n for n in os.listdir(app.CACHE_DIR) if not n.endswith(".mp3")]
        self.assertEqual(left, [])
        self.assertEqual(app.CACHE.ensure("mock-1", p), path)  # 命中缓存


class TestPublicPort(Base):
    """GW_PUBLIC_PORT 用的 PublicHandler：只放行 /play/，其余一律 404。"""

    @classmethod
    def setUpClass(cls):
        os.makedirs(app.CACHE_DIR, exist_ok=True)
        cls.srv = app.GatewayServer(("127.0.0.1", 0), app.PublicHandler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    def test_non_play_paths_blocked(self):
        for p in ("/healthz", "/readyz", "/api/search?q=x", "/api/play?query=x",
                  "/api/playlists", "/api/moods", "/mcp"):
            r, _ = self.req("GET", p, raw=True)
            self.assertEqual(r.status, 404, p)
        r, _ = self.req("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, raw=True)
        self.assertEqual(r.status, 404)
        r, _ = self.req("DELETE", "/mcp", raw=True)
        self.assertEqual(r.status, 404)

    def test_play_still_works(self):
        exp = int(app.time.time()) + 60
        path = "/play/mock-1.mp3?exp=%d&sig=%s" % (exp, app.sign("mock-1", exp))
        r, body = self.req("GET", path, raw=True)
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Content-Type"), "audio/mpeg")
        self.assertGreater(len(body), 1024)
        r2, part = self.req("GET", path, headers={"Range": "bytes=0-99"}, raw=True)
        self.assertEqual(r2.status, 206)
        self.assertEqual(len(part), 100)
        r3, _ = self.req("HEAD", path, raw=True)
        self.assertEqual(r3.status, 200)
        bad = "/play/mock-1.mp3?exp=%d&sig=deadbeef" % exp
        r4, _ = self.req("GET", bad, raw=True)
        self.assertEqual(r4.status, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
