# -*- coding: utf-8 -*-
"""网易云扫码登录：生成二维码、轮询扫码状态、提取 MUSIC_U。

2026 年起网易云对第三方扫码登录增加了行为验证风控（8821 / -462 / 8810）。
这里优先使用新版桌面端接口（interface.music.163.com + type=3），失败时回退旧接口；
遇到风控码不抛 500，而是返回 status=risk，让 WebUI 引导用户改用网页版 Cookie 登录。
"""
import base64
import io
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from http.cookies import SimpleCookie

from .errors import BadRequest, UpstreamError

QR_UNIKEY_URL = "https://interface.music.163.com/api/login/qrcode/unikey"
QR_POLL_URL = "https://interface.music.163.com/api/login/qrcode/client/login"
QR_LEGACY_UNIKEY_URL = "https://music.163.com/api/login/qrcode/unikey"
QR_LEGACY_POLL_URL = "https://music.163.com/api/login/qrcode/client/login"
QR_LOGIN_PREFIX = "https://music.163.com/login?codekey="
QR_BROWSER_LOGIN_URL = "https://music.163.com/#/login"
QR_TIMEOUT = 10
QR_EXPIRES_IN = 300
QR_TYPE = 3
QR_LEGACY_TYPE = 1
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://music.163.com/",
    "Accept": "application/json, text/plain, */*",
}
# 网易云登录风控码：扫码接口无法完成时，引导用户改用网页版 Cookie。
_RISK_HINTS = {
    8821: "网易云触发登录风控（8821：需要行为验证码），扫码登录已被限制。请点「打开网易云网页登录」登录后，把 MUSIC_U Cookie 粘贴到下方输入框并保存。",
    -462: "网易云要求完成人机验证（-462），请稍后再试，或改用网页版登录后粘贴 MUSIC_U Cookie。",
    -460: "网易云检测到网络环境存在风险（-460），请更换网络后重试，或改用网页版登录后粘贴 MUSIC_U Cookie。",
    8810: "网易云检测到网络环境安全风险（8810），请更换网络后重试，或改用网页版登录后粘贴 MUSIC_U Cookie。",
}


class NeteaseLoginError(UpstreamError):
    """网易云扫码登录上游异常。"""
    code = "netease_login_error"


def _cookies_from_response(jar, headers):
    """合并 CookieJar 与原始 Set-Cookie，保留同名最后一个值。"""
    cookies = {}
    try:
        for cookie in jar:
            if cookie.value:
                cookies[cookie.name] = cookie.value
    except Exception:
        pass
    for header in headers.get_all("Set-Cookie") or []:
        jar2 = SimpleCookie()
        try:
            jar2.load(header)
        except Exception:
            continue
        for name, morsel in jar2.items():
            if morsel.value:
                cookies[name] = morsel.value
    return cookies


def _payload_cookies(payload):
    """部分新版接口把 Cookie 放在响应体里，这里兼容字符串/列表/嵌套 data。"""
    raw = payload.get("cookie")
    if raw is None and isinstance(payload.get("data"), dict):
        raw = payload["data"].get("cookie")
    if not raw:
        return {}
    if isinstance(raw, (list, tuple)):
        raw = "; ".join(str(x) for x in raw)
    out = {}
    for part in str(raw).split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name, value = name.strip(), value.strip()
        if name and value:
            out[name] = value
    return out


def _risk_message(code, message):
    hint = _RISK_HINTS.get(code) or "网易云登录触发风控，扫码登录暂不可用。请改用网页版登录后粘贴 MUSIC_U Cookie。"
    detail = str(message or "").strip()
    if detail and detail not in hint:
        return "%s（%s）" % (hint, detail)
    return hint


_LOCK = threading.RLock()
_OPENER = None
_COOKIE_JAR = None
_WARMED = False


def _opener():
    global _OPENER, _COOKIE_JAR
    if _OPENER is None:
        _COOKIE_JAR = CookieJar()
        _OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_COOKIE_JAR))
    return _OPENER


def _warm_session(opener, timeout=QR_TIMEOUT):
    """先访问一次网易云首页，避免 unikey 接口被误判为异常网络环境。"""
    global _WARMED
    if _WARMED:
        return
    try:
        req = urllib.request.Request("https://music.163.com/", headers=_HEADERS)
        with opener.open(req, timeout=timeout) as resp:
            resp.read(1024)
        _WARMED = True
    except Exception:
        # 首页预热失败时仍继续请求 unikey，由 unikey 返回明确错误
        pass


def _request_json(url, data=None, timeout=QR_TIMEOUT):
    """请求一个 JSON 接口，返回 (payload, cookies)；data 非空时用 POST 表单。"""
    opener = _opener()
    headers = dict(_HEADERS)
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
    req = urllib.request.Request(
        url, data=body, headers=headers, method="POST" if body is not None else "GET"
    )
    with _LOCK:
        _warm_session(opener, timeout=timeout)
        try:
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
                headers = resp.headers
        except urllib.error.HTTPError as exc:
            raise NeteaseLoginError("网易云接口返回 HTTP %s" % exc.code) from exc
        except Exception as exc:
            raise NeteaseLoginError("连接网易云失败：%s" % exc) from exc
        cookies = _cookies_from_response(_COOKIE_JAR, headers)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise NeteaseLoginError("网易云接口返回非 JSON") from exc
    if not isinstance(payload, dict):
        raise NeteaseLoginError("网易云接口返回格式异常")
    return payload, cookies


def qr_svg(content):
    """把登录链接渲染成 data:image/svg+xml;base64。"""
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError as exc:
        raise NeteaseLoginError("二维码组件未安装，请重建镜像（需要 qrcode）") from exc
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(content)
    qr.make(fit=True)
    image = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
    buf = io.BytesIO()
    image.save(buf)
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def start_qr(timeout=QR_TIMEOUT):
    """获取扫码登录 key 和二维码；优先新版桌面端 type=3，失败回退旧接口。"""
    errors = []
    attempts = (
        ("desktop", QR_UNIKEY_URL, {"type": QR_TYPE}),
        ("legacy", QR_LEGACY_UNIKEY_URL + "?type=%d" % QR_LEGACY_TYPE, None),
    )
    for proto, url, data in attempts:
        try:
            payload, _ = _request_json(url, data=data, timeout=timeout)
        except NeteaseLoginError as exc:
            errors.append(str(exc))
            continue
        key = str(payload.get("unikey") or payload.get("key") or "").strip()
        if not key:
            message = str(payload.get("message") or payload.get("msg") or "未返回二维码 key")
            errors.append("获取网易云二维码失败：%s" % message)
            continue
        content = QR_LOGIN_PREFIX + urllib.parse.quote(key, safe="")
        return {
            "key": key,
            "proto": proto,
            "qr_content": content,
            "qr_svg": qr_svg(content),
            "expires_in": QR_EXPIRES_IN,
            "browser_login_url": QR_BROWSER_LOGIN_URL,
        }
    raise NeteaseLoginError("；".join(errors) or "获取网易云二维码失败")


def _poll_once(key, proto, timeout):
    if proto == "legacy":
        url = QR_LEGACY_POLL_URL + "?key=" + urllib.parse.quote(key, safe="") + "&type=%d" % QR_LEGACY_TYPE
        return _request_json(url, timeout=timeout)
    return _request_json(QR_POLL_URL, data={"key": key, "type": QR_TYPE}, timeout=timeout)


def poll_qr(key, timeout=QR_TIMEOUT, proto="desktop"):
    """查询扫码状态；成功时返回待保存的 MUSIC_U Cookie，风控时返回 status=risk。"""
    key = str(key or "").strip()
    if not key or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key):
        raise BadRequest("二维码 key 非法")
    proto = "legacy" if str(proto or "").strip() == "legacy" else "desktop"
    try:
        payload, cookies = _poll_once(key, proto, timeout)
    except NeteaseLoginError:
        if proto == "legacy":
            raise
        # 新版接口不可用时回退旧接口，保证老部署仍能轮询。
        payload, cookies = _poll_once(key, "legacy", timeout)
    try:
        code = int(payload.get("code") or 0)
    except (TypeError, ValueError):
        code = 0
    message = str(payload.get("message") or payload.get("msg") or "")
    if code in _RISK_HINTS:
        return {
            "status": "risk",
            "code": code,
            "message": _risk_message(code, message),
            "hint": "cookie",
        }
    if code == 803:
        merged = dict(cookies)
        for name, value in _payload_cookies(payload).items():
            merged.setdefault(name, value)
        music_u = merged.get("MUSIC_U") or ""
        if not music_u:
            raise NeteaseLoginError("网易云登录成功，但未返回 MUSIC_U")
        parts = ["MUSIC_U=" + music_u]
        csrf = merged.get("__csrf") or merged.get("__csrf__") or ""
        if csrf:
            parts.append("__csrf=" + csrf)
        return {
            "status": "success",
            "code": 803,
            "message": "登录成功，已保存 Cookie",
            "cookie": "; ".join(parts),
        }
    if code == 802:
        return {"status": "scanned", "code": 802, "message": "已扫码，请在手机上确认"}
    if code == 801:
        return {"status": "waiting", "code": 801, "message": "等待扫码"}
    if code == 800:
        return {"status": "expired", "code": 800, "message": "二维码已过期，请重新生成"}
    raise NeteaseLoginError("网易云扫码状态异常：code=%s %s" % (code, message))
