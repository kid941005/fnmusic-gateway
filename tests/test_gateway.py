# -*- coding: utf-8 -*-
"""fnmusic-gateway 回归测试（mock 模式，进程内起服务，不依赖 NAS）。

    python tests/test_gateway.py -v

覆盖：签名/直链、纯函数、MCP JSON-RPC、REST、Range 下载、参数校验。
"""
import gc
import http.client
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.parse
import weakref

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

DATA = tempfile.mkdtemp(prefix="gw-test-")
os.environ.setdefault("GW_DATA", DATA)
os.environ["GW_MOCK"] = "1"
os.environ["GW_LYRICS"] = "0"  # 单测不联网查歌词
os.environ.pop("GW_PUBLIC_BASE", None)

import app  # noqa: E402
import fnmusic_client  # noqa: E402
import gwlib.sources as gw_sources  # noqa: E402
import gwlib.storage as gw_storage  # noqa: E402
import gwlib.config as gw_config  # noqa: E402
try:
    import gwlib.musicdl_core as gw_musicdl_core  # noqa: E402
except Exception:  # 测试环境可能没有 httpx/musicdl 依赖
    gw_musicdl_core = None


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


class TestAdminToken(Base):
    """单端口模式：GW_ADMIN_TOKEN 保护管理接口，/play 与 /lyrics 保持匿名。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._old = app.ADMIN_TOKEN
        app.ADMIN_TOKEN = "test-secret"

    @classmethod
    def tearDownClass(cls):
        app.ADMIN_TOKEN = cls._old
        super().tearDownClass()

    def test_healthz_is_public(self):
        r, body = self.req("GET", "/healthz")
        self.assertEqual(r.status, 200)
        self.assertEqual(body.get("auth_required"), True)

    def test_api_requires_token(self):
        r, body = self.req("GET", "/api/quality")
        self.assertEqual(r.status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")

    def test_api_accepts_header_and_bearer(self):
        r, body = self.req("GET", "/api/quality", headers={"X-Admin-Token": "test-secret"})
        self.assertEqual(r.status, 200)
        r, body = self.req("GET", "/api/quality", headers={"Authorization": "Bearer test-secret"})
        self.assertEqual(r.status, 200)

    def test_play_path_is_public(self):
        r, _ = self.req("GET", "/play/not-a-guid.mp3?exp=1&sig=bad")
        self.assertNotEqual(r.status, 401)

    def test_playlist_path_is_public(self):
        r, _ = self.req("GET", "/playlist/not-a-token.mp3?exp=1&sig=bad")
        self.assertNotEqual(r.status, 401)

    def test_search_stream_sse(self):
        r, data = self.req("GET", "/api/search/stream?q=" + urllib.parse.quote("晴天") + "&limit=5",
                           raw=True, headers={"X-Admin-Token": "test-secret"})
        self.assertEqual(r.status, 200)
        self.assertIn("text/event-stream", r.getheader("Content-Type"))
        self.assertIn(b'"type": "track"', data)
        self.assertIn(b'"type": "done"', data)


class TestPure(unittest.TestCase):
    def test_device_base_validation(self):
        self.assertEqual(app._device_base("https://music.example.com/"), "https://music.example.com")
        self.assertEqual(app._device_base(" http://192.168.1.2:8766 "), "http://192.168.1.2:8766")
        self.assertEqual(app._device_base("ftp://x"), "")
        self.assertEqual(app._device_base("not-a-url"), "")

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

    def test_signature_binds_quality(self):
        exp = int(app.time.time()) + 60
        url = app.make_url("http://h:1", "g1", 60, quality="high")
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(qs["q"][0], "high")
        self.assertTrue(app.verify("g1", qs["exp"][0], qs["sig"][0], "high"))
        self.assertFalse(app.verify("g1", qs["exp"][0], qs["sig"][0]))
        self.assertFalse(app.verify("g1", qs["exp"][0], qs["sig"][0], "standard"))

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

    def test_search(self):
        self.assertIn("晴天", app.call_tool("music_search", {"query": "晴天"}, "http://h"))

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
        self.assertEqual(d["mode"], "mock")
        self.assertIn("mcp_server", d)
        self.assertEqual(d["mcp_server"]["engine"], "mcp>=2.3")

    def test_readyz(self):
        r, d = self.req("GET", "/readyz")
        self.assertEqual(r.status, 200)
        self.assertTrue(d["ok"])
        self.assertEqual(d["mode"], "mock")

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

    def test_playlist_stream(self):
        out = app.call_tool("music_play", {"playlist": "睡前"}, "http://h")
        url = [ln for ln in out.splitlines() if "http" in ln][0].split("：")[-1]
        parsed = urllib.parse.urlparse(url)
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        r, body = self.req("GET", path, raw=True)
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Content-Type"), "audio/mpeg")
        self.assertGreater(len(body), 1024)

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

    def test_api_play_by_guid(self):
        row = app.MockProvider().search("晴天", 1)[0]
        guid = row["guid"]
        qs = urllib.parse.urlencode({"guid": guid, "title": row["title"],
                                     "artist": row.get("artist", "")})
        r, d = self.req("GET", "/api/play?" + qs)
        self.assertEqual(r.status, 200)
        self.assertEqual(d["guid"], guid)
        self.assertEqual(d["label"], "直链")
        self.assertEqual((d["index"], d["count"]), (1, 1))
        self.assertTrue(d["web_url"].startswith("/play/"), d["web_url"])

    def test_api_play_has_metadata(self):
        r, d = self.req("GET", "/api/play?query=" + urllib.parse.quote("晴天"))
        self.assertEqual(r.status, 200)
        self.assertIn("lyric_url", d)
        self.assertEqual(d["device_args"]["title"], d["title"])
        self.assertEqual(d["device_args"]["url"], d["url"])

    def test_api_play_favorites_continuous(self):
        r, d = self.req("GET", "/api/play?favorites=1")
        self.assertEqual(r.status, 200)
        self.assertEqual(d["count"], 2)
        self.assertTrue(d["web_url"].startswith("/playlist/"), d["web_url"])

    def test_mcp_accepts_chunked_body(self):
        init = {"jsonrpc": "2.0", "id": 9, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "t", "version": "0"}}}
        payload = json.dumps(init).encode()
        chunk = ("%x\r\n" % len(payload)).encode() + payload + b"\r\n0\r\n\r\n"
        s = socket.create_connection(("127.0.0.1", self.port), 10)
        try:
            s.sendall(b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                      b"Content-Type: application/json\r\n"
                      b"Accept: application/json, text/event-stream\r\n"
                      b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n" + chunk)
            data = b""
            while True:
                part = s.recv(65536)
                if not part:
                    break
                data += part
        finally:
            s.close()
        head, _, body = data.partition(b"\r\n\r\n")
        self.assertIn(b" 200 ", head.split(b"\r\n")[0])
        self.assertIn(b"fnmusic-gateway", body)

    def test_mcp_rejects_oversized_chunked_body(self):
        old_cap = app.MCP_BODY_CAP
        app.MCP_BODY_CAP = 64
        try:
            payload = (b'{"jsonrpc":"2.0","id":1,"method":"initialize",'
                       b'"params":{"pad":"' + b"x" * 200 + b'"}}')
            self.assertGreater(len(payload), app.MCP_BODY_CAP)
            chunk = ("%x\r\n" % len(payload)).encode() + payload + b"\r\n0\r\n\r\n"
            s = socket.create_connection(("127.0.0.1", self.port), 10)
            data = b""
            try:
                s.sendall(b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                          b"Content-Type: application/json\r\n"
                          b"Accept: application/json, text/event-stream\r\n"
                          b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n" + chunk)
                while True:
                    part = s.recv(65536)
                    if not part:
                        break
                    data += part
            finally:
                s.close()
        finally:
            app.MCP_BODY_CAP = old_cap
        head, _, body = data.partition(b"\r\n\r\n")
        self.assertIn(b" 413 ", head.split(b"\r\n")[0])
        self.assertIn(b"payload too large", body)

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
        self.assertEqual(len(d["result"]["tools"]), 16)
        r, d = self.req("POST", "/mcp", {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                         "params": {"name": "music_status", "arguments": {}}})
        self.assertIn("content", d["result"])
        r, d = self.req("POST", "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(r.status, 202)
        r, _ = self.req("POST", "/mcp", b"not json", raw=True)
        self.assertEqual(r.status, 400)
        r, _ = self.req("DELETE", "/mcp", raw=True)
        self.assertEqual(r.status, 204)

    def test_mcp_uses_forwarded_public_base(self):
        body = {"jsonrpc": "2.0", "id": 21, "method": "tools/call",
                "params": {"name": "music_play", "arguments": {"favorites": True}}}
        r, d = self.req("POST", "/mcp", body, headers={
            "X-GW-Base": "https://device.example.com",
            "X-GW-Session": "base-test",
        })
        self.assertEqual(r.status, 200)
        text = d["result"]["content"][0]["text"]
        self.assertIn("https://device.example.com/playlist/", text)

    def test_mcp_session_header_isolates_playlist_cursor(self):
        body = {"jsonrpc": "2.0", "id": 22, "method": "tools/call",
                "params": {"name": "music_play", "arguments": {"favorites": True}}}
        self.req("POST", "/mcp", body, headers={"X-GW-Session": "mcp-iso-a"})

        def status(session):
            req = {"jsonrpc": "2.0", "id": 23, "method": "tools/call",
                   "params": {"name": "music_status", "arguments": {}}}
            r, d = self.req("POST", "/mcp", req, headers={"X-GW-Session": session})
            self.assertEqual(r.status, 200)
            return d["result"]["content"][0]["text"]

        self.assertIn("来自：收藏", status("mcp-iso-a"))
        self.assertNotIn("来自：收藏", status("mcp-iso-b"))


class TestBridgeForwarding(unittest.TestCase):
    """桥接器：会话提示与转发头。"""

    def test_session_hint_and_headers(self):
        import bridge.mcp_pipe_http as pipe
        self.assertEqual(pipe._session_hint({"params": {"_meta": {"session_id": "dev-1"}}}), "dev-1")
        self.assertEqual(pipe._session_hint({"session_id": "dev-2"}), "dev-2")
        old_base, old_token = pipe.GW_PUBLIC_BASE, pipe.GW_ADMIN_TOKEN
        try:
            pipe.GW_PUBLIC_BASE = "https://device.example.com"
            pipe.GW_ADMIN_TOKEN = ""
            h = pipe._headers("dev-3")
            self.assertEqual(h["X-GW-Session"], "dev-3")
            self.assertEqual(h["X-GW-Base"], "https://device.example.com")
        finally:
            pipe.GW_PUBLIC_BASE, pipe.GW_ADMIN_TOKEN = old_base, old_token


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


class TestHome(Base):
    """单端口原生首页：Cookie 管理鉴权、首页与设置页。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._old_token = app.ADMIN_TOKEN
        app.ADMIN_TOKEN = "test-secret"

    @classmethod
    def tearDownClass(cls):
        app.ADMIN_TOKEN = cls._old_token
        super().tearDownClass()

    def _cookie(self):
        r, _ = self.req("GET", "/?token=" + app.ADMIN_TOKEN, raw=True)
        self.assertEqual(r.status, 302)
        self.assertEqual(r.getheader("Location"), "/")
        raw = r.getheader("Set-Cookie") or ""
        self.assertIn("fnmusic_admin=", raw)
        return raw.split(";")[0]

    def test_root_requires_auth(self):
        r, _ = self.req("GET", "/", raw=True)
        self.assertEqual(r.status, 302)
        self.assertEqual(r.getheader("Location"), "/login?next=%2F")

    def test_token_sets_cookie_and_shows_home(self):
        cookie = self._cookie()
        r, body = self.req("GET", "/", headers={"Cookie": cookie}, raw=True)
        self.assertEqual(r.status, 200)
        self.assertIn(b"fnmusic-gateway", body)
        self.assertIn(("v" + app.VERSION).encode("utf-8"), body)
        self.assertIn("在线音源".encode("utf-8"), body)
        self.assertIn(b'id="player"', body)
        self.assertIn(b'id="dlq"', body)
        self.assertIn(b"loadQuality", body)
        self.assertIn(b"fmtSize", body)
        self.assertIn("下载音质".encode("utf-8"), body)

    def test_home_search_has_source_switch(self):
        from gwlib import home_page
        text = home_page.HOME_HTML
        self.assertIn('id="srcSeg"', text)
        for value in ("all", "local", "online"):
            self.assertIn('data-src="%s"' % value, text)
        self.assertIn("&source=", text)
        self.assertIn("function setSource", text)
        self.assertIn("function searchStat", text)
        self.assertIn("本地曲库没有搜到结果", text)
        self.assertIn("在线音源没有搜到结果", text)

    def test_settings_page_has_online_card(self):
        cookie = self._cookie()
        r, body = self.req("GET", "/settings", headers={"Cookie": cookie}, raw=True)
        self.assertEqual(r.status, 200)
        self.assertIn("在线音源".encode("utf-8"), body)
        self.assertIn(("v" + app.VERSION).encode("utf-8"), body)
        self.assertIn("/api/settings/online".encode("utf-8"), body)

    def test_settings_page_quality_default_is_dropdown(self):
        from gwlib import settings_page
        text = settings_page.SETTINGS_HTML
        self.assertIn("var QUALITY_OPTIONS=", text)
        self.assertIn("var DOWNLOAD_QUALITY_OPTIONS=", text)
        self.assertIn("k==='quality_default'", text)
        self.assertIn("k==='download_quality'", text)
        self.assertIn("<select data-adv=", text)
        self.assertIn("无损（保留原始文件）", text)
        for value in ("standard", "high", "exhigh", "lossless"):
            self.assertIn('"value": "%s"' % value, text)
        self.assertIn("标准（128k MP3）", text)
        self.assertIn("无损（播放 320k MP3）", text)

    def test_pages_offer_theme_with_system_default(self):
        from gwlib import home_page, login_page, settings_page, theme
        pages = (home_page.HOME_HTML, settings_page.SETTINGS_HTML,
                 login_page.login_html())
        for text in pages:
            self.assertIn('id="themeSel"', text)
            self.assertIn('<option value="system">跟随系统</option>', text)
            self.assertIn('<option value="light">浅色</option>', text)
            self.assertIn('data-theme', text)
            self.assertNotIn("__THEME_", text)
        self.assertIn(':root[data-theme="light"]', theme.THEME_STYLE)
        self.assertIn("color-scheme:light", theme.THEME_STYLE)
        self.assertIn("prefers-color-scheme", theme.THEME_SCRIPT)
        self.assertIn(":'system'", theme.THEME_SCRIPT)
        self.assertIn("fnmusic-theme", theme.THEME_SCRIPT)

    def test_settings_platform_test_results_inline_and_qr_entry(self):
        from gwlib import settings_page
        text = settings_page.SETTINGS_HTML
        self.assertIn('data-out="', text)
        self.assertIn('data-qr="1"', text)
        self.assertIn("扫码登录（推荐）", text)
        self.assertIn('class="test-out"', text)
        self.assertIn('id="qrPanel"', text)
        self.assertIn("function showTestResult", text)
        self.assertIn("showTestResult(p,", text)
        self.assertIn("net.parentNode.insertBefore(panel,net.nextSibling)", text)
        self.assertNotIn("out('onlineOut','测试 '+p+' 中", text)
        self.assertIn('id="qrBrowser"', text)
        self.assertIn("8821", text)
        self.assertIn("MUSIC_U Cookie", text)
        self.assertIn("s.status==='risk'", text)
        self.assertIn("&proto=", text)
        self.assertIn("browser_login_url", text)

    def test_healthz_reports_online(self):
        r, body = self.req("GET", "/healthz")
        self.assertEqual(r.status, 200)
        self.assertIn("online", body)
        self.assertIn("enabled", body["online"])

    def test_settings_redirects_to_login(self):
        r, _ = self.req("GET", "/settings", raw=True)
        self.assertEqual(r.status, 302)
        loc = r.getheader("Location") or ""
        self.assertTrue(loc.startswith("/login?next="))
        self.assertIn("settings", loc)


class TestLoginPage(Base):
    """登录页：密码换 Cookie，支持 next 回跳，拒绝开放重定向。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._old_token = app.ADMIN_TOKEN
        app.ADMIN_TOKEN = "login-secret"

    @classmethod
    def tearDownClass(cls):
        app.ADMIN_TOKEN = cls._old_token
        super().tearDownClass()

    def _post(self, password, nxt="/"):
        payload = urllib.parse.urlencode({"password": password, "next": nxt}).encode()
        return self.req("POST", "/login", body=payload,
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                        raw=True)

    def test_login_page_renders(self):
        r, body = self.req("GET", "/login", raw=True)
        self.assertEqual(r.status, 200)
        self.assertIn(b'type="password"', body)
        self.assertIn(b'name="next"', body)
        self.assertIn(("v" + app.VERSION).encode("utf-8"), body)
        self.assertNotIn(b"login-secret", body)

    def test_wrong_password_rejected(self):
        r, body = self._post("bad")
        self.assertEqual(r.status, 401)
        self.assertIn("密码错误".encode("utf-8"), body)
        self.assertIsNone(r.getheader("Set-Cookie"))

    def test_correct_password_sets_cookie(self):
        r, _ = self._post("login-secret")
        self.assertEqual(r.status, 302)
        self.assertEqual(r.getheader("Location"), "/")
        raw = r.getheader("Set-Cookie") or ""
        self.assertIn("fnmusic_admin=", raw)
        self.assertIn("HttpOnly", raw)
        cookie = raw.split(";")[0]
        r, body = self.req("GET", "/", headers={"Cookie": cookie}, raw=True)
        self.assertEqual(r.status, 200)
        self.assertIn(b"fnmusic-gateway", body)

    def test_next_settings_honored(self):
        r, _ = self._post("login-secret", "/settings")
        self.assertEqual(r.status, 302)
        self.assertEqual(r.getheader("Location"), "/settings")
        cookie = (r.getheader("Set-Cookie") or "").split(";")[0]
        r, body = self.req("GET", "/settings", headers={"Cookie": cookie}, raw=True)
        self.assertEqual(r.status, 200)
        self.assertIn("网关设置".encode("utf-8"), body)

    def test_open_redirect_rejected(self):
        r, _ = self._post("login-secret", "//evil.example/x")
        self.assertEqual(r.status, 302)
        self.assertEqual(r.getheader("Location"), "/")

    def test_logout_clears_cookie(self):
        r, _ = self.req("GET", "/logout", raw=True)
        self.assertEqual(r.status, 302)
        self.assertEqual(r.getheader("Location"), "/login")
        raw = r.getheader("Set-Cookie") or ""
        self.assertIn("Max-Age=0", raw)


class TestConfigStore(unittest.TestCase):
    """运行期配置存储：读写、空值删除、字段白名单。"""

    def test_roundtrip_and_delete(self):
        tmp = tempfile.mkdtemp(prefix="gwcfg-")
        try:
            store = gw_config.ConfigStore(os.path.join(tmp, "config.json"))
            self.assertEqual(store.section("fnos"), {})
            store.update("fnos", {"base": "http://a", "username": "u", "password": "p"})
            self.assertEqual(store.section("fnos")["base"], "http://a")
            # 重载（模拟重启）
            store.reload()
            self.assertEqual(store.section("fnos")["username"], "u")
            # 空字符串删除该键，未知字段被忽略
            store.update("fnos", {"password": "", "evil": "x"})
            sec = store.section("fnos")
            self.assertNotIn("password", sec)
            self.assertNotIn("evil", sec)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestFnSettings(Base):
    """网关设置页/接口：飞牛音乐地址与账号的读写、连通测试、令牌清理。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._old_token = app.ADMIN_TOKEN
        cls._old_cfg = app.FNOS_CONFIG
        cls._old_tokfile = app.FNOS_TOKEN_FILE
        cls.tmp = tempfile.mkdtemp(prefix="gwcfg-http-")
        app.ADMIN_TOKEN = "tok-settings"
        app.FNOS_CONFIG = gw_config.ConfigStore(os.path.join(cls.tmp, "config.json"))
        app.FNOS_TOKEN_FILE = os.path.join(cls.tmp, "fnmusic_token.json")

    @classmethod
    def tearDownClass(cls):
        app.ADMIN_TOKEN = cls._old_token
        app.FNOS_CONFIG = cls._old_cfg
        app.FNOS_TOKEN_FILE = cls._old_tokfile
        shutil.rmtree(cls.tmp, ignore_errors=True)
        super().tearDownClass()

    def _h(self):
        return {"X-Admin-Token": "tok-settings"}

    def test_settings_requires_auth(self):
        r, _ = self.req("GET", "/api/settings/fnos")
        self.assertEqual(r.status, 401)

    def test_settings_roundtrip_masks_password(self):
        r, body = self.req("PUT", "/api/settings/fnos",
                           body={"base": "http://127.0.0.1:59820", "username": "kid",
                                 "password": "secret-pw"}, headers=self._h())
        self.assertEqual(r.status, 200)
        self.assertTrue(body["configured"])
        self.assertTrue(body["password_set"])
        self.assertNotIn("password", body)
        self.assertNotIn("secret-pw", json.dumps(body))
        r, body = self.req("GET", "/api/settings/fnos", headers=self._h())
        self.assertEqual(body["base"], "http://127.0.0.1:59820")
        self.assertNotIn("password", body)
        # 空密码表示保持不变
        r, body = self.req("PUT", "/api/settings/fnos", body={"password": ""}, headers=self._h())
        self.assertEqual(r.status, 200)
        self.assertTrue(body["password_set"])

    def test_settings_rejects_bad_base(self):
        r, _ = self.req("PUT", "/api/settings/fnos", body={"base": "ftp://x"}, headers=self._h())
        self.assertEqual(r.status, 400)

    def test_settings_base_normalizes_music_suffix(self):
        r, _ = self.req("PUT", "/api/settings/fnos",
                        body={"base": "http://127.0.0.1:59820/music"}, headers=self._h())
        self.assertEqual(r.status, 200)
        r, body = self.req("GET", "/api/settings/fnos", headers=self._h())
        self.assertEqual(body["base"], "http://127.0.0.1:59820")

    def test_settings_test_connection_reports_failure(self):
        started = time.time()
        r, body = self.req("POST", "/api/settings/fnos/test",
                           body={"base": "http://127.0.0.1:1", "username": "kid",
                                 "password": "x"}, headers=self._h())
        self.assertEqual(r.status, 200)
        self.assertFalse(body["ok"])
        self.assertTrue(body.get("error"))
        self.assertLess(time.time() - started, 15)

    def test_settings_page_renders(self):
        r, data = self.req("GET", "/settings", headers=self._h(), raw=True)
        self.assertEqual(r.status, 200)
        self.assertIn("网关设置".encode("utf-8"), data)

    def test_logout_clears_cached_token(self):
        path = app.FNOS_TOKEN_FILE
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}")
        r, body = self.req("POST", "/api/settings/fnos/logout", body={}, headers=self._h())
        self.assertEqual(r.status, 200)
        self.assertFalse(os.path.exists(path))

    def test_settings_tls_toggle(self):
        r, body = self.req("PUT", "/api/settings/fnos", body={"insecure_tls": True},
                           headers=self._h())
        self.assertEqual(r.status, 200)
        self.assertTrue(body["insecure_tls"])
        self.assertEqual(app.FNOS_CONFIG.section("fnos").get("insecure_tls"), "1")
        self.assertEqual(os.environ.get("FNMUSIC_INSECURE_TLS"), "1")
        r, body = self.req("PUT", "/api/settings/fnos", body={"insecure_tls": False},
                           headers=self._h())
        self.assertEqual(r.status, 200)
        self.assertFalse(body["insecure_tls"])
        self.assertNotIn("insecure_tls", app.FNOS_CONFIG.section("fnos"))

    def test_settings_online_roundtrip(self):
        r, body = self.req("PUT", "/api/settings/online",
                           body={"enabled": True, "platforms": ["KuwoMusicClient"],
                                 "extra_platforms": ["QQMusicClient"],
                                 "search_limit": 8, "timeout": 12,
                                 "total_limit": 20, "total_timeout": 30},
                           headers=self._h())
        self.assertEqual(r.status, 200)
        self.assertTrue(body["enabled"])
        self.assertEqual(body["platforms"], ["KuwoMusicClient"])
        self.assertEqual(body["extra_platforms"], ["QQMusicClient"])
        self.assertEqual(body["search_limit"], 8)
        self.assertEqual(body["timeout"], 12)
        self.assertIn("services", body)
        r, got = self.req("GET", "/api/settings/online", headers=self._h())
        self.assertEqual(got["platforms"], ["KuwoMusicClient"])
        self.assertEqual(got["total_timeout"], 30)
        self.assertIn("deps_available", got)

    def test_settings_online_cookie_masked_and_clear(self):
        r, body = self.req("PUT", "/api/settings/online",
                           body={"enabled": True, "platforms": ["NeteaseMusicClient"],
                                 "netease_cookie": "MUSIC_U=secret-cookie"},
                           headers=self._h())
        self.assertEqual(r.status, 200)
        self.assertTrue(body["netease_cookie_set"])
        self.assertNotIn("netease_cookie", body)
        self.assertNotIn("secret-cookie", json.dumps(body, ensure_ascii=False))
        r, got = self.req("GET", "/api/settings/online", headers=self._h())
        self.assertTrue(got["netease_cookie_set"])
        self.assertNotIn("netease_cookie", got)
        self.assertNotIn("secret-cookie", json.dumps(got, ensure_ascii=False))
        r, got = self.req("PUT", "/api/settings/online",
                          body={"clear_netease_cookie": True}, headers=self._h())
        self.assertEqual(r.status, 200)
        self.assertFalse(got["netease_cookie_set"])
        self.assertNotIn("netease_cookie", app.FNOS_CONFIG.section("online"))

    def test_settings_online_rejects_bad_platform(self):
        r, _ = self.req("PUT", "/api/settings/online",
                        body={"platforms": ["../evil"]}, headers=self._h())
        self.assertEqual(r.status, 400)

    def test_settings_online_requires_platform_when_enabled(self):
        before = app.FNOS_CONFIG.section("online")
        r, _ = self.req("PUT", "/api/settings/online",
                        body={"enabled": True, "platforms": []}, headers=self._h())
        self.assertEqual(r.status, 400)
        self.assertEqual(app.FNOS_CONFIG.section("online"), before)

    def test_settings_online_test_calls_probe(self):
        class FakeProvider:
            def __init__(self):
                self.closed = False
            def probe(self, name):
                return {"platform": name, "ok": True, "count": 3, "ms": 1, "error": ""}
            def close(self):
                self.closed = True
        old = app.new_native_provider
        made = []
        def fake(*a, **k):
            p = FakeProvider()
            made.append(p)
            return p
        app.new_native_provider = fake
        try:
            r, body = self.req("POST", "/api/settings/online/test",
                               body={"platform": "KuwoMusicClient"}, headers=self._h())
        finally:
            app.new_native_provider = old
        self.assertEqual(r.status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["results"][0]["count"], 3)
        self.assertTrue(made and made[0].closed)

    def test_settings_online_qr_login_saves_cookie(self):
        old_start = app.netease_login.start_qr
        old_poll = app.netease_login.poll_qr

        def fake_start():
            return {"key": "abc123", "qr_content": "https://music.163.com/login?codekey=abc123",
                    "qr_svg": "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=", "expires_in": 300}

        def fake_poll(key, proto="desktop"):
            self.assertEqual(key, "abc123")
            self.assertEqual(proto, "desktop")
            return {"status": "success", "code": 803, "message": "登录成功",
                    "cookie": "MUSIC_U=qr-cookie; __csrf=csrf-x"}

        app.netease_login.start_qr = fake_start
        app.netease_login.poll_qr = fake_poll
        try:
            r, body = self.req("POST", "/api/settings/online/qr", body={}, headers=self._h())
            self.assertEqual(r.status, 200)
            self.assertEqual(body["key"], "abc123")
            self.assertIn("qr_svg", body)
            r, body = self.req("GET", "/api/settings/online/qr?key=abc123", headers=self._h())
            self.assertEqual(r.status, 200)
            self.assertEqual(body["status"], "success")
            self.assertTrue(body["netease_cookie_set"])
            self.assertNotIn("qr-cookie", json.dumps(body, ensure_ascii=False))
            self.assertEqual(app.FNOS_CONFIG.section("online").get("netease_cookie"),
                             "MUSIC_U=qr-cookie; __csrf=csrf-x")
        finally:
            app.netease_login.start_qr = old_start
            app.netease_login.poll_qr = old_poll

    def test_settings_online_qr_requires_key(self):
        r, _ = self.req("GET", "/api/settings/online/qr", headers=self._h())
        self.assertEqual(r.status, 400)


class TestNeteaseQrProtocol(unittest.TestCase):
    """网易云扫码：新版桌面端 type=3 优先，风控码降级为 risk 并引导 Cookie 登录。"""

    def setUp(self):
        import gwlib.netease_login as nl
        self.nl = nl
        self._old_req = nl._request_json
        self._old_svg = nl.qr_svg
        self.calls = []
        nl.qr_svg = lambda content: "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4="

    def tearDown(self):
        self.nl._request_json = self._old_req
        self.nl.qr_svg = self._old_svg

    def test_start_qr_uses_desktop_type3_first(self):
        def fake(url, data=None, timeout=None):
            self.calls.append((url, dict(data or {})))
            return {"code": 200, "unikey": "uk-new"}, {}
        self.nl._request_json = fake
        info = self.nl.start_qr()
        self.assertEqual(info["proto"], "desktop")
        self.assertEqual(info["key"], "uk-new")
        self.assertEqual(self.calls[0][0], self.nl.QR_UNIKEY_URL)
        self.assertEqual(self.calls[0][1], {"type": 3})
        self.assertIn("codekey=uk-new", info["qr_content"])
        self.assertEqual(info["browser_login_url"], self.nl.QR_BROWSER_LOGIN_URL)

    def test_start_qr_falls_back_to_legacy(self):
        def fake(url, data=None, timeout=None):
            self.calls.append((url, dict(data or {})))
            if url == self.nl.QR_UNIKEY_URL:
                raise self.nl.NeteaseLoginError("new api down")
            return {"code": 200, "unikey": "uk-old"}, {}
        self.nl._request_json = fake
        info = self.nl.start_qr()
        self.assertEqual(info["proto"], "legacy")
        self.assertEqual(info["key"], "uk-old")
        self.assertEqual(self.calls[1][0],
                         self.nl.QR_LEGACY_UNIKEY_URL + "?type=1")
        self.assertEqual(self.calls[1][1], {})

    def test_poll_qr_uses_desktop_post_type3(self):
        def fake(url, data=None, timeout=None):
            self.calls.append((url, dict(data or {})))
            return {"code": 801, "message": "等待扫码"}, {}
        self.nl._request_json = fake
        result = self.nl.poll_qr("abc123")
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(self.calls[0][0], self.nl.QR_POLL_URL)
        self.assertEqual(self.calls[0][1], {"key": "abc123", "type": 3})

    def test_poll_qr_risk_code_returns_risk_hint(self):
        def fake(url, data=None, timeout=None):
            return {"code": 8821, "message": "状态异常"}, {}
        self.nl._request_json = fake
        result = self.nl.poll_qr("abc123")
        self.assertEqual(result["status"], "risk")
        self.assertEqual(result["hint"], "cookie")
        self.assertIn("MUSIC_U", result["message"])
        self.assertIn("8821", result["message"])

    def test_poll_qr_legacy_uses_get_type1(self):
        def fake(url, data=None, timeout=None):
            self.calls.append((url, data))
            return {"code": 801, "message": "等待扫码"}, {}
        self.nl._request_json = fake
        self.nl.poll_qr("abc123", proto="legacy")
        self.assertEqual(self.calls[0][0],
                         self.nl.QR_LEGACY_POLL_URL + "?key=abc123&type=1")
        self.assertIsNone(self.calls[0][1])

    def test_poll_qr_falls_back_to_legacy_when_desktop_fails(self):
        def fake(url, data=None, timeout=None):
            self.calls.append((url, data))
            if url == self.nl.QR_POLL_URL:
                raise self.nl.NeteaseLoginError("desktop failed")
            return {"code": 802, "message": "已扫码"}, {}
        self.nl._request_json = fake
        result = self.nl.poll_qr("abc123")
        self.assertEqual(result["status"], "scanned")
        self.assertIn("type=1", self.calls[1][0])

    def test_poll_qr_success_reads_payload_cookie(self):
        def fake(url, data=None, timeout=None):
            return {"code": 803, "cookie": "MUSIC_U=u-1; __csrf=c-1"}, {}
        self.nl._request_json = fake
        result = self.nl.poll_qr("abc123")
        self.assertEqual(result["status"], "success")
        self.assertIn("MUSIC_U=u-1", result["cookie"])
        self.assertIn("__csrf=c-1", result["cookie"])


class TestNeteaseCookie(unittest.TestCase):
    """网易云 MUSIC_U Cookie：解析与传入 musicdl 客户端配置。"""

    def setUp(self):
        if gw_musicdl_core is None:
            self.skipTest("musicdl_core 依赖不可用")

    def test_parse_cookie_value_and_header(self):
        self.assertEqual(gw_musicdl_core.parse_netease_cookie(""), {})
        self.assertEqual(gw_musicdl_core.parse_netease_cookie("abc"),
                         {"MUSIC_U": "abc"})
        self.assertEqual(gw_musicdl_core.parse_netease_cookie("MUSIC_U=abc; __csrf=xyz"),
                         {"MUSIC_U": "abc", "__csrf": "xyz"})
        self.assertEqual(gw_musicdl_core.parse_netease_cookie("plain-token"),
                         {"MUSIC_U": "plain-token"})

    def test_search_one_source_injects_cookie_only_for_netease(self):
        seen = {}
        original = gw_musicdl_core._build_client

        class FakeClient:
            def search(self, keyword):
                return {}

        def fake(source, cfg, overrides):
            seen.clear()
            seen.update(cfg)
            return FakeClient()

        gw_musicdl_core._build_client = fake
        try:
            gw_musicdl_core.search_one_source("NeteaseMusicClient", "晴天", 3,
                                              netease_cookie="MUSIC_U=abc")
            self.assertEqual(seen["default_search_cookies"], {"MUSIC_U": "abc"})
            self.assertEqual(seen["default_parse_cookies"], {"MUSIC_U": "abc"})
            self.assertEqual(seen["default_download_cookies"], {"MUSIC_U": "abc"})
            gw_musicdl_core.search_one_source("KuwoMusicClient", "晴天", 3,
                                              netease_cookie="MUSIC_U=abc")
            self.assertNotIn("default_search_cookies", seen)
        finally:
            gw_musicdl_core._build_client = original


class TestAdvancedSettings(Base):
    """高级设置：运行参数写入 config.json 并立即作用到全局/服务对象。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._old_token = app.ADMIN_TOKEN
        cls._old_cfg = app.FNOS_CONFIG
        cls.tmp = tempfile.mkdtemp(prefix="gwadv-http-")
        app.ADMIN_TOKEN = "tok-adv"
        app.FNOS_CONFIG = gw_config.ConfigStore(os.path.join(cls.tmp, "config.json"))

    @classmethod
    def tearDownClass(cls):
        app.ADMIN_TOKEN = cls._old_token
        app.FNOS_CONFIG = cls._old_cfg
        app.apply_advanced_settings()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        super().tearDownClass()

    def _h(self):
        return {"X-Admin-Token": "tok-adv"}

    def test_advanced_requires_auth(self):
        r, _ = self.req("GET", "/api/settings/advanced")
        self.assertEqual(r.status, 401)

    def test_advanced_roundtrip_applies_hot(self):
        body = {"quality_default": "high", "download_quality": "lossless",
                "max_limit": 30, "url_ttl": 600,
                "cache_max_mb": 32, "library_max_mb": 64,
                "transcode_concurrency": 3, "smart_search": False,
                "tee_save": False, "lyrics_enabled": False,
                "lrclib_url": "off", "lyrics_retry": 120,
                "fnos_timeout": 8, "mcp_timeout": 120}
        r, resp = self.req("PUT", "/api/settings/advanced", body=body, headers=self._h())
        self.assertEqual(r.status, 200)
        self.assertEqual(resp["quality_default"], "high")
        self.assertEqual(resp["download_quality"], "lossless")
        self.assertEqual(resp["max_limit"], 30)
        # 全局与服务对象都已生效
        self.assertEqual(app.QUALITY_DEFAULT, "high")
        self.assertEqual(app.DOWNLOAD_QUALITY, "lossless")
        self.assertEqual(app.MAX_LIMIT, 30)
        self.assertEqual(app.URL_TTL, 600)
        self.assertEqual(app.CACHE.max, 32 * 1024 ** 2)
        self.assertEqual(app.LIBRARY.max_bytes, 64 * 1024 ** 2)
        self.assertEqual(app.TRANSCODE_CONCURRENCY, 3)
        self.assertEqual(app.PLAYBACK.default_quality, "high")
        self.assertEqual(app.PLAYBACK.download_quality, "lossless")
        self.assertEqual(app.PLAYBACK.ttl, 600)
        self.assertFalse(app.SMART_SEARCH)
        self.assertFalse(app.TEE_SAVE)
        self.assertFalse(app.LYRICS_ENABLED)
        self.assertEqual(app.LRCLIB_URL, "")
        self.assertEqual(app.FNOS_TIMEOUT, 8)
        self.assertEqual(app.MCP_BRIDGE.timeout, 120)
        self.assertEqual(app.FNOS_CONFIG.section("advanced").get("max_limit"), "30")
        r, got = self.req("GET", "/api/settings/advanced", headers=self._h())
        self.assertEqual(got["cache_max_mb"], 32)
        self.assertEqual(got["lrclib_url"], "")

    def test_advanced_rejects_bad_values(self):
        for body in ({"quality_default": "bogus"}, {"download_quality": "bogus"},
                     {"max_limit": 0},
                     {"mcp_timeout": 5}, {"lrclib_url": "ftp://x"},
                     {"transcode_concurrency": "abc"}):
            r, _ = self.req("PUT", "/api/settings/advanced", body=body, headers=self._h())
            self.assertEqual(r.status, 400, body)


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

    def track_info(self, guid):
        for row in self._rows:
            if row.get("guid") == guid:
                return dict(row)
        return None


class _FakeLocal:
    def __init__(self, rows=None):
        self._rows = rows or []

    def search(self, q, limit=10):
        return [t for t in self._rows if q in t.get("title", "")][:limit]

    def list_tracks(self, limit=20):
        return self._rows

    def stream(self, guid):
        return "audio/wav", b""

    def track_info(self, guid):
        for row in self._rows:
            if row.get("guid") == guid:
                return dict(row)
        return None


class TestFallback(unittest.TestCase):
    def test_native_search_stream_yields_entries(self):
        class FakeWorker:
            def iter_search(self, keyword, limit, timeout, stop_event=None):
                yield [{"id": "kuwo:1", "source": "KuwoMusicClient", "title": "晴天",
                        "artist": "周杰伦", "download_url": "http://x/a.flac",
                        "ext": "flac"}, {}, ""]
        p = app.NativeMusicDLProvider(["KuwoMusicClient"], work_dir=DATA)
        p._worker = lambda platform: FakeWorker()
        try:
            rows = list(p.search_stream("晴天", 3))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["guid"], "ext:musicdl:kuwo:1")
            self.assertEqual(p.track_info("ext:musicdl:kuwo:1")["title"], "晴天")
        finally:
            p.close()

    def test_ext_guid_helpers(self):
        self.assertTrue(app.is_ext_guid("ext:musicdl:123"))
        self.assertFalse(app.is_ext_guid("mock-1"))
        self.assertEqual(gw_sources.ext_split("ext:lxmusic:ab:cd"), ("lxmusic", "ab:cd"))

    def test_spec_svc(self):
        self.assertEqual(gw_sources.parse_spec("musicdl@http://x:1")[0], "musicdl")
        self.assertEqual(gw_sources.parse_spec("http://127.0.0.1:8772")[0], "lxmusic")
        self.assertEqual(gw_sources.parse_spec("http://127.0.0.1:8776")[0], "neteasefree")

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

    def test_search_stream_local_first_and_online_incremental(self):
        class StreamExt:
            def search_stream(self, q, limit=10):
                yield {"guid": "ext:musicdl:e3", "title": "晴天", "artist": "周杰伦",
                       "source": "musicdl"}
                yield {"guid": "ext:musicdl:e4", "title": "七里香", "artist": "周杰伦",
                       "source": "musicdl"}
        local = _FakeLocal([{"guid": "l1", "title": "晴天", "artist": "周杰伦"}])
        fp = app.FallbackProvider(local, ["musicdl@http://x:1"])
        fp.exts = [StreamExt()]
        rows = list(fp.search_stream("晴天", 5))
        # 本地已有可播放结果就短路，不再访问在线源。
        self.assertEqual([r["guid"] for r in rows], ["l1"])
        meta = fp.last_search_meta()
        self.assertEqual(meta["local_count"], 1)
        self.assertEqual(meta["online_count"], 0)

    def test_search_meta_counts_local_and_online(self):
        local = _FakeLocal([{"guid": "l1", "title": "晴天", "artist": "周杰伦"}])
        ext = _FakeExt([{"guid": "ext:musicdl:e2", "title": "晴天", "artist": "陈奕迅",
                         "source": "musicdl"}])
        fp = app.FallbackProvider(local, ["musicdl@http://x:1"])
        fp.exts = [ext]
        rows = fp.search("晴天", 5)
        meta = fp.last_search_meta()
        # 本地命中即短路：在线结果不再补入。
        self.assertEqual(len(rows), 1)
        self.assertEqual(meta["local_count"], 1)
        self.assertEqual(meta["online_count"], 0)
        self.assertEqual(meta["local_error"], "")

    def test_search_meta_reports_local_error(self):
        class Boom:
            def search(self, q, limit=10):
                raise RuntimeError("bad base")
        fp = app.FallbackProvider(Boom(), [])
        rows = fp.search("晴天", 5)
        meta = fp.last_search_meta()
        self.assertEqual(rows, [])
        self.assertEqual(meta["local_count"], 0)
        self.assertIn("bad base", meta["local_error"])

    def test_track_info_routes_local_and_ext(self):
        local = _FakeLocal([{"guid": "l1", "title": "晴天", "artist": "周杰伦"}])
        ext = _FakeExt([{"guid": "ext:musicdl:e1", "title": "七里香", "artist": "周杰伦",
                         "source": "musicdl"}])
        fp = app.FallbackProvider(local, ["musicdl@http://x:1"])
        fp.exts = [ext]
        self.assertEqual(fp.track_info("l1")["title"], "晴天")
        self.assertEqual(fp.track_info("ext:musicdl:e1")["title"], "七里香")

    def test_search_source_local_and_online(self):
        local = _FakeLocal([{"guid": "l1", "title": "晴天", "artist": "周杰伦"}])
        ext = _FakeExt([{"guid": "ext:musicdl:e1", "title": "晴天", "artist": "陈奕迅",
                         "source": "musicdl"}])
        fp = app.FallbackProvider(local, ["musicdl@http://x:1"])
        fp.exts = [ext]
        only_local = fp.search("晴天", 5, source="local")
        self.assertEqual([r["guid"] for r in only_local], ["l1"])
        self.assertEqual(fp.last_search_meta()["online_count"], 0)
        only_online = fp.search("晴天", 5, source="online")
        self.assertEqual([r["guid"] for r in only_online], ["ext:musicdl:e1"])
        self.assertEqual(fp.last_search_meta()["local_count"], 0)

    def test_search_stream_source_online_skips_local(self):
        local = _FakeLocal([{"guid": "l1", "title": "晴天", "artist": "周杰伦"}])
        ext = _FakeExt([{"guid": "ext:musicdl:e1", "title": "晴天", "artist": "陈奕迅",
                         "source": "musicdl"}])
        fp = app.FallbackProvider(local, ["musicdl@http://x:1"])
        fp.exts = [ext]
        rows = list(fp.search_stream("晴天", 5, source="online"))
        self.assertEqual([r["guid"] for r in rows], ["ext:musicdl:e1"])
        rows = list(fp.search_stream("晴天", 5, source="local"))
        self.assertEqual([r["guid"] for r in rows], ["l1"])

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


class TestSearchSourceHttp(Base):
    """/api/search?source=local|online 只返回对应来源。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._old_token = app.ADMIN_TOKEN
        app.ADMIN_TOKEN = "tok-src"

    @classmethod
    def tearDownClass(cls):
        app.ADMIN_TOKEN = cls._old_token
        super().tearDownClass()

    def _cookie(self):
        r, _ = self.req("GET", "/?token=" + app.ADMIN_TOKEN, raw=True)
        self.assertEqual(r.status, 302)
        return (r.getheader("Set-Cookie") or "").split(";")[0]

    def test_api_search_source_filters(self):
        local = _FakeLocal([{"guid": "l1", "title": "晴天", "artist": "周杰伦"}])
        ext = _FakeExt([{"guid": "ext:musicdl:e1", "title": "晴天", "artist": "陈奕迅",
                         "source": "musicdl"}])
        fp = app.FallbackProvider(local, ["musicdl@http://x:1"])
        fp.exts = [ext]
        old = app._PROVIDER
        app._PROVIDER = fp
        try:
            cookie = self._cookie()
            headers = {"Cookie": cookie}
            r, body = self.req("GET", "/api/search?q=%E6%99%B4%E5%A4%A9&limit=10&source=local",
                               headers=headers)
            self.assertEqual(r.status, 200)
            self.assertEqual([t["guid"] for t in body["tracks"]], ["l1"])
            r, body = self.req("GET", "/api/search?q=%E6%99%B4%E5%A4%A9&limit=10&source=online",
                               headers=headers)
            self.assertEqual(r.status, 200)
            self.assertEqual([t["guid"] for t in body["tracks"]], ["ext:musicdl:e1"])
        finally:
            app._PROVIDER = old


class TestFnClientBase(unittest.TestCase):
    def test_normalize_base_strips_music_suffix(self):
        n = fnmusic_client.normalize_base
        self.assertEqual(n("https://fn.hqgg.top:59826/music"), "https://fn.hqgg.top:59826")
        self.assertEqual(n("https://fn.hqgg.top:59826/music/"), "https://fn.hqgg.top:59826")
        self.assertEqual(n("https://fn.hqgg.top:59826/music/api/v1"), "https://fn.hqgg.top:59826")
        self.assertEqual(n("https://fn.hqgg.top:59826/"), "https://fn.hqgg.top:59826")
        self.assertEqual(n(""), "")


class TestOnlineOnly(unittest.TestCase):
    """2.4.x：不登录飞牛账号，FallbackProvider(base=None) 只走在线音源。"""

    def _fp(self, rows=None):
        fp = app.FallbackProvider(None, ["musicdl@http://x:1"])
        fp.exts = [_FakeExt(rows or [{"guid": "ext:musicdl:e1", "title": "晴天",
                                      "artist": "周杰伦", "source": "musicdl"}])]
        return fp

    def test_search_uses_only_online_sources(self):
        rows = self._fp().search("晴天", 5)
        self.assertEqual([r["guid"] for r in rows], ["ext:musicdl:e1"])

    def test_local_library_calls_raise_bad_request(self):
        fp = self._fp()
        for call in (lambda: fp.playlists(), lambda: fp.favorites(),
                     lambda: fp.list_tracks(1), lambda: fp.albums(1),
                     lambda: fp.artists(1), lambda: fp.add_favorite("x"),
                     lambda: fp.remove_favorite("x")):
            with self.assertRaises(app.BadRequest):
                call()

    def test_stream_rejects_non_ext_guid(self):
        with self.assertRaises(app.BadRequest):
            self._fp().stream("mock-1")

    def test_stream_routes_ext_guid(self):
        fp = self._fp()
        fp.exts = [_FakeExt(wav=b"RIFFxxxx")]
        self.assertEqual(fp.stream("ext:musicdl:abc"), ("audio/wav", b"RIFFxxxx"))

    def test_provider_requires_online_sources(self):
        old = (app._PROVIDER, app._PROVIDER_ERR, app.MOCK, app.online_settings)
        app._PROVIDER, app._PROVIDER_ERR, app.MOCK = None, None, False
        app.online_settings = lambda: {"enabled": False, "platforms": [],
                                       "extra_platforms": [], "search_limit": 10,
                                       "timeout": 25, "total_limit": 50,
                                       "total_timeout": 40}
        try:
            with self.assertRaises(RuntimeError) as ctx:
                app.provider()
            self.assertIn("未配置音源", str(ctx.exception))
        finally:
            app._PROVIDER, app._PROVIDER_ERR, app.MOCK, app.online_settings = old


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
                    self.server.last_search_query = u.query
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
        e = gw_sources.ExtProvider("http://127.0.0.1:%d" % self.port, "musicdl")
        rows = e.search("晴天", 5)
        self.assertEqual(rows[0]["guid"], "ext:musicdl:s1")
        self.assertEqual(rows[0]["title"], "晴天")
        self.assertEqual(rows[0]["source"], "musicdl")

    def test_search_limit_capped_at_10(self):
        e = gw_sources.ExtProvider("http://127.0.0.1:%d" % self.port, "musicdl")
        e.search("晴天", 50)
        q = urllib.parse.parse_qs(self.srv.last_search_query)
        self.assertEqual(q.get("limit"), ["10"])

    def test_stream_and_lyrics(self):
        e = gw_sources.ExtProvider("http://127.0.0.1:%d" % self.port, "musicdl")
        ctype, body = e.stream("ext:musicdl:s1")
        self.assertIn("mpeg", ctype)
        self.assertGreater(len(body), 100)
        self.assertIn("hello", e.lyrics("ext:musicdl:s1"))

    def test_stream_to_path(self):
        e = gw_sources.ExtProvider("http://127.0.0.1:%d" % self.port, "musicdl")
        d = tempfile.mkdtemp(prefix="gw-stream-", dir=DATA)
        fp = os.path.join(d, "a.mp3")
        ctype, n = e.stream_to_path("ext:musicdl:s1", fp)
        self.assertIn("mpeg", ctype)
        self.assertEqual(n, os.path.getsize(fp))
        self.assertGreater(n, 100)


class TestFnMusicInvalidToken(unittest.TestCase):
    """飞牛 token 失效时 HTTP 200 + code=99999 也要自动重登录并重试。"""

    @classmethod
    def setUpClass(cls):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, obj, status=200):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length", "0") or 0)
                self.rfile.read(n)
                if self.path.startswith("/music/api/v1/user/password-login"):
                    self.server.login_count += 1
                    self._json({"code": 0, "data": {"userToken": "new-token"}})
                else:
                    self._json({"code": 99999, "msg": "INVALID TOKEN", "data": None})

            def do_GET(self):
                cookie = self.headers.get("Cookie", "")
                old = "old-token" in cookie
                if "/music/api/v1/search/track" in self.path:
                    if old:
                        self._json({"code": 99999, "msg": "INVALID TOKEN", "data": None})
                    else:
                        self._json({"code": 0, "data": {"list": [{"guid": "g1", "title": "晴天"}]}})
                    return
                if "/music/api/v1/track/stream" in self.path:
                    if old:
                        self._json({"code": 99999, "msg": "INVALID TOKEN", "data": None})
                    else:
                        body = b"ID3fakeaudio" * 10
                        self.send_response(200)
                        self.send_header("Content-Type", "audio/mpeg")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    return
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        cls.srv.login_count = 0
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def client(self):
        return fnmusic_client.FnMusicClient(
            "http://127.0.0.1:%d" % self.port, username="KID", password="{{SECRET_1l78j5kv}}",
            token="old-token", timeout=5)

    def setUp(self):
        self.srv.login_count = 0

    def test_request_relogin_on_http200_invalid_token(self):
        c = self.client()
        data = c.search_track("晴天")
        self.assertEqual(data["list"][0]["title"], "晴天")
        self.assertEqual(self.srv.login_count, 1)
        self.assertEqual(c.token, "new-token")

    def test_stream_to_path_relogin_on_json_error(self):
        c = self.client()
        d = tempfile.mkdtemp(prefix="gw-fn-token-", dir=DATA)
        fp = os.path.join(d, "a.mp3")
        ctype, n = c.stream_to_path("g1", fp)
        self.assertIn("mpeg", ctype)
        self.assertGreater(n, 0)
        self.assertEqual(self.srv.login_count, 1)
        with open(fp, "rb") as fh:
            self.assertTrue(fh.read().startswith(b"ID3"))

    def test_stream_bytes_relogin_on_json_error(self):
        c = self.client()
        ctype, raw = c.stream_bytes("g1")
        self.assertIn("mpeg", ctype)
        self.assertTrue(raw.startswith(b"ID3"))
        self.assertEqual(self.srv.login_count, 1)


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
        self.assertEqual(len(names), 16)
        for n in ("music_download", "music_sources", "music_storage"):
            self.assertIn(n, names)
        play = [t for t in app.TOOLS if t["name"] == "music_play"][0]
        self.assertIn("quality", play["inputSchema"]["properties"])

    def test_cache_key_separates_quality(self):
        self.assertNotEqual(app.CACHE.key("g", "standard"), app.CACHE.key("g", "high"))
        self.assertEqual(app.CACHE.key("g", "flac"), app.CACHE.key("g", "lossless"))


class TestV2Playback(unittest.TestCase):
    class _Cache:
        def __init__(self, fail_first=False):
            self.calls = []
            self.fail_first = fail_first

        def ensure(self, guid, provider, timeout=180, quality=None):
            self.calls.append((guid, quality))
            if self.fail_first and guid == "ext:musicdl:bad":
                raise RuntimeError("bad stream")
            return "fake.mp3"

    class _Library:
        def __init__(self):
            self.saved = []

        def enabled(self):
            return True

        def save_async(self, provider, track, quality=None):
            self.saved.append((track["guid"], quality))
            return True

    class _Provider:
        def search(self, q, limit=10):
            return [{"guid": "ext:musicdl:bad", "title": "晴天", "artist": "周杰伦"},
                    {"guid": "ext:musicdl:good", "title": "晴天", "artist": "周杰伦"}]

        def stream(self, guid, quality=None):
            return "audio/mpeg", b"x"

    def _svc(self, cache=None, library=None):
        return app.PlaybackService(
            cache=cache or self._Cache(), library=library or self._Library(),
            make_url=lambda b, g, ttl=None, quality=None: "http://x/play/%s?q=%s" % (g, quality),
            lyric_url_for=lambda b, t, ttl=None: "",
            audio_seconds=lambda p: 100.0, save_state=lambda s: None,
            default_quality="standard", download_quality="lossless",
            tee_save=True)

    def test_tee_save_only_for_online_source(self):
        lib = self._Library()
        info = self._svc(library=lib).prepare(
            {"guid": "ext:musicdl:e1", "title": "晴天", "artist": "周杰伦", "source": "musicdl"},
            self._Provider(), "http://h")
        self.assertTrue(info["saving"])
        # 边播边存走下载音质，不跟随在线试听音质。
        self.assertEqual(lib.saved, [("ext:musicdl:e1", "lossless")])
        info2 = self._svc(library=lib).prepare(
            {"guid": "mock-1", "title": "晴天", "artist": "周杰伦"}, self._Provider(), "http://h")
        self.assertFalse(info2["saving"])

    def test_music_play_playlist_returns_continuous_stream(self):
        out = app.call_tool("music_play", {"playlist": "睡前"}, "http://h")
        self.assertIn("连续播放", out)
        url = [ln for ln in out.splitlines() if "http" in ln][0].split("：")[-1]
        path = urllib.parse.urlparse(url).path
        self.assertIn("/playlist/", path)
        token = path.split("/playlist/")[1].rsplit(".mp3", 1)[0]
        self.assertTrue(app.PLAYLISTS.get(token))

    def test_music_play_favorites_returns_continuous_stream(self):
        out = app.call_tool("music_play", {"favorites": True}, "http://h")
        self.assertIn("连续播放", out)
        self.assertIn("收藏", out)

    def test_prepare_fast_returns_before_cache_is_ready(self):
        info = self._svc().prepare(
            {"guid": "ext:musicdl:e2", "title": "晴天", "artist": "周杰伦", "source": "musicdl"},
            self._Provider(), "http://h", wait=False)
        self.assertTrue(info["preparing"])
        self.assertTrue(info["url"].startswith("http://x/play/"))

    def test_failover_reselects_source(self):
        cache = self._Cache(fail_first=True)
        info = self._svc(cache=cache).prepare(
            {"guid": "ext:musicdl:bad", "title": "晴天", "artist": "周杰伦", "source": "musicdl"},
            self._Provider(), "http://h")
        self.assertEqual(info["guid"], "ext:musicdl:good")
        self.assertEqual(cache.calls[0][0], "ext:musicdl:bad")
        self.assertEqual(cache.calls[-1][0], "ext:musicdl:good")


class TestV2Api(Base):
    def test_quality_and_storage_endpoints(self):
        r, d = self.req("GET", "/api/quality")
        self.assertEqual(r.status, 200)
        self.assertEqual(d["default"], "standard")
        self.assertEqual(d["download"], "lossless")
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
            self.assertEqual(d["quality"], "standard")
            self.assertEqual(d["quality_label"], "标准（128k MP3）")
            self.assertGreater(d["bytes"], 0)
            out = app.call_tool("music_download", {"query": "晴天"}, "http://h")
            self.assertIn("已保存到本地曲库", out)
            st = app.call_tool("music_storage", {}, "http://h")
            self.assertIn("本地曲库", st)
        finally:
            app.DOWNLOAD_DIR = old

    def test_api_download_defaults_to_download_quality(self):
        old_dir, old_q = app.DOWNLOAD_DIR, app.DOWNLOAD_QUALITY
        app.DOWNLOAD_DIR = os.path.join(DATA, "dl-default-q")
        app.DOWNLOAD_QUALITY = "exhigh"
        try:
            r, d = self.req("POST", "/api/download", {"guid": "mock-1"})
            self.assertEqual(r.status, 200, d)
            self.assertEqual(d["quality"], "exhigh")
            self.assertEqual(d["quality_label"], "极高（320k MP3）")
        finally:
            app.DOWNLOAD_DIR = old_dir
            app.DOWNLOAD_QUALITY = old_q

    def test_api_download_by_guid_uses_track_title(self):
        old = app.DOWNLOAD_DIR
        app.DOWNLOAD_DIR = os.path.join(DATA, "dl-guid")
        try:
            # 只传 guid：服务端应通过 provider.track_info 还原「标题 - 歌手」
            r, d = self.req("POST", "/api/download", {"guid": "mock-1"})
            self.assertEqual(r.status, 200, d)
            name = os.path.basename(d["path"])
            self.assertIn("晴天", name)
            self.assertIn("周杰伦", name)
            self.assertNotIn("mock-1", name)
        finally:
            app.DOWNLOAD_DIR = old


class TestAuditFixes(unittest.TestCase):
    """本轮审计修复的回归用例。"""

    def test_env_bool_blank_uses_default(self):
        old = os.environ.get("GW_TEE_SAVE")
        os.environ["GW_TEE_SAVE"] = ""
        try:
            self.assertTrue(app._env_bool("GW_TEE_SAVE", True))
            self.assertFalse(app._env_bool("GW_TEE_SAVE", False))
        finally:
            if old is None:
                os.environ.pop("GW_TEE_SAVE", None)
            else:
                os.environ["GW_TEE_SAVE"] = old

    def test_music_sources_uses_source_healthy_fields(self):
        old_settings, old_status = app.online_settings, app.online_status
        app.online_settings = lambda: {"enabled": True, "platforms": ["KuwoMusicClient"],
                                       "extra_platforms": [], "search_limit": 10,
                                       "timeout": 25, "total_limit": 50,
                                       "total_timeout": 40}
        app.online_status = lambda: [{"source": "musicdl", "platform": "KuwoMusicClient",
                                      "url": "native", "healthy": True}]
        try:
            out = app.call_tool("music_sources", {}, "http://h")
            self.assertIn("musicdl / KuwoMusicClient  正常", out)
        finally:
            app.online_settings, app.online_status = old_settings, old_status

    def test_status_and_storage_do_not_need_provider(self):
        old = app.provider
        app.provider = lambda: (_ for _ in ()).throw(RuntimeError("nas down"))
        try:
            self.assertIsInstance(app.call_tool("music_status", {}, "http://h"), str)
            self.assertIn("播放缓存", app.call_tool("music_storage", {}, "http://h"))
            old_settings = app.online_settings
            app.online_settings = lambda: {"enabled": False, "platforms": [],
                                           "extra_platforms": [], "search_limit": 10,
                                           "timeout": 25, "total_limit": 50,
                                           "total_timeout": 40}
            try:
                out = app.call_tool("music_sources", {}, "http://h")
                self.assertIn("未启用在线音源", out)
            finally:
                app.online_settings = old_settings
        finally:
            app.provider = old

    def test_parse_spec_url_with_at(self):
        svc, url = gw_sources.parse_spec("http://user@host:8772")
        self.assertEqual(svc, "lxmusic")
        self.assertEqual(url, "http://user@host:8772")

    def test_media_cache_zero_means_unlimited(self):
        d = tempfile.mkdtemp(prefix="gw-cache-", dir=DATA)
        cache = gw_storage.MediaCache(d, 0)
        fp = os.path.join(d, "a.mp3")
        with open(fp, "wb") as fh:
            fh.write(b"x" * 2048)
        cache.evict()
        self.assertTrue(os.path.exists(fp))

    def test_library_max_bytes_enforced(self):
        d = tempfile.mkdtemp(prefix="gw-lib-", dir=DATA)
        lib = gw_storage.LibraryStore(lambda: d, max_bytes=8)
        with self.assertRaises(gw_storage.StorageError):
            lib.save_bytes({"guid": "g", "title": "t", "artist": "a"}, "audio/mpeg", b"0123456789")

    def test_fallback_library_gets_max_bytes(self):
        fp = app.FallbackProvider(_FakeLocal(), ["musicdl@http://x:1"], max_bytes=123)
        self.assertEqual(fp.library.max_bytes, 123)

class TestOptim213(unittest.TestCase):
    """2.13.0：guid 直取播放、MCP 快速流式搜索、标准音质 MP3 直出。"""

    def test_search_outputs_guid(self):
        out = app.call_tool("music_search", {"query": "晴天"}, "http://h")
        self.assertIn("guid=mock-1", out)
        self.assertIn("本地", out)

    def test_play_by_guid(self):
        out = app.call_tool("music_play", {"guid": "mock-1"}, "http://h")
        self.assertIn("按 guid", out)
        self.assertIn("self.music.play_url", out)
        url = [ln for ln in out.splitlines() if "http" in ln][0].split("：")[-1]
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        guid = urllib.parse.unquote(url.split("/play/")[1].split(".mp3")[0])
        self.assertEqual(guid, "mock-1")
        self.assertTrue(app.verify(guid, qs["exp"][0], qs["sig"][0]))

    def test_fast_search_prefers_stream_and_does_not_call_search(self):
        class StreamOnly:
            def search(self, *a, **k):
                raise AssertionError("不应退回整段 search")

            def search_stream(self, q, limit=10, deadline_s=None):
                yield {"guid": "s-1", "title": "晴天", "artist": "周杰伦"}
                yield {"guid": "s-2", "title": "晴天（钢琴版）", "artist": "纯音乐"}

        rows, _ = app.fast_search_tracks(StreamOnly(), "晴天", limit=5, budget=1)
        self.assertEqual([r["guid"] for r in rows], ["s-1", "s-2"])

    def test_standard_mp3_source_skips_ffmpeg(self):
        from unittest import mock

        class Mp3Prov:
            def stream_to_path(self, guid, path, quality=None):
                with open(path, "wb") as fh:
                    fh.write(b"ID3" + b"\x00" * 4096)
                return "audio/mpeg", 4099

        d = tempfile.mkdtemp(prefix="gw-mp3-", dir=DATA)
        cache = gw_storage.MediaCache(d, 0)
        with mock.patch.object(gw_storage.subprocess, "run",
                               side_effect=AssertionError("不应调用 ffmpeg")):
            out = cache.ensure("ext:musicdl:mp3", Mp3Prov(), quality="standard")
        self.assertTrue(os.path.exists(out))
        with open(out, "rb") as fh:
            self.assertEqual(fh.read(3), b"ID3")

    def test_resolve_playlist_picks_best_name_among_many(self):
        class P:
            def playlists(self):
                return [{"guid": "p2", "name": "儿歌大全"},
                        {"guid": "p1", "name": "儿歌"},
                        {"guid": "p3", "name": "睡前"}]

        self.assertEqual(app.resolve_playlist(P(), "播放儿歌歌单")["guid"], "p1")
        self.assertEqual(app.resolve_playlist(P(), "儿歌大全")["guid"], "p2")
        self.assertEqual(app.resolve_playlist(P(), "随便听听睡前")["guid"], "p3")

    def test_music_play_artist_returns_continuous_stream(self):
        out = app.call_tool("music_play", {"artist": "周杰伦"}, "http://h")
        self.assertIn("歌手「周杰伦」", out)
        self.assertIn("共 2 首", out)
        url = [ln for ln in out.splitlines() if "http" in ln][0].split("：")[-1]
        token = urllib.parse.urlparse(url).path.split("/playlist/")[1].rsplit(".mp3", 1)[0]
        self.assertEqual([t["guid"] for t in app.PLAYLISTS.get(token)["tracks"]],
                         ["mock-1", "mock-2"])

    def test_music_play_album_returns_continuous_stream(self):
        out = app.call_tool("music_play", {"album": "叶惠美"}, "http://h")
        self.assertIn("专辑「叶惠美」", out)
        self.assertIn("共 1 首", out)

    def test_music_play_query_artist_name_auto_plays_artist(self):
        out = app.call_tool("music_play", {"query": "周杰伦"}, "http://h")
        self.assertIn("歌手「周杰伦」", out)
        self.assertIn("共 2 首", out)

    def test_music_play_favorites_shuffle_reorders_tracks(self):
        from unittest import mock
        with mock.patch.object(app.random, "shuffle", side_effect=lambda xs: xs.reverse()):
            out = app.call_tool("music_play", {"favorites": True, "shuffle": True}, "http://h")
        self.assertIn("随机播放", out)
        self.assertIn("随机顺序", out)
        url = [ln for ln in out.splitlines() if "http" in ln][0].split("：")[-1]
        token = urllib.parse.urlparse(url).path.split("/playlist/")[1].rsplit(".mp3", 1)[0]
        item = app.PLAYLISTS.get(token)
        self.assertEqual([t["guid"] for t in item["tracks"]], ["mock-5", "mock-1"])

    def test_music_play_natural_random_favorites_phrase(self):
        from unittest import mock
        with mock.patch.object(app.random, "shuffle", side_effect=lambda xs: xs.reverse()):
            out = app.call_tool("music_play", {"playlist": "随机播放收藏的歌单"}, "http://h")
        self.assertIn("随机播放", out)
        self.assertIn("收藏", out)
        url = [ln for ln in out.splitlines() if "http" in ln][0].split("：")[-1]
        token = urllib.parse.urlparse(url).path.split("/playlist/")[1].rsplit(".mp3", 1)[0]
        self.assertEqual(len(app.PLAYLISTS.get(token)["tracks"]), 2)

    def test_music_play_playlist_shuffle_reorders_tracks(self):
        from unittest import mock
        with mock.patch.object(app.random, "shuffle", side_effect=lambda xs: xs.reverse()):
            out = app.call_tool("music_play", {"playlist": "睡前", "shuffle": True}, "http://h")
        self.assertIn("随机播放", out)
        url = [ln for ln in out.splitlines() if "http" in ln][0].split("：")[-1]
        token = urllib.parse.urlparse(url).path.split("/playlist/")[1].rsplit(".mp3", 1)[0]
        self.assertEqual([t["guid"] for t in app.PLAYLISTS.get(token)["tracks"]],
                         ["mock-6", "mock-5"])

    def _device_args(self, out):
        line = [ln for ln in out.splitlines() if ln.startswith("device_args：")][-1]
        return json.loads(line.split("：", 1)[1])

    def test_music_next_returns_next_track_and_clamps(self):
        first = app.call_tool("music_play", {"favorites": True}, "http://h")
        self.assertIn("当前第 1 首", first)
        args0 = self._device_args(first)
        nxt = app.call_tool("music_next", {}, "http://h")
        self.assertIn("准备播放下一首", nxt)
        self.assertIn("第 2/2 首", nxt)
        args1 = self._device_args(nxt)
        self.assertNotEqual(args0["url"], args1["url"])
        self.assertTrue(args1["title"])
        over = app.call_tool("music_next", {}, "http://h")
        self.assertIn("已经是最后一首", over)
        prev = app.call_tool("music_previous", {}, "http://h")
        self.assertIn("准备播放上一首", prev)
        self.assertIn("第 1/2 首", prev)

    def test_music_status_reports_current_track_and_lyric(self):
        app.call_tool("music_play", {"favorites": True}, "http://h")
        out = app.call_tool("music_status", {}, "http://h")
        self.assertIn("正在播放", out)
        self.assertIn("第 1/2 首", out)
        self.assertIn("时长：", out)

    def test_music_play_favorites_index_uses_absolute_index(self):
        out = app.call_tool("music_play", {"favorites": True, "index": 2,
                                           "_session_id": "t-index"}, "http://h")
        self.assertIn("共 2 首", out)
        self.assertIn("当前第 2 首", out)
        st = app.call_tool("music_status", {"_session_id": "t-index"}, "http://h")
        self.assertIn("第 2/2 首", st)

    def test_single_play_clears_only_its_playlist_cursor(self):
        app.call_tool("music_play", {"favorites": True, "_session_id": "s-a"}, "http://h")
        app.call_tool("music_play", {"query": "晴天", "_session_id": "s-a"}, "http://h")
        st = app.call_tool("music_status", {"_session_id": "s-a"}, "http://h")
        self.assertNotIn("来自：收藏", st)
        self.assertIn("上一次播放", st)
        app.call_tool("music_play", {"favorites": True, "_session_id": "s-b"}, "http://h")
        st_b = app.call_tool("music_status", {"_session_id": "s-b"}, "http://h")
        self.assertIn("来自：收藏", st_b)

    def test_playlist_cursor_isolated_per_session(self):
        app.call_tool("music_play", {"favorites": True, "_session_id": "c1"}, "http://h")
        app.call_tool("music_play", {"query": "晴天", "_session_id": "c2"}, "http://h")
        s1 = app.call_tool("music_status", {"_session_id": "c1"}, "http://h")
        s2 = app.call_tool("music_status", {"_session_id": "c2"}, "http://h")
        self.assertIn("来自：收藏", s1)
        self.assertNotIn("来自：收藏", s2)

    def test_playlist_limit_is_explicit_not_silently_truncated(self):
        class P:
            def favorites(self, limit=200):
                return [{"guid": "x%d" % i, "title": "t%d" % i, "artist": "a"}
                        for i in range(app.LIBRARY_PLAY_LIMIT + 1)]

        with self.assertRaises(app.BadRequest) as cm:
            app.build_list_playback(P(), "http://h", favorites=True)
        self.assertIn("列表过长", str(cm.exception))


    def test_music_play_library_uses_all_tracks(self):
        out = app.call_tool("music_play", {"library": True, "_session_id": "lib-play"}, "http://h")
        self.assertIn("音乐库", out)
        self.assertIn("共 6 首", out)
        st = app.call_tool("music_status", {"_session_id": "lib-play"}, "http://h")
        self.assertIn("第 1/6 首", st)

    def test_music_single_loop_uses_playlist_stream(self):
        out = app.call_tool("music_play", {"query": "晴天", "loop": "one",
                                           "_session_id": "loop-one"}, "http://h")
        self.assertIn("单曲循环", out)
        self.assertIn("共 1 首", out)
        url = [ln for ln in out.splitlines() if "http" in ln][0].split("：")[-1]
        token = urllib.parse.urlparse(url).path.split("/playlist/")[1].rsplit(".mp3", 1)[0]
        item = app.PLAYLISTS.get(token)
        self.assertTrue(item["loop"])
        self.assertEqual(len(item["tracks"]), 1)

    def test_music_loop_mode_all_then_off(self):
        app.call_tool("music_play", {"favorites": True, "_session_id": "loop-mode"}, "http://h")
        all_out = app.call_tool("music_loop", {"mode": "all", "_session_id": "loop-mode"}, "http://h")
        self.assertIn("循环播放", all_out)
        token = urllib.parse.urlparse(
            [ln for ln in all_out.splitlines() if "http" in ln][0].split("：")[-1]
        ).path.split("/playlist/")[1].rsplit(".mp3", 1)[0]
        self.assertTrue(app.PLAYLISTS.get(token)["loop"])
        off_out = app.call_tool("music_loop", {"mode": "off", "_session_id": "loop-mode"}, "http://h")
        self.assertIn("顺序播放", off_out)

    def test_music_stop_then_resume_playlist(self):
        app.call_tool("music_play", {"favorites": True, "_session_id": "stop-resume"}, "http://h")
        stopped = app.call_tool("music_stop", {"_session_id": "stop-resume"}, "http://h")
        self.assertIn("已停止", stopped)
        resumed = app.call_tool("music_resume", {"_session_id": "stop-resume"}, "http://h")
        self.assertIn("继续播放", resumed)
        self.assertIn("共 2 首", resumed)

    def test_music_resume_playlist_uses_saved_position(self):
        tracks = [dict(x) for x in app.MockProvider.TRACKS[:2]]
        app._resume_save("pl-pos", "playlist", tracks=tracks, label="收藏",
                         quality="standard", index=0, position=30, token="")
        out = app.call_tool("music_resume", {"_session_id": "pl-pos"}, "http://h")
        self.assertIn("继续播放", out)
        self.assertIn("start=30", out)

    def test_music_resume_single_uses_signed_start(self):
        track = dict(app.MockProvider.TRACKS[0])
        app._resume_save("single-pos", "single", track=track,
                         quality="standard", position=12)
        out = app.call_tool("music_resume", {"_session_id": "single-pos"}, "http://h")
        self.assertIn("从约 12 秒继续", out)
        self.assertIn("start=12", out)

    def test_music_resume_single_position_override(self):
        track = dict(app.MockProvider.TRACKS[0])
        app._resume_save("single-override", "single", track=track,
                         quality="standard", position=12)
        out = app.call_tool("music_resume", {"_session_id": "single-override",
                                             "position": 99}, "http://h")
        self.assertIn("从约 99 秒继续", out)
        self.assertIn("start=99", out)

    def test_music_favorite_current_and_by_guid(self):
        app.call_tool("music_play", {"favorites": True, "_session_id": "fav-x"}, "http://h")
        added = app.call_tool("music_favorite", {"action": "add", "current": True,
                                                 "_session_id": "fav-x"}, "http://h")
        self.assertIn("已收藏", added)
        removed = app.call_tool("music_favorite", {"action": "remove", "guid": "mock-1"}, "http://h")
        self.assertIn("已取消收藏：晴天", removed)

    def test_make_url_start_is_signed(self):
        url = app.make_url("http://h", "g", 60, quality="standard", start=15)
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(qs["start"], ["15"])
        self.assertTrue(app.verify("g", qs["exp"][0], qs["sig"][0], "standard", start=15))
        self.assertFalse(app.verify("g", qs["exp"][0], qs["sig"][0], "standard"))


class TestOptim2135(unittest.TestCase):
    """2.13.5：provider 生命周期、缓存保护、搜索兜底、列表优先级。"""

    def test_online_status_closes_temporary_provider(self):
        from unittest import mock

        class P:
            def __init__(self):
                self.closed = False

            def status(self):
                return [{"name": "x", "ok": True}]

            def close(self):
                self.closed = True

        p = P()
        with mock.patch.object(app, "online_active", return_value=True), \
                mock.patch.object(app, "new_native_provider", return_value=p):
            self.assertEqual(app.online_status(), [{"name": "x", "ok": True}])
        self.assertTrue(p.closed)

    def test_reset_provider_retires_old_instance(self):
        class P:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        old = P()
        saved_list, saved_prov = app._RETIRED_PROVIDERS[:], app._PROVIDER
        try:
            app._RETIRED_PROVIDERS[:] = []
            app._PROVIDER = old
            app.reset_provider()
            self.assertFalse(old.closed, "活跃播放可能仍持有旧 provider，不能立刻关闭")
            self.assertIn(old, app._RETIRED_PROVIDERS)
        finally:
            app._RETIRED_PROVIDERS[:] = saved_list
            app._PROVIDER = saved_prov

    def test_retired_queue_closes_only_overflow(self):
        class P:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        olds = [P() for _ in range(app._RETIRED_MAX)]
        new = P()
        saved_list, saved_prov = app._RETIRED_PROVIDERS[:], app._PROVIDER
        try:
            app._RETIRED_PROVIDERS[:] = olds
            app._PROVIDER = new
            app.reset_provider()
            self.assertTrue(olds[0].closed)
            self.assertFalse(olds[1].closed)
            self.assertFalse(new.closed)
            self.assertEqual(len(app._RETIRED_PROVIDERS), app._RETIRED_MAX)
        finally:
            app._RETIRED_PROVIDERS[:] = saved_list
            app._PROVIDER = saved_prov

    def test_cache_rejects_single_file_larger_than_limit(self):
        class P:
            def stream_to_path(self, guid, path, quality=None):
                with open(path, "wb") as fh:
                    fh.write(b"ID3" + b"\x00" * 4096)
                return "audio/mpeg", 4099

        d = tempfile.mkdtemp(prefix="gw-small-", dir=DATA)
        cache = gw_storage.MediaCache(d, 10)
        with self.assertRaises(gw_storage.StorageError):
            cache.ensure("ext:musicdl:big", P(), quality="standard")
        self.assertEqual([x for x in os.listdir(d) if x.endswith(".mp3")], [])

    def test_evict_never_removes_protected_file(self):
        d = tempfile.mkdtemp(prefix="gw-protect-", dir=DATA)
        cache = gw_storage.MediaCache(d, 120)
        keep = os.path.join(d, "keep.mp3")
        drop = os.path.join(d, "drop.mp3")
        for fp in (keep, drop):
            with open(fp, "wb") as fh:
                fh.write(b"x" * 100)
        cache.evict(protect=keep)
        self.assertTrue(os.path.exists(keep))
        self.assertFalse(os.path.exists(drop))

    def test_library_checks_capacity_before_download(self):
        class P:
            def stream_to_path(self, guid, path, quality=None):
                raise AssertionError("容量不足时不应开始下载")

        d = tempfile.mkdtemp(prefix="gw-libcap-", dir=DATA)
        with open(os.path.join(d, "old.mp3"), "wb") as fh:
            fh.write(b"x" * 20)
        lib = gw_storage.LibraryStore(lambda: d, max_bytes=10)
        with self.assertRaises(gw_storage.StorageError):
            lib.save_from_provider(P(), {"guid": "ext:x:1", "title": "t", "artist": "a"})
        self.assertEqual([x for x in os.listdir(d) if x.endswith(".part")], [])

    def test_fast_search_falls_back_to_original_query(self):
        from unittest import mock

        class P:
            def search_stream(self, q, limit=10, deadline_s=None):
                return iter(())

            def search(self, q, limit=10):
                if q == "原词":
                    return [{"guid": "orig-1", "title": "晴天", "artist": "周杰伦"}]
                return []

        with mock.patch.object(app, "apply_aliases", return_value=("晴天", "原词")):
            rows, fixed = app.fast_search_tracks(P(), "原词", limit=5, budget=2)
        self.assertEqual(fixed, "晴天")
        self.assertEqual([r["guid"] for r in rows], ["orig-1"])

    def test_playlist_name_wins_over_favorites_phrase(self):
        class P(app.MockProvider):
            def playlists(self):
                return [{"guid": "pl-fav", "name": "收藏", "count": 1}]

            def playlist_tracks(self, guid, limit=200):
                return [self.TRACKS[2]] if guid == "pl-fav" else []

        info, label, _start, _shuffle, _action, rows = app.build_list_playback(
            P(), "http://h", playlist="收藏")
        self.assertEqual(label, "歌单「收藏」")
        self.assertEqual([r["guid"] for r in rows], ["mock-3"])

    def test_normal_query_does_not_list_artists(self):
        class P(app.MockProvider):
            def artist_items(self, *a, **k):
                raise AssertionError("普通搜歌不应拉歌手全表")

        out = app._tool_music_play({"query": "晴天"}, P(), "http://h", 10, "standard")
        self.assertIn("准备播放", out)


class TestPlaybackOptimizations(unittest.TestCase):
    """播放优化：本地优先搜索、整库分页/容量、播放阶段日志基建。"""

    def test_pick_tracks_prefers_local_results_without_waiting_online(self):
        class SlowOnline:
            def __init__(self):
                self.calls = []

            def search_stream(self, q, limit=10, deadline_s=None):
                self.calls.append((q, deadline_s))
                yield {"guid": "local-1", "title": "晴天", "artist": "本地", "source": "local"}
                end = time.monotonic() + max(0.0, float(deadline_s or 0))
                while time.monotonic() < end:
                    time.sleep(0.01)

            def search(self, q, limit=10):
                raise AssertionError("播放应优先走 search_stream 快速路径")

        p = SlowOnline()
        t0 = time.monotonic()
        rows, label = app.pick_tracks(p, "晴天", limit=20, budget=0.4)
        elapsed = time.monotonic() - t0
        self.assertEqual([r["guid"] for r in rows], ["local-1"])
        self.assertLess(elapsed, 1.5)
        self.assertIn("晴天", label)
        self.assertEqual(p.calls[0][0], "晴天")

    def test_fn_provider_list_tracks_pages_and_caches(self):
        class FakeClient:
            def __init__(self, total):
                self.total = total
                self.calls = []

            def track_list(self, page, page_size):
                self.calls.append((page, page_size))
                start = (page - 1) * page_size
                return {"list": [{"guid": "t%d" % i, "title": "t%d" % i, "artist": "a"}
                                 for i in range(start, min(start + page_size, self.total))]}

        p = object.__new__(app.FnProvider)
        p.client = FakeClient(1342)
        p._tracks_cache = (0.0, [])
        p._tracks_cache_ttl = 60.0
        p._tracks_lock = threading.Lock()
        rows = p.list_tracks(1200)
        self.assertEqual(len(rows), 1200)
        self.assertEqual([c[1] for c in p.client.calls], [200] * 6)
        calls = len(p.client.calls)
        self.assertEqual(len(p.list_tracks(20)), 20)
        self.assertEqual(len(p.client.calls), calls)

    def test_library_playback_uses_more_than_500_tracks(self):
        class P:
            def list_tracks(self, limit=20):
                return [{"guid": "lib-%d" % i, "title": "歌曲%d" % i, "artist": "a",
                         "duration_s": 10} for i in range(limit)]

        out = app._tool_music_play({"library": True}, P(), "http://h", 10, "standard")
        self.assertIn("共 %d 首" % app.LIBRARY_PLAY_LIMIT, out)


class TestLibraryStoreDownloadOnly(unittest.TestCase):
    """仅飞牛库模式：LibraryStore 只负责落盘，不再维护本地搜索索引。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gw-lib-", dir=DATA)
        self.store = gw_storage.LibraryStore(lambda: self.dir, app.LOG, max_bytes=0)

    def test_save_writes_file_without_local_index(self):
        track = {"guid": "ext:musicdl:abc", "title": "晴天", "artist": "周杰伦"}
        fp, size = self.store.save_bytes(track, "audio/wav", b"RIFF" + b"\x00" * 64)
        self.assertTrue(os.path.isfile(fp))
        self.assertGreater(size, 0)
        self.assertFalse(os.path.exists(os.path.join(self.dir, ".library-index.json")))
        self.assertFalse(hasattr(self.store, "search"))
        self.assertFalse(hasattr(self.store, "list_all"))
        self.assertFalse(hasattr(self.store, "iter_stream"))



class TestLibraryScanAfterDownload(unittest.TestCase):
    """可选：下载完成后触发飞牛扫描（命令 / HTTP 回调），留空时完全关闭。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gw-lib-scan-", dir=DATA)

    def test_scan_disabled_by_default(self):
        store = gw_storage.LibraryStore(lambda: self.dir, app.LOG)
        self.assertFalse(store.scan_configured())
        fp, _ = store.save_bytes({"guid": "ext:x:1", "title": "t", "artist": "a"},
                                 "audio/mpeg", b"ID3" + b"\x00" * 16)
        self.assertTrue(os.path.isfile(fp))
        self.assertIsNone(store._scan_job)

    def test_download_triggers_scan_command_in_background(self):
        from unittest import mock
        done = threading.Event()

        def fake_run(*args, **kwargs):
            done.set()

        store = gw_storage.LibraryStore(lambda: self.dir, app.LOG,
                                        scan_cmd_getter=lambda: "echo scan")
        with mock.patch.object(gw_storage.subprocess, "run", side_effect=fake_run) as run:
            store.save_bytes({"guid": "ext:x:2", "title": "t", "artist": "a"},
                             "audio/mpeg", b"ID3" + b"\x00" * 16)
            job = store._scan_job
            self.assertIsNotNone(job)
            job.join(3.0)
        self.assertTrue(done.is_set())
        run.assert_called_once()

    def test_scan_url_posts_when_configured(self):
        from unittest import mock
        store = gw_storage.LibraryStore(lambda: self.dir, app.LOG,
                                        scan_url_getter=lambda: "http://fnos.local/scan")
        with mock.patch.object(gw_storage.urllib.request, "urlopen") as urlopen:
            self.assertTrue(store.run_scan())
        urlopen.assert_called_once()
        req = urlopen.call_args[0][0]
        self.assertEqual(req.get_method(), "POST")


class TestBuiltInLibraryScan(unittest.TestCase):
    """下载后默认调用已配置的飞牛音乐接口扫库，不需要额外扫描地址。"""

    def test_client_scans_first_shared_library_guid(self):
        client = fnmusic_client.FnMusicClient(base="http://fnos.local:5666")
        client.get = lambda path, **kwargs: {"list": [{"guid": "lib-1"}, {"guid": "lib-2"}]}
        calls = []
        client.post = lambda path, body=None, **kwargs: calls.append((path, body))
        self.assertTrue(client.scan_library())
        self.assertEqual(calls, [("/shared-library/scan", {"guid": "lib-1"})])

    def test_client_falls_back_to_scan_all_without_guid(self):
        client = fnmusic_client.FnMusicClient(base="http://fnos.local:5666")
        client.get = lambda path, **kwargs: {"list": []}
        calls = []
        client.post = lambda path, body=None, **kwargs: calls.append((path, body))
        self.assertTrue(client.scan_library())
        self.assertEqual(calls, [("/shared-library/scan-all", None)])

    def test_library_store_runs_provider_callback_first(self):
        called = []
        store = gw_storage.LibraryStore(
            lambda: self.dir, app.LOG,
            scan_callback_getter=lambda: lambda: called.append(1) or True)
        self.assertTrue(store.scan_configured())
        self.assertTrue(store.run_scan())
        self.assertEqual(called, [1])

    def test_app_callback_uses_configured_provider(self):
        from unittest import mock
        called = []

        class FakeProvider:
            def scan_library(self):
                called.append(1)
                return True

        with mock.patch.object(app, "provider", return_value=FakeProvider()):
            self.assertTrue(app._scan_library_via_provider())
        self.assertEqual(called, [1])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gw-builtin-scan-", dir=DATA)


class TestAutoDownload(unittest.TestCase):
    """同一首在线歌曲第二次开播后，后台自动下载并写入飞牛曲库目录。"""

    def test_second_play_triggers_background_download(self):
        from unittest import mock
        import gwlib.playcount as gw_playcount
        stats = gw_playcount.PlayStats(os.path.join(tempfile.mkdtemp(dir=DATA), "p.json"))
        stats.dedupe_seconds = 0.0
        saved = []
        qualities = []
        done = threading.Event()

        class FakeLibrary:
            def enabled(self):
                return True

            def save_from_provider(self, provider, track, quality=None):
                saved.append(dict(track))
                qualities.append(quality)
                done.set()
                return "/tmp/x.mp3", 1

        track = {"guid": "ext:musicdl:abc", "title": "晴天", "artist": "周杰伦"}
        with mock.patch.object(app, "PLAY_STATS", stats), \
                mock.patch.object(app, "LIBRARY", FakeLibrary()), \
                mock.patch.object(app, "DOWNLOAD_QUALITY", "exhigh"):
            app._AUTO_DOWNLOADING.clear()
            app._note_online_play(track["guid"], track, "standard", object())
            self.assertFalse(saved)
            app._note_online_play(track["guid"], track, "standard", object())
            self.assertTrue(done.wait(3.0))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["title"], "晴天")
        # 在线试听传的是 standard，自动补齐必须用下载音质 exhigh。
        self.assertEqual(qualities, ["exhigh"])


class TestDownloadQuality(unittest.TestCase):
    """下载音质与在线试听音质分离：未显式指定时统一走下载音质。"""

    class _Provider:
        def search(self, q, limit=10):
            return [{"guid": "ext:musicdl:dq", "title": "晴天", "artist": "周杰伦",
                     "source": "musicdl"}]

    class _Library:
        def __init__(self):
            self.qualities = []

        def enabled(self):
            return True

        def save_from_provider(self, provider, track, quality=None):
            self.qualities.append(quality)
            return "/tmp/x.mp3", 1

    def test_download_quality_labels(self):
        from unittest import mock
        with mock.patch.object(app, "DOWNLOAD_QUALITY", "lossless"):
            self.assertEqual(app.download_quality_info(),
                             ("lossless", "无损（保留原始文件）"))
            self.assertEqual(app.download_quality_info("standard"),
                             ("standard", "标准（128k MP3）"))

    def test_music_download_defaults_to_download_quality(self):
        from unittest import mock
        lib = self._Library()
        with mock.patch.object(app, "LIBRARY", lib), \
                mock.patch.object(app, "DOWNLOAD_QUALITY", "lossless"):
            out = app._tool_music_download({"query": "晴天"}, self._Provider(), "", 10, None)
        self.assertEqual(lib.qualities, ["lossless"])
        self.assertIn("已保存到本地曲库", out)

    def test_music_download_explicit_quality_wins(self):
        from unittest import mock
        lib = self._Library()
        with mock.patch.object(app, "LIBRARY", lib), \
                mock.patch.object(app, "DOWNLOAD_QUALITY", "lossless"):
            app._tool_music_download({"query": "晴天"}, self._Provider(), "", 10, "high")
        self.assertEqual(lib.qualities, ["high"])

    def test_favorite_add_uses_download_quality(self):
        from unittest import mock
        seen = []

        class Provider(self._Provider):
            def download_ext(self, track, quality=None):
                seen.append(quality)
                return "/tmp/x.mp3", 1

        with mock.patch.object(app, "DOWNLOAD_QUALITY", "exhigh"):
            out = app._tool_music_favorite(
                {"action": "add", "query": "晴天"}, Provider(), "", 10, None)
        self.assertEqual(seen, ["exhigh"])
        self.assertIn("已收藏并下载", out)


class TestRelease201(unittest.TestCase):
    """2.0.1：流式落盘、弱引用锁、单版本常量、搜索总上限/总超时。"""

    def test_single_version_constant(self):
        import gwlib
        self.assertEqual(app.VERSION, "2.13.17")
        self.assertEqual(gw_sources.VERSION, app.VERSION)
        self.assertEqual(gwlib.__version__, app.VERSION)

    def test_media_cache_locks_are_weak(self):
        d = tempfile.mkdtemp(prefix="gw-locks-", dir=DATA)
        cache = gw_storage.MediaCache(d, 1024)
        lock = cache._lock("k")
        self.assertIs(cache._lock("k"), lock)
        self.assertIsInstance(cache._locks, weakref.WeakValueDictionary)
        ref = weakref.ref(lock)
        del lock
        gc.collect()
        self.assertIsNone(ref())

    def test_fallback_total_limit_caps_merged_rows(self):
        rows = [{"guid": "ext:musicdl:%d" % i, "title": "t%d" % i,
                 "artist": "a", "source": "musicdl"} for i in range(20)]
        fp = app.FallbackProvider(_FakeLocal(), ["musicdl@http://x:1"],
                                  total_limit=3, total_timeout=2)
        fp.exts = [_FakeExt(rows)]
        out = fp.search("t", 10)
        self.assertEqual(len(out), 3)

    def test_fallback_total_timeout_stops_waiting(self):
        class SlowExt:
            svc = "musicdl"

            def search(self, q, limit=10):
                app.time.sleep(0.4)
                return [{"guid": "ext:musicdl:slow", "title": q, "artist": "a"}]

        fp = app.FallbackProvider(_FakeLocal(), ["musicdl@http://x:1"],
                                  total_limit=5, total_timeout=1)
        fp.exts = [SlowExt()]
        fp.total_timeout = 0.05
        started = app.time.monotonic()
        out = fp.search("x", 5)
        elapsed = app.time.monotonic() - started
        self.assertLess(elapsed, 0.35)
        self.assertEqual(out, [])

    def test_library_save_from_provider_streams_to_file(self):
        class StreamProv:
            def stream_to_path(self, guid, path, quality=None):
                with open(path, "wb") as fh:
                    fh.write(b"ID3" * 100)
                return "audio/mpeg", 300

        d = tempfile.mkdtemp(prefix="gw-lib-stream-", dir=DATA)
        lib = gw_storage.LibraryStore(lambda: d)
        fp, size = lib.save_from_provider(StreamProv(), {"guid": "ext:x:1", "title": "t", "artist": "a"})
        self.assertEqual(size, 300)
        self.assertTrue(fp.endswith(".mp3"))
        self.assertEqual(os.path.getsize(fp), 300)

class TestPrefetchNext(Base):
    """连续播放/切歌时后台预取下一首，降低二次起播等待。"""

    def _tracks(self):
        return [dict(x) for x in app.MockProvider.TRACKS[:3]]

    def test_prefetch_helper_skips_last_without_loop_and_wraps_with_loop(self):
        from unittest import mock
        tracks = self._tracks()
        with mock.patch.object(app.CACHE, "prefetch") as pf:
            app._prefetch_playlist_next(tracks, 0, app.MockProvider(), "standard", False)
            pf.assert_called_once_with("mock-2", mock.ANY, quality="standard")
            pf.reset_mock()
            app._prefetch_playlist_next(tracks, 2, app.MockProvider(), "standard", False)
            pf.assert_not_called()
            app._prefetch_playlist_next(tracks, 2, app.MockProvider(), "standard", True)
            pf.assert_called_once_with("mock-1", mock.ANY, quality="standard")

    def test_music_status_prefetches_next_playlist_track(self):
        from unittest import mock
        app.call_tool("music_play", {"favorites": True, "_session_id": "pf-status"}, "http://h")
        with mock.patch.object(app.CACHE, "prefetch") as pf:
            app.call_tool("music_status", {"_session_id": "pf-status"}, "http://h")
        pf.assert_any_call("mock-5", mock.ANY, quality="standard")

    def test_music_next_prefetches_following_track(self):
        from unittest import mock
        app.call_tool("music_play", {"library": True, "_session_id": "pf-next"}, "http://h")
        with mock.patch.object(app.CACHE, "prefetch") as pf:
            app.call_tool("music_next", {"_session_id": "pf-next"}, "http://h")
        pf.assert_any_call("mock-3", mock.ANY, quality="standard")

    def test_playlist_stream_prefetches_second_track(self):
        from unittest import mock
        tracks = [dict(x) for x in app.MockProvider.TRACKS[:2]]
        token = app.PLAYLISTS.put(tracks, quality="standard", label="prefetch")
        path = os.path.join(tempfile.mkdtemp(prefix="gw-prefetch-", dir=DATA), "a.mp3")
        with open(path, "wb") as fh:
            fh.write(b"ID3" + b"\x00" * 4096)
        with mock.patch.object(app.CACHE, "ensure", return_value=path), \
                mock.patch.object(app.CACHE, "prefetch") as pf:
            url = app.make_playlist_url("", token, ttl=app.URL_TTL, quality="standard")
            r, data = self.req("GET", url, raw=True)
        self.assertEqual(r.status, 200)
        pf.assert_any_call("mock-2", mock.ANY, quality="standard")


class TestResumePollingOptimization(unittest.TestCase):
    """2.13.11：status 轮询不深拷贝整表、续播记录只存必要字段、30 秒去抖。"""

    def test_resume_tracks_keep_only_required_fields(self):
        track = {"guid": "g1", "title": "t", "artist": "a", "duration_s": 12,
                 "album": "al", "source": "ext:musicdl:x", "extra": "x"}
        app._resume_save("compact-resume", "playlist", tracks=[track], label="L",
                         quality="standard", index=0)
        st = app._resume_get("compact-resume")
        self.assertEqual(set(st["tracks"][0]),
                         {"guid", "title", "artist", "duration_s"})
        self.assertEqual(st["tracks"][0]["title"], "t")

    def test_playlist_get_can_return_tracks_without_copy(self):
        tracks = [dict(x) for x in app.MockProvider.TRACKS[:2]]
        token = app.PLAYLISTS.put(tracks, quality="standard", label="nocopy")
        first = app.PLAYLISTS.get(token, copy_tracks=False)
        second = app.PLAYLISTS.get(token, copy_tracks=False)
        self.assertIs(first["tracks"], second["tracks"])

    def test_music_status_resume_save_is_throttled(self):
        from unittest import mock
        session = "status-throttle"
        app.call_tool("music_play", {"favorites": True, "_session_id": session}, "http://h")
        with app._STATUS_RESUME_LOCK:
            app._STATUS_RESUME_LAST.clear()
        with mock.patch.object(app, "_resume_save") as save:
            app.call_tool("music_status", {"_session_id": session}, "http://h")
            app.call_tool("music_status", {"_session_id": session}, "http://h")
        self.assertEqual(save.call_count, 1)


class TestPrefetchScheduler(unittest.TestCase):
    """2.13.11：预取固定 worker + 去重 + 失败退避，转码槽给前台留余量。"""

    def test_transcode_slots_try_enter_reserves_foreground_slot(self):
        slots = gw_storage.TranscodeSlots(2)
        self.assertTrue(slots.try_enter(reserve=1))
        self.assertFalse(slots.try_enter(reserve=1))
        slots.release()
        self.assertTrue(slots.try_enter(reserve=1))
        slots.release()
        # 只有一个转码槽时必须完全让给前台，预取不得占用。
        one = gw_storage.TranscodeSlots(1)
        self.assertFalse(one.try_enter(reserve=1))

    def test_prefetch_coalesces_duplicates_and_uses_bounded_workers(self):
        d = tempfile.mkdtemp(prefix="gw-prefetchq-", dir=DATA)
        calls = []

        class P:
            def stream_to_path(self, guid, path, quality=None):
                calls.append(guid)
                with open(path, "wb") as fh:
                    fh.write(b"ID3" + b"\x00" * 2048)
                return "audio/mpeg", 2051

        cache = gw_storage.MediaCache(d, 10 * 1024 * 1024)
        cache.prefetch("a", P())
        cache.prefetch("a", P())
        cache.prefetch("b", P())
        deadline = time.time() + 3
        while time.time() < deadline:
            files = [n for n in os.listdir(d) if n.endswith(".mp3")]
            if len(files) >= 2:
                break
            time.sleep(0.02)
        self.assertEqual(len([n for n in os.listdir(d) if n.endswith(".mp3")]), 2)
        self.assertEqual(sorted(calls), ["a", "b"])
        self.assertEqual(len(cache._prefetch_workers), gw_storage.PREFETCH_WORKERS)

    def test_prefetch_failure_is_backed_off(self):
        d = tempfile.mkdtemp(prefix="gw-prefetchfail-", dir=DATA)

        class Fail:
            calls = 0

            def stream_to_path(self, guid, path, quality=None):
                Fail.calls += 1
                raise RuntimeError("boom")

        cache = gw_storage.MediaCache(d, 10 * 1024 * 1024)
        cache.prefetch("bad", Fail())
        deadline = time.time() + 3
        while time.time() < deadline:
            with cache._prefetch_guard:
                done = "bad" in cache._prefetch_failed and not cache._prefetching
            if done:
                break
            time.sleep(0.02)
        self.assertEqual(Fail.calls, 1)
        cache.prefetch("bad", Fail())
        time.sleep(0.08)
        self.assertEqual(Fail.calls, 1)  # 失败 TTL 内不再重复预取

    def test_prefetch_transcode_skips_when_slot_is_busy(self):
        d = tempfile.mkdtemp(prefix="gw-prefetchbusy-", dir=DATA)

        class P:
            def stream_to_path(self, guid, path, quality=None):
                with open(path, "wb") as fh:
                    fh.write(b"fLaC" + b"\x00" * 2048)
                return "audio/flac", 2052

        slots = gw_storage.TranscodeSlots(1)
        self.assertTrue(slots.try_enter())
        cache = gw_storage.MediaCache(d, 10 * 1024 * 1024, transcode_slots=slots)
        with self.assertRaises(gw_storage.PrefetchBusy):
            cache.ensure("x", P(), quality="high", prefetch=True)
        slots.release()


class TestStreamingPlayback(Base):
    """2.13.11：未缓存曲目边收边播，首块不等待全量下载。"""

    @staticmethod
    def _chunks():
        return [b"ID3" + b"a" * 4096, b"b" * 4096, b"c" * 4096]

    def test_media_cache_stream_yields_first_chunk_before_full_download(self):
        d = tempfile.mkdtemp(prefix="gw-stream-", dir=DATA)
        chunks = self._chunks()

        class SlowProvider:
            def iter_stream(self, guid, quality=None):
                def gen():
                    for i, chunk in enumerate(chunks):
                        if i:
                            time.sleep(0.05)
                        yield chunk
                return "audio/mpeg", gen()

        cache = gw_storage.MediaCache(d, 10 * 1024 * 1024)
        gen = cache.stream("stream-key", SlowProvider())
        started = time.monotonic()
        first = next(gen)
        first_ms = (time.monotonic() - started) * 1000.0
        rest = list(gen)
        total_ms = (time.monotonic() - started) * 1000.0
        self.assertEqual(first, chunks[0])
        self.assertEqual(first + b"".join(rest), b"".join(chunks))
        self.assertLess(first_ms, 100.0)
        self.assertGreater(total_ms, first_ms + 40.0)
        cached = cache.path("stream-key")
        self.assertTrue(os.path.exists(cached))
        self.assertEqual(os.path.getsize(cached), sum(len(c) for c in chunks))

    def test_media_cache_stream_uses_cached_file_without_provider(self):
        d = tempfile.mkdtemp(prefix="gw-stream-hit-", dir=DATA)
        cache = gw_storage.MediaCache(d, 10 * 1024 * 1024)
        dst = cache.path("hit-key")
        with open(dst, "wb") as fh:
            fh.write(b"ID3" + b"z" * 2048)

        class NeverCalled:
            def iter_stream(self, guid, quality=None):
                raise AssertionError("provider should not be called for cached file")

        self.assertEqual(b"".join(cache.stream("hit-key", NeverCalled())),
                         b"ID3" + b"z" * 2048)

    def test_media_cache_stream_falls_back_when_provider_has_no_iter_stream(self):
        d = tempfile.mkdtemp(prefix="gw-stream-fallback-", dir=DATA)

        class LegacyProvider:
            def stream(self, guid, quality=None):
                return "audio/mpeg", b"ID3" + b"legacy" * 500

        cache = gw_storage.MediaCache(d, 10 * 1024 * 1024)
        self.assertEqual(b"".join(cache.stream("legacy-key", LegacyProvider())),
                         b"ID3" + b"legacy" * 500)

    def test_media_cache_stream_cleans_tmp_on_provider_failure(self):
        d = tempfile.mkdtemp(prefix="gw-stream-fail-", dir=DATA)

        class Broken:
            def iter_stream(self, guid, quality=None):
                def gen():
                    raise RuntimeError("upstream boom")
                    yield b""
                return "audio/mpeg", gen()

        cache = gw_storage.MediaCache(d, 10 * 1024 * 1024)
        with self.assertRaises(gw_storage.StorageError):
            next(cache.stream("broken-key", Broken()))
        self.assertFalse(os.path.exists(cache.path("broken-key")))
        self.assertFalse([n for n in os.listdir(d) if ".stream-" in n])

    def test_play_streams_without_content_length(self):
        from unittest import mock
        guid = "ext:test:stream-http"

        class Fake:
            chunks = [b"ID3" + b"h" * 1024, b"t" * 1024]

            def iter_stream(self, guid, quality=None):
                return "audio/mpeg", iter(self.chunks)

        with mock.patch.object(app, "provider", return_value=Fake()):
            url = app.make_url("", guid, ttl=app.URL_TTL, quality="standard")
            r, data = self.req("GET", url, raw=True)
        self.assertEqual(r.status, 200)
        self.assertIsNone(r.getheader("Content-Length"))
        self.assertEqual(data, b"".join(Fake.chunks))

    def test_playlist_streams_first_track_without_content_length(self):
        from unittest import mock
        tracks = [
            {"guid": "ext:test:pl-1", "title": "A", "artist": "a", "duration_s": 1},
            {"guid": "ext:test:pl-2", "title": "B", "artist": "b", "duration_s": 1},
        ]
        token = app.PLAYLISTS.put(tracks, quality="standard")
        url = app.make_playlist_url("", token, ttl=app.URL_TTL, quality="standard")

        class Fake:
            def iter_stream(self, guid, quality=None):
                return "audio/mpeg", iter([b"\xff\xfb" + b"p" * 2048])

        with mock.patch.object(app, "provider", return_value=Fake()):
            r, data = self.req("GET", url, raw=True)
        self.assertEqual(r.status, 200)
        self.assertIsNone(r.getheader("Content-Length"))
        self.assertTrue(data.startswith(b"\xff\xfb"))


if __name__ == "__main__":
    unittest.main()
