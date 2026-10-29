import base64
import hashlib
import hmac
import json

import tempfile
import unittest
from pathlib import Path
from test_pipeline import page
from fleet.store import Store
from fleet.__main__ import iso
from fleet.security import verify_webhook


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.tmp.name) / "test.sqlite"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def alert(self, identity='delivery-1', occurred=1000):
        return {'eventId': identity, 'eventType': 'Alert', 'event': {
            'device': {'id': '123'}, 'alertConditionId': 'HarshEvent',
            'startMs': occurred * 1000, 'summary': 'Test braking'}}

    def test_history_dedup_and_late_data(self):
        self.store.ingest(page(), now=1000)
        self.store.ingest(page(), now=1000)
        self.store.ingest(page(at=900, speed=10), now=1000)
        data = self.store.historical_analytics(800, 1100)['vehicles'][0]
        self.assertEqual(data['sample_count'], 2)
        self.assertEqual(data['average_sample_speed_mph'], 30)
        self.assertEqual(self.store.view(now=1000)[0]['speed_mph'], 50)

    def test_dedup_outbox_and_delivery_retry(self):
        self.store.accept_webhook(self.alert(), now=1010)
        self.store.accept_webhook(self.alert(), now=1010)
        self.store.accept_webhook(self.alert('different-delivery'), now=1010)
        self.store.process_inbox(now=1010)
        self.assertEqual(len(self.store.incident_list()), 1)
        self.assertEqual(self.store.operations_status()['outbox'], {'pending': 1})
        self.store.deliver_local(now=1010, fail=True)
        self.store.deliver_local(now=1011)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM notifications').fetchone()[0], 0)
        self.store.deliver_local(now=1012)
        self.store.deliver_local(now=1013)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM notifications').fetchone()[0], 1)

    def test_late_event_recorded_without_urgent_notification(self):
        self.store.accept_webhook(self.alert(occurred=500), now=1010)
        self.store.process_inbox(now=1010)
        self.assertEqual(len(self.store.incident_list()), 1)
        self.assertEqual(self.store.operations_status()['outbox'], {})

    def test_event_time_driver_matching_and_correction(self):
        def assignment(driver, start, end=None):
            return {'vehicle': {'id': '123'}, 'driver': {'id': driver},
                    'startTime': iso(start), 'endTime': iso(end) if end else None,
                    'isPassenger': False, 'assignmentType': 'driverApp'}
        self.store.replace_assignments([assignment('A', 800, 1005), assignment('B', 1005)], 1010, 800)
        self.store.accept_webhook(self.alert(), now=1010)
        self.store.process_inbox(now=1010)
        self.assertEqual(self.store.incident_list()[0]['attribution']['driver']['id'], 'A')
        self.store.replace_assignments([assignment('C', 800, 1005), assignment('B', 1005)], 1020, 800)
        self.assertEqual(self.store.incident_list()[0]['attribution']['driver']['id'], 'C')
        self.assertEqual(self.store.driver_at('123', 700)['status'], 'outside_coverage')

    def test_unsupported_and_bad_events_retained(self):
        self.store.accept_webhook({'eventId': 'v2', 'eventType': 'AlertIncident'}, now=1000)
        self.store.accept_webhook({'eventId': 'bad', 'eventType': 'Alert'}, now=1000)
        self.store.process_inbox(now=1000)
        self.assertEqual(self.store.operations_status()['inbox'], {'quarantined': 1, 'unsupported': 1})

    def test_signature_tampering_and_replay(self):
        body = json.dumps(self.alert()).encode()
        key = b'test-secret'
        secret = base64.b64encode(key).decode()
        signature = 'v1=' + hmac.new(key, b'v1:1000:' + body, hashlib.sha256).hexdigest()
        self.assertTrue(verify_webhook(body, '1000', signature, secret, now=1000))
        self.assertFalse(verify_webhook(body + b' ', '1000', signature, secret, now=1000))
        self.assertFalse(verify_webhook(body, '1000', signature, secret, now=1400))
