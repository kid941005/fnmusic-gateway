"""网关设置页（飞牛音乐连接）。纯静态 HTML，接口走同源 Cookie 鉴权。"""

SETTINGS_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>网关设置 · 飞牛音乐</title>
<style>
:root{color-scheme:light dark}
*{box-sizing:border-box}
body{margin:0;padding:24px;font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif;background:#f5f6f8;color:#1f2328}
main{max-width:720px;margin:0 auto}
h1{font-size:20px;margin:0 0 4px}
p.sub{margin:0 0 20px;color:#6b7280;font-size:13px}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:18px 20px;margin-bottom:16px}
label{display:block;font-size:13px;color:#374151;margin:12px 0 6px}
label:first-child{margin-top:0}
input{width:100%;padding:9px 11px;border:1px solid #d1d5db;border-radius:8px;font:14px ui-monospace,monospace;background:#fff;color:#111}
input:focus{outline:2px solid #93c5fd;border-color:#60a5fa}
.row{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}
button{padding:9px 16px;border-radius:8px;border:1px solid #d1d5db;background:#fff;font-size:14px;cursor:pointer}
button.primary{background:#2563eb;border-color:#2563eb;color:#fff}
button:disabled{opacity:.55;cursor:default}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;background:#eef2ff;color:#3730a3;margin-left:8px;vertical-align:middle}
pre{white-space:pre-wrap;word-break:break-all;margin:14px 0 0;padding:12px;border-radius:8px;background:#0f172a;color:#e2e8f0;font:12.5px/1.5 ui-monospace,monospace;display:none}
pre.show{display:block}
pre.ok{background:#052e16;color:#bbf7d0}
pre.bad{background:#450a0a;color:#fecaca}
.meta{font-size:12.5px;color:#6b7280;margin-top:10px}
a{color:#2563eb}
</style></head><body><main>
<h1>网关设置 · 飞牛音乐</h1>
<p class="sub">飞牛音乐优先播放，在线音源兜底。这里填 NAS 上音乐服务的地址与账号，保存后立即生效。</p>

<div class="card">
  <label>飞牛音乐地址<span class="badge" id="srcBadge">未配置</span></label>
  <input id="base" placeholder="http://192.168.50.100:59820" autocomplete="off">
  <label>账号</label>
  <input id="username" placeholder="KID" autocomplete="username">
  <label>密码（留空表示不修改）</label>
  <input id="password" type="password" placeholder="••••••" autocomplete="current-password">
  <label>设备 ID（可留空）</label>
  <input id="device_id" placeholder="可选" autocomplete="off">
  <div class="row">
    <button class="primary" id="save">保存</button>
    <button id="test">测试连接</button>
    <button id="logout">清除登录令牌</button>
    <a href="/" style="align-self:center;font-size:13px">← 返回音乐管理台</a>
  </div>
  <pre id="out"></pre>
  <div class="meta" id="meta"></div>
</div>

<div class="card">
  <div style="font-size:14px;font-weight:600;margin-bottom:8px">在线音源</div>
  <div class="meta" id="fallback">加载中…</div>
  <div class="meta">在线音源的启用/账号/脚本请到 <a href="/">音乐管理台</a> 里配置（平台状态、洛雪源、网易扫码）。</div>
</div>
</main>
<script>
(function(){
  var $ = function(id){ return document.getElementById(id); };
  var out = $('out');
  function show(ok, text){
    out.className = 'show ' + (ok ? 'ok' : 'bad');
    out.textContent = text;
  }
  function fill(d){
    $('base').value = d.base || '';
    $('username').value = d.username || '';
    $('device_id').value = d.device_id || '';
    $('password').value = '';
    $('srcBadge').textContent = d.configured ? '已配置' : '未配置';
    var bits = [];
    if (d.download_dir) bits.push('下载目录 ' + d.download_dir);
    if (d.token_cached) bits.push('已缓存登录令牌');
    if (d.source) bits.push('配置来源 ' + d.source);
    $('meta').textContent = bits.join(' · ');
    var fb = (d.fallback && d.fallback.length) ? d.fallback.join('、') : '未配置（只走飞牛音乐）';
    $('fallback').textContent = '兜底音源：' + fb;
  }
  function load(){
    fetch('/api/settings/fnos', {credentials:'same-origin'}).then(function(r){
      if (r.status === 401) { show(false, '需要管理令牌：请用 http://NAS:8766/?token=<GW_ADMIN_TOKEN> 打开一次'); return null; }
      return r.json();
    }).then(function(d){ if (d) fill(d); }).catch(function(e){ show(false, String(e)); });
  }
  function payload(){
    return {base: $('base').value.trim(), username: $('username').value.trim(),
            password: $('password').value, device_id: $('device_id').value.trim()};
  }
  $('save').onclick = function(){
    var btn = this; btn.disabled = true;
    fetch('/api/settings/fnos', {method:'PUT', credentials:'same-origin',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload())})
      .then(function(r){ return r.json().then(function(d){ return [r.status, d]; }); })
      .then(function(pair){
        if (pair[0] === 200) { fill(pair[1]); show(true, '已保存，下一首歌按新配置连接'); }
        else { show(false, '保存失败：' + JSON.stringify(pair[1])); }
      }).catch(function(e){ show(false, String(e)); })
      .then(function(){ btn.disabled = false; });
  };
  $('test').onclick = function(){
    var btn = this; btn.disabled = true; show(true, '连接中…');
    fetch('/api/settings/fnos/test', {method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload())})
      .then(function(r){ return r.json(); })
      .then(function(d){
        if (d.ok) { show(true, '连接成功：' + d.ms + ' ms，取到 ' + (d.tracks||0) + ' 首曲目'); }
        else { show(false, '连接失败（' + (d.ms||0) + ' ms）：' + (d.error || d.message || '未知错误')); }
      }).catch(function(e){ show(false, String(e)); })
      .then(function(){ btn.disabled = false; });
  };
  $('logout').onclick = function(){
    fetch('/api/settings/fnos/logout', {method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'}, body:'{}'})
      .then(function(r){ return r.json(); }).then(function(){ show(true, '已清除缓存令牌，下次播放重新登录'); load(); })
      .catch(function(e){ show(false, String(e)); });
  };
  load();
})();
</script></body></html>
"""
