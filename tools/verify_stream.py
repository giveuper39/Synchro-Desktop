"""Use the running service to decode the actual MKV over HTTP, then hand back.

This test captures the currently paused player and checkpoints the same position.
The pairing key and media URLs are intentionally omitted from the output.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', default='http://127.0.0.1:8765')
    args = parser.parse_args()
    key = (ROOT / '.data/pairing-key.txt').read_text().strip()
    def post(endpoint, data):
        request = urllib.request.Request(args.server + endpoint,
                                          json.dumps(data).encode(),
                                          {'Authorization': 'Bearer ' + key,
                                           'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(request, timeout=40) as response:
            return json.load(response)
    state = post('/api/takeover', {})
    item = state['items'][state['index']]
    evidence = {'queueLength': len(state['items']), 'playlistSource': state['playlistSource']}
    try:
        request = urllib.request.Request(item['url'], headers={'Range': 'bytes=0-1048575'})
        with urllib.request.urlopen(request, timeout=10) as response:
            data = response.read()
            evidence['rangeStatus'] = response.status
            evidence['firstMiB_SHA256'] = hashlib.sha256(data).hexdigest()
            if response.status != 206 or len(data) != 1048576:
                raise AssertionError('HTTP range streaming failed')
        process = subprocess.run(['ffmpeg', '-v', 'error', '-ss', str(state['positionMs'] / 1000),
                                  '-i', item['url'], '-t', '1', '-map', '0:v:0', '-map', '0:a:0',
                                  '-f', 'null', '-'], capture_output=True, timeout=30,
                                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        evidence['decodeSucceeded'] = process.returncode == 0
        # ffmpeg diagnostics can include the credential-bearing URL, so retain only status.
        if process.returncode:
            raise AssertionError('HTTP video/audio decoding failed')
    finally:
        returned = post('/api/progress', {'sessionId': state['sessionId'], 'sequence': 1,
                                          'index': state['index'], 'positionMs': state['positionMs'],
                                          'paused': True, 'audioIndex': state['audioIndex'],
                                          'subtitleIndex': state['subtitleIndex'],
                                          'subtitleEnabled': state['subtitleEnabled'],
                                          'subtitleDelayMs': state['subtitleDelayMs']})
        evidence['handedBackToPC'] = returned['owner'] == 'pc'
        (ROOT / '.data/stream-validation.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
    print(json.dumps(evidence, indent=2))


if __name__ == '__main__':
    main()
