"""End-to-end API smoke test for the handoff re-estimation workflow."""

import unittest
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from src.main import app


class HandoffApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.client.__enter__()
        login = cls.client.post('/api/auth/login', json={
            'email': 'analyst@demo.local', 'password': 'Demo-2611!'}).json()
        cls.headers = {'Authorization': f"Bearer {login['access_token']}"}
        cls.dataset_id = cls.client.get('/api/datasets', headers=cls.headers).json()[0]['id']
        cls.pending = [row['id'] for row in
                       cls.client.get(f'/api/datasets/{cls.dataset_id}',
                                      headers=cls.headers).json()['rows']
                       if row['split'] == 'pending']
        # Fetch all pages of pending shots (first page only covers 50 rows).
        offset = 50
        while True:
            page = cls.client.get(f'/api/datasets/{cls.dataset_id}?offset={offset}&limit=200',
                                  headers=cls.headers).json()['rows']
            cls.pending.extend(row['id'] for row in page if row['split'] == 'pending')
            offset += 200
            if not page:
                break
        # The demo seed may already own events on some pending shots; use free ones.
        occupied = {event['shot_id'] for event in cls.client.get(
            f'/api/datasets/{cls.dataset_id}/work-events', headers=cls.headers).json()}
        cls.free_pending = [shot_id for shot_id in cls.pending if shot_id not in occupied]
        assert len(cls.free_pending) >= 2, '演示台账至少要保留两个无事件待排镜头供测试使用'

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def test_full_handoff_workflow(self):
        first, second = self.free_pending[0], self.free_pending[1]
        # 1. Ingest progress events; validation rejects non-monotonic minutes.
        response = self.client.post(
            f'/api/datasets/{self.dataset_id}/work-events', headers=self.headers,
            json={'events': [
                {'shot_id': first, 'kind': 'progress', 'minutes': 40},
                {'shot_id': second, 'kind': 'progress', 'minutes': 25}]})
        self.assertEqual(response.status_code, 201, response.text)
        bad = self.client.post(
            f'/api/datasets/{self.dataset_id}/work-events', headers=self.headers,
            json={'events': [{'shot_id': first, 'kind': 'progress', 'minutes': 30}]})
        self.assertEqual(bad.status_code, 422)

        cutoff = datetime.now(timezone.utc).isoformat(timespec='seconds')
        # 2. Freeze the 13:00 handoff.
        created = self.client.post('/api/handoffs', headers=self.headers, json={
            'label': '13:00 擦线交班', 'dataset_id': self.dataset_id, 'as_of': cutoff})
        self.assertEqual(created.status_code, 201, created.text)
        handoff_id = created.json()['id']
        detail = self.client.get(f'/api/handoffs/{handoff_id}', headers=self.headers).json()
        self.assertEqual(detail['status'], 'completed')
        first_snapshot = detail['result']['tables']
        queue = {row['id']: row for row in first_snapshot[0]['rows']}
        self.assertIn(first, queue)
        self.assertIn(second, queue)
        self.assertEqual({queue[first]['elapsed_minutes'], queue[second]['elapsed_minutes']},
                         {40.0, 25.0})
        for row in queue.values():
            if row['p90_remaining_minutes'] is None:
                self.assertIsNone(row['reserve_minutes'])
                self.assertEqual(row['action'], '顺延复核')

        # 3. A completion arrives after the cutoff (late record).
        late = self.client.post(
            f'/api/datasets/{self.dataset_id}/work-events', headers=self.headers,
            json={'events': [{'shot_id': first, 'kind': 'completion', 'minutes': 96.5}]})
        self.assertEqual(late.status_code, 201, late.text)

        # 4. Re-opening the old handoff returns byte-identical tiers.
        rerun = self.client.get(f'/api/handoffs/{handoff_id}', headers=self.headers).json()
        self.assertEqual(rerun['result']['tables'], first_snapshot)

        # 5. A new handoff counts the completion from receipt, not retroactively.
        newer = self.client.post('/api/handoffs', headers=self.headers, json={
            'label': '15:00 擦线交班', 'dataset_id': self.dataset_id})
        self.assertEqual(newer.status_code, 201, newer.text)
        new_detail = self.client.get(f'/api/handoffs/{newer.json()["id"]}',
                                     headers=self.headers).json()
        shots = {row['id']: row for row in new_detail['result']['tables'][1]['rows']}
        self.assertEqual(shots[first]['state'], '已完工')
        self.assertEqual(shots[first]['completed_minutes'], 96.5)
        self.assertNotIn(first, {row['id'] for row in new_detail['result']['tables'][0]['rows']})

        # 6. CSV exports work for every table and carry headers.
        for key in ('handoff_queue', 'handoff_shots', 'handoff_profile'):
            exported = self.client.get(
                f'/api/handoffs/{newer.json()["id"]}/export/{key}', headers=self.headers)
            self.assertEqual(exported.status_code, 200, exported.text)
            self.assertTrue(exported.text.startswith('﻿'))
            self.assertIn('text/csv', exported.headers['content-type'])

        # 7. Events are listable and unknown tables 404.
        events = self.client.get(
            f'/api/datasets/{self.dataset_id}/work-events', headers=self.headers).json()
        own = {event['shot_id'] for event in events if event['shot_id'] in (first, second)}
        self.assertEqual(own, {first, second})
        self.assertGreaterEqual(len(events), 3)
        missing = self.client.get(
            f'/api/handoffs/{newer.json()["id"]}/export/nope', headers=self.headers)
        self.assertEqual(missing.status_code, 404)


if __name__ == '__main__':
    unittest.main()
