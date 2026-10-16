# Third-party components

## Interface and QR scanning

- Material Components for Android `1.13.0`, Apache License 2.0:
  <https://github.com/material-components/material-components-android>
- AndroidX AppCompat `1.7.1` and AndroidX dependencies, Apache License 2.0:
  <https://android.googlesource.com/platform/frameworks/support/>
- ZXing Android Embedded `4.3.0` and ZXing Core, Apache License 2.0:
  <https://github.com/journeyapps/zxing-android-embedded> and <https://github.com/zxing/zxing>

The Apache 2.0 text is included in `app/src/main/assets/APACHE-2.0.txt` and
available in the application's component licenses. Application icons are
original Android vector assets supplied under the project's MIT license.

## Video playback

This application uses the unmodified Maven dependency `org.videolan.android:libvlc-all:3.7.7`, published by VideoLAN. The Java source headers identify copyright VideoLAN / VLC authors / VideoLabs and license the library under GNU LGPL version 2.1 or, at the recipient's option, a later version.

- Source project: <https://code.videolan.org/videolan/vlc-android>
- Exact Java sources distributed with this Maven version: <https://repo.maven.apache.org/maven2/org/videolan/android/libvlc-all/3.7.7/libvlc-all-3.7.7-sources.jar>
- Maven artifact and metadata: <https://repo.maven.apache.org/maven2/org/videolan/android/libvlc-all/3.7.7/>
- VLC native source: <https://code.videolan.org/videolan/vlc>
- VLC Android build tools / native build sources: <https://code.videolan.org/videolan/vlc-android/-/tree/master/buildsystem>
- GNU LGPL 2.1 text is bundled at `app/src/main/assets/LGPL-2.1.txt` and available through the application's «Лицензии» button. It was obtained from VideoLAN's official VLC source mirror: <https://github.com/videolan/vlc/blob/3.0.x/COPYING.LIB>.

The `.aar` includes native shared libraries for the selected Android ABIs. This application does not modify those libraries, does not restrict debugging modifications of those libraries, and is supplied as source so recipients can change the Gradle dependency and rebuild/relink the APK. Library authors do not endorse this application.

LibVLC contains or links additional codec and supporting libraries with their own copyright and license notices. Before public binary distribution, review the exact AAR/native build's complete corresponding source and component notices against VideoLAN's source/build information; the Java sources JAR alone is not complete corresponding native source. These notes record the dependency used by this prototype and do not claim that a future Play Store or commercial distribution has completed that review.

## Native 16 KB page-size verification boundary

The 3.7.7 AAR's ARM64 and x86-64 shared libraries have 16 KB `PT_LOAD` alignment, improving on 3.6.1's 4 KB load alignment. Five of the six shared libraries nevertheless fail the additional GNU_RELRO end-address modulo-16-KB check described by the current [Android documentation](https://developer.android.com/guide/practices/page-sizes#relro). This project records that limitation and does not claim complete runtime compatibility with every 16 KB Android device. See `verify-apk.ps1 -StrictRelro` and the README. No native library has been binary-patched to conceal the issue.

## Windows distribution

- CPython 3.14.8, Python Software Foundation license; official embeddable distribution includes LICENSE.txt: https://www.python.org/downloads/release/python-3148/
- qrcode 8.2, BSD-3-Clause: https://github.com/lincolnloop/python-qrcode/tree/v8.2
- zeroconf 0.151.5, LGPL-2.1-or-later: https://github.com/python-zeroconf/python-zeroconf/tree/0.151.5 . Complete unmodified source is included in third-party-sources/ in the Windows ZIP. Python modules are replaceable in runtime/Lib/site-packages.
- ifaddr 0.2.0, MIT: https://github.com/p-stannis/ifaddr . Its license is included in installed distribution metadata.
- Android Platform Tools / ADB, Apache 2.0 and component licenses: https://android.googlesource.com/platform/packages/modules/adb/ . The full SDK NOTICE.txt is included in runtime/adb/.
- PotPlayer and FFmpeg are not bundled.

## Typography and image clipboard

- Manrope, SIL Open Font License 1.1. Font source: https://github.com/google/fonts/tree/main/ofl/manrope . The OFL is bundled alongside the font in assets/Manrope-OFL.txt and in the Android assets.
- Pillow 12.3.0, HPND / component notices: https://github.com/python-pillow/Pillow/tree/12.3.0 . The Windows runtime includes its distribution metadata and licenses; it processes clipboard images and does not transcode video.
