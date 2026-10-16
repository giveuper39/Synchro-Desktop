import {test} from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {youtubePage} from './youtube-page.js';
import {YouTubeSync, youtubeOrigin} from './youtube-sync.js';

function pageFixture() {
  const video = {currentTime: 12.5, duration: 180, paused: false, ended: false, playbackRate: 1, volume: .4, muted: false,
    pause() { this.paused = true; this.pauses = (this.pauses || 0) + 1; }, async play() { this.paused = false; }};
  const metadata = {video_id: 'abcdefghijk', title: 'Own fixture'};
  const player = {classList: {contains: () => false}, getVideoData: () => metadata};
  const context = {URL, Map, Number, Date, Promise, setTimeout, window: {}, location: {origin: 'https://www.youtube.com', href: 'https://www.youtube.com/watch?v=abcdefghijk'},
    document: {title: 'Own fixture - YouTube', querySelector: () => video, getElementById: () => player}};
  return {video, player, metadata, context, run: command => vm.runInNewContext(`(${youtubePage.toString()})(command)`, {...context, command})};
}

test('automatic page capture ignores ads and non-YouTube documents', async () => {
  const page = pageFixture();
  assert.equal((await page.run()).video.videoId, 'abcdefghijk');
  page.player.classList.contains = () => true;
  assert.equal((await page.run()).video, null);
  page.context.location.origin = 'https://www.youtube.com.evil.test';
  assert.equal(await page.run(), null);
});

test('commands preserve local volume, reject PC edits and deduplicate lost replies', async () => {
  const page = pageFixture(), id = 'a'.repeat(32);
  const command = {id, action: 'seek', positionMs: 50000, rate: 1.5};
  assert.equal((await page.run(command)).error, false);
  assert.equal(page.video.currentTime, 50);
  assert.equal(page.video.playbackRate, 1.5);
  const pauses = page.video.pauses;
  await page.run(command);
  assert.equal(page.video.pauses, pauses);
  assert.equal(page.video.volume, .4);
  assert.equal(page.video.muted, false);
  page.video.paused = false;
  const rejected = await page.run({...command, id: 'b'.repeat(32), expected: {videoId: 'abcdefghijk', positionMs: 50000}});
  assert.equal(rejected.error, 'pc_changed');
  assert.equal(page.video.paused, false);
});

test('capable existing YouTube player cues the next video paused', async () => {
  const page = pageFixture();
  Object.assign(page.player, {getPlayerState: () => 2, isMuted: () => false, mute() { page.video.muted = true; },
    unMute() { page.video.muted = false; }, pauseVideo() { page.video.pause(); },
    cueVideoById(value) { page.metadata.video_id = value.videoId; page.video.currentTime = value.startSeconds; }});
  const result = await page.run({id: 'c'.repeat(32), action: 'open', videoId: 'lmnopqrstuv', positionMs: 60000, rate: 1.25});
  assert.equal(result.error, false);
  assert.equal(result.video.videoId, 'lmnopqrstuv');
  assert.equal(result.video.paused, true);
  assert.equal(page.video.currentTime, 60);
  assert.equal(page.video.volume, .4);
  assert.equal(page.video.muted, false);
});

function browserFixture() {
  const storage = {local: {clientId: '12345678-1234-1234-1234-123456789abc'}, session: {}};
  const calls = [], requests = [];
  let tabs = [{id: 7, url: 'https://www.youtube.com/watch?v=abcdefghijk'}], command = true;
  const browser = {runtime: {id: 'our-extension', getManifest: () => ({version: '0.4.4'})},
    tabs: {query: async () => tabs}, storage: Object.fromEntries(['local', 'session'].map(area => [area,
      {get: async () => storage[area], set: async value => Object.assign(storage[area], value)}])),
    scripting: {executeScript: async options => { calls.push(options); return [{documentId: 'document-one', result: options.args
      ? {ack: options.args[0].id, error: false} : {video: {videoId: 'abcdefghijk', title: 'Fixture', positionMs: 1000, durationMs: 10000, paused: false, rate: 1}}}]; }}};
  const fetcher = async (url, options) => {
    const body = JSON.parse(options.body); requests.push({url, options, body});
    return {ok: true, json: async () => url.endsWith('/connect') ? {token: 'own-synthetic-key'} : {pages: command
      ? [{tabId: 7, documentId: 'document-one', commands: [{id: 'a'.repeat(32), action: 'pause'}]}] : []}};
  };
  return {browser, fetcher, storage, calls, requests, noCommands: () => { command = false; }, close: () => { tabs = []; }};
}

test('worker pairs automatically and acknowledges only the exact document', async () => {
  const fixture = browserFixture(), sync = new YouTubeSync(fixture.browser, fixture.fetcher);
  assert.equal(await sync.tick(), true);
  const action = fixture.calls.find(call => call.args);
  assert.deepEqual(action.target, {tabId: 7, documentIds: ['document-one']});
  assert.equal(action.world, 'MAIN');
  assert.equal(JSON.stringify(action).includes('own-synthetic-key'), false);
  fixture.noCommands(); await sync.tick();
  assert.equal(fixture.requests.at(-1).body.pages[0].acks[0].id, 'a'.repeat(32));
  assert.equal(fixture.requests.at(-1).options.headers.Authorization, 'Bearer own-synthetic-key');
  assert.equal(fixture.storage.session.connection.connected, true);
  fixture.close(); assert.equal(await sync.tick(), false);
  assert.deepEqual(fixture.requests.at(-1).body.pages, []);
});

test('worker never dispatches a reply for another document and does not persist browsing state', async () => {
  const fixture = browserFixture();
  const fetcher = async (url, options) => url.endsWith('/connect') ? fixture.fetcher(url, options) :
    {ok: true, json: async () => ({pages: [{tabId: 7, documentId: 'replaced-document', commands: [{id: 'b'.repeat(32), action: 'pause'}]}]})};
  await new YouTubeSync(fixture.browser, fetcher).tick();
  assert.equal(fixture.calls.some(call => call.args), false);
  assert.equal(JSON.stringify(fixture.storage).includes('abcdefghijk'), false);
  assert.equal(youtubeOrigin('https://www.youtube.com.evil.test/watch?v=abcdefghijk'), null);
  assert.equal(youtubeOrigin('http://www.youtube.com/watch?v=abcdefghijk'), null);
});
