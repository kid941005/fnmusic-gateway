# -*- coding: utf-8 -*-
"""网关设置页：飞牛音乐、内置在线音源、高级设置。"""

import json

from . import __version__ as VERSION
from .quality import describe as describe_quality
from .theme import THEME_CONTROL, THEME_SCRIPT, THEME_STYLE

SETTINGS_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>网关设置 · fnmusic-gateway v__VERSION__</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--card:#141b2d;--line:#263149;--mut:#93a4c4;--acc:#4f8cff;--ok:#37c98a;--bad:#ff6b6b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#e8eefc;font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:22px 18px 50px}
header{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px;gap:12px}
h1{font-size:20px;margin:0}.sub{color:var(--mut);font-size:13px}
a{color:#8db4ff;text-decoration:none}.btn{border:1px solid var(--line);background:#1b2540;color:#e8eefc;border-radius:9px;padding:9px 14px;cursor:pointer;font:inherit}
.btn.primary{background:var(--acc);border-color:var(--acc);color:#fff}.btn.small{padding:5px 9px;font-size:12.5px}.btn:disabled{opacity:.45}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;margin-bottom:16px}
.card h2{font-size:16px;margin:0 0 4px}.hint{color:var(--mut);font-size:12.5px;margin:0 0 14px}
.field{display:flex;flex-direction:column;gap:5px;margin-bottom:12px}.field label{font-size:12.5px;color:var(--mut)}
input[type=text],input[type=password],input[type=number],select{background:#0e1526;border:1px solid var(--line);color:#e8eefc;border-radius:9px;padding:10px 12px;font:inherit;width:100%}
.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}.row3{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.check{display:flex;align-items:center;gap:8px;color:#e8eefc}.check input{width:16px;height:16px}
.plat{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:9px 0;border-top:1px solid var(--line)}
.plat:first-child{border-top:0}.plat .l{display:flex;align-items:center;gap:9px}
.plat .r{display:flex;align-items:center;gap:8px;flex-shrink:0}
.test-out{font-size:12px;color:var(--mut);max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.test-out.ok{color:var(--ok)}.test-out.bad{color:var(--bad)}
.qr-panel{display:flex;gap:14px;align-items:flex-start;flex-wrap:wrap;background:var(--input);border:1px solid var(--line);border-radius:10px;padding:10px;margin:2px 0 12px}
.qr-panel .out{margin-top:6px}
.out{min-height:20px;color:var(--mut);font-size:12.5px;white-space:pre-wrap;margin-top:8px}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;background:#1b2540;color:var(--mut);font-size:11.5px}
.pill.ok{background:rgba(55,201,138,.15);color:var(--ok)}.pill.bad{background:rgba(255,107,107,.15);color:var(--bad)}
.adv{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
@media(max-width:680px){.row,.row3{grid-template-columns:1fr}header{flex-direction:column;align-items:flex-start}.plat{flex-wrap:wrap}.plat .r{width:100%;justify-content:flex-end}}
__THEME_STYLE__
</style>
__THEME_SCRIPT__
</head>
<body>
<div class="wrap">
<header>
  <div><h1>网关设置</h1><div class="sub">v__VERSION__ · 配置会写入 data/config.json，保存后立即生效</div></div>
  <div>__THEME_CONTROL__ <a class="btn" href="/">返回首页</a> <a class="btn" href="/logout">退出</a></div>
</header>

<div class="card">
  <h2>飞牛音乐</h2>
  <p class="hint">只填协议 + IP + 端口，例如 http://192.168.50.100:59820，不要带 /music 或 /music/api/v1。配置后本地曲库优先，在线音源补足。</p>
  <div class="row">
    <div class="field"><label>地址</label><input id="base" type="text" placeholder="http://NAS_IP:59820"></div>
    <div class="field"><label>用户名</label><input id="username" type="text" placeholder="大写 KID"></div>
  </div>
  <div class="row">
    <div class="field"><label>密码（留空表示不修改）</label><input id="password" type="password" placeholder="••••••"></div>
    <div class="field"><label>设备 ID（通常留空）</label><input id="device_id" type="text" placeholder="留空使用默认值"></div>
  </div>
  <label class="check"><input id="insecure_tls" type="checkbox"> 允许自签 HTTPS 证书（飞牛使用自签证书时打开）</label>
  <div style="margin-top:14px" class="row"><div></div><button class="btn primary" id="save">保存飞牛设置</button></div>
  <div class="out" id="fnosOut"></div>
</div>

<div class="card">
  <h2>在线音源（内置 musicdl）</h2>
  <p class="hint">无需额外容器。勾选平台后网关会自行搜索、解析直链、播放与下载；平台越多越慢，建议保留 1~3 个。</p>
  <label class="check" style="margin-bottom:10px"><input id="online_enabled" type="checkbox"> 启用在线音源</label>
  <div id="platList"></div>
  <div id="qrPanel" class="qr-panel" style="display:none">
    <div id="qrBox" style="display:none;background:#fff;padding:6px;border-radius:10px"><img id="qrImg" alt="网易云登录二维码" style="width:180px;height:180px;display:block"></div>
    <div style="flex:1;min-width:220px"><div class="sub">网易云扫码登录（推荐）</div><button class="btn" id="qrStart" type="button">生成登录二维码</button><div class="out" id="qrOut"></div></div>
  </div>
  <div class="field" style="margin-top:12px"><label>附加平台（可选，逗号分隔的 musicdl MusicClient 类名）</label><input id="extra_platforms" type="text" placeholder="例如 QQMusicClient,BilibiliMusicClient"></div>
  <div class="field"><label>网易云 MUSIC_U Cookie（可选，留空不修改）</label><input id="netease_cookie" type="password" autocomplete="off" placeholder="粘贴 MUSIC_U=... 或整段 Cookie"></div>
  <label class="check" style="margin-bottom:12px"><input id="clear_netease_cookie" type="checkbox"> 清除已保存的网易云 Cookie</label>
  <div class="row3">
    <div class="field"><label>单平台搜索条数（≤10）</label><input id="search_limit" type="number" min="1" max="10"></div>
    <div class="field"><label>单平台超时（秒）</label><input id="timeout" type="number" min="3" max="120"></div>
    <div class="field"><label>聚合总条数</label><input id="total_limit" type="number" min="1" max="500"></div>
    <div class="field"><label>聚合总超时（秒）</label><input id="total_timeout" type="number" min="1" max="300"></div>
  </div>
  <div class="row"><button class="btn primary" id="onlineSave">保存在线音源</button><button class="btn" id="onlineTest">测试全部</button></div>
  <div class="out" id="onlineOut"></div>
</div>

<div class="card">
  <h2>高级设置</h2>
  <p class="hint">音质、超时、缓存等运行参数；保存后重建播放服务与音源。</p>
  <div class="adv" id="adv"></div>
  <div style="margin-top:14px"><button class="btn primary" id="advSave">保存高级设置</button></div>
  <div class="out" id="advOut"></div>
</div>
</div>
<script>
var PLATFORMS=['NeteaseMusicClient','KuwoMusicClient','KugouMusicClient','MiguMusicClient','QQMusicClient','QianqianMusicClient','BilibiliMusicClient','SodaMusicClient']
var QUALITY_OPTIONS=__QUALITY_OPTIONS__
var DOWNLOAD_QUALITY_OPTIONS=__DOWNLOAD_QUALITY_OPTIONS__
var LABELS={quality_default:'在线试听音质',download_quality:'下载音质',max_limit:'搜索返回上限',preview_min_seconds:'试听判定阈值（秒）',url_ttl:'直链有效期（秒）',cache_max_mb:'播放缓存上限（MB）',library_max_mb:'本地曲库上限（MB）',transcode_concurrency:'同时转码数',smart_search:'智能搜索（别名纠错）',tee_save:'边播边存',lyrics_enabled:'启用歌词',lrclib_url:'LRCLIB 地址',lyrics_retry:'歌词重试间隔（秒）',fnos_timeout:'飞牛请求超时（秒）',mcp_timeout:'MCP 上游超时（秒）'}
function el(id){return document.getElementById(id)}
function out(id,t){el(id).textContent=t||''}
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
async function api(url,opt){var r=await fetch(url,opt||{});var d=await r.json().catch(function(){return {}});if(!r.ok)throw new Error((d&&d.error&&d.error.message)||d.error||('HTTP '+r.status));return d}
function post(url,body){return api(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})})}
function put(url,body){return api(url,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})})}

async function loadFnos(){
  try{var d=await api('/api/settings/fnos')
    el('base').value=d.base||'';el('username').value=d.username||'';el('device_id').value=d.device_id||''
    el('insecure_tls').checked=!!d.insecure_tls
    out('fnosOut',d.configured?'状态：已配置'+(d.token_cached?'（已缓存登录）':''):'状态：未配置')
  }catch(e){out('fnosOut','读取失败：'+e.message)}
}
async function saveFnos(){
  out('fnosOut','保存中…')
  try{var d=await put('/api/settings/fnos',{base:el('base').value.trim(),username:el('username').value.trim(),password:el('password').value,device_id:el('device_id').value.trim(),insecure_tls:el('insecure_tls').checked})
    el('password').value='';out('fnosOut',d.configured?'已保存，飞牛音乐已配置':'已保存（尚未配置完整）')
  }catch(e){out('fnosOut','保存失败：'+e.message)}
}

var onlineData=null
function renderPlatforms(chosen,extra){
  var all=PLATFORMS.slice();(chosen||[]).forEach(function(p){if(all.indexOf(p)<0)all.push(p)});(extra||[]).forEach(function(p){if(all.indexOf(p)<0)all.push(p)})
  var panel=el('qrPanel')
  var qrOpen=!!panel&&panel.style.display!=='none'
  if(panel&&panel.parentNode)panel.parentNode.removeChild(panel)
  el('platList').innerHTML=all.map(function(p){
    var on=(chosen||[]).indexOf(p)>=0
    var qr=p==='NeteaseMusicClient'?'<button class="btn small" data-qr="1" type="button">'+(qrOpen?'收起扫码':'扫码登录（推荐）')+'</button>':''
    return '<div class="plat" data-plat-row="'+esc(p)+'"><label class="l"><input type="checkbox" data-plat="'+esc(p)+'"'+(on?' checked':'')+'>'+esc(p)+'</label><div class="r"><span class="test-out" data-out="'+esc(p)+'"></span>'+qr+'<button class="btn small" data-test="'+esc(p)+'" type="button">测试</button></div></div>'
  }).join('')
  el('platList').querySelectorAll('[data-test]').forEach(function(b){b.onclick=function(){testOne(b.getAttribute('data-test'))}})
  el('platList').querySelectorAll('[data-qr]').forEach(function(b){b.onclick=toggleQr})
  var net=el('platList').querySelector('[data-plat-row="NeteaseMusicClient"]')
  if(panel&&net)net.parentNode.insertBefore(panel,net.nextSibling)
}
async function loadOnline(){
  try{var d=await api('/api/settings/online');onlineData=d
    el('online_enabled').checked=!!d.enabled
    el('extra_platforms').value=(d.extra_platforms||[]).join(',')
    el('netease_cookie').value=''
    el('netease_cookie').placeholder=d.netease_cookie_set?'已设置；留空不修改':'未设置；粘贴 MUSIC_U=... 或整段 Cookie'
    el('clear_netease_cookie').checked=false
    renderPlatforms(d.platforms||[],d.extra_platforms||[])
    el('search_limit').value=d.search_limit;el('timeout').value=d.timeout;el('total_limit').value=d.total_limit;el('total_timeout').value=d.total_timeout
    out('onlineOut',d.deps_available?'依赖就绪':'缺少依赖：'+(d.deps_error||''))
  }catch(e){out('onlineOut','读取失败：'+e.message)}
}
function checkedPlatforms(){return Array.prototype.map.call(el('platList').querySelectorAll('input[data-plat]:checked'),function(x){return x.getAttribute('data-plat')})}
async function saveOnline(){
  out('onlineOut','保存中…')
  try{var extra=el('extra_platforms').value.split(',').map(function(x){return x.trim()}).filter(Boolean)
    var d=await put('/api/settings/online',{enabled:el('online_enabled').checked,platforms:checkedPlatforms(),extra_platforms:extra,search_limit:Number(el('search_limit').value),timeout:Number(el('timeout').value),total_limit:Number(el('total_limit').value),total_timeout:Number(el('total_timeout').value),netease_cookie:el('netease_cookie').value,clear_netease_cookie:el('clear_netease_cookie').checked})
    onlineData=d;el('netease_cookie').value='';el('netease_cookie').placeholder=d.netease_cookie_set?'已设置；留空不修改':'未设置；粘贴 MUSIC_U=... 或整段 Cookie';el('clear_netease_cookie').checked=false;renderPlatforms(d.platforms||[],d.extra_platforms||[]);out('onlineOut','已保存，音源已按新配置重建'+(d.netease_cookie_set?'（网易云 Cookie 已设置）':''))
  }catch(e){out('onlineOut','保存失败：'+e.message)}
}
function testSlot(p){
  var nodes=el('platList').querySelectorAll('[data-out]')
  for(var i=0;i<nodes.length;i++){if(nodes[i].getAttribute('data-out')===p)return nodes[i]}
  return null
}
function showTestResult(p,r){
  var s=testSlot(p);if(!s)return
  if(!r){s.textContent='测试中…';s.className='test-out';s.title='';return}
  s.textContent=r.ok?('可用 '+(r.ms||0)+' ms'+(r.count!=null?' · '+r.count+' 条':'')):('失败'+(r.ms?(' '+r.ms+' ms'):''))
  s.className='test-out '+(r.ok?'ok':'bad')
  s.title=r.error||''
}
async function testOne(p){
  showTestResult(p,null)
  try{var d=await post('/api/settings/online/test',{platform:p});showTestResult(p,(d.results||[])[0]||{ok:false,ms:0,error:'没有返回结果'})}
  catch(e){showTestResult(p,{ok:false,ms:0,error:e.message})}
}
async function testAll(){
  out('onlineOut','测试全部平台中…')
  el('platList').querySelectorAll('[data-out]').forEach(function(s){s.textContent='测试中…';s.className='test-out';s.title=''})
  try{var d=await post('/api/settings/online/test',{});var rs=d.results||[];rs.forEach(function(r){showTestResult(r.platform,r)});out('onlineOut',rs.length?'全部测试完成':'没有可测试平台')}
  catch(e){out('onlineOut','测试失败：'+e.message)}
}

function setQrOpen(open){
  var panel=el('qrPanel');if(!panel)return
  panel.style.display=open?'flex':'none'
  el('platList').querySelectorAll('[data-qr]').forEach(function(b){b.textContent=open?'收起扫码':'扫码登录（推荐）'})
}
function toggleQr(){
  var panel=el('qrPanel');if(!panel)return
  var open=panel.style.display==='none'
  setQrOpen(open)
  if(open)startQr();else stopQr()
}
var qrTimer=null
function stopQr(){if(qrTimer){clearInterval(qrTimer);qrTimer=null}}
async function startQr(){
  stopQr();out('qrOut','获取二维码中…');el('qrBox').style.display='none'
  try{
    var d=await post('/api/settings/online/qr',{})
    if(!d.qr_svg)throw new Error('二维码为空')
    el('qrImg').src=d.qr_svg;el('qrBox').style.display='block'
    out('qrOut','请用网易云音乐 App 扫码（二维码约 5 分钟内有效）')
    var deadline=Date.now()+((d.expires_in||300)*1000)
    qrTimer=setInterval(async function(){
      if(Date.now()>deadline){stopQr();out('qrOut','二维码已过期，请重新生成');return}
      try{var s=await api('/api/settings/online/qr?key='+encodeURIComponent(d.key))
        if(s.status==='success'){stopQr();el('qrBox').style.display='none';await loadOnline();out('qrOut','登录成功，网易云 Cookie 已保存')}
        else if(s.status==='expired'){stopQr();out('qrOut','二维码已过期，请重新生成')}
        else out('qrOut',s.message||'等待扫码…')
      }catch(e){out('qrOut','查询失败：'+e.message)}
    },2000)
  }catch(e){out('qrOut','生成失败：'+e.message)}
}
window.addEventListener('beforeunload',stopQr)

async function loadAdvanced(){
  try{var d=await api('/api/settings/advanced');var html=''
    Object.keys(d).forEach(function(k){
      var v=d[k];var label=LABELS[k]||k;var ctl=''
      if(k==='quality_default'||k==='download_quality'){var opts=k==='download_quality'?DOWNLOAD_QUALITY_OPTIONS:QUALITY_OPTIONS;ctl='<select data-adv="'+k+'" data-kind="str">'+opts.map(function(o){return '<option value="'+esc(o.value)+'"'+(o.value===String(v)?' selected':'')+'>'+esc(o.label)+'</option>'}).join('')+'</select>'}
      else if(typeof v==='boolean')ctl='<label class="check"><input type="checkbox" data-adv="'+k+'" data-kind="bool"'+(v?' checked':'')+'> 启用</label>'
      else if(typeof v==='number')ctl='<input type="number" data-adv="'+k+'" data-kind="number" value="'+v+'">'
      else ctl='<input type="text" data-adv="'+k+'" data-kind="str" value="'+esc(v)+'">'
      html+='<div class="field"><label>'+esc(label)+'</label>'+ctl+'</div>'
    })
    el('adv').innerHTML=html
  }catch(e){out('advOut','读取失败：'+e.message)}
}
async function saveAdvanced(){
  out('advOut','保存中…')
  try{var body={}
    el('adv').querySelectorAll('[data-adv]').forEach(function(x){
      var k=x.getAttribute('data-adv'),kind=x.getAttribute('data-kind')
      body[k]=kind==='bool'?x.checked:(kind==='number'?Number(x.value):x.value)
    })
    var d=await put('/api/settings/advanced',body);out('advOut','已保存并生效（'+Object.keys(d).length+' 项）')
  }catch(e){out('advOut','保存失败：'+e.message)}
}

el('save').onclick=saveFnos;el('onlineSave').onclick=saveOnline;el('onlineTest').onclick=testAll;el('qrStart').onclick=startQr;el('advSave').onclick=saveAdvanced
loadFnos();loadOnline();loadAdvanced()
</script>
</body>
</html>
"""

_QUALITY_CHOICES = [
    {
        "value": p["name"],
        "label": (
            "无损（播放 %s MP3）" % p["bitrate"]
            if p.get("download_original")
            else "%s（%s MP3）" % (p["label"], p["bitrate"])
        ),
    }
    for p in describe_quality()
]
_DOWNLOAD_QUALITY_CHOICES = [
    {
        "value": p["name"],
        "label": (
            "无损（保留原始文件）"
            if p.get("download_original")
            else "%s（%s MP3）" % (p["label"], p["bitrate"])
        ),
    }
    for p in describe_quality()
]
SETTINGS_HTML = SETTINGS_HTML.replace("__QUALITY_OPTIONS__", json.dumps(_QUALITY_CHOICES, ensure_ascii=False))
SETTINGS_HTML = SETTINGS_HTML.replace("__DOWNLOAD_QUALITY_OPTIONS__", json.dumps(_DOWNLOAD_QUALITY_CHOICES, ensure_ascii=False))
SETTINGS_HTML = (SETTINGS_HTML.replace("__VERSION__", VERSION)
                 .replace("__THEME_STYLE__", THEME_STYLE)
                 .replace("__THEME_SCRIPT__", THEME_SCRIPT)
                 .replace("__THEME_CONTROL__", THEME_CONTROL))
