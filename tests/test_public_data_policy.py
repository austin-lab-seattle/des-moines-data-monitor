import json
import unittest
from datetime import datetime, timezone
from unittest import mock

import lambda_api as api
import public_data_policy as policy
from test_pipeline_snapshots import MemoryS3


class PublicDataPolicyTests(unittest.TestCase):
    def setUp(self):
        self.clock = self.enterContext(mock.patch.object(policy, 'now_utc',
            return_value=datetime(2026, 11, 22, 20, 20, tzinfo=timezone.utc)))

    def event(self, path, **params):
        return {'rawPath': path, 'requestContext': {'http': {'method': 'GET'}},
                'queryStringParameters': {'instrument': 'NO2-CAPS', **params}}

    def test_every_public_data_route_rejects_history_before_reading_or_signing(self):
        with (mock.patch.object(api, 'require_public_api_access', return_value=None) as auth,
              mock.patch.object(api, 'get_observation_page') as observations,
              mock.patch.object(api, 'get_cached_silver_view') as series,
              mock.patch.object(api, 'get_observations_export') as exports):
            for routes in (api.API_ROUTES, api.KEYED_API_ROUTES):
                for name in ('observations', 'timeseries', 'observations_export'):
                    result = api._dispatch_request(self.event(routes[name], start='2026-10-28T19:00:00Z'), None)
                    self.assertEqual(403, result['statusCode'])
            self.assertEqual(6, auth.call_count)
            observations.assert_not_called(); series.assert_not_called(); exports.assert_not_called()

    def test_public_without_dates_gets_bounded_index_pages_and_cannot_page_into_history(self):
        s3 = MemoryS3()
        self.enterContext(mock.patch.object(api, 's3_client', s3))
        self.enterContext(mock.patch.object(api, 'require_public_api_access', return_value=None))
        silver = 'Timestamp,Concentration\n2026-10-01 00:00:00,999\n2026-11-21 00:00:00,1.23456789\n'
        etag = s3.put_object(Bucket=api.BUCKET, Key='NO2-CAPS/silver/NO2-CAPS_data.csv', Body=silver)['ETag']
        api.publish_observation_index('NO2-CAPS', silver, etag)
        s3.gets.clear()
        for cursor, expected in [('0', ['1.23456789']), ('1', [])]:
            result = api._dispatch_request(self.event(api.API_ROUTES['observations'], cursor=cursor), None)
            self.assertEqual(200, result['statusCode'])
            rows = json.loads(result['body'])['rows']
            self.assertEqual(expected, [row['values']['Concentration'] for row in rows])
        counted = api._dispatch_request(self.event(api.API_ROUTES['observations'], count_only='true'), None)
        self.assertEqual(1, json.loads(counted['body'])['count'])
        self.assertNotIn('NO2-CAPS/silver/NO2-CAPS_data.csv', s3.gets)

    def test_public_key_cannot_unlock_research_options(self):
        for option in ('include_flagged', 'full_history', 'dataset', 'source_key'):
            _, denied = policy.restrict(api, self.event(api.API_ROUTES['observations'], **{option: 'true'}))
            self.assertEqual(403, denied['statusCode'])

    def test_authentication_still_precedes_window_policy(self):
        with mock.patch.object(api, 'require_public_api_access', return_value=api.response(401)):
            result = api._dispatch_request(self.event(api.KEYED_API_ROUTES['observations'], start='2020-01-01'), None)
        self.assertEqual(401, result['statusCode'])

    def test_window_matches_chart_hours_without_allowing_future_samples(self):
        bounded, denied = policy.restrict(api, self.event(api.API_ROUTES['timeseries'], end='2026-11-22T21:00:00Z'))
        self.assertIsNone(denied)
        self.assertEqual('2026-11-08T21:00:00+00:00', bounded['queryStringParameters']['start'])
        self.assertEqual('2026-11-22T20:20:00+00:00', bounded['queryStringParameters']['end'])
        for params in ({'start': 'invalid'}, {'start': '2026-11-23T00:00:00Z'},
                       {'start': '2026-11-21T01:00:00Z', 'end': '2026-11-20T01:00:00Z'}):
            self.assertEqual(400, policy.restrict(api, self.event('/', **params))[1]['statusCode'])

    def test_retired_urls_redirect_to_the_same_restricted_handler(self):
        with mock.patch.object(api, 'require_public_api_access', return_value=None):
            for name in ('observations', 'timeseries', 'observations_export'):
                old = api._dispatch_request(self.event(api.LEGACY_API_ROUTES[name], start='2020-01-01'), None)
                self.assertEqual(308, old['statusCode'])
                current = api._dispatch_request(self.event(api.API_ROUTES[name], start='2020-01-01'), None)
                self.assertEqual(403, current['statusCode'])
