import {linkPayload} from './protocol.js';

const base = 'http://127.0.0.1:8765';
const ui = Object.fromEntries(['title','domain','status','send','connect','card','badge'].map(id=>[id,document.getElementById(id)]));
let selected, token, sending = false, requestId = crypto.randomUUID();
function status(message, error = false) {ui.status.textContent=message;ui.status.dataset.error=String(error);}
async function request(path, body, authenticated = true) {
  const headers = {'Content-Type':'application/json','X-Synchro-Extension':chrome.runtime.id};
  if (authenticated) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(base+path,{method:'POST',headers,body:JSON.stringify(body),signal:AbortSignal.timeout(6000)});
  if (!response.ok) throw new Error(response.status === 401 ? 'Подключитесь к ПК заново' : 'Запустите сервис «Synchro» на ПК');
  return response.json();
}
async function pair() {
  const result = await request('/api/extension/connect',{},false);
  token=result.token;
  await chrome.storage.local.set({token});
  ui.badge.textContent='Подключено';
  status('Готово. Отправьте вкладку кнопкой или свайпом вправо.');
}
async function send() {
  if (sending || !selected) return;
  sending=true;ui.send.disabled=true;
  try {
    if (!token) await pair();
    await request('/api/links',linkPayload(selected,requestId));
    status('Отправлено. Откройте «Вкладки» в Synchro на телефоне.');
    ui.send.textContent='Отправлено ✓';
  } catch(error) {status(error.message,true);ui.send.disabled=false;}
  finally {sending=false;}
}
ui.send.addEventListener('click',send);
ui.connect.addEventListener('click',()=>pair().catch(error=>status(error.message,true)));
let down;
ui.card.addEventListener('pointerdown',event=>{down=[event.clientX,event.clientY];ui.card.setPointerCapture(event.pointerId);});
ui.card.addEventListener('pointerup',event=>{if(down && event.clientX-down[0]>65 && Math.abs(event.clientY-down[1])<60)send();down=null;});
try {
  token=(await chrome.storage.local.get('token')).token;
  [selected]=await chrome.tabs.query({active:true,lastFocusedWindow:true});
  const payload=linkPayload(selected,requestId);
  ui.title.textContent=payload.title || new URL(payload.url).hostname;
  ui.domain.textContent=new URL(payload.url).hostname;
  ui.send.disabled=false;
} catch(error) {status(error.message,true);ui.title.textContent='Выберите обычную веб-страницу';}
