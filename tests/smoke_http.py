"""End-to-end demo test; needs permission to bind a localhost socket."""
import json
import base64
import hashlib
import hmac
import os
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen, Request


def get(path):
    request = Request('http://127.0.0.1:18761' + path, headers={'Authorization': 'Bearer demo-only-key'})
    with urlopen(request, timeout=2) as response:
        return json.load(response)


with tempfile.TemporaryDirectory() as directory:
    process = subprocess.Popen([
        'python3', '-m', 'fleet', '--demo', '--db',
        str(Path(directory) / 'demo.sqlite'), '--port', '18761',
    ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
       env={**os.environ, 'DASHBOARD_API_KEY': 'demo-only-key',
            'SAMSARA_WEBHOOK_SECRET': base64.b64encode(b'smoke-secret').decode()})
    try:
        for attempt in range(50):
            if process.poll() is not None:
                raise RuntimeError(process.stderr.read().decode())
            try:
                if get('/health')['ready']:
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError('Demo never became ready')
        summary = get('/analytics')
        assert summary['vehicle_count'] == 3, summary
        assert summary['above_configured_speed_count'] == 1, summary
        vehicles = get('/vehicles?state=moving&sort=speed_desc&limit=1')
        assert vehicles['total_matching'] == 1, vehicles
        assert vehicles['data'][0]['driver']['id'] == 'demo-driver', vehicles
        try:
            get('/vehicles?sort=invalid')
            raise AssertionError('Bad sort accepted')
        except HTTPError as error:
            assert error.code == 400
        try:
            urlopen('http://127.0.0.1:18761/analytics')
            raise AssertionError('Unauthenticated request accepted')
        except HTTPError as error:
            assert error.code == 401
        raw = json.dumps({'eventId': 'smoke-ping', 'eventType': 'Ping', 'event': {'text': 'Ping'}}).encode()
        stamp = str(int(time.time()))
        signature = 'v1=' + hmac.new(b'smoke-secret', b'v1:' + stamp.encode() + b':' + raw, hashlib.sha256).hexdigest()
        for duplicate in (False, True):
            request = Request('http://127.0.0.1:18761/webhooks/samsara', data=raw,
                headers={'X-Samsara-Timestamp': stamp, 'X-Samsara-Signature': signature,
                         'Content-Type': 'application/json'})
            with urlopen(request) as response:
                assert response.status == 202
                assert json.load(response)['duplicate'] == duplicate
        for attempt in range(40):
            if get('/notifications')['data']:
                break
            time.sleep(0.1)
        else:
            raise AssertionError('Outbox did not deliver local notification')
        assert get('/incidents')['data'][0]['attribution']['driver']['id'] == 'demo-driver'
        assert get('/analytics/history')['vehicles']
        assert get('/operations')['telemetry_rows'] >= 6
        print(json.dumps(summary, indent=2))
        print('HTTP smoke passed: authentication, signed webhook, deduplication, history, driver matching and outbox.')
    finally:
        process.terminate()
        process.wait(timeout=5)
        process.stderr.close()
