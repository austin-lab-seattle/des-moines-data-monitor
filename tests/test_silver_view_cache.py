"""Versioned response reuse must preserve numerical and review correctness."""
import io
import json
import unittest
from unittest import mock

import lambda_api as api


class SilverViewCacheTests(unittest.TestCase):
    def setUp(self):
        api._silver_view_cache.clear()
        self.etag = '"silver-one"'
        self.silver = 'Concentration,Timestamp\n10.123456789,2026-10-07 00:00:00\n11.876543211,2026-10-07 00:00:05\n'
        self.reviews = ([], [])
        self.shared = {}
        self.s3 = mock.Mock()
        self.s3.exceptions.NoSuchKey = type('NoSuchKey', (Exception,), {})
        self.s3.head_object.side_effect = lambda **kwargs: {"ETag": self.etag}
        self.s3.get_object.side_effect = self.read
        self.s3.put_object.side_effect = lambda **kwargs: self.shared.update({kwargs['Key']: kwargs['Body']})
        self.client_patch = mock.patch.object(api, 's3_client', self.s3)
        self.review_patch = mock.patch.object(api, 'read_review_objects', side_effect=lambda instrument, kind: self.reviews[0 if kind == 'flags' else 1])
        self.client_patch.start()
        self.review_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.addCleanup(self.review_patch.stop)
        self.addCleanup(api._silver_view_cache.clear)

    def read(self, **kwargs):
        if kwargs['Key'].startswith('_dashboard_cache/'):
            if kwargs['Key'] not in self.shared:
                raise self.s3.exceptions.NoSuchKey()
            return {'Body': io.BytesIO(self.shared[kwargs['Key']])}
        if 'IfMatch' in kwargs:
            self.assertEqual(self.etag, kwargs['IfMatch'])
        return {'Body': io.BytesIO(self.silver.encode())}

    def source_reads(self):
        return sum(call.kwargs['Key'].endswith('_data.csv') for call in self.s3.get_object.call_args_list)

    def query(self, kind='series', **params):
        event = {'queryStringParameters': {'instrument': 'NO2-CAPS', **params}}
        result = api.get_cached_silver_view(event, kind)
        return result['statusCode'], json.loads(result['body'])

    def test_unchanged_version_reuses_mean_but_checks_source_every_time(self):
        first = self.query()
        self.assertEqual([{'t': '2026-10-07T07:00:00Z', 'v': 11.0, 'n': 2}], first[1]['series'])
        self.assertEqual(first, self.query())
        self.assertEqual(2, self.s3.head_object.call_count)
        self.assertEqual(1, self.source_reads())
        self.etag = '"silver-two"'
        self.silver = 'Concentration,Timestamp\n20,2026-10-07 00:00:00\n40,2026-10-07 00:00:05\n'
        self.assertEqual(30, self.query()[1]['series'][0]['v'])
        self.assertEqual(2, self.source_reads())

    def test_recent_records_preserve_precision_and_refresh_review_changes(self):
        first = self.query('records', order='desc', limit='1')[1]
        row = first['rows'][0]
        self.assertEqual('11.876543211', row['values']['Concentration'])
        self.assertEqual(1, first['next_cursor'])
        self.assertEqual('normal', row['status'])
        self.assertEqual(first, self.query('records', order='desc', limit='1')[1])
        self.assertEqual(1, self.source_reads())
        self.reviews = ([{'id': 'flag', 'row_key': row['row_key'], 'status': 'active', 'reason': 'Review'}], [])
        changed = self.query('records', order='desc', limit='1')[1]['rows'][0]
        self.assertEqual('flagged', changed['status'])
        self.assertEqual('11.876543211', changed['values']['Concentration'])
        self.assertEqual(2, self.source_reads())

    def test_shared_cache_survives_a_cold_runtime_and_checks_source_and_code_version(self):
        first = self.query()
        api._silver_view_cache.clear()
        self.assertEqual(first, self.query())
        self.assertEqual(1, self.source_reads())
        api._silver_view_cache.clear()
        with mock.patch.object(api, 'SILVER_VIEW_CODE_VERSION', 'new-implementation'):
            self.assertEqual(first, self.query())
        self.assertEqual(2, self.source_reads())

    def test_cache_write_failure_keeps_fresh_values_and_bad_shared_json_is_ignored(self):
        self.s3.put_object.side_effect = RuntimeError('Cache writes unavailable')
        self.assertEqual(11, self.query()[1]['series'][0]['v'])
        api._silver_view_cache.clear()
        self.shared['_dashboard_cache/v1/NO2-CAPS/series-default.json'] = b'{invalid'
        self.assertEqual(11, self.query()[1]['series'][0]['v'])
        self.assertEqual(2, self.source_reads())

    def test_shared_result_for_a_different_instrument_is_never_reused(self):
        self.query()
        name = '_dashboard_cache/v1/NO2-CAPS/series-default.json'
        entry = json.loads(self.shared[name])
        entry['result']['body'] = json.dumps({'instrument_id': 'SMPS', 'series': [{'v': 999}]})
        self.shared[name] = json.dumps(entry).encode()
        api._silver_view_cache.clear()
        self.assertEqual(11, self.query()[1]['series'][0]['v'])
        self.assertEqual(2, self.source_reads())

    def test_filters_and_measurement_choices_do_not_share_results(self):
        self.query()
        status, result = self.query(start='2026-10-07T07:00:05Z')
        self.assertEqual(200, status)
        self.assertEqual([{'t': '2026-10-07T07:00:00Z', 'v': 11.877, 'n': 1}], result['series'])
        self.assertEqual(400, self.query(measurement='HHMMSS')[0])
        self.assertEqual(2, len(api._silver_view_cache))

    def test_failed_version_check_does_not_return_old_success(self):
        self.query()
        self.s3.head_object.side_effect = RuntimeError('S3 unavailable')
        self.s3.get_object.side_effect = RuntimeError('S3 unavailable')
        self.assertEqual(500, self.query()[0])

    def test_rebuild_between_head_and_get_falls_back_to_current_data(self):
        self.silver = 'Concentration,Timestamp\n42,2026-10-07 00:00:00\n'
        def changed(**kwargs):
            if 'IfMatch' in kwargs:
                raise RuntimeError('PreconditionFailed')
            return {'Body': io.BytesIO(self.silver.encode())}
        self.s3.get_object.side_effect = changed
        self.assertEqual(42, self.query()[1]['series'][0]['v'])
        self.assertEqual(0, len(api._silver_view_cache))

    def test_cache_has_a_small_fixed_size_and_no_failed_responses(self):
        with mock.patch.object(api, 'SILVER_VIEW_CACHE_ENTRIES', 2):
            for bucket in ['1', '5', '15']:
                self.query(bucket_minutes=bucket)
            self.assertEqual(2, len(api._silver_view_cache))
        api._silver_view_cache.clear()
        with mock.patch.object(api, 'SILVER_VIEW_CACHE_MAX_BODY_BYTES', 1):
            self.assertEqual(200, self.query()[0])
            self.assertEqual(0, len(api._silver_view_cache))

    def test_authentication_runs_before_cached_route_results(self):
        with mock.patch.object(api, 'require_public_api_access', return_value={'statusCode': 403, 'body': '{}'}):
            result = api.lambda_handler({'rawPath': '/air-quality/v1/keyed/timeseries',
                'requestContext': {'http': {'method': 'GET'}}, 'queryStringParameters': {'instrument': 'NO2-CAPS'}}, None)
        self.assertEqual(403, result['statusCode'])
        self.s3.head_object.assert_not_called()


class ChronologicalSelectionTests(unittest.TestCase):
    def test_latest_page_matches_complete_chronological_order_with_ties_and_bad_rows(self):
        silver = '\n'.join(['Concentration,Timestamp',
            '1,2026-10-07 00:00:10', '999,invalid', '2,2026-10-07 00:00:05',
            '3,2026-10-07 00:00:10', '4,2026-10-07 00:00:00', 'bad,row,extra'])
        for order, expected in [('desc', ['3', '1', '2', '4']), ('asc', ['4', '2', '1', '3'])]:
            result = []
            cursor = 0
            with mock.patch.object(api, 'get_silver_text', return_value=silver), mock.patch.object(api, 'read_review_objects', return_value=[]):
                while cursor is not None:
                    _, rows, cursor, _ = api.parse_silver_records('NO2-CAPS', order=order, limit=1, cursor=cursor)
                    result.extend(row['values']['Concentration'] for row in rows)
            self.assertEqual(expected, result)

    def test_slash_dates_keep_day_first_precedence_and_legacy_fallback(self):
        cases = [('11/08/2026 10:40:50', (2026, 8, 11)),
                 ('08/31/2026 10:40:50', (2026, 8, 31)),
                 ('11/08/2026 10:40:50.123456', (2026, 8, 11)),
                 ('2026/08/11 10:40:50', (2026, 8, 11))]
        for stamp, expected in cases:
            date = api.parse_datetime_value(stamp)
            self.assertEqual(expected, (date.year, date.month, date.day))
