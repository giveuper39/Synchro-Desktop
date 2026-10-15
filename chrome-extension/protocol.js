/** Only a user-selected ordinary website may be transferred; no scripts, files or browser internals. */
export function shareableUrl(value) {
  if (typeof value !== 'string' || value.length > 8192 || /[\s\x00-\x1f]/u.test(value)) throw new Error('Эту вкладку передать нельзя');
  const url = new URL(value);
  if (!['http:', 'https:'].includes(url.protocol) || !url.hostname || url.username || url.password) throw new Error('Передаются только веб-страницы');
  return value;
}

export function linkPayload(tab, requestId) {
  return {url:shareableUrl(tab.url), title:(tab.title || '').slice(0,500), target:'phone', requestId};
}
