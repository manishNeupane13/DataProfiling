"""Run: python3 -m fleet --help"""
import argparse
import hmac
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .client import Samsara, page_parts
from .store import Store, analytics
from .security import verify_webhook

LOG = logging.getLogger('fleet')


def iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def poll_feed(path, client, stop):
    store = Store(path)
    try:
        while not stop.is_set():
            started = time.perf_counter()
            try:
                params = {'types': 'gps,engineStates'}
                cursor = store.get_meta('cursor')
                if cursor:
                    params['after'] = cursor
                response = client.get('/fleet/vehicles/stats/feed', params)
                _, next_cursor, more = page_parts(response)
                if more and next_cursor == cursor:
                    raise ValueError('Feed cursor did not progress')
                received = time.perf_counter()
                store.ingest(response)
                LOG.info('feed api_ms=%.1f processing_ms=%.1f vehicles=%d',
                         (received - started) * 1000,
                         (time.perf_counter() - received) * 1000, len(response['data']))
                stop.wait(0.25 if more else 5)
            except Exception as exc:
                # Do not dump response bodies or credentials into logs.
                LOG.error('feed failed: %s; checkpoint preserved', type(exc).__name__)
                stop.wait(15)
    finally:
        store.close()


def poll_assignments(path, client, stop, assignment_type):
    store = Store(path)
    try:
        while not stop.is_set():
            try:
                at = time.time()
                records = client.assignments(iso(at), assignment_type, start=iso(at - 86400))
                store.replace_assignments(records, at, coverage_start=at - 86400)
                LOG.info('assignment snapshot records=%d', len(records))
            except Exception as exc:
                LOG.error('assignments failed: %s; previous snapshot ages out', type(exc).__name__)
            stop.wait(30)
    finally:
        store.close()


def demo(path, stop):
    """Synthetic inputs in the Samsara response shape; never calls Samsara."""
    store = Store(path)
    tick = 0
    try:
        while not stop.is_set():
            now = time.time()
            data = []
            for vid, name, speed, engine in [
                ('demo-1', 'Demo Highway Truck', 68 + tick % 4, 'On'),
                ('demo-2', 'Demo Stationary Truck', 0, 'Idle'),
                ('demo-3', 'Demo Parked Truck', 0, 'Off'),
            ]:
                data.append({'id': vid, 'name': name,
                             'gps': [{'time': iso(now), 'speedMilesPerHour': speed,
                                      'latitude': 41.88, 'longitude': -87.63}],
                             'engineStates': [{'time': iso(now), 'value': engine}]})
            store.ingest({'data': data, 'pagination': {'endCursor': f'demo-{tick}', 'hasNextPage': False}})
            store.replace_assignments([{
                'vehicle': {'id': 'demo-1'}, 'driver': {'id': 'demo-driver', 'name': 'Demo Driver'},
                'startTime': iso(now - 3600), 'isPassenger': False, 'assignmentType': 'driverApp',
            }], now, coverage_start=now - 3600)
            if tick % 6 == 0:
                event = {'eventId': f'demo-brake-{int(now)}', 'eventType': 'Alert',
                         'eventMs': int(now * 1000), 'event': {
                             'device': {'id': 'demo-1'}, 'alertConditionId': 'HarshEvent',
                             'startMs': int((now - 10) * 1000), 'summary': 'Synthetic harsh-braking event'}}
                store.accept_webhook(event)
                store.accept_webhook(event)  # Real-world duplicate delivery scenario.
            tick += 1
            stop.wait(5)
    finally:
        store.close()


def action_worker(path, stop):
    store = Store(path)
    try:
        while not stop.is_set():
            try:
                store.process_inbox()
                store.deliver_local()
            except Exception:
                LOG.exception('Action processing failed; pending work remains durable')
            stop.wait(1)
    finally:
        store.close()


def handler_for(path, threshold, api_key=None, webhook_secret=None):
    class Handler(BaseHTTPRequestHandler):
        def reply(self, code, payload):
            body = json.dumps(payload, allow_nan=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if urlparse(self.path).path != '/webhooks/samsara':
                return self.reply(404, {'error': 'Not found'})
            if not webhook_secret:
                return self.reply(503, {'error': 'Webhook secret is not configured'})
            try:
                self.connection.settimeout(10)
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 1_000_000 or self.headers.get('Transfer-Encoding'):
                    return self.reply(413, {'error': 'Expected bounded Content-Length body'})
                raw = self.rfile.read(size)
                if len(raw) != size or not verify_webhook(raw,
                        self.headers.get('X-Samsara-Timestamp'),
                        self.headers.get('X-Samsara-Signature'), webhook_secret):
                    return self.reply(401, {'error': 'Invalid signature or timestamp'})
                event = json.loads(raw)
                store = Store(path)
                try:
                    inserted = store.accept_webhook(event)
                finally:
                    store.close()
                return self.reply(202, {'accepted': True, 'duplicate': not inserted})
            except (ValueError, TypeError, UnicodeError):
                return self.reply(400, {'error': 'Invalid JSON payload'})
            except Exception:
                LOG.exception('Webhook receipt failed')
                return self.reply(503, {'error': 'Could not durably accept event'})

        def do_GET(self):
            url = urlparse(self.path)
            if url.path != '/health' and (not api_key or not hmac.compare_digest(
                    self.headers.get('Authorization', ''), 'Bearer ' + api_key)):
                return self.reply(401, {'error': 'Dashboard API bearer token required'})
            query = parse_qs(url.query)
            code = 200
            store = Store(path)
            try:
                if url.path == '/health':
                    last = store.get_meta('last_feed_success')
                    age = time.time() - last if last is not None else None
                    code = 200 if age is not None and age <= 60 else 503
                    payload = {'feed_age_seconds': age, 'ready': code == 200,
                               'mode': store.get_meta('mode'),
                               'assignment_observed_at': store.get_meta('assignment_observed_at')}
                elif url.path in ('/vehicles', '/analytics'):
                    vehicles = store.view(speed_threshold=threshold)
                    if url.path == '/analytics':
                        payload = analytics(vehicles)
                    else:
                        state = query.get('state', [None])[0]
                        sort = query.get('sort', ['speed_desc'])[0]
                        if sort not in ('speed_desc', 'vehicle_id'):
                            raise ValueError('sort must be speed_desc or vehicle_id')
                        limit = int(query.get('limit', ['100'])[0])
                        if not 1 <= limit <= 1000:
                            raise ValueError('limit must be between 1 and 1000')
                        if state:
                            vehicles = [v for v in vehicles if v['motion_state'] == state]
                        if sort == 'speed_desc':
                            vehicles.sort(key=lambda v: (-(v['speed_mph'] if v['speed_mph'] is not None else -1),
                                                         v['vehicle_id']))
                        else:
                            vehicles.sort(key=lambda v: v['vehicle_id'])
                        payload = {'total_matching': len(vehicles), 'data': vehicles[:limit]}
                elif url.path in ('/incidents', '/notifications'):
                    limit = int(query.get('limit', ['100'])[0])
                    offset = int(query.get('offset', ['0'])[0])
                    if not 1 <= limit <= 1000 or offset < 0:
                        raise ValueError('Invalid limit or offset')
                    if url.path == '/incidents':
                        payload = {'data': store.incident_list(limit, offset)}
                    else:
                        rows = store.db.execute('SELECT payload,delivered_at FROM notifications ORDER BY delivered_at DESC,id LIMIT ? OFFSET ?', (limit, offset)).fetchall()
                        payload = {'data': [{'notification': json.loads(row[0]), 'delivered_at': row[1]} for row in rows]}
                elif url.path == '/analytics/history':
                    until = float(query.get('to', [str(time.time())])[0])
                    since = float(query.get('from', [str(until - 3600)])[0])
                    if not 0 <= since < until or until - since > 86400 * 7:
                        raise ValueError('Use Unix seconds and a range no longer than seven days')
                    payload = store.historical_analytics(since, until)
                elif url.path == '/operations':
                    payload = store.operations_status()
                else:
                    code, payload = 404, {'error': 'Use /health, /vehicles or /analytics'}
            except ValueError as exc:
                code, payload = 400, {'error': str(exc)}
            except Exception:
                LOG.exception('Local request failed')
                code, payload = 500, {'error': 'Internal error'}
            finally:
                store.close()
            self.reply(code, payload)

        def log_message(self, fmt, *args):
            LOG.info(fmt, *args)
    return Handler


def main():
    parser = argparse.ArgumentParser(description='Local Samsara analytics backend')
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('--db', help='Defaults to separate demo/live SQLite files')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--assignment-type', default='driverApp',
                        help='Choose the assignment source actually used by your fleet')
    parser.add_argument('--speed-threshold', type=float, default=65)
    args = parser.parse_args()
    if not 0 < args.speed_threshold < 300:
        parser.error('speed threshold must be between 0 and 300 mph')
    token = os.environ.get('SAMSARA_API_TOKEN')
    api_key = os.environ.get('DASHBOARD_API_KEY') or ('demo-only-key' if args.demo else None)
    if not api_key:
        parser.error('Set DASHBOARD_API_KEY for the local dashboard API')
    if not args.demo and not token:
        parser.error('Set SAMSARA_API_TOKEN or use --demo')
    path = args.db or ('demo.sqlite' if args.demo else 'fleet.sqlite')
    mode = 'demo' if args.demo else 'live'
    store = Store(path)
    try:
        previous = store.get_meta('mode')
        if previous and previous != mode:
            parser.error('Use a separate database for demo and live modes')
        with store.db:
            store.set_meta('mode', mode)
    finally:
        store.close()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler_for(
        path, args.speed_threshold, api_key, os.environ.get('SAMSARA_WEBHOOK_SECRET')))
    stop = threading.Event()
    if args.demo:
        jobs = [(demo, (path, stop))]
    else:
        jobs = [(poll_feed, (path, Samsara(token), stop)),
                (poll_assignments, (path, Samsara(token), stop, args.assignment_type))]
    jobs.append((action_worker, (path, stop)))
    threads = [threading.Thread(target=fn, args=values, daemon=True) for fn, values in jobs]
    for thread in threads:
        thread.start()
    LOG.info('Listening on http://127.0.0.1:%d in %s mode', args.port, mode)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        for thread in threads:
            thread.join(timeout=1)


if __name__ == '__main__':
    main()
