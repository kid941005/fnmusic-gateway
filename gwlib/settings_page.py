"""网关设置页（飞牛音乐连接 + 兜底音源）。纯静态 HTML，接口走同源 Cookie 鉴权。"""

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
label.check{display:flex;align-items:center;gap:8px;margin-top:14px;font-size:13px;color:#374151}
label.check input{width:auto;margin:0}
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
.fb-row{display:flex;gap:8px;margin-top:10px}
.fb-row input{flex:1}
.fb-row button{padding:7px 12px;font-size:13px;white-space:nowrap}
.adv-group{font-size:13px;font-weight:600;color:#374151;margin:18px 0 8px;padding-top:14px;border-top:1px solid #eef0f3}
.adv-group:first-of-type{border-top:0;padding-top:0;margin-top:6px}
.adv-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px 14px}
.adv-grid label{margin:0;font-size:12.5px}
.adv-grid input,.adv-grid select{width:100%;padding:8px 10px;border:1px solid #d1d5db;border-radius:8px;font:14px ui-monospace,monospace;background:#fff;color:#111}
.adv-grid .check{margin-top:0;padding-top:22px}
.adv-help{font-size:12px;color:#9ca3af;margin-top:4px}
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
  <label class="check"><input type="checkbox" id="insecure_tls">使用自签 HTTPS 证书（跳过证书校验）</label>
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
  <div style="font-size:14px;font-weight:600;margin-bottom:8px">兜底在线音源</div>
  <div class="meta">格式 <code>类型@地址</code>，类型 musicdl / lxmusic / musicbox / neteasefree，可只写地址自动识别。按顺序尝试，前面的优先。</div>
  <div id="fbList"></div>
  <div class="row">
    <button id="fbAdd">+ 添加音源</button>
    <button class="primary" id="fbSave">保存音源</button>
  </div>
  <pre id="fbOut"></pre>
  <div class="meta">在线音源的平台/账号/脚本请到 <a href="/">音乐管理台</a> 里配置。</div>
</div>

<div class="card">
  <div style="font-size:14px;font-weight:600;margin-bottom:4px">高级设置</div>
  <div class="meta">保存后立即生效；端口、数据目录、GW_ADMIN_TOKEN 等启动参数仍需在 .env 里设置。</div>
  <div class="adv-group">播放</div>
  <div class="adv-grid">
    <label>默认音质
      <select id="adv_quality_default">
        <option value="standard">标准 128k</option>
        <option value="high">高 192k</option>
        <option value="exhigh">极高 320k</option>
        <option value="lossless">无损（下载优先）</option>
      </select>
    </label>
    <label>搜索返回上限（首）<input id="adv_max_limit" type="number" min="1" max="500"></label>
    <label>直链有效期（秒）<input id="adv_url_ttl" type="number" min="60" max="86400"></label>
    <label>试听判定阈值（秒）<input id="adv_preview_min_seconds" type="number" min="1" max="3600"></label>
    <label class="check"><input type="checkbox" id="adv_tee_save">边播边存到本地曲库</label>
  </div>
  <div class="adv-group">缓存与转码</div>
  <div class="adv-grid">
    <label>播放缓存上限（MB，0=不限）<input id="adv_cache_max_mb" type="number" min="0"></label>
    <label>本地曲库上限（MB，0=不限）<input id="adv_library_max_mb" type="number" min="0"></label>
    <label>同时转码数<input id="adv_transcode_concurrency" type="number" min="1" max="8"></label>
  </div>
  <div class="adv-group">搜索与歌词</div>
  <div class="adv-grid">
    <label class="check"><input type="checkbox" id="adv_smart_search">智能搜索（别名纠错 + 相关度）</label>
    <label class="check"><input type="checkbox" id="adv_lyrics_enabled">启用歌词</label>
    <label>LRCLIB 地址<input id="adv_lrclib_url" placeholder="https://lrclib.net/api/get 或 off"></label>
    <label>歌词重试间隔（秒）<input id="adv_lyrics_retry" type="number" min="60"></label>
  </div>
  <div class="adv-group">音源与超时</div>
  <div class="adv-grid">
    <label>音源单请求超时（秒）<input id="adv_fallback_timeout" type="number" min="1" max="120"></label>
    <label>单音源搜索条数<input id="adv_fallback_search_limit" type="number" min="1" max="50"></label>
    <label>聚合搜索总条数<input id="adv_fallback_total_limit" type="number" min="1" max="500"></label>
    <label>聚合搜索总超时（秒）<input id="adv_fallback_total_timeout" type="number" min="1" max="300"></label>
    <label>飞牛音乐请求超时（秒）<input id="adv_fnos_timeout" type="number" min="3" max="120"></label>
    <label>MCP 上游超时（秒）<input id="adv_mcp_timeout" type="number" min="30" max="3600"></label>
  </div>
  <div class="row">
    <button class="primary" id="advSave">保存高级设置</button>
  </div>
  <pre id="advOut"></pre>
</div>
</main>
<script>
(function(){
  var $ = function(id){ return document.getElementById(id); };
  function showAt(el, ok, text){
    el.className = 'show ' + (ok ? 'ok' : 'bad');
    el.textContent = text;
  }
  function fill(d){
    $('base').value = d.base || '';
    $('username').value = d.username || '';
    $('device_id').value = d.device_id || '';
    $('password').value = '';
    $('insecure_tls').checked = !!d.insecure_tls;
    $('srcBadge').textContent = d.configured ? '已配置' : '未配置';
    var bits = [];
    if (d.download_dir) bits.push('下载目录 ' + d.download_dir);
    if (d.token_cached) bits.push('已缓存登录令牌');
    if (d.source) bits.push('配置来源 ' + d.source);
    $('meta').textContent = bits.join(' · ');
    fbRows = (d.fallback_urls || []).slice();
    renderFb();
  }
  function load(){
    fetch('/api/settings/fnos', {credentials:'same-origin'}).then(function(r){
      if (r.status === 401) { showAt($('out'), false, '需要管理令牌：请用 http://NAS:8766/?token=<GW_ADMIN_TOKEN> 打开一次'); return null; }
      return r.json();
    }).then(function(d){ if (d) fill(d); }).catch(function(e){ showAt($('out'), false, String(e)); });
  }
  function payload(){
    return {base: $('base').value.trim(), username: $('username').value.trim(),
            password: $('password').value, device_id: $('device_id').value.trim(),
            insecure_tls: $('insecure_tls').checked};
  }
  $('save').onclick = function(){
    var btn = this; btn.disabled = true;
    fetch('/api/settings/fnos', {method:'PUT', credentials:'same-origin',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload())})
      .then(function(r){ return r.json().then(function(d){ return [r.status, d]; }); })
      .then(function(pair){
        if (pair[0] === 200) { fill(pair[1]); showAt($('out'), true, '已保存，下一首歌按新配置连接'); }
        else { showAt($('out'), false, '保存失败：' + JSON.stringify(pair[1])); }
      }).catch(function(e){ showAt($('out'), false, String(e)); })
      .then(function(){ btn.disabled = false; });
  };
  $('test').onclick = function(){
    var btn = this; btn.disabled = true; showAt($('out'), true, '连接中…');
    fetch('/api/settings/fnos/test', {method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload())})
      .then(function(r){ return r.json(); })
      .then(function(d){
        if (d.ok) { showAt($('out'), true, '连接成功：' + d.ms + ' ms，取到 ' + (d.tracks||0) + ' 首曲目'); }
        else { showAt($('out'), false, '连接失败（' + (d.ms||0) + ' ms）：' + (d.error || d.message || '未知错误')); }
      }).catch(function(e){ showAt($('out'), false, String(e)); })
      .then(function(){ btn.disabled = false; });
  };
  $('logout').onclick = function(){
    fetch('/api/settings/fnos/logout', {method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'}, body:'{}'})
      .then(function(r){ return r.json(); }).then(function(){ showAt($('out'), true, '已清除缓存令牌，下次播放重新登录'); load(); })
      .catch(function(e){ showAt($('out'), false, String(e)); });
  };

  var fbRows = [];
  function renderFb(){
    var box = $('fbList');
    box.innerHTML = '';
    if (!fbRows.length) {
      var empty = document.createElement('div');
      empty.className = 'meta'; empty.textContent = '尚未配置兜底音源';
      box.appendChild(empty);
      return;
    }
    fbRows.forEach(function(v, i){
      var row = document.createElement('div'); row.className = 'fb-row';
      var inp = document.createElement('input');
      inp.value = v; inp.placeholder = 'musicdl@http://fnmusic-sources:8001';
      inp.oninput = function(){ fbRows[i] = inp.value; };
      var test = document.createElement('button'); test.textContent = '测试';
      test.onclick = function(){ testSpec(inp.value, test); };
      var del = document.createElement('button'); del.textContent = '删除';
      del.onclick = function(){ fbRows.splice(i, 1); renderFb(); };
      row.appendChild(inp); row.appendChild(test); row.appendChild(del);
      box.appendChild(row);
    });
  }
  function testSpec(spec, btn){
    spec = (spec || '').trim();
    if (!spec) { showAt($('fbOut'), false, '请先填写音源地址'); return; }
    btn.disabled = true; btn.textContent = '测试中';
    fetch('/api/settings/fallback/test', {method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({spec: spec})})
      .then(function(r){ return r.json(); })
      .then(function(d){
        if (d.ok) showAt($('fbOut'), true, spec + ' 可用（' + d.ms + ' ms，类型 ' + d.service + '）');
        else showAt($('fbOut'), false, spec + ' 不可用（' + (d.ms||0) + ' ms）：' + (d.error || '未知错误'));
      }).catch(function(e){ showAt($('fbOut'), false, String(e)); })
      .then(function(){ btn.disabled = false; btn.textContent = '测试'; });
  }
  var ADV_TEXT = ['lrclib_url'];
  var ADV_CHECK = ['tee_save','smart_search','lyrics_enabled'];
  var ADV_NUMBER = ['max_limit','url_ttl','preview_min_seconds','cache_max_mb',
                    'library_max_mb','transcode_concurrency','lyrics_retry',
                    'fallback_timeout','fallback_search_limit','fallback_total_limit',
                    'fallback_total_timeout','fnos_timeout','mcp_timeout'];
  function advFill(d){
    $('adv_quality_default').value = d.quality_default || 'standard';
    ADV_CHECK.forEach(function(k){ $('adv_' + k).checked = !!d[k]; });
    ADV_NUMBER.forEach(function(k){ $('adv_' + k).value = (d[k] === undefined ? '' : d[k]); });
    ADV_TEXT.forEach(function(k){ $('adv_' + k).value = d[k] || ''; });
  }
  function advLoad(){
    fetch('/api/settings/advanced', {credentials:'same-origin'})
      .then(function(r){ if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(advFill)
      .catch(function(e){ showAt($('advOut'), false, '读取高级设置失败：' + e); });
  }
  $('advSave').onclick = function(){
    var btn = this; btn.disabled = true;
    var body = {quality_default: $('adv_quality_default').value};
    ADV_CHECK.forEach(function(k){ body[k] = $('adv_' + k).checked; });
    ADV_NUMBER.forEach(function(k){
      var v = parseInt($('adv_' + k).value, 10);
      body[k] = isNaN(v) ? 0 : v;
    });
    ADV_TEXT.forEach(function(k){ body[k] = $('adv_' + k).value.trim(); });
    fetch('/api/settings/advanced', {method:'PUT', credentials:'same-origin',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)})
      .then(function(r){ return r.json().then(function(d){ return [r.status, d]; }); })
      .then(function(pair){
        if (pair[0] === 200) { advFill(pair[1]); showAt($('advOut'), true, '已保存，运行参数已立即生效'); }
        else { showAt($('advOut'), false, '保存失败：' + JSON.stringify(pair[1])); }
      }).catch(function(e){ showAt($('advOut'), false, String(e)); })
      .then(function(){ btn.disabled = false; });
  };
  $('fbAdd').onclick = function(){ fbRows.push(''); renderFb(); };
  $('fbSave').onclick = function(){
    var btn = this; btn.disabled = true;
    var urls = fbRows.map(function(x){ return (x||'').trim(); }).filter(Boolean);
    fetch('/api/settings/fallback', {method:'PUT', credentials:'same-origin',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({urls: urls})})
      .then(function(r){ return r.json().then(function(d){ return [r.status, d]; }); })
      .then(function(pair){
        if (pair[0] === 200) { fbRows = (pair[1].urls || []).slice(); renderFb(); showAt($('fbOut'), true, '已保存，兜底音源已热重建'); }
        else { showAt($('fbOut'), false, '保存失败：' + JSON.stringify(pair[1])); }
      }).catch(function(e){ showAt($('fbOut'), false, String(e)); })
      .then(function(){ btn.disabled = false; });
  };
  load();
  advLoad();
})();
</script></body></html>
"""
