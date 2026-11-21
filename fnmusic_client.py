"""飞牛音乐（fnOS Music）API 客户端 —— 仅标准库。

协议（全部实测自 NAS 192.168.50.100:59820，2026-11-15）：
  - 基础路径 {base}/music/api/v1{path}
  - authx 头: nonce=6位&timestamp=ms&sign=md5([SECRET,pathname,nonce,ts,bodyHash,KEY].join('_'))
      bodyHash: GET = md5(按 key 排序后 "k=v&k=v" 原串)  POST = md5(json.dumps(body,separators=(',',':')))
      注意: GET 签名用未 URL 编码的原值；实际发出的 query 必须 URL 编码（中文参数）
  - 登录 POST /user/password-login {username, password: sha256(pwd), deviceId}
      **username 必须是 "KID"（全大写），kid1412 会返回 code=120001**
      -> data.userToken
  - 之后带 Cookie: music-token={userToken}（urllib.parse.quote 后）
  - 信封 {code,msg,data}，code in (0,200) 为成功；无效 token 为 401 code=99999
实测端点（参数名严格）：
  GET  /track/list?page&pageSize                       全库 1342 首，忽略任何过滤参数
  GET  /album/list|/artist/list|/playlist/list?page&pageSize
  GET  /album/detail?guid  /artist/detail?guid  /playlist/detail?guid
  GET  /track/album-detail/list?albumGUID&page&size[&sort=trackNo,asc]
  GET  /track/artist-detail/list?artistGUID&page&size
  GET  /track/playlist-detail/list?playlistGUID&page&size
  GET  /search/track?q&page&pageSize        （参数名必须是 q）
  GET  /search/album?q  /search/artist?q
  GET  /favorite-track/list?page&pageSize
  POST /favorite-track/create {trackGuid}   POST /favorite-track/delete {trackGuid}
  GET  /track/stream?guid                   -> 音频字节（audio/flac 或 mp3），不是 JSON
"""
import hashlib
import json
import os
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# 飞牛音乐前端协议使用的固定常量（不是用户密码），允许部署方覆盖。
API_SECRET = os.environ.get("FNMUSIC_API_SECRET", "NDzZTVxnRKP8Z0jXg1VAMonaG8akvh")
API_KEY = os.environ.get("FNMUSIC_API_KEY", "6D5602D4-A342-4799-A0F0-BB795E7167D0")
SUCCESS_CODES = (0, 200)
API_PREFIX = "/music/api/v1"
DEFAULT_BASE = os.environ.get("FNMUSIC_DEFAULT_BASE", "http://192.168.50.100:59820")
DEFAULT_USERNAME = os.environ.get("FNMUSIC_DEFAULT_USERNAME", "KID")
DEFAULT_DEVICE_ID = os.environ.get("FNMUSIC_DEFAULT_DEVICE_ID", "7bcec31d00cb7d9ba2e1ad5279ff5812")


class FnMusicError(Exception):
    def __init__(self, msg, status=None, code=None):
        super().__init__(msg)
        self.status = status
        self.code = code


def normalize_base(base):
    """统一飞牛音乐地址：去尾部斜杠；用户误把界面路径 /music 或 /music/api/v1 贴进来时纠正掉。

    客户端自己会拼 API_PREFIX，所以 base 只应保留协议+主机+端口（可带反向代理前缀）。
    """
    value = str(base or "").strip().rstrip("/")
    for suffix in ("/music/api/v1", "/music"):
        if value.lower().endswith(suffix):
            value = value[: -len(suffix)].rstrip("/")
            break
    return value


def _md5(s):
    return hashlib.md5(str(s).encode("utf-8")).hexdigest()


def _sha256(s):
    return hashlib.sha256(str(s).encode("utf-8")).hexdigest()


def _sorted_qs(params):
    if not params:
        return ""
    return "&".join("%s=%s" % (k, params[k]) for k in sorted(params) if params[k] is not None)


def build_authx(pathname, method, params=None, body=None):
    if str(method).upper() == "GET":
        src = _sorted_qs(params)
    else:
        src = "" if body is None else json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    body_hash = _md5(src)
    nonce = str(100000 + secrets.randbelow(900000))
    ts = str(int(time.time() * 1000))
    sign = _md5("_".join([API_SECRET, pathname, nonce, ts, body_hash, API_KEY]))
    return "nonce=%s&timestamp=%s&sign=%s" % (nonce, ts, sign)


def encode_query(params):
    if not params:
        return ""
    return urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})


class FnMusicClient:
    def __init__(self, base=DEFAULT_BASE, username=DEFAULT_USERNAME, password=None, device_id=None,
                 token=None, token_file=None, timeout=12.0, insecure_tls=None):
        self.base = normalize_base(base or DEFAULT_BASE)
        self.username = username or DEFAULT_USERNAME
        self.password = password
        self.device_id = device_id or DEFAULT_DEVICE_ID
        self.token = token
        self.token_file = token_file
        self.timeout = timeout
        self._lock = threading.Lock()
        self._ssl = ssl.create_default_context()
        # 默认校验证书；内网自签 HTTPS 才显式关闭。insecure_tls 显式传参优先于环境变量，
        # 避免「测试连接」临时改 os.environ 影响并发请求。
        if insecure_tls is None:
            insecure_tls = os.environ.get("FNMUSIC_INSECURE_TLS", "").strip().lower() in (
                "1", "true", "yes", "on")
        if insecure_tls:
            self._ssl.check_hostname = False
            self._ssl.verify_mode = ssl.CERT_NONE
        if not self.token and token_file:
            self._load_token()

    # ---------- token 持久化 ----------
    def _load_token(self):
        try:
            with open(self.token_file, "r", encoding="utf-8") as f:
                d = json.load(f)
            if normalize_base(d.get("base", "")) == self.base and d.get("token"):
                self.token = d["token"]
        except Exception:
            pass

    def _save_token(self):
        if not self.token_file:
            return
        try:
            d = {"base": self.base, "token": self.token, "deviceId": self.device_id, "at": int(time.time())}
            tmp = self.token_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self.token_file)
        except Exception:
            pass

    # ---------- 底层 ----------
    def _http(self, url, method, headers, payload=None):
        req = urllib.request.Request(url, data=payload, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl) as resp:
            return resp.status, resp.read(), dict(resp.headers)

    def _build(self, path, params, body):
        pathname = path if path.startswith("/") else "/" + path
        if not pathname.startswith(API_PREFIX):
            pathname = API_PREFIX + pathname
        query = encode_query(params)
        url = self.base + pathname + ("?" + query if query else "")
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Cookie"] = "music-token=" + urllib.parse.quote(str(self.token), safe="")
        payload = None
        if body is not None:
            payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        headers["authx"] = build_authx(pathname, "GET" if body is None else "POST", params, body)
        return url, headers, payload

    def request(self, method, path, params=None, body=None, retry=True):
        url, headers, payload = self._build(path, params, body)
        try:
            status, raw, _ = self._http(url, method, headers, payload)
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        except Exception as e:
            raise FnMusicError("network: %s (%s)" % (e, url))
        try:
            env = json.loads(raw.decode("utf-8", "replace")) if raw else None
        except Exception:
            env = None
        if status == 401 and retry and self.username and self.password:
            self.login()
            return self.request(method, path, params, body, retry=False)
        if not isinstance(env, dict):
            raise FnMusicError("bad response status=%s head=%r" % (status, raw[:120]), status)
        if env.get("code") not in SUCCESS_CODES:
            raise FnMusicError("api error code=%s msg=%s" % (env.get("code"), env.get("msg")),
                               status, env.get("code"))
        return env.get("data")

    def get(self, path, params=None, **kw):
        return self.request("GET", path, params=params, **kw)

    def post(self, path, body=None, params=None, **kw):
        return self.request("POST", path, params=params, body=body, **kw)

    # ---------- 认证 ----------
    def login(self):
        if not self.username or not self.password:
            raise FnMusicError("missing username/password")
        with self._lock:
            data = self.request(
                "POST", "/user/password-login",
                body={"username": self.username, "password": _sha256(self.password),
                      "deviceId": self.device_id},
                retry=False,
            )
            token = (data or {}).get("userToken")
            if not token:
                raise FnMusicError("login response missing userToken")
            self.token = token
            self._save_token()
            return data

    def ensure_token(self):
        if not self.token:
            return self.login()
        return None

    def logout(self):
        try:
            self.post("/user/logout")
        finally:
            self.token = None

    def me(self):
        return self.get("/user/me")

    # ---------- 浏览 ----------
    def track_list(self, page=1, page_size=50):
        return self.get("/track/list", {"page": page, "pageSize": page_size})

    def album_list(self, page=1, page_size=50):
        return self.get("/album/list", {"page": page, "pageSize": page_size})

    def artist_list(self, page=1, page_size=50):
        return self.get("/artist/list", {"page": page, "pageSize": page_size})

    def playlist_list(self, page=1, page_size=50):
        return self.get("/playlist/list", {"page": page, "pageSize": page_size})

    def album_detail(self, guid):
        return self.get("/album/detail", {"guid": guid})

    def artist_detail(self, guid):
        return self.get("/artist/detail", {"guid": guid})

    def playlist_detail(self, guid):
        return self.get("/playlist/detail", {"guid": guid})

    def album_tracks(self, album_guid, page=1, size=200, sort="trackNo,asc"):
        return self.get("/track/album-detail/list",
                        {"albumGUID": album_guid, "page": page, "size": size, "sort": sort})

    def artist_tracks(self, artist_guid, page=1, size=200, sort="trackNo,asc"):
        return self.get("/track/artist-detail/list",
                        {"artistGUID": artist_guid, "page": page, "size": size, "sort": sort})

    def playlist_tracks(self, playlist_guid, page=1, size=200):
        return self.get("/track/playlist-detail/list",
                        {"playlistGUID": playlist_guid, "page": page, "size": size})

    def search_track(self, keyword, page=1, page_size=20):
        return self.get("/search/track", {"q": keyword, "page": page, "pageSize": page_size})

    def search_album(self, keyword, page=1, page_size=20):
        return self.get("/search/album", {"q": keyword, "page": page, "pageSize": page_size})

    def search_artist(self, keyword, page=1, page_size=20):
        return self.get("/search/artist", {"q": keyword, "page": page, "pageSize": page_size})

    def search_playlist(self, keyword, page=1, page_size=20):
        return self.get("/search/playlist", {"q": keyword, "page": page, "pageSize": page_size})

    # ---------- 收藏 ----------
    def favorite_list(self, page=1, page_size=50):
        return self.get("/favorite-track/list", {"page": page, "pageSize": page_size})

    def favorite_add(self, track_guid):
        return self.post("/favorite-track/create", {"trackGuid": track_guid})

    def favorite_remove(self, track_guid):
        return self.post("/favorite-track/delete", {"trackGuid": track_guid})

    # ---------- 歌单 ----------
    def playlist_create(self, name):
        return self.post("/playlist/create", {"name": name})

    def playlist_edit(self, playlist_guid, name):
        return self.post("/playlist/edit", {"guid": playlist_guid, "name": name})

    def playlist_delete(self, playlist_guid):
        return self.post("/playlist/delete", {"guid": playlist_guid})

    def playlist_add_tracks(self, playlist_guid, track_guids):
        return self.post("/playlist/add-track",
                         {"guid": playlist_guid, "trackGUIDs": list(track_guids)})

    def playlist_remove_tracks(self, playlist_guid, track_guids):
        return self.post("/playlist/remove-track",
                         {"guid": playlist_guid, "trackGUIDs": list(track_guids)})

    # ---------- 播放 ----------
    def track_metadata(self, track_guid):
        return self.get("/track/metadata", {"guid": track_guid})

    def stream_to_path(self, track_guid, path, retry=True):
        """把 /track/stream 直接流式写入 path，返回 (content_type, bytes)。"""
        url, headers, _ = self._build("/track/stream", {"guid": track_guid}, None)
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl)
        except urllib.error.HTTPError as e:
            head = e.read()[:80]
            e.close()
            if e.code == 401 and retry and self.username and self.password:
                self.login()
                return self.stream_to_path(track_guid, path, retry=False)
            raise FnMusicError("stream status=%s head=%r" % (e.code, head), e.code)
        except Exception as e:
            raise FnMusicError("stream network: %s (%s)" % (e, url))
        ctype = resp.headers.get("Content-Type", "")
        total = 0
        with resp, open(path, "wb") as fh:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                total += len(chunk)
        if not total:
            raise FnMusicError("stream empty body (%s)" % url)
        return ctype, total

    def stream_bytes(self, track_guid, retry=True):
        """下载 /track/stream 原始音频字节，返回 (content_type, bytes)。"""
        url, headers, _ = self._build("/track/stream", {"guid": track_guid}, None)
        try:
            status, raw, hdr = self._http(url, "GET", headers, None)
        except urllib.error.HTTPError as e:
            status, raw, hdr = e.code, e.read(), {}
        except Exception as e:
            raise FnMusicError("stream network: %s (%s)" % (e, url))
        if status == 401 and retry and self.username and self.password:
            self.login()
            return self.stream_bytes(track_guid, retry=False)
        if status != 200 or not raw:
            raise FnMusicError("stream status=%s head=%r" % (status, raw[:80]), status)
        return hdr.get("Content-Type", ""), raw
