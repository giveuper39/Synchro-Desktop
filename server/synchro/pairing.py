"""Local-only QR pairing page. QR matrices never leave the computer."""
from pathlib import Path
import socket
from urllib.parse import urlencode


def pairing_uri(address, token, name=None):
    values = {'v': '1', 'server': address, 'token': token,
              'name': (name or socket.gethostname())[:80]}
    return 'synchro://pair?' + urlencode(values)


def connection_url(address, token, name=None):
    """A camera-friendly link; the pairing key never enters an HTTP request."""
    values = {'v': '1', 'token': token, 'name': (name or socket.gethostname())[:80]}
    return address.rstrip('/') + '/connect#' + urlencode(values)


def pairing_details(address, token, name=None):
    link = connection_url(address, token, name)
    return {'url': link, 'qrSvg': qr_svg(link), 'name': name or socket.gethostname(), 'server': address}


def qr_svg(contents):
    import qrcode
    from qrcode.image.svg import SvgPathImage
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=6, border=4)
    qr.add_data(contents)
    qr.make(fit=True)
    return qr.make_image(image_factory=SvgPathImage).to_string().decode('utf-8')


def pairing_page(address, token):
    from .setup_page import setup_page
    # Keep the old bookmarked desktop route; it now uses the same single-QR flow.
    return setup_page()


def download_page():
    return (Path(__file__).with_name('ui') / 'connect.html').read_text(encoding='utf-8')
