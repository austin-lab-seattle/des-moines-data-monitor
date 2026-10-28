"""Cost contracts: bounded request work and identical published observations."""
import gzip
import hashlib
import io
import json
import unittest
from datetime import datetime, timezone
from unittest import mock

from botocore.exceptions import ClientError
import lambda_api as api
from test_silver_builder import silver_builder as builder


class MemoryS3:
    def __init__(self):
        self.objects = {}
        self.gets = []
        self.puts = []
        self.exceptions = type('Errors', (), {'NoSuchKey': type('NoSuchKey', (Exception,), {})})

    def put_object(self, Bucket, Key, Body, **kwargs):
        body = Body.encode() if isinstance(Body, str) else Body
        self.objects[Key] = {'Body': body, 'Key': Key, 'Size': len(body),
            'ETag': '"' + hashlib.md5(body).hexdigest() + '"',
            'LastModified': datetime(2026, 10, 28, 18, 0, tzinfo=timezone.utc)}
        self.puts.append(Key)
        return {'ETag': self.objects[Key]['ETag']}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({'Error': {'Code': '404'}}, 'HeadObject')
        return {k: v for k, v in self.objects[Key].items() if k != 'Body'}

    def get_object(self, Bucket, Key, **kwargs):
        self.gets.append(Key)
        if Key not in self.objects:
            raise ClientError({'Error': {'Code': 'NoSuchKey'}}, 'GetObject')
        obj = self.objects[Key]
        if kwargs.get('IfMatch', obj['ETag']) != obj['ETag']:
            raise ClientError({'Error': {'Code': 'PreconditionFailed'}}, 'GetObject')
        return {**obj, 'Body': io.BytesIO(obj['Body'])}

    def get_paginator(self, name):
        return self

    def paginate(self, Bucket, Prefix):
        return [{'Contents': [obj for key, obj in self.objects.items() if key.startswith(Prefix)]}]


class PipelineSnapshotsTests(unittest.TestCase):
    def setUp(self):
        self.s3 = MemoryS3()
        self.patches = [mock.patch.object(api, 's3_client', self.s3),
                        mock.patch.object(builder, 's3_client', self.s3)]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        api._silver_view_cache.clear()
        api._missing_silver_cache.clear()
        self.addCleanup(api._silver_view_cache.clear)
        self.addCleanup(api._missing_silver_cache.clear)
        self.source('a', [1, 3])
        self.source('b', [3, 8])

    def source(self, name, values):
        text = '\n'.join(f'2026-10-28 00:00:{v:02d}\t0000{v:02d},{v},1,747,301,100,1,0,1' for v in values) + '\n'
        self.s3.put_object(Bucket=api.BUCKET, Key=f'NO2-CAPS/bronze/{name}.txt', Body=text)

    def state(self):
        return json.loads(self.s3.objects['_pipeline_state/v1/NO2-CAPS.json']['Body'])

    def assert_full_replay_equal(self):
        texts = sorted((key, obj['Body'].decode()) for key, obj in self.s3.objects.items() if key.startswith('NO2-CAPS/bronze/'))
        header, rows, _, _, _, _ = builder.consolidate('NO2-CAPS', texts)
        header, rows = builder.transform_silver('NO2-CAPS', header, rows)
        expected = (header + '\n' if header else '') + '\n'.join(rows) + ('\n' if rows else '')
        self.assertEqual(expected, self.s3.objects['NO2-CAPS/silver/NO2-CAPS_data.csv']['Body'].decode())

    def test_first_build_then_unchanged_reads_zero_bronze_and_writes_no_silver(self):
        first = builder.build_instrument('NO2-CAPS')
        self.assertEqual((2, 3, 1), (first['bronze_reads'], first['unique'], first['duplicates']))
        self.assertEqual(4, self.state()['inventory']['bronzeRows'])
        self.assert_full_replay_equal()
        self.s3.gets.clear(); self.s3.puts.clear()
        second = builder.build_instrument('NO2-CAPS')
        self.assertTrue(second['skipped_unchanged'])
        self.assertEqual(0, second['bronze_reads'])
        self.assertFalse(any('/bronze/' in key for key in self.s3.gets))
        self.assertFalse(any('/silver/' in key for key in self.s3.puts))

    def test_new_changed_deleted_and_late_rows_match_original_full_consolidation(self):
        builder.build_instrument('NO2-CAPS')
        self.source('c', [0, 9])
        self.assertEqual(1, builder.build_instrument('NO2-CAPS')['bronze_reads'])
        self.assert_full_replay_equal()
        self.source('a', [2, 4])
        self.assertEqual(1, builder.build_instrument('NO2-CAPS')['bronze_reads'])
        self.assert_full_replay_equal()
        del self.s3.objects['NO2-CAPS/bronze/b.txt']
        self.assertEqual(0, builder.build_instrument('NO2-CAPS')['bronze_reads'])
        self.assert_full_replay_equal()

    def test_corrupt_or_missing_raw_bundle_rereads_real_bronze(self):
        builder.build_instrument('NO2-CAPS')
        key = next(key for key in self.s3.objects if key.endswith('.json.gz'))
        saved = json.loads(gzip.decompress(self.s3.objects[key]['Body']))
        saved['objects']['NO2-CAPS/bronze/a.txt']['text'] = 'fabricated'
        self.s3.put_object(Bucket=api.BUCKET, Key=key, Body=gzip.compress(json.dumps(saved).encode()))
        self.source('c', [6])
        self.assertEqual(2, builder.build_instrument('NO2-CAPS')['bronze_reads'])
        self.assert_full_replay_equal()
        del self.s3.objects[key]
        self.source('c', [7])
        self.assertEqual(3, builder.build_instrument('NO2-CAPS')['bronze_reads'])
        self.assert_full_replay_equal()

    def test_changed_source_during_read_does_not_publish_or_advance_state(self):
        builder.build_instrument('NO2-CAPS')
        old_state = self.s3.objects['_pipeline_state/v1/NO2-CAPS.json']['Body']
        old_silver = self.s3.objects['NO2-CAPS/silver/NO2-CAPS_data.csv']['Body']
        self.source('c', [6])
        with mock.patch.object(builder, 'download_text', side_effect=RuntimeError('Source changed during read')):
            with self.assertRaises(RuntimeError): builder.build_instrument('NO2-CAPS')
        self.assertEqual(old_state, self.s3.objects['_pipeline_state/v1/NO2-CAPS.json']['Body'])
        self.assertEqual(old_silver, self.s3.objects['NO2-CAPS/silver/NO2-CAPS_data.csv']['Body'])

    def test_code_change_replays_cached_inputs_without_reusing_derived_values(self):
        builder.build_instrument('NO2-CAPS')
        with mock.patch.object(api, 'SILVER_VIEW_CODE_VERSION', 'new-reader'):
            self.assertEqual(0, builder.build_instrument('NO2-CAPS')['bronze_reads'])
            self.assertEqual('new-reader', self.state()['views_version'])
        self.assert_full_replay_equal()

    def test_precomputed_views_are_identical_to_direct_api_and_no_silver_body_download(self):
        builder.build_instrument('NO2-CAPS')
        silver = self.s3.objects['NO2-CAPS/silver/NO2-CAPS_data.csv']['Body'].decode()
        for kind, params in [('series', {'instrument': 'NO2-CAPS'}),
                             ('records', {'instrument': 'NO2-CAPS', 'order': 'desc', 'limit': '1'}),
                             ('records', {'instrument': 'NO2-CAPS', 'order': 'desc', 'limit': '8'})]:
            event = {'queryStringParameters': params}
            expected = api.get_series(event, silver_text=silver) if kind == 'series' else api.get_silver_records(event, silver_text=silver, review_items=([], []))
            self.s3.gets.clear(); api._silver_view_cache.clear()
            actual = api.get_cached_silver_view(event, kind)
            self.assertEqual(json.loads(expected['body']), json.loads(actual['body']))
            self.assertNotIn('NO2-CAPS/silver/NO2-CAPS_data.csv', self.s3.gets)

    def test_summary_never_downloads_bronze_even_when_a_snapshot_is_missing(self):
        builder.build_instrument('NO2-CAPS')
        self.s3.gets.clear()
        with mock.patch.object(api, 'count_data_rows', side_effect=AssertionError('Historical recount')):
            inventory = api.compute_inventory()
        no2 = next(row for row in inventory['instruments'] if row['id'] == 'NO2-CAPS')
        self.assertEqual((4, 3), (no2['bronzeRows'], no2['silverRows']))
        missing = next(row for row in inventory['instruments'] if row['id'] == 'SMPS')
        self.assertIsNone(missing['bronzeRows'])
        self.assertFalse(any('/bronze/' in key for key in self.s3.gets))

    def test_missing_silver_negative_cache_expires_and_does_not_mask_other_errors(self):
        event = {'queryStringParameters': {'instrument': 'BC-MA200'}}
        with mock.patch.object(api.time, 'time', return_value=100):
            self.assertEqual(404, api.get_cached_silver_view(event, 'series')['statusCode'])
        with mock.patch.object(api.time, 'time', return_value=120), mock.patch.object(self.s3, 'head_object') as head:
            self.assertEqual(404, api.get_cached_silver_view(event, 'series')['statusCode']); head.assert_not_called()
        with mock.patch.object(api.time, 'time', return_value=161), mock.patch.object(self.s3, 'head_object', wraps=self.s3.head_object) as head:
            self.assertEqual(404, api.get_cached_silver_view(event, 'series')['statusCode']); head.assert_called_once()

    def test_request_metrics_never_log_query_strings_keys_or_user_identity(self):
        event = {'rawPath': '/air-quality/v1/summary', 'queryStringParameters': {'view': 'weather', 'token': 'private-token'},
                 'headers': {'x-api-key': 'private-key', 'authorization': 'private-login'}}
        with mock.patch.object(api, '_dispatch_request', return_value=api.response(200, {})), self.assertLogs('dashboard.requests', level='INFO') as captured:
            api.lambda_handler(event, None)
        logged = ' '.join(captured.output)
        self.assertIn('dashboard_request', logged)
        self.assertNotIn('private-', logged)
