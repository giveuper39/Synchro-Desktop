import {YouTubeSync, youtubeOrigin} from './youtube-sync.js';

const sync = new YouTubeSync(chrome);
let running = false;

async function start() {
  if (running) return;
  running = true;
  try {
    // Each round uses Chrome APIs and a bounded network poll. An idle browser
    // releases its worker; the alarm and tab events recover after suspension.
    let failures = 0;
    while (true) {
      try {
        const active = await sync.tick();
        failures = 0;
        if (!active) break;
      } catch (error) {
        if (++failures >= 3) throw error;
        await new Promise(resolve => setTimeout(resolve, 1000));
      }
      await new Promise(resolve => setTimeout(resolve, 150));
    }
  } catch (error) {
    await chrome.storage.session.set({connection: {connected: false, youtubeTabs: 0, error: error.message, updatedAt: Date.now()}});
  } finally { running = false; }
}

chrome.runtime.onInstalled.addListener(() => { void start(); });
chrome.runtime.onStartup.addListener(() => { void start(); });
chrome.tabs.onUpdated.addListener((_id, change, tab) => {
  if (youtubeOrigin(tab.url) && (change.status === 'complete' || change.url)) void start();
});
chrome.tabs.onRemoved.addListener(() => { void start(); });
chrome.alarms.onAlarm.addListener(alarm => { if (alarm.name === 'synchro-reconnect') void start(); });
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (sender.id !== chrome.runtime.id || sender.tab || message?.type !== 'synchro:refresh') return;
  void start(); reply({ok: true});
});
chrome.runtime.onMessageExternal.addListener((message, sender, reply) => {
  try {
    const url = new URL(sender.url);
    if (url.origin !== sync.base || url.pathname !== '/setup' || message?.type !== 'synchro:server-ready') return;
    void start(); reply({ok: true, version: chrome.runtime.getManifest().version});
  } catch (_) {}
});
void chrome.alarms.create('synchro-reconnect', {periodInMinutes: .5});
void start();
