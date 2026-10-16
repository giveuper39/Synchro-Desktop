// Execute the shipped bookmarklet against a controlled main-world page fixture.
// This proves the message/capability contract, not availability on a remote site.
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { webcrypto } from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import vm from 'node:vm';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const python = process.env.SYNCHRO_PYTHON || path.join(root, '.venv', 'Scripts', 'python.exe');
const result = spawnSync(python, ['-X', 'utf8', '-c',
  'import sys,json;sys.path.insert(0,"server");from synchro.browser_page import bookmarklet,connection_page;print(json.dumps({"bookmarklet":bookmarklet("http://127.0.0.1:18765"),"page":connection_page().decode("utf-8")}))'],
{ cwd: root, encoding: 'utf8' });
assert.equal(result.status, 0, result.stderr);
const shipped = JSON.parse(result.stdout);
const source = decodeURIComponent(shipped.bookmarklet.slice('javascript:'.length));

function page({ capable = true, muted = false } = {}) {
  const messages = [], listeners = {}, service = 'http://127.0.0.1:18765';
  const video = { currentTime: 12, duration: 120, paused: false, ended: false,
    playbackRate: 1, volume: .73, muted, pause() { this.paused = true; },
    async play() { this.paused = false; } };
  const player = { identifier: 'aqz-KE-bpKQ', title: 'Big Buck Bunny', muted, cues: 0,
    cueVideoById({ videoId, startSeconds }) { this.cues++; this.identifier = videoId; video.currentTime = startSeconds; video.paused = true; },
    getVideoData() { return { video_id: this.identifier, title: this.title }; },
    getPlayerState() { return video.paused ? 2 : 1; }, pauseVideo() { video.paused = true; },
    mute() { this.muted = true; }, isMuted() { return this.muted; }, unMute() { this.muted = false; } };
  const popup = { closed: false, postMessage(message, origin) { messages.push({ message, origin }); } };
  const context = { crypto: webcrypto, URL, Uint8Array, console, Promise,
    location: new URL('https://www.youtube.com/watch?v=aqz-KE-bpKQ'),
    document: { title: 'Big Buck Bunny - YouTube', querySelector: () => video,
      getElementById: () => capable ? player : null },
    addEventListener: (type, listener) => { listeners[type] = listener; },
    removeEventListener: type => { delete listeners[type]; },
    setInterval: () => 1, clearInterval() {}, setTimeout,
    open() { return popup; }, alert(message) { throw Error(message); } };
  context.window = context;
  vm.runInNewContext(source, context, { timeout: 1000 });
  const nonce = messages.at(-1).message.nonce;
  async function command(value, overrides = {}) {
    await listeners.message({ source: popup, origin: service,
      data: { type: 'synchro:command', nonce, command: value }, ...overrides });
  }
  return { video, player, popup, messages, command, nonce, service };
}

test('only the nonce, exact service origin and popup identity can pause the page', async () => {
  const fixture = page();
  const command = { id: '1', action: 'pause' };
  await fixture.command(command, { origin: 'http://evil.test' });
  await fixture.command(command, { source: {} });
  await fixture.command(command, { data: { type: 'synchro:command', nonce: 'wrong', command } });
  assert.equal(fixture.video.paused, false);
  await fixture.command(command);
  assert.equal(fixture.video.paused, true);
  assert.equal(fixture.messages.at(-1).message.ack, '1');
});

test('seek applies the position and rate while preserving device volume', async () => {
  const fixture = page();
  await fixture.command({ id: '2', action: 'seek', positionMs: 60500, rate: 1.5 });
  assert.equal(fixture.video.currentTime, 60.5);
  assert.equal(fixture.video.paused, true);
  assert.equal(fixture.video.playbackRate, 1.5);
  assert.equal(fixture.video.volume, .73);
});

test('cue switches only the existing player, stays paused, restores local mute and deduplicates retries', async () => {
  for (const muted of [false, true]) {
    const fixture = page({ muted });
    const command = { id: '3', action: 'open', videoId: 'YE7VzlLtp-4', positionMs: 45000, rate: 1.25 };
    await fixture.command(command);
    await fixture.command(command);
    assert.equal(fixture.player.identifier, 'YE7VzlLtp-4');
    assert.equal(fixture.player.cues, 1);
    assert.equal(fixture.player.muted, muted);
    assert.equal(fixture.video.paused, true);
    assert.equal(fixture.video.currentTime, 45);
    assert.equal(fixture.messages.at(-1).message.video.videoId, 'YE7VzlLtp-4');
    assert.equal(fixture.messages.at(-1).message.error, false);
  }
});

test('a browser without the optional player API can still pause and seek the selected video', async () => {
  const fixture = page({ capable: false });
  assert.equal(fixture.messages.at(-1).message.video.canChangeVideo, false);
  await fixture.command({ id: '4', action: 'seek', positionMs: 32000, rate: 1 });
  assert.equal(fixture.video.currentTime, 32);
  await fixture.command({ id: '5', action: 'open', videoId: 'YE7VzlLtp-4', positionMs: 45000 });
  assert.equal(fixture.messages.at(-1).message.error, true);
  assert.equal(fixture.player.cues, 0);
});

test('a manual PC resume immediately before a delayed phone seek is never overwritten', async () => {
  const fixture = page();
  fixture.video.currentTime = 28;
  fixture.video.paused = false;
  await fixture.command({ id: '6', action: 'seek', positionMs: 60000, rate: 1,
    expected: { videoId: 'aqz-KE-bpKQ', positionMs: 12000, paused: true } });
  assert.equal(fixture.video.currentTime, 28);
  assert.equal(fixture.video.paused, false);
  assert.equal(fixture.messages.at(-1).message.error, 'pc_changed');
});

test('the local popup requires an exact opener handshake and explicit user confirmation', async () => {
  const nonce = 'a'.repeat(48), nodes = {}, listeners = {}, requests = [], messages = [];
  const opener = { postMessage(message, origin) { messages.push({ message, origin }); } };
  const video = { videoId: 'aqz-KE-bpKQ', title: 'Big Buck Bunny', positionMs: 12000,
    durationMs: 120000, paused: false, rate: 1, canChangeVideo: true };
  const context = { URLSearchParams, Date, Promise, AbortSignal,
    location: { hash: '#nonce=' + nonce, pathname: '/browser/connect' },
    history: { replaceState(_state, _title, url) { assert.equal(url, '/browser/connect'); } },
    document: { getElementById(id) { return nodes[id] ||= { disabled: true, hidden: false, textContent: '' }; } },
    addEventListener(type, handler) { listeners[type] = handler; },
    setTimeout() { return 1; },
    async fetch(url, options) {
      requests.push({ url, data: JSON.parse(options.body) });
      if (url === '/local/browser/register') return { ok: true, async json() { return { bridgeId: 'page-id', bridgeKey: 'page-only-capability' }; } };
      return new Promise(() => {}); // Keep one deliberate long poll in flight.
    } };
  context.window = { opener };
  const script = shipped.page.match(/<script>([\s\S]*)<\/script>/)[1];
  vm.runInNewContext(script, context, { timeout: 1000 });
  const data = { type: 'synchro:state', nonce, video };
  listeners.message({ source: {}, origin: 'https://www.youtube.com', data });
  listeners.message({ source: opener, origin: 'https://www.youtube.com.evil.test', data });
  listeners.message({ source: opener, origin: 'https://www.youtube.com', data: { ...data, nonce: 'wrong' } });
  assert.equal(nodes.connect.disabled, true);
  assert.equal(requests.length, 0);
  listeners.message({ source: opener, origin: 'https://www.youtube.com', data });
  assert.equal(nodes.connect.disabled, false);
  assert.equal(nodes.title.textContent, 'Big Buck Bunny');
  assert.equal(requests.length, 0, 'Receiving metadata must never grant control without the button click');
  await nodes.connect.onclick();
  assert.equal(requests[0].url, '/local/browser/register');
  assert.deepEqual(Object.keys(requests[0].data).sort(), ['nonce', 'origin', 'video']);
  assert.equal(nodes.connect.hidden, true);
  assert.equal(nodes.disconnect.hidden, false);
  assert.equal(messages.at(-1).origin, 'https://www.youtube.com');
  assert.equal(JSON.stringify(messages).includes('page-only-capability'), false, 'The page must never receive even the popup capability');
});
