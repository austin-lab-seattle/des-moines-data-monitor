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

from botocore.exceptions import ClientError

import lambda_api as api
import research_exports as research
import research_keys
import research_key_admin as key_admin
import boto3
from moto import mock_aws
from types import SimpleNamespace
from test_pipeline_snapshots import MemoryS3


class ExportS3(MemoryS3):
    def put_object(self, Bucket, Key, Body, **kwargs):
        exists = self.objects.get(Key)
        if (kwargs.get('IfNoneMatch') == '*' and exists
                or kwargs.get('IfMatch') and (not exists or exists['ETag'] != kwargs['IfMatch'])):
            raise ClientError({'Error': {'Code': 'PreconditionFailed'}}, 'PutObject')
        return super().put_object(Bucket=Bucket, Key=Key, Body=Body, **kwargs)

    def upload_fileobj(self, stream, bucket, key, ExtraArgs):
        self.put_object(Bucket=bucket, Key=key, Body=stream.read(), **ExtraArgs)

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        return f'https://private.invalid/{Params["Key"]}?expires={ExpiresIn}'


class ResearchExportsTests(unittest.TestCase):
    def setUp(self):
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

    def key_setup(self):
        self.enterContext(mock_aws())
        ddb = boto3.client('dynamodb', region_name='us-west-2')
        ddb.create_table(TableName=api.API_KEYS_TABLE, BillingMode='PAY_PER_REQUEST',
            KeySchema=[{'AttributeName': 'key_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': name, 'AttributeType': kind} for name, kind in
                [('key_id', 'S'), ('status', 'S'), ('created_at_epoch', 'N')]],
            GlobalSecondaryIndexes=[{'IndexName': 'status-created-at-index',
                'KeySchema': [{'AttributeName': 'status', 'KeyType': 'HASH'}, {'AttributeName': 'created_at_epoch', 'KeyType': 'RANGE'}],
                'Projection': {'ProjectionType': 'ALL'}}])
        ddb.create_table(TableName=api.RATE_LIMITS_TABLE, BillingMode='PAY_PER_REQUEST',
            KeySchema=[{'AttributeName': 'counter_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'counter_id', 'AttributeType': 'S'}])
        self.enterContext(mock.patch.object(api, 'dynamodb_client', ddb))
        self.enterContext(mock.patch.object(api, 'API_KEY_HASH_PEPPER', 'CI-only-research-pepper'))
        self.key = 'aqk_0123456789abcdef_' + 'C' * 43
        self.key_id = '0123456789abcdef'
        self.email = 'researcher-one@example.org'
        self.key_item = {'key_id': {'S': self.key_id}, 'key_hash': {'S': api.key_hash(self.key)},
            'email': {'S': self.email}, 'status': {'S': 'ACTIVE'}, 'created_at_epoch': {'N': '1'},
            'scopes': {'SS': [*api.READ_SCOPES.values(), research_keys.SCOPE]},
            'research_subject': {'S': 'researcher-one'}}
        ddb.put_item(TableName=api.API_KEYS_TABLE, Item=self.key_item)
        self.groups.admin_get_user.return_value = {'Username': 'researcher-one', 'Enabled': True,
            'UserAttributes': [{'Name': 'sub', 'Value': 'researcher-one'}, {'Name': 'email', 'Value': self.email},
                               {'Name': 'email_verified', 'Value': 'true'}]}
        self.enterContext(mock.patch.object(key_admin, 'cognito', self.groups))
        self.enterContext(mock.patch.object(key_admin, 's3', self.s3))
        self.enterContext(mock.patch.dict(os.environ, {'RESEARCH_EXPORT_BUCKET': research.EXPORT_BUCKET}))
        self.admin_context = SimpleNamespace(invoked_function_arn='arn:aws:lambda:us-west-2:123456789012:function:aq-research-key-admin', aws_request_id='ci-admin-call')
        return ddb

    def key_event(self, path='/exports', method='GET', body=None):
        event = self.event(path, method, body, authenticated=False)
        event['rawPath'] = research_keys.ROOT + path
        event['headers'] = {'x-api-key': self.key}
        return event

    def test_key_exports_use_live_approval_precise_values_toggle_and_existing_usage_tracking(self):
        self.key_setup()
        for include, expected in [(False, 1), (True, 2)]:
            result = research.lambda_handler(self.key_event(method='POST', body={'instrument': 'NO2-CAPS', 'include_flagged': include}), None)
            self.assertEqual(202, result['statusCode'], result)
            job_id = json.loads(result['body'])['job_id'];job = research.get_item(job_id)
            self.assertEqual(self.key_id, job['requested_via_key'])
            rows = self.finish(job);self.assertEqual(expected, len(rows))
            download = research.lambda_handler(self.key_event('/exports/' + job_id + '/download'), None)
            self.assertEqual(200, download['statusCode'], download)
            self.assertIn('url', json.loads(download['body']))
            self.assertEqual('yes', download['headers']['X-API-Usage-Recorded'])
        stats = api.api_usage.read_usage(api.dynamodb_client, api.RATE_LIMITS_TABLE, 'key', self.key_id, '2026-01-01')['lifetime']
        self.assertEqual(4, stats['requests']);self.assertEqual(2, stats['exports_prepared'])
        self.assertEqual(3, stats['export_rows'])
        self.assertEqual('-1.234567891', rows[0]['Concentration'])

    def test_keys_need_valid_hash_explicit_scope_and_stable_verified_account_binding(self):
        ddb = self.key_setup()
        for field, value in [('key_hash', {'S': 'bad'}), ('status', {'S': 'REVOKED'}),
                             ('scopes', {'SS': list(api.READ_SCOPES.values())}), ('research_subject', {'S': 'another-account'})]:
            ddb.put_item(TableName=api.API_KEYS_TABLE, Item={**self.key_item, field: value})
            result = research.lambda_handler(self.key_event('/access'), None)
            self.assertEqual(403, result['statusCode'], field)
        ddb.put_item(TableName=api.API_KEYS_TABLE, Item=self.key_item)
        for change in [('Enabled', False), ('email_verified', 'false'), ('sub', 'reassigned-email-owner')]:
            user = self.groups.admin_get_user.return_value
            original = json.loads(json.dumps(user))
            if change[0] == 'Enabled':user['Enabled'] = False
            else:
                next(item for item in user['UserAttributes'] if item['Name'] == change[0])['Value'] = change[1]
            self.assertEqual(403, research.lambda_handler(self.key_event('/access'), None)['statusCode'])
            self.groups.admin_get_user.return_value = original
        self.assertNotIn(research_keys.SCOPE, api.READ_SCOPES.values())

    def test_missing_key_or_token_claims_cannot_authorize_keyed_routes_or_cross_users(self):
        self.key_setup()
        forged = self.event('/access');forged['rawPath'] = research_keys.ROOT + '/access'
        self.assertEqual(401, research.lambda_handler(forged, None)['statusCode'])
        job_id, _ = self.create()
        self.groups.admin_get_user.return_value['UserAttributes'][0]['Value'] = 'different-owner'
        api.dynamodb_client.put_item(TableName=api.API_KEYS_TABLE, Item={**self.key_item, 'research_subject': {'S': 'different-owner'}})
        self.assertEqual(404, research.lambda_handler(self.key_event('/exports/' + job_id + '/download'), None)['statusCode'])
        wrong_path = self.key_event('/access');wrong_path['rawPath'] = research.ROOT + '/access'
        self.assertEqual(401, research.lambda_handler(wrong_path, None)['statusCode'])

    def test_key_or_group_revocation_blocks_status_and_download_with_same_original_key(self):
        ddb = self.key_setup()
        job_id, job = self.create();self.finish(job)
        self.groups.admin_list_groups_for_user.return_value = {'Groups': [{'GroupName': 'Viewer'}]}
        for path in ('/exports', '/exports/' + job_id, '/exports/' + job_id + '/download'):
            self.assertEqual(403, research.lambda_handler(self.key_event(path), None)['statusCode'])
        self.groups.admin_list_groups_for_user.return_value = {'Groups': [{'GroupName': research.ROLE}]}
        ddb.put_item(TableName=api.API_KEYS_TABLE, Item={**self.key_item, 'status': {'S': 'REVOKED'}})
        self.assertEqual(403, research.lambda_handler(self.key_event('/exports/' + job_id + '/download'), None)['statusCode'])

    def test_accounting_failure_and_key_lookup_failure_do_not_issue_a_download(self):
        self.key_setup()
        with mock.patch.object(api.api_usage, 'record_usage', side_effect=RuntimeError('offline')):
            self.assertEqual(503, research.lambda_handler(self.key_event('/access'), None)['statusCode'])
        with mock.patch.object(api.dynamodb_client, 'get_item', side_effect=RuntimeError('offline')):
            self.assertEqual(503, research.lambda_handler(self.key_event('/access'), None)['statusCode'])

    def test_key_rate_limits_and_deleted_accounts_fail_closed(self):
        self.key_setup()
        with mock.patch.object(api, 'PER_MINUTE_LIMIT', 1):
            self.assertEqual(200, research.lambda_handler(self.key_event('/access'), None)['statusCode'])
            self.assertEqual(429, research.lambda_handler(self.key_event('/access'), None)['statusCode'])
        self.groups.admin_get_user.side_effect = ClientError({'Error': {'Code': 'UserNotFoundException'}}, 'AdminGetUser')
        self.assertEqual(403, research.lambda_handler(self.key_event('/access'), None)['statusCode'])

    def test_multiple_keys_remain_active_while_temporary_key_is_created_expired_and_revoked(self):
        ddb = self.key_setup()
        event = {'operation': 'create-test-key', 'email': self.email,
                 'actor': {'email': 'operator@example.org', 'arn': 'operator-arn'}}
        created = key_admin.command(event, self.admin_context)
        self.assertNotEqual(self.key_id, created['key_id'])
        self.assertTrue(created['key_id'].startswith('smok'))
        self.assertEqual(2, len(key_admin.active_keys(self.email)))
        original = ddb.get_item(TableName=api.API_KEYS_TABLE, Key={'key_id': {'S': self.key_id}})['Item']
        self.assertEqual(self.key_item, original)
        self.key = created['api_key']
        self.assertEqual(200, research.lambda_handler(self.key_event('/access'), None)['statusCode'])
        ddb.update_item(TableName=api.API_KEYS_TABLE, Key={'key_id': {'S': created['key_id']}},
            UpdateExpression='SET expires_at = :past', ExpressionAttributeValues={':past': {'N': '1'}})
        self.assertEqual(403, research.lambda_handler(self.key_event('/access'), None)['statusCode'])
        self.groups.admin_get_user.side_effect = RuntimeError('account no longer accessible')
        revoked = key_admin.command({**event, 'operation': 'revoke-test-key', 'key_id': created['key_id']}, self.admin_context)
        self.assertFalse(revoked['active'])
        self.assertEqual(403, research.lambda_handler(self.key_event('/access'), None)['statusCode'])
        self.assertEqual(1, len(key_admin.active_keys(self.email)))
        original = ddb.get_item(TableName=api.API_KEYS_TABLE, Key={'key_id': {'S': self.key_id}})['Item']
        self.assertEqual(self.key_item, original)
        with self.assertRaises(ValueError):
            key_admin.command({**event, 'operation': 'revoke-test-key', 'key_id': self.key_id}, self.admin_context)

    def test_operator_grants_only_selected_active_key_and_audits_before_mutation(self):
        ddb = self.key_setup()
        event = {'operation': 'plan', 'email': self.email}
        result = key_admin.command(event, self.admin_context)
        self.assertEqual([self.key_id], [key['key_id'] for key in result['keys']])
        self.assertNotIn('key_hash', json.dumps(result));self.assertNotIn(self.key, json.dumps(result))
        grant = {**event, 'operation': 'grant', 'actor': {'email': 'operator@example.org', 'arn': 'operator-arn'}}
        ddb.put_item(TableName=api.API_KEYS_TABLE, Item={**self.key_item, 'scopes': {'SS': list(api.READ_SCOPES.values())}})
        result = key_admin.command(grant, self.admin_context)
        self.assertTrue(result['enabled'])
        current = ddb.get_item(TableName=api.API_KEYS_TABLE, Key={'key_id': {'S': self.key_id}})['Item']
        self.assertIn(research_keys.SCOPE, current['scopes']['SS'])
        self.assertEqual('researcher-one', current['research_subject']['S'])
        self.assertEqual(200, research.lambda_handler(self.key_event('/access'), None)['statusCode'])
        key_admin.command({**grant, 'operation': 'revoke'}, self.admin_context)
        self.assertEqual(403, research.lambda_handler(self.key_event('/access'), None)['statusCode'])
        self.assertEqual(4, len([key for key in self.s3.objects if key.startswith('audit/research-key-access/')]))

    def test_operator_never_accepts_http_events_unapproved_accounts_or_audit_failure(self):
        ddb = self.key_setup()
        event = {'operation': 'grant', 'email': self.email, 'actor': {'email': 'operator@example.org', 'arn': 'operator-arn'}}
        with self.assertRaises(ValueError): key_admin.command({**event, 'requestContext': {}}, self.admin_context)
        self.groups.admin_list_groups_for_user.return_value = {'Groups': [{'GroupName': 'Viewer'}]}
        with self.assertRaises(ValueError): key_admin.command(event, self.admin_context)
        self.groups.admin_list_groups_for_user.return_value = {'Groups': [{'GroupName': research.ROLE}]}
        with mock.patch.object(self.s3, 'put_object', side_effect=RuntimeError('offline')):
            with self.assertRaises(RuntimeError):key_admin.command(event, self.admin_context)
        current = ddb.get_item(TableName=api.API_KEYS_TABLE, Key={'key_id': {'S': self.key_id}})['Item']
        self.assertEqual(self.key_item, current)

    def test_duplicate_workers_and_conditional_update_conflicts_are_safe(self):
        job_id, job = self.create()
        self.finish(job)
        self.assertEqual({'state': 'ALREADY_STARTED'}, research.worker_handler({'dataset_id': job['dataset_id']}, None))
        original_write = research.store.write
        calls = []
        def collide_once(item, etag=None):
            calls.append(item)
            if len(calls) == 1:
                current, latest = research.store.read(job_id)
                original_write({**current, 'concurrent_field': 'retained'}, latest)
                raise research.Conflict()
            return original_write(item, etag)
        with mock.patch.object(research.store, 'write', side_effect=collide_once):
            research.store.update(job_id, {'last_link_at': 'new'})
        saved = research.get_item(job_id)
        self.assertEqual('retained', saved['concurrent_field'])
        self.assertEqual('new', saved['last_link_at'])

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
        self.assertTrue(all(key.startswith('state/') for key in self.s3.gets))

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
