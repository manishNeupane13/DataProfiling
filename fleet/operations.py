"""Durable webhook inbox, incident history, and transactional local outbox."""
import json
import time


class Operations:
    def init_operations(self):
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS telemetry (
                id INTEGER PRIMARY KEY, vehicle_id TEXT NOT NULL, metric TEXT NOT NULL,
                event_time REAL NOT NULL, received_at REAL NOT NULL, payload TEXT NOT NULL,
                UNIQUE(vehicle_id, metric, event_time));
            CREATE INDEX IF NOT EXISTS telemetry_time ON telemetry(event_time);
            CREATE INDEX IF NOT EXISTS telemetry_vehicle_time ON telemetry(vehicle_id,event_time);
            CREATE TABLE IF NOT EXISTS assignment_history (
                vehicle_id TEXT, driver_id TEXT, name TEXT, start REAL, end REAL, source TEXT);
            CREATE INDEX IF NOT EXISTS history_assignment_vehicle ON assignment_history(vehicle_id,start,end);
            CREATE TABLE IF NOT EXISTS inbox (
                event_id TEXT PRIMARY KEY, payload TEXT NOT NULL, received_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', reason TEXT);
            CREATE TABLE IF NOT EXISTS incidents (
                id TEXT PRIMARY KEY, event_id TEXT NOT NULL, vehicle_id TEXT NOT NULL,
                kind TEXT NOT NULL, occurred_at REAL NOT NULL, received_at REAL NOT NULL,
                summary TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS incident_time ON incidents(occurred_at);
            CREATE TABLE IF NOT EXISTS outbox (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS notifications (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, delivered_at REAL NOT NULL);
        ''')

    def accept_webhook(self, event, now=None):
        now = time.time() if now is None else now
        if not isinstance(event, dict) or not isinstance(event.get('eventId'), str) or not event['eventId']:
            raise ValueError('eventId must be a nonempty string')
        with self.db:
            count = self.db.execute('INSERT OR IGNORE INTO inbox(event_id,payload,received_at) VALUES (?,?,?)',
                                   (event['eventId'], json.dumps(event, allow_nan=False), now)).rowcount
        return bool(count)

    def process_inbox(self, now=None):
        now = time.time() if now is None else now
        # This sample normalizes documented v1 Alert payloads. Other signed versions
        # are retained as unsupported, never guessed or silently discarded.
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            rows = self.db.execute("SELECT event_id,payload,received_at FROM inbox WHERE status='pending' LIMIT 100").fetchall()
            for event_id, raw, received in rows:
                event = json.loads(raw)
                if event.get('eventType') == 'Ping':
                    self.db.execute("UPDATE inbox SET status='processed' WHERE event_id=?", (event_id,))
                    continue
                if event.get('eventType') != 'Alert':
                    self.db.execute("UPDATE inbox SET status='unsupported', reason='No adapter for this eventType' WHERE event_id=?", (event_id,))
                    continue
                try:
                    detail = event['event']
                    vehicle_id = str(detail['device']['id'])
                    kind = detail['alertConditionId']
                    occurred = detail['startMs'] / 1000
                    if not vehicle_id or not isinstance(kind, str) or not 0 < occurred <= now + 30:
                        raise ValueError('Invalid incident fields')
                    # Stable incident identity also collapses distinct delivery IDs.
                    identity = json.dumps([vehicle_id, kind, detail['startMs']], separators=(',', ':'))
                    summary = str(detail.get('summary', kind))
                except (KeyError, TypeError, ValueError):
                    self.db.execute("UPDATE inbox SET status='quarantined',reason='Invalid Alert payload' WHERE event_id=?", (event_id,))
                    continue
                inserted = self.db.execute('INSERT OR IGNORE INTO incidents VALUES (?,?,?,?,?,?,?)',
                    (identity, event_id, vehicle_id, kind, occurred, received, summary)).rowcount
                # Historical events are recorded, but do not become urgent notifications.
                if inserted and 0 <= now - occurred <= 300:
                    payload = json.dumps({'incident_id': identity, 'vehicle_id': vehicle_id,
                                          'kind': kind, 'occurred_at': occurred, 'summary': summary})
                    self.db.execute('INSERT OR IGNORE INTO outbox(id,payload) VALUES (?,?)', (identity, payload))
                self.db.execute("UPDATE inbox SET status='processed' WHERE event_id=?", (event_id,))

    def deliver_local(self, now=None, fail=False):
        """Idempotent database sink: demonstrates delivery without messaging people.

        A remote sink needs an idempotency key and receiver-side deduplication;
        a local transaction cannot promise exactly-once remote delivery.
        """
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            rows = self.db.execute("SELECT id,payload,attempts FROM outbox WHERE status='pending' AND next_attempt<=? LIMIT 100", (now,)).fetchall()
            for identity, payload, attempts in rows:
                if fail:
                    attempts += 1
                    self.db.execute('UPDATE outbox SET attempts=?,next_attempt=?,status=? WHERE id=?',
                        (attempts, now + min(300, 2 ** attempts), 'failed' if attempts >= 5 else 'pending', identity))
                    continue
                self.db.execute('INSERT OR IGNORE INTO notifications VALUES (?,?,?)', (identity, payload, now))
                self.db.execute("UPDATE outbox SET status='delivered',attempts=attempts+1 WHERE id=?", (identity,))

    def driver_at(self, vehicle_id, occurred):
        coverage = self.get_meta('assignment_coverage')
        if not coverage or not coverage[0] <= occurred <= coverage[1]:
            return {'status': 'outside_coverage', 'driver': None}
        rows = self.db.execute('''SELECT DISTINCT driver_id,name,source FROM assignment_history
            WHERE vehicle_id=? AND start<=? AND (end IS NULL OR end>?)''',
            (vehicle_id, occurred, occurred)).fetchall()
        drivers = {row[0]: {'id': row[0], 'name': row[1], 'source': row[2]} for row in rows}
        return {'status': 'matched' if len(drivers) == 1 else 'conflict' if drivers else 'unknown',
                'driver': next(iter(drivers.values())) if len(drivers) == 1 else None}

    def incident_list(self, limit=100, offset=0):
        rows = self.db.execute('''SELECT id,vehicle_id,kind,occurred_at,received_at,summary
            FROM incidents ORDER BY occurred_at DESC,id LIMIT ? OFFSET ?''', (limit, offset)).fetchall()
        return [dict(zip(('id','vehicle_id','kind','occurred_at','received_at','summary'), row),
                     attribution=self.driver_at(row[1], row[3])) for row in rows]

    def historical_analytics(self, since, until):
        # Sample-weighted, explicitly NOT a trip/time-weighted average.
        rows = self.db.execute('''SELECT vehicle_id,COUNT(*),AVG(json_extract(payload,'$.speedMilesPerHour')),
            MAX(json_extract(payload,'$.speedMilesPerHour')) FROM telemetry
            WHERE metric='gps' AND event_time>=? AND event_time<?
            AND json_extract(payload,'$.speedMilesPerHour') IS NOT NULL GROUP BY vehicle_id''', (since, until)).fetchall()
        incidents = self.db.execute('''SELECT kind,COUNT(*) FROM incidents
            WHERE occurred_at>=? AND occurred_at<? GROUP BY kind''', (since, until)).fetchall()
        return {'from': since, 'to': until, 'speed_aggregation': 'sample_weighted',
                'vehicles': [dict(zip(('vehicle_id','sample_count','average_sample_speed_mph','max_sample_speed_mph'), row)) for row in rows],
                'incidents_by_kind': dict(incidents)}

    def operations_status(self):
        result = {}
        for table in ('inbox', 'outbox'):
            result[table] = dict(self.db.execute(f'SELECT status,COUNT(*) FROM {table} GROUP BY status'))
        result['assignment_coverage'] = self.get_meta('assignment_coverage')
        result['telemetry_rows'] = self.db.execute('SELECT COUNT(*) FROM telemetry').fetchone()[0]
        return result
