import csv
import gzip
import io
import json
import unittest
import os
import sys
import base64
import contextlib
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['AWS_ACCESS_KEY_ID'] = 'testing'
os.environ['AWS_SECRET_ACCESS_KEY'] = 'testing'
os.environ['AWS_DEFAULT_REGION'] = 'us-west-2'
os.environ['AWS_EC2_METADATA_DISABLED'] = 'true'

import boto3
from moto import mock_aws

import lambda_api as api
import research_exports as research
from test_pipeline_snapshots import MemoryS3


class ExportS3(MemoryS3):
    def upload_fileobj(self, stream, bucket, key, ExtraArgs):
        self.put_object(Bucket=bucket, Key=key, Body=stream.read(), **ExtraArgs)

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        return f'https://private.invalid/{Params["Key"]}?expires={ExpiresIn}'


class ResearchExportsTests(unittest.TestCase):
    def setUp(self):
        self.aws = mock_aws()
        self.aws.start()
        self.addCleanup(self.aws.stop)
        table = boto3.resource('dynamodb', region_name='us-west-2').create_table(
            TableName=research.TABLE, KeySchema=[{'AttributeName': 'job_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': name, 'AttributeType': 'S'} for name in ('job_id', 'owner', 'created_at')],
            BillingMode='PAY_PER_REQUEST', GlobalSecondaryIndexes=[{'IndexName': 'owner-created-index',
                'KeySchema': [{'AttributeName': 'owner', 'KeyType': 'HASH'}, {'AttributeName': 'created_at', 'KeyType': 'RANGE'}],
                'Projection': {'ProjectionType': 'ALL'}}])
        self.enterContext(mock.patch.object(research, 'db', table))
        self.enterContext(mock.patch.object(research, 'EXPORT_BUCKET', 'private-research-test'))
        self.s3 = ExportS3()
        self.enterContext(mock.patch.object(api, 's3_client', self.s3))
        self.groups = mock.Mock()
        self.groups.admin_list_groups_for_user.return_value = {'Groups': [{'GroupName': research.ROLE}]}
        self.enterContext(mock.patch.object(research, 'cognito', self.groups))
        self.worker = mock.Mock()
        self.worker.invoke.return_value = {'StatusCode': 202}
        self.enterContext(mock.patch.object(research, 'lambda_client', self.worker))
        self.silver = 'Timestamp,Concentration\n2026-09-01 00:00:00,-1.234567891\n2026-11-01 00:00:00,2.987654321\n'
        self.silver_key = 'NO2-CAPS/silver/NO2-CAPS_data.csv'
        etag = self.s3.put_object(Bucket=api.BUCKET, Key=self.silver_key, Body=self.silver)['ETag']
        api.publish_observation_index('NO2-CAPS', self.silver, etag)
        full = api.parse_silver_records('NO2-CAPS', silver_text=self.silver, review_items=([], []))[1]
        self.flag = {'row_key': full[0]['row_key'], 'reason': 'CI-only quality concern', 'created_by': 'reviewer@example.org'}
        self.s3.put_object(Bucket=api.BUCKET, Key='NO2-CAPS/flags/flag.json', Body=json.dumps(self.flag))
        self.s3.gets.clear()

    def event(self, path='/exports', method='GET', body=None, owner='researcher-one', authenticated=True):
        result = {'rawPath': research.ROOT + path, 'requestContext': {'http': {'method': method}}}
        if authenticated:
            result['requestContext']['authorizer'] = {'jwt': {'claims': {'sub': owner,
                'email': f'{owner}@example.org', 'email_verified': 'true', 'cognito:groups': ['ResearchExporter']}}}
        if body is not None:
            result['body'] = json.dumps(body)
        return result

    def create(self, **options):
        result = research.lambda_handler(self.event(method='POST', body={'instrument': 'NO2-CAPS', **options}), None)
        self.assertEqual(202, result['statusCode'], result)
        job_id = json.loads(result['body'])['job_id']
        return job_id, research.get_item(job_id)

    def finish(self, job):
        result = research.worker_handler({'dataset_id': job['dataset_id']}, None)
        self.assertEqual('READY', result['state'])
        dataset = research.get_item(job['dataset_id'])
        raw = self.s3.objects[dataset['result']['object_key']]['Body']
        return list(csv.DictReader(io.StringIO(gzip.decompress(raw).decode())))

    def test_no_token_api_key_or_unverified_email_can_retrieve_access_or_exports(self):
        for path in ('/access', '/exports', '/' + 'a' * 36):
            event = self.event(path, authenticated=False)
            event['headers'] = {'x-api-key': 'CI-only-public-key'}
            self.assertEqual(401, research.lambda_handler(event, None)['statusCode'])
        event = self.event('/access')
        event['requestContext']['authorizer']['jwt']['claims']['email_verified'] = 'false'
        self.assertEqual(401, research.lambda_handler(event, None)['statusCode'])
        self.groups.admin_list_groups_for_user.assert_not_called()
        self.assertEqual([], self.s3.gets)

    def test_live_membership_not_token_claims_controls_access_and_revocation(self):
        self.groups.admin_list_groups_for_user.return_value = {'Groups': [{'GroupName': 'Viewer'}]}
        access = research.lambda_handler(self.event('/access'), None)
        self.assertFalse(json.loads(access['body'])['enabled'])
        self.assertEqual(403, research.lambda_handler(self.event(), None)['statusCode'])
        self.groups.admin_list_groups_for_user.return_value = {'Groups': [{'GroupName': 'ResearchExporter'}]}
        job_id, job = self.create()
        self.finish(job)
        self.groups.admin_list_groups_for_user.return_value = {'Groups': [{'GroupName': 'Viewer'}]}
        self.assertEqual(403, research.lambda_handler(self.event(f'/exports/{job_id}'), None)['statusCode'])

    def test_default_excludes_flags_and_include_toggle_preserves_full_history_and_annotations(self):
        default_id, default = self.create()
        default_rows = self.finish(default)
        self.assertEqual(['2.987654321'], [row['Concentration'] for row in default_rows])
        all_id, all_job = self.create(include_flagged=True)
        all_rows = self.finish(all_job)
        self.assertEqual(['-1.234567891', '2.987654321'], [row['Concentration'] for row in all_rows])
        self.assertEqual('flagged', all_rows[0]['_research_status'])
        self.assertEqual(self.flag['reason'], json.loads(all_rows[0]['_research_flags'])[0]['reason'])
        self.assertNotIn('_s3_key', json.loads(all_rows[0]['_research_flags'])[0])
        ready = research.lambda_handler(self.event(f'/exports/{all_id}/download'), None)
        payload = json.loads(ready['body'])
        self.assertEqual(200, ready['statusCode'])
        self.assertEqual(2, payload['rows'])
        self.assertEqual(1, payload['included_flagged'])
        self.assertEqual(300, payload['expires_in_seconds'])
        self.assertNotIn('object_key', payload)
        self.assertNotIn(self.silver_key, self.s3.gets)


    def test_identical_snapshot_is_reused_but_jobs_are_owned(self):
        job_id, job = self.create()
        self.finish(job)
        second_id, second = self.create()
        self.assertNotEqual(job_id, second_id)
        self.assertEqual(job['dataset_id'], second['dataset_id'])
        self.assertEqual(1, self.worker.invoke.call_count)
        self.assertEqual(404, research.lambda_handler(self.event(f'/exports/{job_id}', owner='another-member'), None)['statusCode'])
        listed = json.loads(research.lambda_handler(self.event(), None)['body'])['exports']
        self.assertEqual(2, len(listed))
        self.assertTrue(all('url' not in entry for entry in listed))

    def test_new_flags_prevent_minting_a_link_to_an_old_review_snapshot(self):
        job_id, job = self.create(include_flagged=True)
        self.finish(job)
        self.s3.put_object(Bucket=api.BUCKET, Key='NO2-CAPS/flags/new.json', Body=json.dumps({'reason': 'new flag'}))
        payload = json.loads(research.lambda_handler(self.event(f'/exports/{job_id}/download'), None)['body'])
        self.assertEqual('OUTDATED', payload['state'])
        self.assertNotIn('url', payload)

    def test_invalid_options_are_rejected_and_worker_corruption_never_publishes_success(self):
        for options in ({'include_flagged': 'true'}, {'instrument': '../../private'}, {'source_key': 'secret'},
                        {'start': 'invalid'}, {'start': '2026-11-01', 'end': '2026-09-01'}):
            result = research.lambda_handler(self.event(method='POST', body={'instrument': 'NO2-CAPS', **options}), None)
            self.assertEqual(400, result['statusCode'])
        job_id, job = self.create()
        chunk = next(key for key in self.s3.objects if '/chunks/' in key)
        self.s3.objects[chunk]['Body'] = b'bad data'
        with self.assertRaises(ValueError):
            research.worker_handler({'dataset_id': job['dataset_id']}, None)
        self.assertEqual('FAILED', research.get_item(job['dataset_id'])['state'])
        payload = json.loads(research.lambda_handler(self.event(f'/exports/{job_id}'), None)['body'])
        self.assertNotIn('url', payload)

    def test_time_filters_are_exact_and_a_stale_index_never_reads_silver(self):
        _, job = self.create(include_flagged=True, start='2026-09-01T07:00:00Z', end='2026-09-01T07:00:00Z')
        self.assertEqual(['-1.234567891'], [row['Concentration'] for row in self.finish(job)])
        self.s3.put_object(Bucket=api.BUCKET, Key=self.silver_key, Body=self.silver + '2026-11-02 00:00:00,3\n')
        result = research.lambda_handler(self.event(method='POST', body={'instrument': 'NO2-CAPS'}), None)
        self.assertEqual(409, result['statusCode'])
        self.assertNotIn(self.silver_key, self.s3.gets)

    def test_each_instrument_keeps_source_precision_columns_and_utc_identity(self):
        cases = [
            ('BC-MA200', 'Date / time local,Timezone offset (mins),IR BCc', '2026-10-28T00:00:00,-420,-12.123456789', 'IR BCc', '-12.123456789'),
            ('CO2-LICOR', 'System_Date_(Y-M-D),System_Time_(h:m:s),CO2_(umol_mol-1)', '2026-10-28,00:00:00,420.123456789', 'CO2_(umol_mol-1)', '420.123456789'),
            ('NEPH-PM25', 'Date_Time,PM2.5 (µg/m³)', '2026-10-28 00:00:00,2.123456789', 'PM2.5 (µg/m³)', '2.123456789'),
            ('NO2-CAPS', 'Timestamp,Concentration', '2026-10-28 00:00:00,3.123456789', 'Concentration', '3.123456789'),
            ('SMPS', 'DateTime Sample Start,Total Concentration (#/cm³)', '28/10/2026 00:00:00,1000.123456789', 'Total Concentration (#/cm³)', '1000.123456789'),
        ]
        for instrument, header, row, measurement, expected in cases:
            with self.subTest(instrument=instrument):
                text = header + '\n' + row + '\n'
                etag = self.s3.put_object(Bucket=api.BUCKET, Key=f'{instrument}/silver/{instrument}_data.csv', Body=text)['ETag']
                api.publish_observation_index(instrument, text, etag)
                _, job = self.create(instrument=instrument, include_flagged=True)
                exported = self.finish(job)
                self.assertEqual(expected, exported[0][measurement])
                self.assertEqual('2026-10-28T07:00:00+00:00', exported[0]['_research_timestamp_utc'])

    def test_private_export_rate_limit_is_per_person_and_does_not_read_more_data_after_rejection(self):
        for _ in range(10):
            self.create()
        self.s3.gets.clear()
        denied = research.lambda_handler(self.event(method='POST', body={'instrument': 'NO2-CAPS'}), None)
        self.assertEqual(429, denied['statusCode'])
        self.assertEqual([], self.s3.gets)

def browser_contract():
    case = ResearchExportsTests('test_default_excludes_flags_and_include_toggle_preserves_full_history_and_annotations')
    with contextlib.redirect_stdout(io.StringIO()):
        case.setUp()
        try:
            access = json.loads(research.lambda_handler(case.event('/access'), None)['body'])
            results = {}
            for label, include in [('excluded', False), ('included', True)]:
                job_id, job = case.create(include_flagged=include)
                pending_job = json.loads(research.lambda_handler(case.event(f'/exports/{job_id}'), None)['body'])
                case.finish(job)
                ready = json.loads(research.lambda_handler(case.event(f'/exports/{job_id}'), None)['body'])
                download = json.loads(research.lambda_handler(case.event(f'/exports/{job_id}/download'), None)['body'])
                dataset = research.get_item(job['dataset_id'])
                blob = case.s3.objects[dataset['result']['object_key']]['Body']
                results[label] = {'pending': pending_job, 'ready': ready, 'download': download,
                                  'gzip_base64': base64.b64encode(blob).decode()}
            case.groups.admin_list_groups_for_user.return_value = {'Groups': [{'GroupName': 'Viewer'}]}
            denied = json.loads(research.lambda_handler(case.event('/access'), None)['body'])
        finally:
            case.doCleanups()
    return {'synthetic_ci_only': True, 'access': access, 'denied_access': denied, **results}


if __name__ == '__main__':
    if '--contract' in sys.argv:
        print(json.dumps(browser_contract()))
    else:
        unittest.main()
