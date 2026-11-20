"""Index pages must match a full chronological query without reading Silver."""
import json
import unittest
from unittest import mock

import lambda_api as api
import observation_index as index
from test_pipeline_snapshots import MemoryS3


class ObservationIndexTests(unittest.TestCase):
    def setUp(self):
        self.s3 = MemoryS3()
        patch = mock.patch.object(api, 's3_client', self.s3)
        patch.start()
        self.addCleanup(patch.stop)
        # Intentionally unsorted, duplicates, microseconds, invalid dates and rows.
        self.text = ('Concentration,Timestamp\n1.123456789,2026-11-01 00:00:00.123456\n'
                     '9,2026-11-01 02:00:00\n8,invalid\n'
                     '2,2026-11-01 00:00:00.123456\n3,2026-11-01 00:30:00\nbad,row,extra\n')
        self.silver = 'NO2-CAPS/silver/NO2-CAPS_data.csv'
        self.etag = self.s3.put_object(Bucket=api.BUCKET, Key=self.silver, Body=self.text)['ETag']

    def publish(self):
        with mock.patch.object(index, 'CHUNK_BYTES', 100):
            api.publish_observation_index('NO2-CAPS', self.text, self.etag)
        self.s3.gets.clear()

    def query(self, **params):
        return api.get_observation_page({'queryStringParameters': {'instrument': 'NO2-CAPS', **params}})

    def test_pages_filters_ties_and_dst_match_full_scan_exactly(self):
        self.publish()
        for order in ['asc', 'desc']:
            for bounds in [{}, {'start': '2026-11-01T07:00:00.123456Z'},
                           {'end': '2026-11-01T07:30:00Z'},
                           {'start': '2026-11-01T07:00:00.123456Z', 'end': '2026-11-01T07:00:00.123456Z'},
                           {'start': '2026-11-02T00:00:00Z'}]:
                for cursor in range(6):
                    params = {'order': order, 'limit': '2', 'cursor': str(cursor), **bounds}
                    expected = api.get_silver_records({'queryStringParameters': {'instrument': 'NO2-CAPS', **params}},
                                                      silver_text=self.text, review_items=([], []))
                    actual = self.query(**params)
                    self.assertEqual(200, actual['statusCode'])
                    self.assertEqual(json.loads(expected['body']), json.loads(actual['body']))
        self.assertNotIn(self.silver, self.s3.gets)

    def test_flags_change_immediately_without_rebuilding_or_scanning_silver(self):
        self.publish()
        row = json.loads(self.query(order='desc', limit='1')['body'])['rows'][0]
        self.s3.put_object(Bucket=api.BUCKET, Key='NO2-CAPS/flags/flag.json',
                           Body=json.dumps({'row_key': row['row_key'], 'reason': 'review'}))
        result = json.loads(self.query(order='desc', limit='1')['body'])
        self.assertEqual('flagged', result['rows'][0]['status'])
        self.assertEqual(row['raw'], result['rows'][0]['raw'])
        self.assertEqual(row['row_key'], result['rows'][0]['row_key'])
        self.assertNotIn(self.silver, self.s3.gets)

    def test_stale_missing_corrupt_indexes_fail_fast_without_silver_fallback(self):
        self.assertEqual(503, self.query()['statusCode'])
        self.publish()
        chunk = next(key for key in self.s3.objects if '/chunks/' in key)
        original = self.s3.objects[chunk]
        self.s3.objects[chunk] = {**original, 'Body': b'invalid'}
        self.assertEqual(503, self.query()['statusCode'])
        self.s3.objects[chunk] = original
        with mock.patch.object(api, 'SILVER_VIEW_CODE_VERSION', 'new-parser'):
            self.assertEqual(503, self.query()['statusCode'])
        self.s3.put_object(Bucket=api.BUCKET, Key=self.silver, Body=self.text + '5,2026-11-02 00:00:00\n')
        self.assertEqual(503, self.query()['statusCode'])
        self.assertNotIn(self.silver, self.s3.gets)

    def test_count_and_deep_cursor_skip_complete_chunks(self):
        self.publish()
        result = json.loads(self.query(count_only='true')['body'])
        self.assertEqual(4, result['count'])
        self.assertFalse(any('/chunks/' in key for key in self.s3.gets))
        self.s3.gets.clear()
        self.assertEqual(200, self.query(cursor='3', limit='1')['statusCode'])
        self.assertEqual(1, sum('/chunks/' in key for key in self.s3.gets))

    def test_invalid_ranges_and_order_are_rejected_before_s3_work(self):
        for params in [{'start': 'invalid'}, {'start': '2026-11-02', 'end': '2026-11-01'},
                       {'cursor': 'bad'}, {'order': 'anything'}]:
            self.assertEqual(400, self.query(**params)['statusCode'])
        self.assertEqual([], self.s3.gets)

    def test_annotation_read_failure_does_not_label_records_normal(self):
        self.publish()
        with mock.patch.object(api, 'read_review_objects', side_effect=RuntimeError('unavailable')):
            self.assertEqual(503, self.query()['statusCode'])
        self.assertNotIn(self.silver, self.s3.gets)

    def test_route_authentication_precedes_index_access(self):
        with mock.patch.object(api, 'require_public_api_access', return_value=api.response(403)):
            result = api.lambda_handler({'rawPath': '/air-quality/v1/keyed/observations',
                'requestContext': {'http': {'method': 'GET'}},
                'queryStringParameters': {'instrument': 'NO2-CAPS'}}, None)
        self.assertEqual(403, result['statusCode'])
        self.assertEqual([], self.s3.gets)

    def test_large_history_reads_only_one_chunk_for_a_deep_page(self):
        text = 'Concentration,Timestamp\n' + ''.join(
            f'{i}.123456789,2026-11-01 00:00:00.{i:06d}\n' for i in range(5000))
        etag = self.s3.put_object(Bucket=api.BUCKET, Key=self.silver, Body=text)['ETag']
        api.publish_observation_index('NO2-CAPS', text, etag)
        self.s3.gets.clear()
        payload = json.loads(self.query(cursor='3500', limit='100')['body'])
        self.assertEqual('3500.123456789', payload['rows'][0]['values']['Concentration'])
        self.assertEqual(3600, payload['next_cursor'])
        self.assertEqual(1, sum('/chunks/' in key for key in self.s3.gets))
        self.assertNotIn(self.silver, self.s3.gets)


if __name__ == '__main__':
    unittest.main()
