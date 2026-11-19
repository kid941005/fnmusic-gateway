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
os.environ["GW_LYRICS"] = "0"  # 单测不联网查歌词
os.environ.pop("GW_PUBLIC_BASE", None)

import app  # noqa: E402


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 前一个测试类可能清理过临时目录，这里补回来（app.CACHE 是模块级单例）
        os.makedirs(app.CACHE_DIR, exist_ok=True)
        os.makedirs(app.LYRICS_DIR, exist_ok=True)
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

    def test_plain_to_lrc(self):
        lrc = app.plain_to_lrc("第一行\n\n第二行\n第三行", step=5)
        self.assertEqual(lrc.splitlines()[0], "[00:00.00]第一行")
        self.assertEqual(lrc.splitlines()[1], "[00:05.00]第二行")
        self.assertEqual(lrc.splitlines()[2], "[00:10.00]第三行")
        self.assertEqual(len(lrc.splitlines()), 3)  # 空行被丢弃

    def test_fetch_lyrics_prefers_synced(self):
        old_enabled, old_json = app.LYRICS_ENABLED, app._http_json
        app.LYRICS_ENABLED = True
        try:
            app._http_json = lambda url, timeout: {"syncedLyrics": "[00:01.00]hi", "plainLyrics": "hi"}
            self.assertEqual(app.fetch_lyrics("t", "a"), "[00:01.00]hi")
            app._http_json = lambda url, timeout: {"syncedLyrics": "", "plainLyrics": "a\nb"}
            self.assertEqual(app.fetch_lyrics("t", "a").splitlines(),
                             ["[00:00.00]a", "[00:04.00]b"])
            def boom(url, timeout):
                raise OSError("offline")
            app._http_json = boom
            self.assertEqual(app.fetch_lyrics("t", "a"), "")
        finally:
            app.LYRICS_ENABLED, app._http_json = old_enabled, old_json

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

    def test_lyrics_route(self):
        guid = "lyr-1"
        with open(app.lyric_cache_path(guid), "w", encoding="utf-8") as fp:
            fp.write("[00:01.00]第一句\n")
        exp = int(app.time.time()) + 60
        path = "/lyrics/%s.lrc?exp=%d&sig=%s" % (guid, exp, app.sign(guid, exp))
        r, body = self.req("GET", path, raw=True)
        self.assertEqual(r.status, 200)
        self.assertIn("text/plain", r.getheader("Content-Type"))
        self.assertIn("[00:01.00]第一句".encode(), body)
        bad = path.replace("sig=", "sig=0")
        r2, _ = self.req("GET", bad, raw=True)
        self.assertEqual(r2.status, 403)
        exp2 = int(app.time.time()) + 60
        r3, _ = self.req("GET", "/lyrics/nope.lrc?exp=%d&sig=%s" % (exp2, app.sign("nope", exp2)), raw=True)
        self.assertEqual(r3.status, 404)
        r4, _ = self.req("GET", "/lyrics/", raw=True)
        self.assertEqual(r4.status, 400)

    def test_api_play_has_metadata(self):
        r, d = self.req("GET", "/api/play?query=" + urllib.parse.quote("晴天"))
        self.assertEqual(r.status, 200)
        self.assertIn("lyric_url", d)
        self.assertEqual(d["device_args"]["title"], d["title"])
        self.assertEqual(d["device_args"]["url"], d["url"])

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
        self.assertEqual(len(d["result"]["tools"]), 12)
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

    def test_lyrics_allowed(self):
        guid = "lyr-pub"
        os.makedirs(app.LYRICS_DIR, exist_ok=True)
        with open(app.lyric_cache_path(guid), "w", encoding="utf-8") as fp:
            fp.write("[00:02.00]公网歌词\n")
        exp = int(app.time.time()) + 60
        r, body = self.req("GET", "/lyrics/%s.lrc?exp=%d&sig=%s" % (guid, exp, app.sign(guid, exp)), raw=True)
        self.assertEqual(r.status, 200)
        self.assertIn("公网歌词".encode(), body)

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


class TestRanking(unittest.TestCase):
    """搜索排序 + alias 单测（不启服务）。"""

    def setUp(self):
        self.ap = os.path.join(DATA, "aliases-test.json")
        with open(self.ap, "w", encoding="utf-8") as fp:
            fp.write(json.dumps({"青花磁": "晴天", "周董": "周杰伦"}, ensure_ascii=False))
        app.ALIASES_FILE = self.ap
        app._ALIAS_CACHE.update(stamp=None, table={})

    def tearDown(self):
        app.ALIASES_FILE = ""
        app._ALIAS_CACHE.update(stamp=None, table={})

    def test_norm_text(self):
        self.assertEqual(app._norm_text("《晴天》 Live!"), "晴天live")
        self.assertEqual(app._norm_text("ＡＢＣ　１２３"), "abc123")
        self.assertEqual(app._norm_text(None), "")

    def test_aliases_and_apply(self):
        self.assertEqual(app.apply_aliases("青花磁"), ("晴天", True))
        self.assertEqual(app.apply_aliases("周董"), ("周杰伦", True))
        self.assertEqual(app.apply_aliases("夜曲"), ("夜曲", False))
        self.assertIn("周杰伦", app.aliases().values())

    def test_score_prefers_exact_over_version(self):
        exact = {"guid": "a", "title": "晴天", "artist": "周杰伦"}
        variant = {"guid": "b", "title": "晴天（钢琴版）", "artist": "纯音乐"}
        self.assertGreater(app.score_track("晴天", exact), app.score_track("晴天", variant))

    def test_score_artist_match(self):
        t = {"guid": "a", "title": "夜曲", "artist": "周杰伦"}
        self.assertGreater(app.score_track("周杰伦", t), app.score_track("陈奕迅", t))

    def test_rank_tracks_order(self):
        rows = [
            {"guid": "mock-6", "title": "晴天（钢琴版）", "artist": "纯音乐"},
            {"guid": "mock-1", "title": "晴天", "artist": "周杰伦"},
        ]
        self.assertEqual(app.rank_tracks("晴天", rows, 2)[0]["guid"], "mock-1")

    def test_search_tracks_uses_alias(self):
        rows, fixed = app.search_tracks(app.provider(), "青花磁", 5)
        self.assertEqual(fixed, "晴天")
        self.assertTrue(rows)
        self.assertEqual(rows[0]["guid"], "mock-1")


class _FakeExt:
    svc = "musicdl"

    def __init__(self, rows=None, wav=b"", lyric=""):
        self._rows = rows or []
        self._wav = wav
        self._lyric = lyric

    def search(self, q, limit=10):
        return [t for t in self._rows if q in t.get("title", "")][:limit]

    def stream(self, guid):
        return "audio/wav", self._wav

    def lyrics(self, guid):
        return self._lyric


class _FakeLocal:
    def __init__(self, rows=None):
        self._rows = rows or []

    def search(self, q, limit=10):
        return [t for t in self._rows if q in t.get("title", "")][:limit]

    def list_tracks(self, limit=20):
        return self._rows

    def stream(self, guid):
        return "audio/wav", b""


class TestFallback(unittest.TestCase):
    def test_ext_guid_helpers(self):
        self.assertTrue(app.is_ext_guid("ext:musicdl:123"))
        self.assertFalse(app.is_ext_guid("mock-1"))
        self.assertEqual(app.ext_split("ext:lxmusic:ab:cd"), ("lxmusic", "ab:cd"))

    def test_spec_svc(self):
        self.assertEqual(app._spec_svc("musicdl@http://x:1"), "musicdl")
        self.assertEqual(app._spec_svc("http://127.0.0.1:8772"), "lxmusic")
        self.assertEqual(app._spec_svc("http://127.0.0.1:8776"), "neteasefree")

    def test_local_first_then_ext(self):
        local = _FakeLocal([{"guid": "l1", "title": "晴天", "artist": "周杰伦"}])
        ext = _FakeExt([{"guid": "ext:musicdl:e1", "title": "晴天", "artist": "周杰伦", "source": "musicdl"},
                        {"guid": "ext:musicdl:e2", "title": "七里香", "artist": "周杰伦", "source": "musicdl"}])
        fp = app.FallbackProvider(local, ["musicdl@http://x:1"])
        fp.exts = [ext]
        rows = fp.search("晴天", 5)
        self.assertEqual(rows[0]["guid"], "l1")
        self.assertNotIn("ext:musicdl:e1", [r["guid"] for r in rows])
        rows2 = fp.search("七里香", 5)
        self.assertEqual(rows2[0]["guid"], "ext:musicdl:e2")

    def test_stream_routes_ext(self):
        fp = app.FallbackProvider(_FakeLocal(), ["musicdl@http://x:1"])
        fp.exts = [_FakeExt(wav=b"RIFFxxxx")]
        self.assertEqual(fp.stream("ext:musicdl:abc"), ("audio/wav", b"RIFFxxxx"))

    @unittest.skipUnless(shutil.which("ffmpeg"), "需要 ffmpeg")
    def test_audio_seconds(self):
        _ct, wav = app.MockProvider().stream("x")
        fp = os.path.join(DATA, "tone.wav")
        with open(fp, "wb") as f:
            f.write(wav)
        sec = app.audio_seconds(fp)
        self.assertTrue(sec and 3.0 < sec < 6.0)

    @unittest.skipUnless(shutil.which("ffmpeg"), "需要 ffmpeg")
    def test_ext_cache_and_download(self):
        _ct, wav = app.MockProvider().stream("x")
        fp = app.FallbackProvider(_FakeLocal(), ["musicdl@http://x:1"],
                                   download_dir_getter=lambda: app.DOWNLOAD_DIR)
        fp.exts = [_FakeExt(wav=wav)]
        path = app.CACHE.ensure("ext:musicdl:e9", fp)
        self.assertGreater(os.path.getsize(path), 1024)
        old = app.DOWNLOAD_DIR
        app.DOWNLOAD_DIR = os.path.join(DATA, "dl")
        try:
            out, n = fp.download_ext({"guid": "ext:musicdl:e9", "title": "晴天", "artist": "周杰伦"})
            self.assertTrue(os.path.exists(out))
            self.assertGreater(n, 1024)
        finally:
            app.DOWNLOAD_DIR = old

    def test_download_requires_dir(self):
        fp = app.FallbackProvider(_FakeLocal(), ["musicdl@http://x:1"],
                                   download_dir_getter=lambda: app.DOWNLOAD_DIR)
        fp.exts = [_FakeExt(wav=b"x")]
        old = app.DOWNLOAD_DIR
        app.DOWNLOAD_DIR = ""
        try:
            with self.assertRaises(app.BadRequest):
                fp.download_ext({"guid": "ext:musicdl:e", "title": "t", "artist": "a"})
        finally:
            app.DOWNLOAD_DIR = old


class TestExtProviderHttp(unittest.TestCase):
    """本地假上游，验证 ExtProvider 的契约解析（不依赖 NAS）。"""

    @classmethod
    def setUpClass(cls):
        from http.server import BaseHTTPRequestHandler

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                if u.path == "/healthz":
                    body = b'{"ok": true}'
                elif u.path == "/search":
                    body = json.dumps({"data": [{"id": "s1", "name": "晴天", "artist": "周杰伦"}]}).encode()
                elif u.path == "/info":
                    body = json.dumps({"lyric": "[00:01.00]hello"}).encode()
                elif u.path == "/stream":
                    body = b"ID3fakeaudio" * 100
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/mpeg")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                else:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        cls.srv = app.ThreadingHTTPServer(("127.0.0.1", 0), H)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_search_norm_and_guid(self):
        e = app.ExtProvider("http://127.0.0.1:%d" % self.port, "musicdl")
        rows = e.search("晴天", 5)
        self.assertEqual(rows[0]["guid"], "ext:musicdl:s1")
        self.assertEqual(rows[0]["title"], "晴天")
        self.assertEqual(rows[0]["source"], "musicdl")

    def test_stream_and_lyrics(self):
        e = app.ExtProvider("http://127.0.0.1:%d" % self.port, "musicdl")
        ctype, body = e.stream("ext:musicdl:s1")
        self.assertIn("mpeg", ctype)
        self.assertGreater(len(body), 100)
        self.assertIn("hello", e.lyrics("ext:musicdl:s1"))


class TestSearchApi(Base):
    """REST 搜索走排序与别名纠正。"""

    def test_api_search_ranked(self):
        r, body = self.req("GET", "/api/search?q=" + urllib.parse.quote("晴天"))
        self.assertEqual(r.status, 200)
        self.assertEqual(body["tracks"][0]["guid"], "mock-1")
        self.assertNotIn("corrected", body)

    def test_api_search_alias_corrected(self):
        ap = os.path.join(DATA, "aliases-api.json")
        with open(ap, "w", encoding="utf-8") as fp:
            fp.write(json.dumps({"周董": "周杰伦"}, ensure_ascii=False))
        app.ALIASES_FILE = ap
        app._ALIAS_CACHE.update(stamp=None, table={})
        try:
            r, body = self.req("GET", "/api/search?q=" + urllib.parse.quote("周董"))
            self.assertEqual(r.status, 200)
            self.assertEqual(body.get("corrected"), "周杰伦")
            self.assertTrue(body["tracks"])
        finally:
            app.ALIASES_FILE = ""
            app._ALIAS_CACHE.update(stamp=None, table={})

    def test_api_play_prefers_studio_version(self):
        r, body = self.req("GET", "/api/play?query=" + urllib.parse.quote("晴天"))
        self.assertEqual(r.status, 200)
        self.assertEqual(body["guid"], "mock-1")


class TestV2Quality(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(app.normalize_quality(""), "standard")
        self.assertEqual(app.normalize_quality("128"), "standard")
        self.assertEqual(app.normalize_quality("192k"), "high")
        self.assertEqual(app.normalize_quality("320"), "exhigh")
        self.assertEqual(app.normalize_quality("flac"), "lossless")
        self.assertEqual(app.normalize_quality("无损"), "lossless")
        self.assertEqual(len(app.describe_quality()), 4)

    def test_make_url_quality(self):
        url = app.make_url("http://h:1", "g", 60, quality="high")
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(qs["q"][0], "high")
        self.assertEqual(app.make_url("http://h:1", "g", 60), app.make_url("http://h:1", "g", 60, quality="standard"))

    def test_tool_catalog(self):
        names = [t["name"] for t in app.TOOLS]
        self.assertEqual(len(names), 12)
        for n in ("music_download", "music_sources", "music_storage"):
            self.assertIn(n, names)
        play = [t for t in app.TOOLS if t["name"] == "music_play"][0]
        self.assertIn("quality", play["inputSchema"]["properties"])

    def test_cache_key_separates_quality(self):
        self.assertNotEqual(app.CACHE.key("g", "standard"), app.CACHE.key("g", "high"))
        self.assertEqual(app.CACHE.key("g", "flac"), app.CACHE.key("g", "lossless"))


class TestV2Api(Base):
    def test_quality_and_storage_endpoints(self):
        r, d = self.req("GET", "/api/quality")
        self.assertEqual(r.status, 200)
        self.assertEqual(d["default"], "standard")
        self.assertEqual(len(d["profiles"]), 4)
        r, d = self.req("GET", "/api/storage")
        self.assertEqual(r.status, 200)
        self.assertIn("cache", d)
        self.assertIn("library", d)
        r, d = self.req("GET", "/api/sources")
        self.assertEqual(r.status, 200)
        self.assertIn("sources", d)

    def test_api_play_quality(self):
        r, d = self.req("GET", "/api/play?query=%E6%99%B4%E5%A4%A9&quality=high")
        self.assertEqual(r.status, 200)
        self.assertEqual(d["quality"], "high")
        self.assertEqual(d["bitrate"], "192k")
        self.assertIn("q=high", d["url"])
        self.assertIn("title", d["device_args"])
        self.assertIn("lyric_url", d["device_args"])

    def test_api_download_and_tool(self):
        old = app.DOWNLOAD_DIR
        app.DOWNLOAD_DIR = os.path.join(DATA, "dl-v2")
        try:
            r, d = self.req("POST", "/api/download", {"query": "晴天", "quality": "standard"})
            self.assertEqual(r.status, 200, d)
            self.assertTrue(os.path.exists(d["path"]))
            out = app.call_tool("music_download", {"query": "晴天"}, "http://h")
            self.assertIn("已保存到本地曲库", out)
            st = app.call_tool("music_storage", {}, "http://h")
            self.assertIn("本地曲库", st)
        finally:
            app.DOWNLOAD_DIR = old


if __name__ == "__main__":
    unittest.main()
