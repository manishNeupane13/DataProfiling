import tempfile
import unittest
from pathlib import Path

from fleet.__main__ import iso
from fleet.client import Samsara, retry_delay
from fleet.store import Store, analytics


def page(at=1000, speed=50, engine='On', cursor='one'):
    vehicle = {'id': '123', 'name': 'Truck',
               'gps': [{'time': iso(at), 'speedMilesPerHour': speed}]}
    if engine is not None:
        vehicle['engineStates'] = [{'time': iso(at), 'value': engine}]
    return {'data': [vehicle], 'pagination': {'endCursor': cursor, 'hasNextPage': False}}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'test.sqlite')
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_partial_and_out_of_order_readings(self):
        self.store.ingest(page(), now=1000)
        self.store.ingest(page(1010, 0, None, 'two'), now=1010)
        self.store.ingest(page(900, 99, 'Off', 'three'), now=1011)
        row = self.store.view(now=1011)[0]
        self.assertEqual(row['speed_mph'], 0)
        self.assertEqual(row['engine_state_last_known'], 'On')
        self.assertEqual(row['motion_state'], 'engine_on_stationary')

    def test_replay_does_not_duplicate_vehicles(self):
        self.store.ingest(page(), now=1000)
        self.store.ingest(page(), now=1000)
        self.assertEqual(analytics(self.store.view(now=1000))['vehicle_count'], 1)

    def test_bad_page_rolls_back_state_and_cursor(self):
        self.store.ingest(page(), now=1000)
        bad = page(1010, 70, cursor='two')
        bad['data'].append({'id': 'bad', 'gps': [{'time': 'invalid'}]})
        with self.assertRaises(ValueError):
            self.store.ingest(bad, now=1010)
        self.assertEqual(self.store.get_meta('cursor'), 'one')
        self.assertEqual(self.store.view(now=1010)[0]['speed_mph'], 50)

    def test_stale_data_excluded_from_live_analytics(self):
        self.store.ingest(page(speed=80), now=1000)
        rows = self.store.view(now=1100)
        self.assertEqual(rows[0]['motion_state'], 'unknown')
        self.assertFalse(rows[0]['above_configured_speed'])
        self.assertIsNone(analytics(rows)['average_speed_mph_fresh_vehicles'])

    def test_missing_speed_not_zero(self):
        self.store.ingest(page(speed=None), now=1000)
        self.assertEqual(self.store.view(now=1000)[0]['motion_state'], 'unknown')

    def test_passengers_conflicts_and_assignment_expiry(self):
        self.store.ingest(page(), now=1000)
        def assignment(driver, passenger=False, end=None):
            return {'vehicle': {'id': '123'}, 'driver': {'id': driver},
                    'startTime': iso(900), 'endTime': iso(end) if end else None,
                    'isPassenger': passenger, 'assignmentType': 'driverApp'}
        self.store.replace_assignments([assignment('A'), assignment('B', True)], 1000)
        self.assertEqual(self.store.view(now=1000)[0]['driver']['id'], 'A')
        self.store.replace_assignments([assignment('A', end=1000), assignment('B')], 1000)
        self.assertEqual(self.store.view(now=1000)[0]['driver']['id'], 'B')
        self.store.replace_assignments([assignment('A'), assignment('B')], 1000)
        self.assertEqual(self.store.view(now=1000)[0]['driver_status'], 'conflict')
        self.assertEqual(self.store.view(now=1100)[0]['driver_status'], 'unknown')

    def test_checkpoint_survives_restart(self):
        self.store.ingest(page(), now=1000)
        other = Store(self.path)
        try:
            self.assertEqual(other.get_meta('cursor'), 'one')
        finally:
            other.close()

    def test_assignment_pagination(self):
        client = Samsara('fake-test-token')
        calls = []
        def fake_get(path, params):
            calls.append(dict(params))
            return {'data': [{'test': len(calls)}],
                    'pagination': {'endCursor': 'next' if len(calls) == 1 else '',
                                   'hasNextPage': len(calls) == 1}}
        client.get = fake_get
        self.assertEqual(len(client.assignments(iso(1000), 'driverApp')), 2)
        self.assertEqual(calls[1]['after'], 'next')
        self.assertEqual(calls[0]['startTime'], calls[1]['startTime'])

    def test_retry_after_seconds(self):
        self.assertEqual(retry_delay('12', 0), 12)


if __name__ == '__main__':
    unittest.main()
