# -*- coding: utf-8 -*-
"""WebUI 登录页：输入 GW_ADMIN_TOKEN 换取 HttpOnly Cookie。"""

import html

_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>登录 · fnmusic-gateway</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--card:#141b2d;--line:#263149;--mut:#93a4c4;--acc:#4f8cff;--bad:#ff6b6b}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:var(--bg);color:#e8eefc;font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif;padding:18px}
.card{width:100%;max-width:380px;background:var(--card);border:1px solid var(--line);border-radius:16px;padding:28px 24px}
h1{font-size:20px;margin:0 0 6px}.sub{color:var(--mut);font-size:13px;margin-bottom:20px}
label{display:block;font-size:12.5px;color:var(--mut);margin-bottom:6px}
input[type=password]{width:100%;background:#0e1526;border:1px solid var(--line);color:#e8eefc;border-radius:9px;padding:11px 12px;font:inherit}
button{width:100%;margin-top:14px;border:0;border-radius:9px;background:var(--acc);color:#fff;padding:11px 12px;font:inherit;cursor:pointer}
.err{background:rgba(255,107,107,.12);border:1px solid rgba(255,107,107,.35);color:var(--bad);border-radius:9px;padding:9px 11px;font-size:13px;margin-bottom:14px}
.hint{color:var(--mut);font-size:12px;margin-top:14px;line-height:1.5}
</style>
</head>
<body>
<div class="card">
  <h1>fnmusic-gateway</h1>
  <div class="sub">输入管理密码进入 WebUI</div>
  __ERROR__
  <form method="post" action="/login">
    <input type="hidden" name="next" value="__NEXT__">
    <label for="password">管理密码</label>
    <input id="password" name="password" type="password" autocomplete="current-password" autofocus required>
    <button type="submit">登录</button>
  </form>
  <div class="hint">密码即部署时设置的 GW_ADMIN_TOKEN。</div>
</div>
</body>
</html>
"""


def login_html(next_path="/", error=""):
    err = ('<div class="err">%s</div>' % html.escape(error)) if error else ""
    return (_TEMPLATE.replace("__ERROR__", err)
            .replace("__NEXT__", html.escape(next_path or "/", quote=True)))
