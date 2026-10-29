"""Transactional checkpoints and a materialized latest-state view."""
import json
import math
import sqlite3
import time
from collections import Counter
from datetime import datetime

from .client import page_parts
from .operations import Operations


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Timestamp needs a timezone')
    return parsed.timestamp()


class Store(Operations):
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS vehicles (id TEXT PRIMARY KEY, document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS assignments (
                vehicle_id TEXT, driver_id TEXT, name TEXT, start REAL, end REAL, source TEXT);
            CREATE INDEX IF NOT EXISTS assignment_vehicle ON assignments(vehicle_id);
        ''')
        self.init_operations()

    def close(self):
        self.db.close()

    def get_meta(self, key, default=None):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(value)))

    def ingest(self, page, now=None):
        now = time.time() if now is None else now
        data, cursor, more = page_parts(page)
        # On validation or persistence failure, BOTH state and checkpoint roll back.
        with self.db:
            for incoming in data:
                vid = incoming['id']
                if not isinstance(vid, str) or not vid:
                    raise ValueError('Expected string vehicle ID')
                row = self.db.execute('SELECT document FROM vehicles WHERE id=?', (vid,)).fetchone()
                state = json.loads(row[0]) if row else {'vehicle_id': vid}
                state['name'] = incoming.get('name', state.get('name', vid))
                for field in ('gps', 'engineStates'):
                    readings = incoming.get(field, [])
                    if not isinstance(readings, list):
                        raise ValueError('Expected a statistic array')
                    for reading in readings:
                        event_time = timestamp(reading['time'])
                        if event_time > now + 30:
                            raise ValueError('Reading too far in the future')
                        if field == 'gps':
                            speed = reading.get('speedMilesPerHour')
                            if speed is not None and (type(speed) not in (int, float)
                                    or not math.isfinite(speed) or speed < 0):
                                raise ValueError('Invalid speed')
                        elif reading['value'] not in ('On', 'Off', 'Idle'):
                            raise ValueError('Unexpected engine state')
                        self.db.execute('''INSERT OR IGNORE INTO telemetry
                            (vehicle_id, metric, event_time, received_at, payload)
                            VALUES (?,?,?,?,?)''',
                            (vid, field, event_time, now, json.dumps(reading, allow_nan=False)))
                        # Each signal has its own clock. Missing fields preserve old state.
                        if event_time > state.get(field + '_time', -1):
                            state[field] = reading
                            state[field + '_time'] = event_time
                self.db.execute('INSERT OR REPLACE INTO vehicles VALUES (?,?)',
                                (vid, json.dumps(state, allow_nan=False)))
            self.set_meta('cursor', cursor)
            self.set_meta('last_feed_success', now)
        return more

    def replace_assignments(self, records, observed_at, coverage_start=None):
        rows = []
        for record in records:
            if type(record['isPassenger']) is not bool:
                raise ValueError('Expected passenger boolean')
            if record['isPassenger']:
                continue
            rows.append((record['vehicle']['id'], record['driver']['id'],
                         record['driver'].get('name'), timestamp(record['startTime']),
                         timestamp(record['endTime']) if record.get('endTime') else None,
                         record.get('assignmentType', 'unknown')))
        with self.db:
            self.db.execute('DELETE FROM assignments')
            self.db.executemany('INSERT INTO assignments VALUES (?,?,?,?,?,?)', rows)
            self.set_meta('assignment_observed_at', observed_at)
            if coverage_start is not None:
                # A complete rolling window is authoritative, including deletions.
                self.db.execute('DELETE FROM assignment_history')
                self.db.executemany('INSERT INTO assignment_history VALUES (?,?,?,?,?,?)', rows)
                self.set_meta('assignment_coverage', [coverage_start, observed_at])

    def view(self, now=None, fresh_seconds=60, speed_threshold=65):
        now = time.time() if now is None else now
        feed_ok = now - self.get_meta('last_feed_success', 0) <= fresh_seconds
        assignments_ok = now - self.get_meta('assignment_observed_at', 0) <= 90
        assignments = {}
        if assignments_ok:
            for vid, did, name, start, end, source in self.db.execute('SELECT * FROM assignments'):
                if start <= now and (end is None or now < end):
                    assignments.setdefault(vid, {})[did] = {'id': did, 'name': name, 'source': source}
        result = []
        for (document,) in self.db.execute('SELECT document FROM vehicles'):
            record = json.loads(document)
            gps = record.get('gps', {})
            speed = gps.get('speedMilesPerHour')
            gps_age = now - record['gps_time'] if 'gps_time' in record else None
            engine = record.get('engineStates', {}).get('value')
            motion = 'unknown'
            if feed_ok and gps_age is not None and -30 <= gps_age <= fresh_seconds and speed is not None:
                if speed > 3:
                    motion = 'moving'
                elif speed > 1:
                    motion = 'slow_moving'
                elif engine in ('On', 'Idle'):
                    motion = 'engine_on_stationary'
                elif engine == 'Off':
                    motion = 'engine_off_stationary'
                else:
                    motion = 'stationary_engine_unknown'
            drivers = list(assignments.get(record['vehicle_id'], {}).values())
            result.append({
                'vehicle_id': record['vehicle_id'], 'name': record['name'],
                'speed_mph': speed, 'motion_state': motion,
                'engine_state_last_known': engine,
                'engine_state_at': record.get('engineStates_time'),
                'gps_at': record.get('gps_time'), 'gps_age_seconds': gps_age,
                'latitude': gps.get('latitude'), 'longitude': gps.get('longitude'),
                'driver': drivers[0] if len(drivers) == 1 else None,
                'driver_status': ('matched' if len(drivers) == 1 else 'conflict' if drivers else 'unknown'),
                'above_configured_speed': motion != 'unknown' and speed > speed_threshold,
            })
        return result


def analytics(vehicles):
    valid = [v['speed_mph'] for v in vehicles if v['motion_state'] != 'unknown']
    return {'vehicle_count': len(vehicles),
            'counts_by_state': dict(Counter(v['motion_state'] for v in vehicles)),
            'average_speed_mph_fresh_vehicles': sum(valid) / len(valid) if valid else None,
            'above_configured_speed_count': sum(v['above_configured_speed'] for v in vehicles),
            'unresolved_driver_count': sum(v['driver_status'] != 'matched' for v in vehicles)}
