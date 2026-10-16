# YouTube and tabs in one extension

## Automatic connection · Windows 0.4.4

Install the Synchro extension once from **Browser** in the Windows wizard. The server prepares `%LOCALAPPDATA%/Synchro/browser-extension` at startup and shows installation on first use. In Chrome choose **Developer mode → Load unpacked** and select that prepared directory. No ZIP or bookmark is needed. Personal Windows Chrome requires this first confirmation for local extensions outside the Web Store; Synchro does not edit browser policies or profile preferences. See [Chrome's installation rules](https://developer.chrome.com/docs/extensions/how-to/distribute/install-extensions).

Already open YouTube video tabs appear automatically as Android sources. One extension also sends the explicitly selected ordinary tab by button or swipe. Android 0.4.3 is compatible. The automatic path supports modern Chromium browsers using Chrome extension APIs (Chrome 120+), with up to 12 video documents per profile. Firefox is not claimed to support this Chrome extension.

The service worker reads the existing player through `chrome.scripting`. The injected function receives only bounded playback commands, never service credentials. Tab and document IDs prevent a late command from reaching a replacement page; SPA navigation retains its binding. Commands and acknowledgements share the existing coordinator. Ads are excluded. Without video tabs the worker releases itself; tab events and a 30-second alarm reconnect it. The local wizard can wake it with a narrowly scoped `server-ready` message that exposes no credentials or playback commands.

Automatic bridge HTTP calls require the matching extension ID, permitted origin, loopback client/Host and pairing key. Browser profiles remain separate. Titles, URLs and browsing history are not persisted. Volume remains local; next-video return uses the existing player's feature-detected cue API.

`tools/browser_extension_fixture.py` and `tools/test_browser_extension.mjs` check real Chromium APIs and synthetic HTML video on loopback:18766 with a disposable profile. Automatic discovery, native pause/seek, next-video return, SPA/reload identity, tab sending, closed-page removal and 330 seconds in the background passed. During the background interval the test did not wake the worker through evaluate(). The disposable test manifest grants only a synthetic ordinary host to exercise a popup opened as a test tab; production uses `activeTab`. No real player, user profile, clipboard or production key is used. This does not establish live YouTube availability for a particular phone/account.

## Legacy bookmark connector

Previously installed bookmark connections remain compatible. The sections below describe that optional legacy transport; it is no longer the default Chrome setup.

Synchro uses the player's public playback state; it does not download, extract or
redistribute YouTube video. Android plays the public YouTube player. Account,
regional, embedding and age restrictions still apply.

### Connect a page with an existing bookmark

1. Open the Synchro setup page on the computer and save its **Synchro · YouTube**
   bookmark in the browser you already use.
2. Open the desired YouTube video and click the bookmark.
3. The local popup shows the exact YouTube origin and video title. Confirm
   **Подключить эту страницу**. Leave that popup open; it can be minimized.
4. Select this source in Synchro on Android. The computer pauses. Pausing on
   Android returns the position to that same page.

This connector works in browsers that support JavaScript bookmarks and
user-opened popups, including Chromium browsers and Firefox. It does not require
an extension, a restarted browser, a remote debugging port or access to history.
If a browser blocks the popup or severs its opener relationship, allow the
user-requested popup and reconnect. A closed or reloaded page must be connected
again. No persistent background access is installed in the browser.

The fast automatic path uses Windows' media-session API plus a bounded
accessibility query for the currently selected YouTube tab. Windows publishes
the title and timeline, but **does not publish the video URL**. A background tab,
ambiguous matching windows or inaccessible address bar therefore requires the
explicit page connector instead of guessing a URL.

## Returning another video

Same-video pause, seek and playback rate use the connected page's existing HTML
video element. Volume and mute remain local to each device.

Switching to another video is available only when the current YouTube player
exposes `cueVideoById`, `getVideoData`, `getPlayerState` and the required pause and
mute methods. Synchro pauses and temporarily mutes that existing player, cues the
new video, verifies its identity and paused/cued state, then restores the local
mute setting. It does not create a hidden second player. The capability is
reported as `canChangeVideo`; an unavailable capability is shown as a limitation
rather than silently opening a different browser.

`cueVideoById` is documented for the [YouTube IFrame API](https://developers.google.com/youtube/iframe_api_reference).
Availability on YouTube's own main-page player is feature detected, not a stable
site API guarantee. Page metadata outside the video player may continue to show
the originally opened watch page after a cue. Reloading returns to that page.

## Privacy and protocol

- The bookmark reads only the explicitly selected page's video identity, title,
  position, duration, rate and paused state. It does not read other tabs, cookies,
  account data or browser databases.
- Messages check the exact service origin, exact YouTube origin, opening window
  identity and a fresh 192-bit handshake nonce. The popup requires an explicit
  confirmation before registering a source.
- The popup receives a random capability for one page. It never receives or
  sends the application's phone pairing key to YouTube.
- `/local/browser/*` requires a loopback client, loopback Host and same-origin
  request. No cross-origin fetch from YouTube is allowed. The page bridge uses
  `postMessage`, avoiding HTTPS-to-HTTP mixed-content fetches.
- Commands have identities and are deduplicated on the page. Ownership uses a
  session ID and increasing sequence number; delayed messages cannot restore an
  old position. Manual playback changes on the computer revoke phone ownership.
- The capability and video state live in memory. There is no browser history
  archive or access to the system clipboard in this protocol.

## Validation

`tests/test_youtube.py` checks URL boundaries, source readiness, per-page
capabilities, acknowledgement failures, ownership, stale events, manual PC
changes, disconnect checkpoints and next-video return.

`node --test tests/youtube_connector.test.mjs` executes the actual shipped
bookmarklet in a controlled page fixture. It checks origin/window/nonce
boundaries, seek, rate, device-local volume, cue capabilities and retry
deduplication. This fixture does not establish access to a particular live
YouTube video. The live runtime check on the development host was blocked by
`net::ERR_SSL_PROTOCOL_ERROR` from youtube.com; certificate protections were not
changed.
