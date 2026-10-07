import json
import unittest
from datetime import datetime, timezone
from unittest import mock

import lambda_api


class LatestUploadTests(unittest.TestCase):
    def setUp(self):
        self.cache = dict(lambda_api._latest_upload_cache)
        lambda_api._latest_upload_cache.update(data=None, ts=0)

    def tearDown(self):
        lambda_api._latest_upload_cache.update(self.cache)

    def test_upload_uses_latest_s3_modified_time_not_measurement_or_filename(self):
        def objects(prefix):
            if prefix.startswith('NO2-CAPS/'):
                return [{'Key': 'NO2-CAPS/bronze/earlier-name.txt', 'LastModified': datetime(2026, 10, 7, 8, 30, tzinfo=timezone.utc)}]
            if prefix.startswith('CO2-LICOR/'):
                return [{'Key': 'CO2-LICOR/bronze/later-name.txt', 'LastModified': datetime(2026, 10, 7, 8, 20, tzinfo=timezone.utc)}]
            return []
        with mock.patch.object(lambda_api, 'iter_s3_objects', side_effect=objects), mock.patch.object(lambda_api, 'get_inventory') as inventory, mock.patch.object(lambda_api, 'get_silver_text') as silver:
            result = lambda_api.get_latest_upload()
        self.assertEqual({'instrument_id': 'NO2-CAPS', 'uploaded_at': '2026-10-07T08:30:00+00:00'}, json.loads(result['body']))
        inventory.assert_not_called()
        silver.assert_not_called()

    def test_empty_bucket_and_s3_failure_do_not_invent_upload_time(self):
        with mock.patch.object(lambda_api, 'iter_s3_objects', return_value=[]):
            result = lambda_api.get_latest_upload()
        self.assertEqual({'instrument_id': None, 'uploaded_at': None}, json.loads(result['body']))
        lambda_api._latest_upload_cache.update(data=None, ts=0)
        with mock.patch.object(lambda_api, 'iter_s3_objects', side_effect=RuntimeError('unavailable')):
            self.assertEqual(503, lambda_api.get_latest_upload()['statusCode'])

    def test_public_summary_metadata_view_bypasses_inventory_scan(self):
        with mock.patch.object(lambda_api, 'require_public_api_access', return_value=None), mock.patch.object(lambda_api, 'get_latest_upload', return_value={'statusCode': 200}) as upload, mock.patch.object(lambda_api, 'get_inventory') as inventory:
            result = lambda_api.lambda_handler({'rawPath': '/air-quality/v1/summary', 'queryStringParameters': {'view': 'latest-upload'}}, None)
        self.assertEqual(200, result['statusCode'])
        upload.assert_called_once()
        inventory.assert_not_called()
