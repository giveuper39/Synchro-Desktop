import {youtubePage} from './youtube-page.js';

export const youtubePatterns = ['https://youtube.com/*', 'https://www.youtube.com/*', 'https://m.youtube.com/*'];

export function youtubeOrigin(address) {
  try {
    const url = new URL(address);
    return youtubePatterns.some(pattern => url.origin + '/*' === pattern) ? url.origin : null;
  } catch (_) { return null; }
}

export function youtubeVideoUrl(address) {
  if (!youtubeOrigin(address)) return false;
  const url = new URL(address), parts = url.pathname.split('/').filter(Boolean);
  const id = url.pathname === '/watch' ? url.searchParams.get('v') : ['shorts', 'live', 'embed'].includes(parts[0]) ? parts[1] : '';
  return /^[A-Za-z0-9_-]{11}$/.test(id || '');
}

/** The worker alone talks to localhost. Page JavaScript never sees its credentials. */
export class YouTubeSync {
  constructor(browser, fetcher = globalThis.fetch.bind(globalThis), base = 'http://127.0.0.1:8765') {
    this.browser = browser;
    this.fetcher = fetcher;
    this.base = base;
    this.acks = new Map();
  }

  async request(path, body, authenticated = true) {
    const headers = {'Content-Type': 'application/json', 'X-Synchro-Extension': this.browser.runtime.id};
    if (authenticated) headers.Authorization = `Bearer ${this.token}`;
    const response = await this.fetcher(this.base + path, {method: 'POST', headers, body: JSON.stringify(body), signal: AbortSignal.timeout(8000)});
    if (!response.ok) {
      if (response.status === 401) this.token = null;
      throw Error(response.status === 404 ? 'Обновите Synchro на ПК' : 'Нет связи с Synchro на ПК');
    }
    return response.json();
  }

  async initialize() {
    if (this.clientId) return;
    const saved = await this.browser.storage.local.get(['clientId', 'token']);
    this.clientId = saved.clientId || crypto.randomUUID();
    this.token = saved.token;
    await this.browser.storage.local.set({clientId: this.clientId});
  }

  async pair() {
    const result = await this.request('/api/extension/connect', {}, false);
    this.token = result.token;
    await this.browser.storage.local.set({token: this.token});
  }

  async snapshot() {
    const tabs = (await this.browser.tabs.query({url: youtubePatterns})).filter(tab=>youtubeVideoUrl(tab.url)).slice(0, 12);
    this.hasVideoTabs = tabs.length > 0;
    const pages = await Promise.all(tabs.map(async tab => {
      const origin = youtubeOrigin(tab.url);
      if (!origin || !Number.isInteger(tab.id)) return null;
      try {
        const [result] = await this.browser.scripting.executeScript({target: {tabId: tab.id, frameIds: [0]}, world: 'MAIN', func: youtubePage});
        if (!result?.documentId || !result.result?.video) return null;
        const key = `${tab.id}:${result.documentId}`;
        return {tabId: tab.id, documentId: result.documentId, origin, video: result.result.video, acks: this.acks.get(key) || []};
      } catch (_) { return null; } // A closed/navigating tab is removed in this snapshot.
    }));
    return pages.filter(Boolean);
  }

  async tick() {
    await this.initialize();
    if (!this.token) await this.pair();
    const pages = await this.snapshot();
    const response = await this.request('/api/extension/youtube/sync', {clientId: this.clientId,
      version: this.browser.runtime.getManifest().version, pages, waitMs: pages.length ? 800 : 0});
    this.acks.clear();
    for (const batch of response.pages || []) {
      const page = pages.find(value => value.tabId === batch.tabId && value.documentId === batch.documentId);
      if (!page) continue;
      for (const command of (batch.commands || []).slice(0, 8)) {
        try {
          // documentIds prevents a late command from reaching a replacement page in the same tab.
          const [result] = await this.browser.scripting.executeScript({target: {tabId: page.tabId, documentIds: [page.documentId]}, world: 'MAIN', func: youtubePage, args: [command]});
          if (result?.documentId !== page.documentId || result.result?.ack !== command.id) continue;
          const key = `${page.tabId}:${page.documentId}`;
          this.acks.set(key, [...(this.acks.get(key) || []), {id: command.id, error: result.result.error}]);
        } catch (_) {} // Navigation invalidates this page; never redirect its command.
      }
    }
    await this.browser.storage.session.set({connection: {connected: true, youtubeTabs: pages.length, error: '', updatedAt: Date.now()}});
    if (response.extensionVersion && response.extensionVersion !== this.browser.runtime.getManifest().version && response.reloadAvailable) {
      // Reload only when this installation's own files were actually updated.
      const manifest = await this.fetcher(this.browser.runtime.getURL('manifest.json')).then(value => value.json());
      if (manifest.version === response.extensionVersion) this.browser.runtime.reload();
    }
    return this.hasVideoTabs;
  }
}
