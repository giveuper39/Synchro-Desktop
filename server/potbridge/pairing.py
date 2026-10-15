"""Local-only QR pairing page. QR matrices never leave the computer."""
import html
import socket
from urllib.parse import urlencode


def pairing_uri(address, token, name=None):
    values = {'v': '1', 'server': address, 'token': token,
              'name': (name or socket.gethostname())[:80]}
    return 'potplayerreceiver://pair?' + urlencode(values)


def qr_svg(contents):
    import qrcode
    from qrcode.image.svg import SvgPathImage
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=6, border=4)
    qr.add_data(contents)
    qr.make(fit=True)
    return qr.make_image(image_factory=SvgPathImage).to_string().decode('utf-8')


def pairing_page(address, token):
    uri = pairing_uri(address, token)
    connect_qr = qr_svg(uri)
    download_qr = qr_svg(address + '/receiver.apk')
    escaped_address = html.escape(address)
    escaped_token = html.escape(token)
    name = html.escape(socket.gethostname())
    return f'''<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Синхро · Подключение</title>
<style>
:root{{color-scheme:dark;font-family:Inter,"Segoe UI",system-ui,sans-serif;color:#f6f5ef;background:#111214}}
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;background:radial-gradient(ellipse at 50% 15%,#302b1b 0,transparent 48%),#111214}}
header{{max-width:1120px;margin:auto;display:flex;align-items:center;gap:12px;padding:20px 32px}}
.logo{{display:grid;place-items:center;width:38px;height:38px;border-radius:12px;background:#ffd34e;color:#181a1e;font-size:21px}}
.brand{{font-weight:650;font-size:18px;letter-spacing:-.4px}}.online{{margin-left:auto;border:1px solid #3a4b3f;border-radius:20px;padding:8px 13px;font-size:12px;color:#a3dbb9}}.online:before{{content:"";display:inline-block;width:6px;height:6px;background:#94d3ac;border-radius:50%;margin-right:8px}}
main{{max-width:940px;margin:12px auto 40px;padding:0 24px;text-align:center}}.eyebrow{{font-size:11px;color:#b6aa87;letter-spacing:2px;font-weight:600}}h1{{font-size:clamp(28px,3vw,36px);line-height:1.13;letter-spacing:-1.5px;margin:12px 0}}.lead{{color:#a4a7ad;font-size:14px;line-height:1.7;margin:0 0 20px}}
.panel{{max-width:720px;margin:auto;background:#1c1e21;border:1px solid #34363b;border-radius:28px;padding:10px;box-shadow:0 24px 100px #0005}}.tabs{{display:flex;gap:6px;padding:6px;background:#131416;border-radius:19px}}button{{font:inherit;cursor:pointer;border:0}}.tab{{flex:1;background:transparent;color:#a4a7ad;padding:15px 12px;border-radius:14px;font-size:14px;font-weight:600}}.tab.active{{background:#ffd34e;color:#181a1e}}
.pane{{display:none;padding:24px 16px 20px}}.pane.active{{display:block}}.qr{{width:min(248px,100%);aspect-ratio:1;background:white;border-radius:20px;padding:10px;margin:0 auto 18px;box-shadow:0 0 0 1px #ffffff15}}.qr svg{{width:100%;height:100%;display:block;shape-rendering:crispEdges}}h2{{font-size:18px;letter-spacing:-.4px;margin:0 0 10px}}.hint{{color:#a4a7ad;font-size:14px;line-height:1.6;margin:0 auto;max-width:440px}}.device{{margin:16px auto 0;display:inline-flex;gap:10px;align-items:center;background:#292b2f;border-radius:12px;padding:10px 14px;color:#d4d5d8;font-size:13px}}.device span{{color:#8e929a}}a{{color:#ffd34e;text-decoration:none}}.secondary{{margin:20px auto 0;background:#292b2f;color:#f6f5ef;border-radius:12px;padding:12px 18px;display:inline-block;font-size:13px}}
details{{max-width:650px;margin:24px auto;color:#a4a7ad;font-size:13px}}summary{{cursor:pointer;list-style:none}}summary:after{{content:" +"}}details[open] summary:after{{content:" −"}}.manual{{background:#1c1e21;border:1px solid #34363b;border-radius:18px;padding:18px;margin-top:12px;text-align:left}}.manual label{{display:block;color:#8e929a;font-size:11px;margin:12px 0 6px}}code{{display:block;overflow-wrap:anywhere;color:#f6f5ef;font:14px ui-monospace,monospace;padding:10px 0}}footer{{color:#6f737a;font-size:12px;line-height:1.7;margin-top:30px}}@media(max-width:600px){{header{{padding:20px}}main{{margin-top:26px;padding:0 16px}}.pane{{padding:24px 8px}}.brand{{font-size:16px}}.online{{font-size:10px;padding:7px 9px}}}}
</style>
<header><div class="logo">▶</div><div class="brand">Синхро</div><div class="online">Сервис работает</div></header>
<main><div class="eyebrow">КОМПЬЮТЕР → ТЕЛЕФОН</div><h1>Телефон и ПК. В одном месте.</h1><p class="lead">Видео, текст и вкладки по прямому подключению.</p>
<section class="panel"><div class="tabs"><button class="tab active" data-tab="connect">Подключить телефон</button><button class="tab" data-tab="download">Установить приложение</button></div>
<div class="pane active" id="connect"><div class="qr" aria-label="QR-код адреса и ключа подключения">{connect_qr}</div><h2>Откройте камеру в Синхро</h2><p class="hint">Нажмите «Сканировать QR-код» в приложении.<br>Адрес компьютера и ключ заполнятся автоматически.</p><div class="device">▣ {name}<span>{escaped_address}</span></div></div>
<div class="pane" id="download"><div class="qr" aria-label="QR-код скачивания Android-приложения">{download_qr}</div><h2>Установите Android-ресивер</h2><p class="hint">Сканируйте этот код обычной камерой телефона.<br>Затем вернитесь во вкладку «Подключить телефон».</p><a class="secondary" href="{escaped_address}/receiver.apk">Скачать APK</a></div></section>
<details><summary>Подключить вручную</summary><div class="manual"><label>Адрес компьютера</label><code>{escaped_address}</code><label>Ключ подключения</label><code>{escaped_token}</code></div></details>
<p><a href="/setup">Полная настройка по QR: приложение и системный буфер</a></p><footer>Телефон и компьютер должны быть в одной локальной сети.<br>Видео передаётся напрямую с вашего компьютера.</footer></main>
<script>document.querySelectorAll('.tab').forEach(button=>button.onclick=()=>{{document.querySelectorAll('.tab,.pane').forEach(item=>item.classList.remove('active'));button.classList.add('active');document.getElementById(button.dataset.tab).classList.add('active')}});</script></html>'''


def download_page():
    return '''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Синхро</title><style>body{font:16px system-ui;background:#111214;color:#f6f5ef;max-width:500px;margin:12vh auto;padding:28px}h1{font-size:30px;letter-spacing:-1px}p{color:#a4a7ad;line-height:1.7}a{display:block;text-align:center;background:#ffd34e;color:#181a1e;font-weight:650;padding:18px;border-radius:18px;text-decoration:none;margin-top:28px}.icon{background:#ffd34e;color:#181a1e;border-radius:24px;width:80px;height:80px;display:grid;place-items:center;font-size:38px}</style>
<div class="icon">▶</div><h1>Синхро</h1><p id="instructions">Видео, буфер обмена и вкладки между телефоном и ПК. После установки подключитесь по QR-коду на компьютере.</p>
<style>[hidden]{display:none!important}</style><a id="open-receiver" hidden>Открыть ресивер</a><a href="/syncro.apk">Скачать для Android</a><p><a href="/chrome-extension.zip">Расширение для Chrome</a></p>
<script>
// Pairing details stay in the URL fragment: browsers never send it to the server.
try {
  const supplied = new URLSearchParams(location.hash.slice(1)).get('pair');
  const pairing = new URL(supplied);
  if (pairing.protocol === 'potplayerreceiver:' && pairing.hostname === 'pair'
      && !pairing.username && !pairing.password && !pairing.port && !pairing.hash
      && ['', '/'].includes(pairing.pathname)
      && pairing.searchParams.get('server') === location.origin
      && pairing.searchParams.get('v') === '1') {
    const button = document.getElementById('open-receiver');
    button.href = 'intent://pair' + pairing.search
      + '#Intent;scheme=potplayerreceiver;package=dev.potplayer.handoff;end';
    button.hidden = false;
    document.getElementById('instructions').textContent =
      'Нажмите «Открыть ресивер»: адрес и ключ уже в ссылке. Если приложение ещё не установлено, сначала скачайте APK, затем вернитесь сюда.';
  }
} catch (_) { /* An ordinary download link needs no pairing details. */ }
</script></html>'''
