# VLC adapter

Synchro starts a separate VLC instance with an authenticated HTTP interface
bound to `127.0.0.1` and a random private port. Existing VLC preferences and
other VLC instances are untouched. Choose local media through Synchro's PC
source picker; enabling a public VLC web interface is unnecessary.

These two original Lua templates call the modules already installed with VLC
3.x. They expose the live file, precise time, playback state, selected audio
and subtitle IDs, their ordered choices, subtitle delay, speed and active
playlist. The control template accepts only the commands needed for handoff;
it has no volume, crop, aspect-ratio or video-filter setter.

The adapter transfers embedded tracks. Automatic discovery of sidecar subtitle
files is disabled for the managed instance because those files are not part
of the streamed media. Internet streams, discs and unavailable playlist entries
are excluded from the phone queue with a visible warning.

This does not attach to an arbitrary already-open VLC instance: VLC's control
interface must be enabled when the process starts. Closing the desktop service
does not close a movie opened in the managed VLC window.

Public upstream API references:

- [VLC HTTP interface modules](https://github.com/videolan/vlc/tree/3.0.x/share/lua)
- [VLC Lua APIs](https://github.com/videolan/vlc/blob/3.0.x/share/lua/README.txt)
- [Variable choices and selected IDs](https://github.com/videolan/vlc/blob/3.0.x/modules/lua/libs/variables.c)

VLC is installed separately. No VLC binary or upstream implementation is
included in this directory.
