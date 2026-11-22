# -*- coding: utf-8 -*-
"""网易云扫码登录：生成二维码、轮询扫码状态、提取 MUSIC_U。"""
import base64
import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from http.cookies import SimpleCookie

from .errors import BadRequest, UpstreamError

QR_UNIKEY_URL = "https://music.163.com/api/login/qrcode/unikey?type=1"
QR_POLL_URL = "https://music.163.com/api/login/qrcode/client/login"
QR_LOGIN_PREFIX = "https://music.163.com/login?codekey="
QR_TIMEOUT = 10
QR_EXPIRES_IN = 300
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://music.163.com/",
    "Accept": "application/json, text/plain, */*",
    "Cookie": "os=pc",
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


def _request_json(url, timeout=QR_TIMEOUT):
    """GET 一个 JSON 接口，返回 (payload, cookies)。"""
    req = urllib.request.Request(url, headers=_HEADERS)
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
            headers = resp.headers
    except urllib.error.HTTPError as exc:
        raise NeteaseLoginError("网易云接口返回 HTTP %s" % exc.code) from exc
    except Exception as exc:
        raise NeteaseLoginError("连接网易云失败：%s" % exc) from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise NeteaseLoginError("网易云接口返回非 JSON") from exc
    if not isinstance(payload, dict):
        raise NeteaseLoginError("网易云接口返回格式异常")
    return payload, _cookies_from_response(jar, headers)


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
    """获取扫码登录 key 和二维码。"""
    payload, _ = _request_json(QR_UNIKEY_URL, timeout=timeout)
    key = str(payload.get("unikey") or payload.get("key") or "").strip()
    if not key:
        message = str(payload.get("message") or payload.get("msg") or "未返回二维码 key")
        raise NeteaseLoginError("获取网易云二维码失败：%s" % message)
    content = QR_LOGIN_PREFIX + urllib.parse.quote(key, safe="")
    return {
        "key": key,
        "qr_content": content,
        "qr_svg": qr_svg(content),
        "expires_in": QR_EXPIRES_IN,
    }


def poll_qr(key, timeout=QR_TIMEOUT):
    """查询扫码状态；成功时返回待保存的 MUSIC_U Cookie。"""
    key = str(key or "").strip()
    if not key or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key):
        raise BadRequest("二维码 key 非法")
    url = QR_POLL_URL + "?key=" + urllib.parse.quote(key, safe="") + "&type=1"
    payload, cookies = _request_json(url, timeout=timeout)
    try:
        code = int(payload.get("code") or 0)
    except (TypeError, ValueError):
        code = 0
    message = str(payload.get("message") or payload.get("msg") or "")
    if code == 803:
        music_u = cookies.get("MUSIC_U") or ""
        if not music_u:
            raise NeteaseLoginError("网易云登录成功，但未返回 MUSIC_U")
        parts = ["MUSIC_U=" + music_u]
        csrf = cookies.get("__csrf") or cookies.get("__csrf__") or ""
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
