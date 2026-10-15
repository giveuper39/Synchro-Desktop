"""Run the local service: python -m potbridge [--playlist queue.dpl]."""
import argparse
import logging
from pathlib import Path
import secrets
import socket
import os
import subprocess
import threading
import webbrowser
import importlib.util

from .http_server import HandoffHTTPServer
from .library import MediaLibrary
from .sessions import SessionManager


def lan_address():
    # A system VPN can own the default route. Prefer Windows' Private LAN
    # adapter for the address shown to a phone on the home Wi-Fi.
    if os.name == 'nt':
        try:
            powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
            script = ("$ErrorActionPreference='Stop'; Get-NetConnectionProfile | "
                      "Where-Object NetworkCategory -eq 'Private' | ForEach-Object { "
                      "Get-NetIPAddress -InterfaceIndex $_.InterfaceIndex -AddressFamily IPv4 } | "
                      "Select-Object -ExpandProperty IPAddress")
            result = subprocess.check_output([str(powershell), '-NoProfile', '-NonInteractive',
                                               '-Command', script], timeout=8,
                                              creationflags=subprocess.CREATE_NO_WINDOW)
            for candidate in result.decode('ascii').split():
                if not candidate.startswith(('127.', '169.254.')):
                    return candidate
        except (OSError, KeyError, subprocess.SubprocessError):
            pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
            connection.connect(('192.0.2.1', 9))  # Route selection only; no datagram is sent.
            return connection.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())


def main():
    parser = argparse.ArgumentParser(description='Синхро — видео, буфер обмена и вкладки')
    parser.add_argument('--bind', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--advertise-host', help='LAN address shown on the pairing page')
    parser.add_argument('--playlist', type=Path)
    parser.add_argument('--player-exe', type=Path)
    parser.add_argument('--player-pid', type=int)
    parser.add_argument('--data-dir', type=Path, default=Path(__file__).resolve().parents[2] / '.data')
    parser.add_argument('--open', action='store_true', help='Open the local pairing page')
    args = parser.parse_args()
    from .single_instance import claim_service
    instance = claim_service(args.port)
    if instance is None:
        return
    if importlib.util.find_spec('qrcode') is None or importlib.util.find_spec('zeroconf') is None:
        parser.error('Нужны пакеты qrcode и zeroconf. Запустите start-server.cmd: он подготовит окружение автоматически.')
    args.data_dir.mkdir(parents=True, exist_ok=True)
    token_path = args.data_dir / 'pairing-key.txt'
    if not token_path.exists():
        token_path.write_text(secrets.token_urlsafe(24), encoding='ascii')
    token = token_path.read_text(encoding='ascii').strip()
    if len(token) < 24 or not token.isascii():
        parser.error('Invalid pairing key; remove .data/pairing-key.txt to generate another')
    from .player import WindowsPlayer
    from .sharing import DeviceHub
    player = WindowsPlayer(args.player_exe, args.player_pid)
    manager = SessionManager(player, MediaLibrary(), token, args.data_dir, args.playlist)
    apk = Path(__file__).resolve().parents[2] / 'dist' / 'potplayer-receiver.apk'
    address = args.advertise_host or lan_address()
    hub = DeviceHub(Path(__file__).resolve().parents[2])
    from .wireless import WirelessSetup
    wireless = WirelessSetup(Path(__file__).resolve().parents[2], hub, f'http://{address}:{args.port}', token)
    hub.wireless = wireless
    server = HandoffHTTPServer((args.bind, args.port), manager, apk, address, hub=hub)
    instance.publish(server.server_port)
    from .updates import Updates
    server.updates = Updates(Path(__file__).resolve().parents[2], server.server_port)
    import json
    (args.data_dir / 'service.json').write_text(json.dumps({'port': server.server_port, 'bind': args.bind,
                                                         'advertiseHost': address}), encoding='utf-8')
    stopped = threading.Event()
    def monitor():
        while not stopped.wait(1):
            try:
                manager.watchdog()
            except Exception as error:
                logging.warning('Player checkpoint unavailable: %s', type(error).__name__)
    thread = threading.Thread(target=monitor, name='handoff-watchdog', daemon=True)
    thread.start()
    print('Синхро · сервис готов', flush=True)
    print(f'Телефон: http://{address}:{server.server_port}', flush=True)
    print(f'QR-код: http://127.0.0.1:{server.server_port}/pair', flush=True)
    if args.open:
        webbrowser.open(f'http://127.0.0.1:{server.server_port}/setup')
    try:
        server.serve_forever(poll_interval=.3)
    except KeyboardInterrupt:
        pass
    finally:
        stopped.set()
        server.server_close()
        thread.join(timeout=10)
        player.close()
        hub.close()
        wireless.close()
        instance.close()


if __name__ == '__main__':
    main()
