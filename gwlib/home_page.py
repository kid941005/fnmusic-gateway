# -*- coding: utf-8 -*-
"""网关首页：搜索 / 播放 / 下载 / 状态，同端口原生页面。"""

from . import __version__ as VERSION

HOME_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>fnmusic-gateway v__VERSION__ · 音乐管理台</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--card:#141b2d;--line:#263149;--mut:#93a4c4;--acc:#4f8cff;--ok:#37c98a;--bad:#ff6b6b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#e8eefc;font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:22px 18px 48px}
header{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:18px}
h1{font-size:20px;margin:0}.sub{color:var(--mut);font-size:13px}
a{color:#8db4ff;text-decoration:none}.btn{border:1px solid var(--line);background:#1b2540;color:#e8eefc;border-radius:9px;padding:9px 14px;cursor:pointer;font:inherit}
.btn.primary{background:var(--acc);border-color:var(--acc);color:#fff}.btn:disabled{opacity:.45;cursor:not-allowed}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;margin-bottom:16px}
.card h2{font-size:16px;margin:0 0 12px}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
input{background:#0e1526;border:1px solid var(--line);color:#e8eefc;border-radius:9px;padding:10px 12px;font:inherit;min-width:0}
#q{flex:1;min-width:220px}
.pill{display:inline-block;padding:3px 9px;border-radius:999px;background:#1b2540;color:var(--mut);font-size:12px;margin-left:6px}
.pill.ok{background:rgba(55,201,138,.15);color:var(--ok)}.pill.bad{background:rgba(255,107,107,.15);color:var(--bad)}
.track{display:flex;justify-content:space-between;gap:12px;align-items:center;padding:12px 0;border-top:1px solid var(--line)}
.track:first-child{border-top:0}.t-title{font-weight:600}.t-meta{color:var(--mut);font-size:12.5px}
.actions{display:flex;gap:8px;flex-shrink:0}.actions .btn{padding:6px 11px;font-size:13px}
#msg{min-height:22px;color:var(--mut);font-size:13px;white-space:pre-wrap}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
.kv{background:#0e1526;border:1px solid var(--line);border-radius:10px;padding:12px}.kv b{display:block;font-size:12px;color:var(--mut);font-weight:500;margin-bottom:3px}
.empty{color:var(--mut);padding:16px 0}
@media(max-width:620px){.track{align-items:flex-start;flex-direction:column}.actions{width:100%}}
</style>
</head>
<body>
<div class="wrap">
<header>
  <div><h1>fnmusic-gateway <span class="pill">v__VERSION__</span></h1><div class="sub">飞牛音乐优先 · 内置在线音源补足</div></div>
  <div><a class="btn" href="/settings">设置</a> <a class="btn" href="/logout">退出</a></div>
</header>

<div class="card">
  <h2>运行状态 <span id="ver" class="pill">v__VERSION__</span></h2>
  <div class="grid">
    <div class="kv"><b>模式</b><span id="mode">-</span></div>
    <div class="kv"><b>飞牛音乐</b><span id="fnos">-</span></div>
    <div class="kv"><b>在线音源</b><span id="online">-</span></div>
    <div class="kv"><b>播放缓存</b><span id="cache">-</span></div>
  </div>
</div>

<div class="card">
  <h2>搜索歌曲</h2>
  <div class="row">
    <input id="q" placeholder="输入歌名 / 歌手，例如 周杰伦 晴天" autocomplete="off">
    <button class="btn primary" id="go">搜索</button>
  </div>
  <div id="msg"></div>
  <div id="results"><div class="empty">输入关键词开始搜索；本地曲库优先，在线音源补足。</div></div>
</div>
</div>
<script>
function el(id){return document.getElementById(id)}
function say(t){el('msg').textContent=t||''}
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
async function getJSON(url){var r=await fetch(url,{headers:{'Accept':'application/json'}});var d=await r.json().catch(function(){return {}});if(!r.ok)throw new Error((d&&d.error&&d.error.message)||d.error||('HTTP '+r.status));return d}
function dur(sec){var n=Number(sec||0);if(!n)return '';var m=Math.floor(n/60),s=Math.floor(n%60);return m+':'+String(s).padStart(2,'0')}
async function loadStatus(){
  try{
    var h=await getJSON('/healthz')
    el('ver').textContent='v'+h.version
    el('mode').textContent=h.mode||'-'
    var f=h.fnos||{}
    el('fnos').innerHTML=f.configured?'已连接 <span class="pill ok">本地优先</span>':'<span class="pill bad">未配置</span>'
    var o=h.online||{}
    el('online').innerHTML=(o.enabled&&(o.platforms||[]).length)?(esc((o.platforms||[]).join(', '))+' <span class="pill '+(o.deps?'ok':'bad')+'">'+(o.deps?'可用':'缺少依赖')+'</span>'):'<span class="pill bad">未启用</span>'
    var s=await getJSON('/api/storage');var c=s.cache||{}
    el('cache').textContent=(c.files||0)+' 个文件 / '+(Math.round((c.bytes||0)/1048576*10)/10)+' MB'
  }catch(e){el('ver').textContent='未连接';say('状态读取失败：'+e.message)}
}
var lastRows=[]
function render(rows,q){
  var box=el('results')
  if(!rows.length){box.innerHTML='<div class="empty">没有搜到结果，请换个关键词或到设置页检查在线音源。</div>';return}
  box.innerHTML=rows.map(function(t,i){
    var src=t.source==='local'?'本地':(t.platform||t.source||'在线')
    var meta=[src,t.artist,t.album].filter(Boolean).join(' · ')+(t.duration_s?(' · '+dur(t.duration_s)):'')
    return '<div class="track"><div><div class="t-title">'+esc(t.title||t.guid)+'</div><div class="t-meta">'+esc(meta)+'</div></div>'+
      '<div class="actions"><button class="btn primary" data-play="'+i+'">播放</button>'+
      '<button class="btn" data-dl="'+i+'">下载</button></div></div>'
  }).join('')
  box.querySelectorAll('[data-play]').forEach(function(b){b.onclick=function(){var i=Number(b.getAttribute('data-play'));play(lastRows[i],i)}})
  box.querySelectorAll('[data-dl]').forEach(function(b){b.onclick=function(){download(lastRows[Number(b.getAttribute('data-dl'))])}})
}
async function search(){
  var q=el('q').value.trim();if(!q){say('请输入关键词');return}
  el('go').disabled=true;say('搜索中…（在线音源首次启动会慢一些）')
  try{
    var d=await getJSON('/api/search?q='+encodeURIComponent(q)+'&limit=20')
    lastRows=d.tracks||[];render(lastRows,q)
    var stat=''
    if(typeof d.local_count==='number')stat='（本地 '+d.local_count+' · 在线 '+(d.online_count||0)+'）'
    if(d.local_error)stat+='，本地搜索失败：'+d.local_error
    say('共 '+d.count+' 条'+stat+(d.corrected?'（已按别名纠正为「'+d.corrected+'」）':''))
  }catch(e){say('搜索失败：'+e.message)}finally{el('go').disabled=false}
}
async function play(t,i){
  if(!t)return;say('准备播放 '+t.title+'…')
  try{var d=await getJSON('/api/play?guid='+encodeURIComponent(t.guid||'')+'&title='+encodeURIComponent(t.title||'')+'&artist='+encodeURIComponent(t.artist||'')+'&quality=standard')
    var a=new Audio(d.url);a.play();say('正在播放：'+t.title+'\\n直链：'+d.url)}catch(e){say('播放失败：'+e.message)}
}
async function download(t){
  if(!t)return;say('正在保存 '+t.title+'…')
  try{var r=await fetch('/api/download',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({guid:t.guid,title:t.title||'',artist:t.artist||''})})
    var d=await r.json();if(!r.ok)throw new Error(d.error||('HTTP '+r.status));say('已保存：'+d.path+'（'+Math.round(d.bytes/1024)+' KB）')}catch(e){say('下载失败：'+e.message)}
}
el('go').onclick=search;el('q').addEventListener('keydown',function(e){if(e.key==='Enter')search()})
loadStatus()
</script>
</body>
</html>
"""

HOME_HTML = HOME_HTML.replace("__VERSION__", VERSION)
