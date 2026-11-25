# -*- coding: utf-8 -*-
"""WebUI 主题：浅色 / 深色 / 跟随系统，默认跟随系统。"""

THEME_STYLE = """
:root{--text:#e8eefc;--input:#0e1526;--btn:#1b2540;--link:#8db4ff;--pill:#1b2540}
:root[data-theme="light"]{color-scheme:light;--bg:#f4f6fb;--card:#ffffff;--line:#d9e1f0;--mut:#5f6f8f;--acc:#2f6fe4;--ok:#12805c;--bad:#c0392b;--text:#1a2338;--input:#ffffff;--btn:#eef2f9;--link:#2f6fe4;--pill:#eef2f9}
[data-theme="light"] body{color:var(--text)}
[data-theme="light"] .btn:not(.primary){background:var(--btn);color:var(--text)}
[data-theme="light"] input:not([type=checkbox]),[data-theme="light"] select,[data-theme="light"] textarea{background:var(--input);color:var(--text)}
[data-theme="light"] .kv,[data-theme="light"] .pill:not(.ok):not(.bad){background:var(--pill)}
[data-theme="light"] a{color:var(--link)}
[data-theme="light"] .check{color:var(--text)}
.theme{display:inline-flex;align-items:center;gap:6px;color:var(--mut);font-size:12.5px;white-space:nowrap}
.theme select{background:var(--btn);border:1px solid var(--line);color:var(--text);border-radius:9px;padding:6px 8px;font:inherit;cursor:pointer}
.theme-fixed{position:fixed;top:14px;right:16px;z-index:9}
"""

THEME_SCRIPT = """
<script>
(function(){
  var KEY='fnmusic-theme';
  function pref(){try{var v=localStorage.getItem(KEY);return v==='light'||v==='dark'?v:'system'}catch(e){return 'system'}}
  function systemDark(){try{return window.matchMedia('(prefers-color-scheme: dark)').matches}catch(e){return true}}
  function apply(v){
    var dark=(v==='dark')||(v==='system'&&systemDark());
    var root=document.documentElement;
    root.setAttribute('data-theme',dark?'dark':'light');
    root.style.colorScheme=dark?'dark':'light';
    var sel=document.getElementById('themeSel');if(sel&&sel.value!==v)sel.value=v;
  }
  window.__fnTheme={get:pref,apply:apply,set:function(v){try{localStorage.setItem(KEY,v)}catch(e){}apply(v)}};
  apply(pref());
  try{
    var mq=window.matchMedia('(prefers-color-scheme: dark)');
    var onChange=function(){if(pref()==='system')apply('system')};
    if(mq.addEventListener)mq.addEventListener('change',onChange);
    else if(mq.addListener)mq.addListener(onChange);
  }catch(e){}
  document.addEventListener('DOMContentLoaded',function(){
    var sel=document.getElementById('themeSel');if(!sel)return;
    sel.value=pref();
    sel.addEventListener('change',function(){window.__fnTheme.set(sel.value)});
  });
})();
</script>
"""

THEME_CONTROL = """<label class="theme">主题
  <select id="themeSel" aria-label="主题">
    <option value="system">跟随系统</option>
    <option value="light">浅色</option>
    <option value="dark">深色</option>
  </select>
</label>"""
