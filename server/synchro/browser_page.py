"""Explicit, per-page YouTube connector for browsers without an extension.

HTTPS pages cannot fetch an HTTP LAN service reliably. A user-opened loopback
popup instead relays messages with a nonce, exact origins and WindowProxy
identity. It receives a capability for one page only, never the phone pairing
key. All URLs here are local UI routes; video URLs and credentials aren't logs.
"""
import json
from urllib.parse import quote, urlsplit


def bookmarklet(base_url):
    parsed = urlsplit(base_url)
    if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost') or parsed.username or parsed.password:
        raise ValueError('The page connector must use a loopback HTTP address')
    origin = f'http://{parsed.netloc}'
    source = r'''(() => {
      const service = SERVICE;
      const origins = ['https://youtube.com','https://www.youtube.com','https://m.youtube.com'];
      if (!origins.includes(location.origin)) { alert('Откройте нужный ролик YouTube, затем нажмите закладку «Synchro».'); return; }
      const id = () => {
        const u = new URL(location.href), p = u.pathname.split('/').filter(Boolean);
        const value = u.pathname === '/watch' ? u.searchParams.get('v') : ['shorts','live','embed'].includes(p[0]) ? p[1] : '';
        return /^[A-Za-z0-9_-]{11}$/.test(value || '') ? value : null;
      };
      if (!id() || !document.querySelector('video')) { alert('Сначала откройте ролик YouTube.'); return; }
      if (window.__synchroPageConnection) window.__synchroPageConnection();
      const bytes = crypto.getRandomValues(new Uint8Array(24));
      const nonce = Array.from(bytes, b => b.toString(16).padStart(2,'0')).join('');
      const popup = window.open(service + '/browser/connect#nonce=' + nonce, 'synchro-' + nonce, 'popup,width=480,height=660');
      if (!popup) { alert('Разрешите всплывающее окно для подключения «Synchro».'); return; }
      let timer, disposed = false;
      const executing=new Set(),completed=new Map();
      const player = () => document.getElementById('movie_player');
      const canCue = p => p && ['cueVideoById','getVideoData','getPlayerState','pauseVideo','mute','isMuted','unMute'].every(k=>typeof p[k]==='function');
      const state = () => {
        const v = document.querySelector('video');
        if (!v || !id()) return null;
        const p=player();
        let d=null;
        try{if(p&&typeof p.getVideoData==='function')d=p.getVideoData();}catch(_){}
        const identifier=d&&/^[A-Za-z0-9_-]{11}$/.test(d.video_id||'')?d.video_id:id();
        const title=d&&typeof d.title==='string'?d.title:document.title.replace(/\s*[-–—]\s*YouTube\s*$/i,'');
        return {videoId:identifier,title:title.slice(0,500),
          positionMs:Math.max(0,Math.round((v.currentTime || 0)*1000)),
          durationMs:Number.isFinite(v.duration) ? Math.max(0,Math.round(v.duration*1000)) : 0,
          paused:v.paused || v.ended,rate:v.playbackRate,canChangeVideo:!!canCue(p)};
      };
      const send = (ack=null,error=false) => {
        const video = state();
        if (!disposed && video && !popup.closed) popup.postMessage({type:'synchro:state',nonce,video,ack,error},service);
      };
      const dispose = () => { disposed=true; clearInterval(timer); removeEventListener('message',receive); };
      const receive = async e => {
        const d = e.data;
        if (e.source !== popup || e.origin !== service || !d || d.nonce !== nonce) return;
        if (d.type === 'synchro:disconnect') { dispose(); return; }
        if (d.type === 'synchro:hello') { send(); return; }
        if (d.type !== 'synchro:command' || !d.command || typeof d.command.id !== 'string') return;
        const c=d.command, v=document.querySelector('video');
        if(completed.has(c.id)){send(c.id,completed.get(c.id));return;}
        if(executing.has(c.id))return;
        executing.add(c.id);
        let failed=false;
        try {
          if (!v || !id()) throw Error();
          if(c.expected){
            const current=state(),expected=c.expected;
            if(!current||!current.paused||current.videoId!==expected.videoId||Math.abs(current.positionMs-expected.positionMs)>1500)throw Error('pc_changed');
          }
          if (c.action === 'pause') v.pause();
          else if (c.action === 'play') await v.play();
          else if (c.action === 'seek') {
            if (!Number.isInteger(c.positionMs) || c.positionMs<0) throw Error();
            v.pause();
            if (typeof c.rate === 'number' && c.rate>=.25 && c.rate<=4) v.playbackRate=c.rate;
            v.currentTime=Math.min(Number.isFinite(v.duration)?v.duration:c.positionMs/1000,c.positionMs/1000);
          } else if (c.action === 'open') {
            const p=player();
            if(!canCue(p)||!/^[A-Za-z0-9_-]{11}$/.test(c.videoId||'')||!Number.isInteger(c.positionMs)||c.positionMs<0)throw Error();
            const wasMuted=p.isMuted();
            v.pause();p.mute();
            let verified=false;
            try {
              // Feature-detected YouTube player API. cueVideoById must leave
              // the existing player unstarted/paused; no navigation or new stream.
              p.cueVideoById({videoId:c.videoId,startSeconds:c.positionMs/1000});
              const deadline=Date.now()+4500;
              while(Date.now()<deadline){
                p.pauseVideo();
                const d=p.getVideoData(),s=p.getPlayerState();
                if(d&&d.video_id===c.videoId&&[-1,2,5].includes(s)){verified=true;break;}
                await new Promise(resolve=>setTimeout(resolve,70));
              }
              if(!verified)throw Error();
              const current=document.querySelector('video');
              if(current){current.pause();if(Number.isFinite(c.rate)&&c.rate>=.25&&c.rate<=4)current.playbackRate=c.rate;}
            } finally {
              p.pauseVideo();
              if(verified&&!wasMuted)p.unMute();
            }
          } else throw Error();
        } catch (error) { failed=error.message==='pc_changed'?'pc_changed':true; }
        executing.delete(c.id);completed.set(c.id,failed);
        if(completed.size>32)completed.delete(completed.keys().next().value);
        send(c.id,failed);
      };
      addEventListener('message',receive);
      timer=setInterval(() => { if (popup.closed) dispose(); else send(); },700);
      window.__synchroPageConnection=dispose;
      send();
    })();'''.replace('SERVICE', json.dumps(origin))
    # A bookmark URL must not contain literal line breaks or HTML-sensitive text.
    return 'javascript:' + quote(source, safe="()!~*'-._")


def connection_page():
    return r'''<!doctype html>
<html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>Synchro · YouTube</title>
<style>
:root{color-scheme:dark;font:15px/1.55 system-ui,sans-serif;background:#0e1013;color:#f8f8fa}
*{box-sizing:border-box}body{margin:0;padding:32px 26px;max-width:540px}header{display:flex;gap:12px;align-items:center;font-weight:750;letter-spacing:-.4px;font-size:22px}
.mark{display:grid;place-items:center;background:#f9c44e;color:#131414;width:42px;height:42px;border-radius:15px;font-size:28px}
.eyebrow{font-size:11px;letter-spacing:2.2px;color:#f9c44e;font-weight:700;margin-top:42px}h1{font-size:30px;line-height:1.15;letter-spacing:-1px;margin:12px 0 18px}p{color:#a9adb6;margin:0 0 22px}
.card{background:#191c21;border:1px solid #2a2e36;border-radius:22px;padding:23px;margin:24px 0}.title{font-size:19px;line-height:1.35;font-weight:650;overflow-wrap:anywhere}.origin{font-size:12px;color:#a9adb6;margin-top:10px}
button{border:0;border-radius:14px;padding:16px 20px;font:650 15px system-ui;cursor:pointer;width:100%;background:#f9c44e;color:#121315}button:disabled{opacity:.35;cursor:default}.secondary{background:#242830;color:#d3d6df;margin-top:12px}
#status{font-size:14px;margin:18px 0;color:#c6cad2}.dot{display:inline-block;width:7px;height:7px;background:#f9c44e;border-radius:50%;margin-right:8px}.foot{font-size:12px;color:#777e8d;margin-top:28px}a{color:#f9c44e}button:focus-visible{outline:3px solid white;outline-offset:3px}
</style>
<header><span class="mark">⇄</span>Synchro<span style="margin-left:auto;color:#707784;font-size:12px">YOUTUBE</span></header>
<div class="eyebrow">ОДНА СТРАНИЦА. ВАШ ВЫБОР.</div><h1 id="heading">Продолжите<br>на телефоне.</h1>
<p id="intro">Подключите выбранный ролик. Synchro сможет ставить его на паузу и возвращать позицию просмотра.</p>
<div class="card"><div class="title" id="title">Ожидаем выбранную страницу…</div><div class="origin" id="origin"></div></div>
<button id="connect" disabled>Подключить эту страницу</button><button class="secondary" id="disconnect" hidden>Отключить страницу</button>
<div id="status" role="status" aria-live="polite"><span class="dot"></span>Проверяем соединение</div>
<div class="foot">Это окно связывает только выбранную страницу YouTube. Доступ к другим вкладкам, истории и аккаунту не требуется. Оставьте окно открытым — его можно свернуть.</div>
<script>
(() => {
  const allowed=new Set(['https://youtube.com','https://www.youtube.com','https://m.youtube.com']);
  const nonce=new URLSearchParams(location.hash.slice(1)).get('nonce');
  history.replaceState(null,'',location.pathname);
  const $=id=>document.getElementById(id);
  const opener=window.opener;
  let pageOrigin=null,video=null,bridge=null,ack=null,error=false,lastMessage=0,busy=false,stopped=false;
  function status(text){$('status').textContent=text;}
  function valid(v){return v && /^[A-Za-z0-9_-]{11}$/.test(v.videoId||'') && typeof v.title==='string' && v.title.length<=500 && Number.isInteger(v.positionMs) && v.positionMs>=0 && Number.isInteger(v.durationMs) && v.durationMs>=0 && typeof v.paused==='boolean' && Number.isFinite(v.rate) && v.rate>=.25 && v.rate<=4;}
  function send(type,extra={}){if(pageOrigin&&opener)opener.postMessage({type,nonce,...extra},pageOrigin);}
  async function api(path,data){const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data),signal:AbortSignal.timeout(10000)});const body=await response.json();if(!response.ok)throw Error(body.error||'Не удалось подключиться');return body;}
  if(!opener || !/^[A-Za-z0-9_-]{24,128}$/.test(nonce||'')){status('Откройте нужный ролик YouTube и нажмите закладку «Synchro».');return;}
  addEventListener('message',event=>{
    const d=event.data;
    if(event.source!==opener || !allowed.has(event.origin) || !d || d.nonce!==nonce || d.type!=='synchro:state' || !valid(d.video))return;
    if(pageOrigin && pageOrigin!==event.origin)return;
    pageOrigin=event.origin;video=d.video;lastMessage=Date.now();
    $('title').textContent=video.title||'YouTube';$('origin').textContent=pageOrigin;
    if(typeof d.ack==='string' && d.ack.length<=64){ack=d.ack;error=d.error==='pc_changed'?'pc_changed':!!d.error;}
    if(!bridge){$('connect').disabled=false;status('Страница проверена. Подтвердите подключение.');}
  });
  $('connect').onclick=async()=>{
    if(bridge || !video || !pageOrigin)return;
    $('connect').disabled=true;
    try{
      bridge=await api('/local/browser/register',{nonce,origin:pageOrigin,video});
      $('connect').hidden=true;$('disconnect').hidden=false;$('heading').textContent='YouTube подключён.';
      $('intro').textContent='Выберите этот ролик в приложении Synchro на телефоне. При переходе компьютер останется на паузе.';
      status('Связь установлена · окно можно свернуть');send('synchro:hello');void poll();
    }catch(e){status(e.message);$('connect').disabled=false;}
  };
  $('disconnect').onclick=async()=>{
    stopped=true;send('synchro:disconnect');
    try{if(bridge)await api('/local/browser/disconnect',bridge);}catch(_){}
    bridge=null;$('disconnect').hidden=true;status('Страница отключена. Это окно можно закрыть.');
  };
  async function poll(){
    if(stopped || !bridge || busy || !video)return;
    if(Date.now()-lastMessage>5000){status('Страница YouTube недоступна. Вернитесь к ролику и нажмите закладку снова.');return;}
    busy=true;
    const sentAck=ack;
    try{
      // Network-driven long polling also works when background tabs throttle
      // chained JavaScript timers to once a minute. Each reply requests a fresh
      // state through postMessage rather than trusting a stale cached position.
      const response=await api('/local/browser/poll',{...bridge,video,ack:sentAck,error,waitMs:800});
      if(ack===sentAck){ack=null;error=false;}
      send('synchro:hello');
      for(const command of response.commands||[])send('synchro:command',{command});
      status('Связь установлена · окно можно свернуть');
    }catch(e){status(e.message);await new Promise(resolve=>setTimeout(resolve,1000));}
    finally{busy=false;}
    if(bridge&&!stopped)void poll();
  }
  setTimeout(()=>{if(!video)status('Не получили ответ страницы. Разрешите всплывающие окна и нажмите закладку «Synchro» снова.');},6000);
})();
</script></html>'''.encode('utf-8')
