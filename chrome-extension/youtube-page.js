/** Runs in the selected YouTube document. Never receives a server key or arbitrary script. */
export async function youtubePage(command = null) {
  if (!['https://youtube.com', 'https://www.youtube.com', 'https://m.youtube.com'].includes(location.origin)) return null;
  const identifier = value => typeof value === 'string' && /^[A-Za-z0-9_-]{11}$/.test(value);
  const player = () => document.getElementById('movie_player');
  const canCue = p => p && ['cueVideoById', 'getVideoData', 'getPlayerState', 'pauseVideo', 'mute', 'isMuted', 'unMute'].every(key => typeof p[key] === 'function');
  const state = () => {
    const url = new URL(location.href), parts = url.pathname.split('/').filter(Boolean);
    const urlId = url.pathname === '/watch' ? url.searchParams.get('v') : ['shorts', 'live', 'embed'].includes(parts[0]) ? parts[1] : null;
    const video = document.querySelector('video'), p = player();
    if (!identifier(urlId) || !video || p?.classList.contains('ad-showing')) return null;
    let metadata;
    try { metadata = p?.getVideoData?.(); } catch (_) {}
    const videoId = identifier(metadata?.video_id) ? metadata.video_id : urlId;
    const title = typeof metadata?.title === 'string' ? metadata.title : document.title.replace(/\s*[-–—]\s*YouTube\s*$/i, '');
    return {videoId, title: title.slice(0, 500), positionMs: Math.max(0, Math.round((video.currentTime || 0) * 1000)),
      durationMs: Number.isFinite(video.duration) ? Math.max(0, Math.round(video.duration * 1000)) : 0,
      paused: video.paused || video.ended, rate: video.playbackRate, canChangeVideo: !!canCue(p)};
  };
  if (!command) return {video: state()};
  if (typeof command.id !== 'string' || !/^[a-f0-9]{32}$/.test(command.id)) return null;
  // Re-delivery after a lost reply must not perform a second seek or open.
  const completed = window.__synchroExtensionCommands ||= new Map();
  if (completed.has(command.id)) return {video: state(), ack: command.id, error: completed.get(command.id)};
  let error = false;
  try {
    const current = state(), video = document.querySelector('video');
    if (!current || !video) throw Error('unavailable');
    const expected = command.expected;
    if (expected && (!current.paused || current.videoId !== expected.videoId || Math.abs(current.positionMs - expected.positionMs) > 1500)) throw Error('pc_changed');
    if (command.action === 'pause') video.pause();
    else if (command.action === 'play') await video.play();
    else if (command.action === 'seek') {
      if (!Number.isInteger(command.positionMs) || command.positionMs < 0 || !Number.isFinite(command.rate) || command.rate < .25 || command.rate > 4) throw Error('invalid');
      video.pause();
      video.playbackRate = command.rate;
      video.currentTime = Math.min(Number.isFinite(video.duration) ? video.duration : command.positionMs / 1000, command.positionMs / 1000);
    } else if (command.action === 'open') {
      const p = player();
      if (!canCue(p) || !identifier(command.videoId) || !Number.isInteger(command.positionMs) || command.positionMs < 0 || !Number.isFinite(command.rate) || command.rate < .25 || command.rate > 4) throw Error('invalid');
      const wasMuted = p.isMuted();
      video.pause(); p.mute();
      let verified = false;
      try {
        p.cueVideoById({videoId: command.videoId, startSeconds: command.positionMs / 1000});
        const deadline = Date.now() + 4500;
        while (Date.now() < deadline) {
          p.pauseVideo();
          if (p.getVideoData()?.video_id === command.videoId && [-1, 2, 5].includes(p.getPlayerState())) { verified = true; break; }
          await new Promise(resolve => setTimeout(resolve, 70));
        }
        if (!verified) throw Error('unavailable');
        const next = document.querySelector('video');
        if (next) { next.pause(); next.playbackRate = command.rate; }
      } finally {
        p.pauseVideo();
        if (verified && !wasMuted) p.unMute();
      }
    } else throw Error('invalid');
  } catch (failure) { error = failure.message === 'pc_changed' ? 'pc_changed' : true; }
  completed.set(command.id, error);
  if (completed.size > 32) completed.delete(completed.keys().next().value);
  return {video: state(), ack: command.id, error};
}
