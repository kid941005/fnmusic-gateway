# -*- coding: utf-8 -*-
"""网关首页：搜索 / 播放 / 下载 / 状态，同端口原生页面。"""

from . import __version__ as VERSION
from .theme import THEME_CONTROL, THEME_SCRIPT, THEME_STYLE

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
.seg{display:flex;gap:4px;background:var(--input,#0e1526);border:1px solid var(--line);border-radius:9px;padding:3px}
.seg button{border:0;background:transparent;color:var(--mut);border-radius:6px;padding:6px 10px;font:inherit;font-size:13px;cursor:pointer}
.seg button.on{background:var(--acc);color:#fff}
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
__THEME_STYLE__
</style>
__THEME_SCRIPT__
</head>
<body>
<div class="wrap">
<header>
  <div><h1>fnmusic-gateway <span class="pill">v__VERSION__</span></h1><div class="sub">飞牛音乐优先 · 内置在线音源补足</div></div>
  <div>__THEME_CONTROL__ <a class="btn" href="/settings">设置</a> <a class="btn" href="/logout">退出</a></div>
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
  <h2>播放器</h2>
  <audio id="player" controls preload="none" style="width:100%"></audio>
  <div class="row" style="margin-top:10px">
    <span id="now" class="sub">尚未播放</span>
    <a id="rawlink" class="btn" target="_blank" rel="noreferrer" style="display:none">打开直链</a>
  </div>
</div>

<div class="card">
  <h2>搜索歌曲</h2>
  <div class="row">
    <input id="q" placeholder="输入歌名 / 歌手，例如 周杰伦 晴天" autocomplete="off">
    <div class="seg" id="srcSeg" role="group" aria-label="搜索范围">
      <button type="button" class="on" data-src="all">全部</button>
      <button type="button" data-src="local">本地</button>
      <button type="button" data-src="online">在线</button>
    </div>
    <button class="btn primary" id="go">搜索</button>
    <span id="dlq" class="pill">下载音质：-</span>
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
var searchES=null
var searchSource='all'
function stopSearch(){if(searchES){searchES.close();searchES=null}}
function setSource(s){
  searchSource=(s==='local'||s==='online')?s:'all'
  document.querySelectorAll('#srcSeg [data-src]').forEach(function(b){
    b.classList.toggle('on',b.getAttribute('data-src')===searchSource)
  })
}
function sourceQuery(){return '&source='+encodeURIComponent(searchSource)}
function searchStat(local,online){
  if(searchSource==='local')return '（本地 '+local+' 条）'
  if(searchSource==='online')return '（在线 '+online+' 条）'
  return '（本地 '+local+' · 在线 '+online+'）'
}
function emptyText(){
  if(searchSource==='local')return '本地曲库没有搜到结果，请换个关键词。'
  if(searchSource==='online')return '在线音源没有搜到结果，请换个关键词或到设置页检查在线音源。'
  return '没有搜到结果，请换个关键词或到设置页检查在线音源。'
}
function render(rows,fallback){
  var box=el('results')
  if(!rows.length){box.innerHTML='<div class="empty">'+esc(fallback||emptyText())+'</div>';return}
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
async function legacySearch(q){
  try{
    var d=await getJSON('/api/search?q='+encodeURIComponent(q)+'&limit=20'+sourceQuery())
    lastRows=d.tracks||[];render(lastRows)
    var stat=''
    if(typeof d.local_count==='number')stat=searchStat(d.local_count,d.online_count||0)
    if(d.local_error)stat+='，本地搜索失败：'+d.local_error
    say('共 '+d.count+' 条'+stat+(d.corrected?'（已按别名纠正为「'+d.corrected+'」）':''))
  }catch(e){say('搜索失败：'+e.message)}finally{el('go').disabled=false}
}
async function search(){
  var q=el('q').value.trim();if(!q){say('请输入关键词');return}
  stopSearch();lastRows=[];render([], '正在搜索…')
  el('go').disabled=true;say('搜索中…结果会陆续出现，出现即可播放')
  if(!window.EventSource){return legacySearch(q)}
  var got=false,finished=false
  var es=new EventSource('/api/search/stream?q='+encodeURIComponent(q)+'&limit=20'+sourceQuery())
  searchES=es
  es.onmessage=function(ev){
    var d=null;try{d=JSON.parse(ev.data)}catch(e){return}
    if(d.type==='track'&&d.track){
      got=true;lastRows.push(d.track);render(lastRows)
      say('搜索中…已出现 '+lastRows.length+' 条，可直接播放')
    }else if(d.type==='done'){
      finished=true;es.close();if(searchES===es)searchES=null;el('go').disabled=false
      var stat=''
      if(typeof d.local_count==='number')stat=searchStat(d.local_count,d.online_count||0)
      if(d.local_error)stat+=',本地搜索失败：'+d.local_error
      if(d.online_error)stat+=',在线搜索异常：'+d.online_error
      say('共 '+d.count+' 条'+stat)
      if(!lastRows.length)render([])
    }else if(d.type==='error'){
      finished=true;es.close();if(searchES===es)searchES=null;el('go').disabled=false
      say('搜索失败：'+(d.error||'未知错误'))
    }
  }
  es.onerror=function(){
    if(finished)return
    es.close();if(searchES===es)searchES=null
    if(got){el('go').disabled=false;say('搜索已中断，已显示 '+lastRows.length+' 条，可直接播放')}
    else{legacySearch(q)}
  }
}
function errText(e){return (e&&e.message)?e.message:String(e||'未知错误')}
async function play(t,i){
  if(!t)return
  if(!t.guid){say('播放失败：这首歌没有 guid');return}
  var a=el('player'),link=el('rawlink')
  say('正在准备播放 '+t.title+'…（首次播放需要缓存/转码，可能要几秒）')
  var t0=Date.now(),timer=setInterval(function(){say('正在准备播放 '+t.title+'…已等 '+Math.round((Date.now()-t0)/1000)+' 秒')},1000)
  try{
    var d=await getJSON('/api/play?guid='+encodeURIComponent(t.guid)+'&title='+encodeURIComponent(t.title||'')+'&artist='+encodeURIComponent(t.artist||'')+'&quality=standard')
    clearInterval(timer)
    var u=d.web_url||d.url
    a.pause();a.src=u;a.load()
    link.href=u;link.style.display='inline-block'
    el('now').textContent='已就绪：'+(d.title||t.title)+(d.artist?(' - '+d.artist):'')+' · '+(d.quality_label||d.quality||'')+(d.preview?'（试听）':'')
    var played=a.play()
    if(played&&played.then){
      played.then(function(){say('正在播放：'+(d.title||t.title))}).catch(function(e){say('直链已生成，但浏览器拦截了自动播放。请点击上方播放器播放，或点「打开直链」。'+errText(e))})
    }else{say('已就绪：'+(d.title||t.title))}
  }catch(e){clearInterval(timer);say('播放失败：'+errText(e))}
}
function fmtSize(n){var b=Number(n||0);if(b>=1048576)return (b/1048576).toFixed(1)+' MB';if(b>=1024)return Math.round(b/1024)+' KB';return b+' B'}
var dlqLabel=''
async function loadQuality(){
  try{
    var d=await getJSON('/api/quality')
    var name=d.download||''
    var p=(d.profiles||[]).filter(function(x){return x.name===name})[0]||{}
    dlqLabel=p.download_original?'无损（保留原始文件）':((p.label||name||'按设置')+'（'+(p.bitrate||'')+' MP3）')
    el('dlq').textContent='下载音质：'+dlqLabel
  }catch(e){el('dlq').textContent='下载音质：按设置'}
}
async function download(t){
  if(!t)return;var q=dlqLabel||'按设置';say('正在保存 '+t.title+'…（下载音质：'+q+'）')
  try{var r=await fetch('/api/download',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({guid:t.guid,title:t.title||'',artist:t.artist||''})})
    var d=await r.json();if(!r.ok)throw new Error(d.error||('HTTP '+r.status))
    say('已保存：'+d.path+'（音质 '+(d.quality_label||d.quality||q)+'，'+fmtSize(d.bytes)+'）')}catch(e){say('下载失败：'+e.message)}
}
el('go').onclick=search;el('q').addEventListener('keydown',function(e){if(e.key==='Enter')search()})
document.querySelectorAll('#srcSeg [data-src]').forEach(function(b){
  b.onclick=function(){var s=b.getAttribute('data-src');if(s===searchSource)return;setSource(s);if(el('q').value.trim())search()}
})
var params=new URLSearchParams(location.search)
var initialSrc=params.get('source');if(initialSrc)setSource(initialSrc)
var initialQ=params.get('q');if(initialQ){el('q').value=initialQ;search()}
loadStatus();loadQuality()
</script>
</body>
</html>
"""

HOME_HTML = (HOME_HTML.replace("__VERSION__", VERSION)
             .replace("__THEME_STYLE__", THEME_STYLE)
             .replace("__THEME_SCRIPT__", THEME_SCRIPT)
             .replace("__THEME_CONTROL__", THEME_CONTROL))
