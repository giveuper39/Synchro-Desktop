import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import vm from 'node:vm';
import test from 'node:test';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const html = name => readFileSync(path.join(root, 'server/synchro/ui', name + '.html'), 'utf8');
const script = name => html(name).match(/<script>([\s\S]*?)<\/script>/)[1];
const origin = 'http://192.168.1.22:8765';
const secret = 'synthetic_key_with_more_than_24_characters';
const appUri = (server = origin, name = 'Мой компьютер') => 'synchro://pair?' + new URLSearchParams({v:'1',server,token:secret,name});
const fragment = uri => '#' + new URLSearchParams({pair:uri});

function dom() {
  const nodes = new Map();
  const node = () => ({hidden:true, textContent:'', href:'', style:{}, events:{},
    addEventListener(type, callback) { this.events[type] = callback; },
    replaceChildren() {}, append() {}});
  return {nodes, document:{getElementById(id) {
    if (!nodes.has(id)) nodes.set(id, node());
    return nodes.get(id);
  }, querySelectorAll() {return [];}, createElement:node}};
}

function landing(hash = fragment(appUri()), active = false) {
  const {nodes, document} = dom(), navigations = [];
  const context = {document, URL, URLSearchParams, navigator:{userAgent:'Android',userActivation:{isActive:active}},
    location:{origin,hash,replace(value){navigations.push(value);}}};
  vm.runInNewContext(script('connect'), context, {timeout:1000});
  return {nodes, navigations};
}

test('one camera link opens the pinned app with a same-origin installation fallback', () => {
  const {nodes, navigations} = landing();
  assert.equal(nodes.get('open-app').hidden, false);
  assert.equal(nodes.get('computer-name').textContent, 'Мой компьютер');
  const intent = nodes.get('open-app').href;
  assert.match(intent, /scheme=syncro;package=dev\.potplayer\.handoff;/);
  const fallback = new URL(decodeURIComponent(intent.match(/S\.browser_fallback_url=([^;]+);end$/)[1]));
  assert.equal(fallback.origin, origin);
  assert.equal(fallback.pathname, '/connect');
  assert.equal(fallback.search, '');
  const fields = new URLSearchParams(fallback.hash.slice(1));
  assert.equal(fields.get('install'), '1');
  assert.equal(fields.get('token'), secret);
  assert.equal(navigations.length, 0, 'a browser without user activation must retain the explicit button');
  assert.match(html('connect'), /href="\/synchro\.apk" download="Synchro-Android\.apk"/);
});

test('the compact QR derives the computer address from its own origin', () => {
  const {nodes} = landing('#' + new URLSearchParams({v:'1',token:secret,name:'Мой компьютер'}));
  assert.equal(nodes.get('computer-address').textContent, origin);
  assert.match(nodes.get('open-app').href, /server=http%3A%2F%2F192.168.1.22%3A8765/);
});

test('installation fallback retains the connection without redirect loops', () => {
  const first = landing(undefined, true);
  assert.equal(first.navigations.length, 1);
  const fallback = new URL(decodeURIComponent(first.navigations[0].match(/S\.browser_fallback_url=([^;]+);end$/)[1]));
  const second = landing(fallback.hash, true);
  assert.equal(second.navigations.length, 0);
  assert.equal(second.nodes.get('open-app').hidden, false);
  assert.equal(second.nodes.get('after-install').hidden, false);
  assert.equal(second.nodes.get('heading').textContent, 'Установите Synchro');
  assert.equal(second.nodes.get('download').className, 'button');
});

test('foreign origins, duplicate fields and damaged pairing cannot launch a receiver', () => {
  for (const hash of [fragment(appUri('http://foreign.example')), fragment(appUri().replace(secret,'short')),
    fragment(appUri()) + '&pair=' + encodeURIComponent(appUri()), fragment(appUri()) + '&redirect=elsewhere',
    fragment(appUri().replace('pair?', 'pair:999?')), fragment(appUri(origin, 'bad\nname'))]) {
    const {nodes, navigations} = landing(hash, true);
    assert.equal(nodes.get('heading').textContent, 'Откройте новый QR');
    assert.equal(nodes.get('open-app'), undefined);
    assert.equal(navigations.length, 0);
  }
});

test('an ordinary download page does not invent or request connection credentials', () => {
  const {nodes, navigations} = landing('');
  assert.equal(nodes.size, 1);
  assert.equal(nodes.get('download').className, 'button');
  assert.equal(navigations.length, 0);
  assert.match(html('connect'), /name="referrer" content="no-referrer"/);
});

async function setup(installFails = false, search = '') {
  const {nodes, document} = dom(), requests = [];
  const context = {document, URLSearchParams, location:{search}, history:{replaceState(){}}, setInterval(){}, fetch:async (url, options) => {
    requests.push(url);
    const failure = installFails && url === '/local/setup/install';
    const value = failure ? {error:'Телефон отключён'} : url === '/local/pairing'
      ? {url:origin + '/connect' + fragment(appUri()),qrSvg:'<svg/>',server:origin,name:'Мой компьютер'}
      : url === '/local/extension/status' ? {connected:false,prepared:true,folder:'C:/Synthetic/Synchro/browser-extension',youtubeTabs:0}
      : {stage:'scan',message:'Сканируйте QR'};
    return {ok:!failure,json:async()=>value};
  }};
  vm.createContext(context);
  vm.runInContext(script('setup'),context,{timeout:1000});
  await new Promise(resolve => setImmediate(resolve));
  return {nodes,requests,context};
}

test('the primary setup QR needs no wireless-debugging attempt', async () => {
  const {nodes,requests,context} = await setup();
  assert.equal(nodes.get('universalQr').innerHTML, '<svg/>');
  assert.equal(requests.includes('/local/setup/start'), false);
  context.showWireless(true);
  assert.equal(nodes.get('universal').hidden, true);
  assert.equal(nodes.get('wireless').hidden, false);
  assert.equal(requests.includes('/local/setup/start'), true);
});

test('a failed install remains visible when an older paired status arrives', async () => {
  const {nodes,context} = await setup(true);
  await context.finish('synthetic-device');
  context.renderSetup({stage:'paired',device:'synthetic-device',message:'Телефон подключён'});
  assert.equal(nodes.get('message').textContent, 'Телефон отключён');
});

test('installation is visibly busy and a failed worker offers retry', async () => {
  const {nodes,context} = await setup();
  context.renderSetup({stage:'installing',device:'synthetic-device',message:'Передаём APK…'});
  assert.equal(nodes.get('install').disabled, true);
  assert.equal(nodes.get('install').textContent, 'Установка…');
  context.renderSetup({stage:'error',device:'synthetic-device',message:'Android отклонил установку'});
  assert.equal(nodes.get('install').disabled, false);
  assert.equal(nodes.get('install').textContent, 'Повторить установку');
  assert.equal(nodes.get('message').textContent, 'Android отклонил установку');
});

test('first browser setup presents the prepared folder and opens Chrome setup once', async () => {
  const {nodes,requests} = await setup(false, '?section=browser&install=1');
  assert.equal(nodes.get('extensionFolder').textContent, 'C:/Synthetic/Synchro/browser-extension');
  assert.equal(requests.filter(path=>path==='/local/extension/open').length, 1);
  assert.equal(requests.includes('/local/browser/bookmarklet'), false);
  assert.equal(requests.includes('/local/setup/start'), false);
});
