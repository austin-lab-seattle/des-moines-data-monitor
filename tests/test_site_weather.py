import io
import json
import unittest
from unittest import mock

import lambda_api


class SiteWeatherTests(unittest.TestCase):
    def setUp(self):
        self.cache = dict(lambda_api._weather_cache)
        lambda_api._weather_cache.update(data=None, ts=0)

    def tearDown(self):
        lambda_api._weather_cache.update(self.cache)

    def test_fixed_location_and_cache_avoid_tracking_and_repeated_provider_calls(self):
        source = {'current': {'is_day': 1, 'cloud_cover': 70, 'weather_code': 3, 'time': 1800000000}}
        with mock.patch.object(lambda_api.time, 'time', return_value=1800000000), mock.patch.object(lambda_api.urllib.request, 'urlopen', return_value=io.StringIO(json.dumps(source))) as request:
            first = lambda_api.get_site_weather()
            second = lambda_api.get_site_weather()
        self.assertEqual(first, second)
        request.assert_called_once()
        url = request.call_args.args[0].full_url
        self.assertIn('latitude=47.4', url)
        self.assertIn('longitude=-122.33', url)
        self.assertNotIn('apikey', url)
        self.assertEqual({'is_day': True, 'cloud_cover': 70, 'weather_code': 3, 'time': 1800000000}, json.loads(first['body'])['weather'])

    def test_invalid_stale_and_failed_provider_never_become_live_weather(self):
        cases = [{'is_day': 1, 'cloud_cover': 101, 'time': 1800000000}, {'is_day': 1, 'cloud_cover': 40, 'time': 1700000000}]
        for current in cases:
            lambda_api._weather_cache.update(data=None, ts=0)
            with mock.patch.object(lambda_api.time, 'time', return_value=1800000000), mock.patch.object(lambda_api.urllib.request, 'urlopen', return_value=io.StringIO(json.dumps({'current': current}))):
                self.assertIsNone(json.loads(lambda_api.get_site_weather()['body'])['weather'])
        lambda_api._weather_cache.update(data=None, ts=0)
        with mock.patch.object(lambda_api.urllib.request, 'urlopen', side_effect=TimeoutError('provider unavailable')):
            self.assertIsNone(json.loads(lambda_api.get_site_weather()['body'])['weather'])
