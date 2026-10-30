"""Offline real DynamoDB transaction scenarios. Synthetic users only."""
import os,sys,json,unittest
from pathlib import Path
from datetime import datetime,timezone,timedelta
from unittest import mock
os.environ.update(AWS_ACCESS_KEY_ID='testing',AWS_SECRET_ACCESS_KEY='testing',AWS_DEFAULT_REGION='us-west-2',AWS_EC2_METADATA_DISABLED='true')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import boto3
from moto import mock_aws
import api_usage as usage
import lambda_api as api

class FixedDateTime(datetime):
 @classmethod
 def now(cls,tz=None):
  value=cls(2026,10,29,7,30,tzinfo=timezone.utc)
  return value.astimezone(tz) if tz else value.replace(tzinfo=None)

class UsageTests(unittest.TestCase):
 def setUp(self):
  self.aws=mock_aws();self.aws.start();self.addCleanup(self.aws.stop)
  self.db=boto3.client('dynamodb',region_name='us-west-2');self.table='AQApiRateLimits'
  self.db.create_table(TableName=self.table,KeySchema=[{'AttributeName':'counter_id','KeyType':'HASH'}],AttributeDefinitions=[{'AttributeName':'counter_id','AttributeType':'S'}],BillingMode='PAY_PER_REQUEST')
  self.identity={'key_id':'0123456789abcdef','email':'Researcher@example.org'}
  self.at=datetime(2026,10,29,7,30,tzinfo=timezone.utc)
 def put(self,request='request-1',status=200,identity=None,at=None,export=None):
  usage.record_usage(self.db,self.table,identity or self.identity,request,status,export,at or self.at)
 def user(self):
  return usage.read_usage(self.db,self.table,'user',usage.subject_id(self.identity['email'],self.identity['key_id']),'2026-10-29')
 def test_lifetime_daily_key_rotation_success_errors_and_exports(self):
  self.put();self.put('request-2',400)
  self.put('request-3',identity={**self.identity,'key_id':'fedcba9876543210','email':'researcher@EXAMPLE.ORG'},export={'bytes':3145728,'rows':3000})
  result=self.user();self.assertEqual(3,result['lifetime']['requests']);self.assertEqual(3,result['today']['requests'])
  self.assertEqual((2,1,1,3145728,3000),tuple(result['lifetime'][k] for k in ['successful_requests','failed_requests','exports_prepared','export_bytes','export_rows']))
  records=self.db.scan(TableName=self.table)['Items']
  self.assertTrue(all('ttl' not in item for item in records if not item['counter_id']['S'].startswith('usage:event:')))
 def test_same_gateway_request_is_deduplicated(self):
  self.put();self.put();self.assertEqual(1,self.user()['lifetime']['requests'])
 def test_out_of_order_events_keep_max_last_use_and_earliest_tracking_start(self):
  self.put('later',at=self.at+timedelta(hours=2));self.put('earlier',at=self.at)
  result=self.user()['lifetime'];self.assertEqual((self.at+timedelta(hours=2)).isoformat(),result['last_used_at']);self.assertEqual(self.at.isoformat(),result['tracking_since'])
 def test_pacific_day_boundary_and_unknown_history_are_explicit(self):
  self.put('before-midnight',at=datetime(2026,10,29,6,59,tzinfo=timezone.utc));self.put('after-midnight',at=self.at)
  uid=usage.subject_id(self.identity['email'],self.identity['key_id']);life=self.user()['lifetime']
  rows=usage.read_history(self.db,self.table,uid,7,self.at.astimezone(usage.DAY_TIMEZONE).date(),life['tracking_since'])
  self.assertIsNone(rows[0]['requests']);self.assertEqual(1,rows[-2]['requests']);self.assertTrue(rows[-2]['partial_day']);self.assertEqual(1,rows[-1]['requests'])
 def test_atomic_failure_cannot_leave_partial_rollups(self):
  with mock.patch.object(self.db,'transact_write_items',side_effect=RuntimeError('unavailable')):
   with self.assertRaises(RuntimeError):self.put()
  self.assertEqual([],self.db.scan(TableName=self.table)['Items'])
 def test_concurrent_request_retries_without_losing_counts_or_last_use(self):
  self.put('first')
  transact=self.db.transact_write_items
  competing=True
  def race(**kwargs):
   nonlocal competing
   if competing:
    competing=False
    self.put('concurrent',at=self.at+timedelta(hours=3))
   return transact(**kwargs)
  with mock.patch.object(self.db,'transact_write_items',side_effect=race):
   self.put('finishes-later',at=self.at+timedelta(hours=1))
  result=self.user()['lifetime']
  self.assertEqual(3,result['requests'])
  self.assertEqual((self.at+timedelta(hours=3)).isoformat(),result['last_used_at'])
 def test_admin_combines_rotated_keys_and_reads_private_daily_history(self):
  self.put();self.put('other-key',identity={**self.identity,'key_id':'fedcba9876543210'})
  keys=[{'key_id':{'S':key},'email':{'S':'researcher@example.org'},'status':{'S':state}} for key,state in [('0123456789abcdef','ACTIVE'),('fedcba9876543210','REVOKED')]]
  with mock.patch.object(api,'dynamodb_client',self.db),mock.patch.object(api,'query_by_status',side_effect=lambda table,status:[k for k in keys if k['status']['S']==status]):
   result=api.get_team_api_users({}, {'email':'admin@example.org','roles':{'Admin'}})
  body=json.loads(result['body']);self.assertEqual(1,len(body['users']));self.assertEqual(2,body['users'][0]['usage']['lifetime']['requests']);self.assertEqual('America/Los_Angeles',body['usage_timezone'])
 def test_accounting_failure_returns_503_instead_of_untracked_data(self):
  event={'rawPath':'/air-quality/v1/keyed/summary','_api_usage':{**self.identity,'scope':'read:summary','at':self.at.isoformat()}}
  with mock.patch.object(api,'_dispatch_request',return_value=api.response(200,{'private':'data'})),mock.patch.object(usage,'record_usage',side_effect=RuntimeError('unavailable')):
   result=api.lambda_handler(event,None)
  self.assertEqual(503,result['statusCode']);self.assertNotIn('private',result['body']);self.assertEqual('no',result['headers']['X-API-Usage-Recorded'])
 def test_anonymous_dashboard_has_no_personal_usage_writes(self):
  with mock.patch.object(api,'_dispatch_request',return_value=api.response(200,{})),mock.patch.object(usage,'record_usage') as tracker:
   api.lambda_handler({'rawPath':'/air-quality/v1/summary'},None)
  tracker.assert_not_called()

def contract():
 fixture=UsageTests();fixture.setUp()
 try:
  fixture.put();fixture.put('ci-error',status=400);fixture.put('ci-export',export={'bytes':3145728,'rows':3000})
  keys=[{'key_id':{'S':key},'email':{'S':email},'status':{'S':status}} for key,email,status in [
   ('0123456789abcdef','researcher@example.org','ACTIVE'),('fedcba9876543210','researcher@example.org','REVOKED'),('1111111111111111','untracked@example.org','ACTIVE')]]
  uid=usage.subject_id('researcher@example.org','0123456789abcdef')
  with mock.patch.object(api,'datetime',FixedDateTime),mock.patch.object(api,'dynamodb_client',fixture.db),mock.patch.object(api,'query_by_status',side_effect=lambda table,status:[k for k in keys if k['status']['S']==status]):
   result=api.get_team_api_users({'queryStringParameters':{'user_id':uid,'days':'7'}},{'email':'ci-admin@example.org','roles':{'Admin'}})
  assert result['statusCode']==200,result
  return json.loads(result['body'])
 finally:fixture.doCleanups()

if __name__=='__main__':
 if '--contract' in sys.argv:print(json.dumps(contract()))
 else:unittest.main()
