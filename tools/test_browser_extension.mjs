/** Opt-in native Chromium check with synthetic video and an isolated profile/server. */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createRequire} from 'node:module';
import {setTimeout as sleep} from 'node:timers/promises';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(import.meta.url);
const {chromium} = require(path.join(root, '.tools/browser-tests/node_modules/playwright'));
const directory = path.join(root, '.data/browser-native-' + crypto.randomUUID());
const extension = path.join(directory, 'extension'), base = 'http://127.0.0.1:18766';
await fs.mkdir(extension, {recursive:true});
for (const name of await fs.readdir(path.join(root, 'chrome-extension'))) {
  if (!/\.(js|html|css|png|svg)$/.test(name) && name !== 'manifest.json') continue;
  let bytes = await fs.readFile(path.join(root, 'chrome-extension', name));
  if (name.endsWith('.js')) bytes = Buffer.from(bytes.toString().replaceAll('http://127.0.0.1:8765', base));
  // A popup opened as a test tab does not receive the toolbar's activeTab grant.
  // Grant only the synthetic ordinary host in this disposable test manifest.
  if (name === 'manifest.json') {
    const manifest = JSON.parse(bytes); manifest.host_permissions.push('https://example.test/*');
    bytes = Buffer.from(JSON.stringify(manifest));
  }
  await fs.writeFile(path.join(extension, name), bytes);
}
const token = 'synthetic-browser-fixture-only';
async function api(route, body) {
  const response = await fetch(base + route, {method:body ? 'POST':'GET', headers:{Authorization:'Bearer ' + token, 'Content-Type':'application/json'}, body:body ? JSON.stringify(body):undefined});
  const value = await response.json();
  assert.equal(response.ok, true, JSON.stringify(value));
  return value;
}
async function until(predicate, timeout=12000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) { const value = await predicate(); if (value) return value; await sleep(100); }
  throw Error('Native browser scenario timed out');
}
const video = (await fs.readFile(path.join(root, '.data/browser-fixture/sample.webm'))).toString('base64');
await api('/test/reset', {});
const html = `<!doctype html><title>Native synthetic clip - YouTube</title><main><div id="movie_player"><video muted loop src="data:video/webm;base64,${video}"></video></div></main><script>
const v=document.querySelector('video'), p=document.getElementById('movie_player');
let metadata={video_id:new URL(location.href).searchParams.get('v'),title:'Native synthetic clip'};
p.getVideoData=()=>metadata;p.getPlayerState=()=>v.paused?2:1;p.pauseVideo=()=>v.pause();p.isMuted=()=>v.muted;p.mute=()=>v.muted=true;p.unMute=()=>v.muted=false;
p.cueVideoById=next=>{v.pause();metadata={video_id:next.videoId,title:'Next synthetic clip'};v.currentTime=next.startSeconds;};
</script>`;
const context = await chromium.launchPersistentContext(path.join(directory, 'profile'), {
  channel:'chromium', headless:true, args:[`--disable-extensions-except=${extension}`, `--load-extension=${extension}`]});
const failures = [];
const networkErrors = [];
context.on('response', response=>{if(response.url().startsWith(base)&&response.status()>=400)void response.text().then(body=>networkErrors.push({status:response.status(),path:new URL(response.url()).pathname,body}));});
try {
  await context.route('https://www.youtube.com/**', route=>route.fulfill({status:200,contentType:'text/html',body:html}));
  const page = await context.newPage();
  await page.goto('https://www.youtube.com/watch?v=abcdefghijk');
  await page.locator('video').evaluate(async value=>{await value.play();value.currentTime=12;value.volume=.4;});
  let worker = context.serviceWorkers()[0] || await context.waitForEvent('serviceworker');
  worker.on('console', message=>{if(message.type()==='error')failures.push(message.text());});
  const source = await until(async()=>{const data=await api('/api/players');return data.players.find(value=>value.media?.videoId==='abcdefghijk');});
  assert.equal(source.ready,true);
  const first = await api('/api/youtube/takeover',{sourceId:source.id});
  assert.equal(first.owner,'android');
  assert.equal(await page.locator('video').evaluate(value=>value.paused),true);
  await api('/api/youtube/progress',{sessionId:first.sessionId,sequence:1,paused:true,positionMs:50000,rate:1.25});
  await until(()=>page.locator('video').evaluate(value=>value.paused&&Math.abs(value.currentTime-50)<.5&&value.playbackRate===1.25));
  assert.equal(await page.locator('video').evaluate(value=>value.volume),.4);

  // New YouTube video within the same document retains the source identity.
  await page.evaluate(()=>{history.pushState(null,'','/watch?v=lmnopqrstuv');document.getElementById('movie_player').cueVideoById({videoId:'lmnopqrstuv',startSeconds:20});});
  const next = await until(async()=>{const data=await api('/api/players');return data.players.find(value=>value.media?.videoId==='lmnopqrstuv');});
  assert.equal(next.id,source.id);
  const second = await api('/api/youtube/takeover',{sourceId:next.id});
  await api('/api/youtube/progress',{sessionId:second.sessionId,sequence:1,paused:true,videoId:'abcdefghijk',positionMs:60000,rate:1});
  await until(()=>page.locator('video').evaluate(value=>value.paused&&Math.abs(value.currentTime-60)<.5));

  // A reload creates another document binding; old commands cannot target it.
  await page.reload();
  const replaced = await until(async()=>{const data=await api('/api/players');return data.players.find(value=>value.id!==source.id);});
  assert.notEqual(replaced.id,source.id);
  const foreign = await context.newPage();
  await context.route('https://example.test/**', route=>route.fulfill({status:200,contentType:'text/html',body:'<title>Ordinary synthetic tab</title>'}));
  await foreign.goto('https://example.test/share?q=1#part');
  const extensionId = worker.url().split('/')[2];
  const popup = await context.newPage();
  await popup.goto(`chrome-extension://${extensionId}/popup.html`);
  const targetId = await worker.evaluate(async()=> (await chrome.tabs.query({url:'https://example.test/*'}))[0].id);
  await worker.evaluate(id=>chrome.tabs.update(id,{active:true}),targetId);
  await popup.reload();
  await until(async()=>!await popup.locator('#send').isDisabled());
  await popup.locator('#send').click();
  await until(async()=> (await api('/test/links')).links.length===1);
  assert.equal((await api('/test/links')).links[0].url,'https://example.test/share?q=1#part');
  await popup.screenshot({path:path.join(directory,'popup.png')});
  const setup = await context.newPage();
  await setup.goto(base + '/setup?section=browser');
  await until(async()=> (await setup.locator('#browserStatus').textContent())==='Браузер подключён');
  assert.equal(await setup.locator('#extensionFolder').textContent() !== '',true);
  await setup.setViewportSize({width:1440,height:1040});
  await setup.screenshot({path:path.join(directory,'browser-setup.png'),fullPage:true});
  await setup.close();
  const soak = Number(process.env.SYNCHRO_BROWSER_SOAK_MS || 0);
  if (soak > 0) {
    // Observe through HTTP only: do not wake the worker with evaluate() calls.
    const deadline = Date.now() + soak;
    while (Date.now() < deadline) {
      await sleep(Math.min(15000,deadline-Date.now()));
      assert.equal((await api('/api/players')).players.length,1,'Background page disconnected');
    }
  }
  await page.close();
  await until(async()=> (await api('/api/players')).players.length===0);
  assert.equal(failures.length,0,failures.join('\n'));
  console.log(JSON.stringify({result:'passed',automaticYouTube:true,nativePauseAndSeek:true,nextVideoReturn:true,
    spaIdentity:true,reloadIdentity:true,tabSharing:true,closedPageRemoved:true,localVolumePreserved:true,backgroundMs:soak,directory}));
} catch (error) {
  const worker = context.serviceWorkers()[0];
  console.error(JSON.stringify({networkErrors, connection:worker ? await worker.evaluate(async()=> (await chrome.storage.session.get('connection')).connection).catch(()=>null):null}));
  throw error;
} finally { await context.close(); }
