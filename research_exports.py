"""Team-only, asynchronous exports from immutable observation chunks.

The API never scans Silver or builds a CSV. A separate worker prepares one
versioned gzip CSV; source values and review annotations remain reproducible.
"""
import base64
import csv
import gzip
import hashlib
import io
import json
import os
import re
import tempfile
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

import lambda_api as api
import observation_index as index

ROOT = '/air-quality/internal/v1/research'
TABLE = os.environ.get('RESEARCH_EXPORT_TABLE', 'AQResearchExports')
EXPORT_BUCKET = os.environ.get('RESEARCH_EXPORT_BUCKET', '')
POOL = os.environ.get('TEAM_USER_POOL_ID', 'us-west-2_oR7CIzxQG')
WORKER = os.environ.get('RESEARCH_EXPORT_WORKER', 'aq-research-export-worker')
CONTACT = os.environ.get('RESEARCH_CONTACT_EMAIL', 'pavands@uw.edu')
ROLE = 'ResearchExporter'
TEAM_GROUPS = api.TEAM_ROLES | {ROLE}
ANNOTATIONS = ['_research_row_key', '_research_timestamp_utc', '_research_status',
               '_research_flags', '_research_corrections']
db = boto3.resource('dynamodb').Table(TABLE)
cognito = boto3.client('cognito-idp')
lambda_client = boto3.client('lambda')


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def reply(status, body):
    body = json.loads(json.dumps(body, default=lambda value: int(value) if isinstance(value, Decimal) else str(value)))
    result = api.response(status, body, {'Cache-Control': 'private, no-store'})
    # A bearer token is required, regardless of browser origin.
    result['headers'].pop('Access-Control-Allow-Origin', None)
    return result


def identity(event):
    claims = event.get('requestContext', {}).get('authorizer', {}).get('jwt', {}).get('claims', {})
    subject = str(claims.get('sub') or '')
    email = api.normalize_email(claims.get('email'))
    if not subject or not email or str(claims.get('email_verified')).lower() != 'true':
        return None, reply(401, {'error': 'Sign in with your verified team account.'})
    username = claims.get('cognito:username') or subject
    groups, token = set(), None
    # Check live membership so granting or revoking access takes effect without
    # trusting an old token's groups or requiring an awkward re-login.
    while True:
        args = {'UserPoolId': POOL, 'Username': username, 'Limit': 60}
        if token:
            args['NextToken'] = token
        result = cognito.admin_list_groups_for_user(**args)
        groups.update(group['GroupName'] for group in result.get('Groups', []))
        token = result.get('NextToken')
        if not token:
            break
    if not groups.intersection(TEAM_GROUPS):
        return None, reply(403, {'error': 'An invited team account is required.'})
    return {'subject': subject, 'email': email, 'roles': sorted(groups & TEAM_GROUPS)}, None


def review_snapshot(instrument):
    items = [api.read_review_objects(instrument, kind, strict=True) for kind in ('flags', 'corrections')]
    for values in items:
        values.sort(key=lambda item: json.dumps(item, sort_keys=True))
    revision = hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()
    return items, revision


def get_item(pk):
    return db.get_item(Key={'job_id': pk}, ConsistentRead=True).get('Item')


def private_json(key):
    body = api.s3_client.get_object(Bucket=EXPORT_BUCKET, Key=key)['Body']
    try:
        raw = body.read(4 * 1024 * 1024 + 1)
    finally:
        body.close()
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError('Export input exceeds its size limit')
    return json.loads(raw)


def export_spec(event):
    raw = event.get('body') or '{}'
    if event.get('isBase64Encoded'):
        raw = base64.b64decode(raw).decode()
    if len(raw) > 8192:
        raise ValueError('Request is too large')
    params = json.loads(raw)
    if not isinstance(params, dict) or set(params) - {'instrument', 'include_flagged', 'start', 'end'}:
        raise ValueError('Unsupported export options')
    instrument = params.get('instrument')
    if not api.is_valid_instrument(instrument):
        raise ValueError('Select a supported instrument')
    include = params.get('include_flagged', False)
    if not isinstance(include, bool):
        raise ValueError('include_flagged must be true or false')
    start = api.datetime_for_compare(params.get('start'))
    end = api.datetime_for_compare(params.get('end'))
    api.validate_time_range(params.get('start'), params.get('end'), start, end)
    normalize = lambda value: value.replace(tzinfo=timezone.utc).isoformat() if value else None
    return {'instrument': instrument, 'include_flagged': include,
            'start': normalize(start), 'end': normalize(end)}


def check_rate(subject):
    now = int(time.time())
    db.update_item(Key={'job_id': f'rate#{subject}#{now // 3600}'},
        UpdateExpression='SET expires_at = :ttl ADD #requests :one',
        ConditionExpression='attribute_not_exists(#requests) OR #requests < :limit',
        ExpressionAttributeNames={'#requests': 'requests'},
        ExpressionAttributeValues={':ttl': now + 7200, ':one': 1, ':limit': 10})


def create_export(event, user):
    try:
        spec = export_spec(event)
    except (ValueError, TypeError, json.JSONDecodeError):
        return reply(400, {'error': 'Choose a valid instrument, date range and Include flagged records setting.'})
    try:
        check_rate(user['subject'])
    except ClientError as exc:
        if exc.response['Error']['Code'] == 'ConditionalCheckFailedException':
            return reply(429, {'error': 'You can prepare up to 10 research exports per hour.'})
        raise
    instrument = spec['instrument']
    etag = api.s3_client.head_object(Bucket=api.BUCKET, Key=f'{instrument}/silver/{instrument}_data.csv')['ETag']
    manifest = index.read_json(api, index.manifest_key(instrument, api.SILVER_VIEW_CODE_VERSION), index.MAX_MANIFEST_BYTES)
    if (manifest.get('silver_etag') != etag or manifest.get('version') != api.SILVER_VIEW_CODE_VERSION
            or manifest.get('instrument_id') != instrument):
        return reply(409, {'error': 'New readings are being prepared. Please retry shortly.'})
    reviews, revision = review_snapshot(instrument)
    spec.update(source_etag=etag, reader_version=api.SILVER_VIEW_CODE_VERSION, review_revision=revision)
    digest = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    dataset_id = f'dataset#{digest}'
    dataset = get_item(dataset_id)
    now = int(time.time())
    if dataset and (int(dataset.get('expires_at', 0)) <= now
                    or dataset.get('state') == 'FAILED'
                    or dataset.get('state') == 'PROCESSING' and now - int(dataset.get('started_epoch', now)) > 960):
        # A new attempt uses a different key. Old Lambda retries can never
        # publish into the new attempt after a failure or timeout.
        digest = hashlib.sha256(f'{digest}:{uuid.uuid4()}'.encode()).hexdigest()
        dataset_id = f'dataset#{digest}'
        dataset = None
    if not dataset:
        request_key = f'requests/{digest}.json'
        source = {'spec': spec, 'manifest': manifest, 'reviews': reviews, 'requested_at': utc_now()}
        api.s3_client.put_object(Bucket=EXPORT_BUCKET, Key=request_key,
            Body=json.dumps(source).encode(), ContentType='application/json')
        dataset = {'job_id': dataset_id, 'kind': 'dataset', 'state': 'PENDING',
                   'spec': spec, 'request_key': request_key, 'created_at': utc_now(), 'expires_at': now + 86400}
        try:
            db.put_item(Item=dataset, ConditionExpression='attribute_not_exists(job_id)')
            result = lambda_client.invoke(FunctionName=WORKER, InvocationType='Event',
                                           Payload=json.dumps({'dataset_id': dataset_id}).encode())
            if result.get('StatusCode') != 202:
                raise RuntimeError('Worker did not accept the export')
        except ClientError as exc:
            if exc.response['Error']['Code'] != 'ConditionalCheckFailedException':
                fail_dataset(dataset_id)
                raise
            dataset = get_item(dataset_id)
        except Exception:
            fail_dataset(dataset_id)
            raise
    job_id = str(uuid.uuid4())
    job = {'job_id': job_id, 'kind': 'request', 'owner': user['subject'], 'requested_by': user['email'],
           'dataset_id': dataset_id, 'created_at': utc_now(), 'expires_at': int(dataset['expires_at']),
           'spec': spec}
    db.put_item(Item=job, ConditionExpression='attribute_not_exists(job_id)')
    return reply(202, {**job_result(job), 'instrument': instrument,
                      'include_flagged': spec['include_flagged'], 'created_at': job['created_at'],
                      'spec': spec, 'requested_by': user['email']})


def fail_dataset(dataset_id):
    db.update_item(Key={'job_id': dataset_id}, UpdateExpression='SET #state = :state',
                   ExpressionAttributeNames={'#state': 'state'}, ExpressionAttributeValues={':state': 'FAILED'})


def safe_job(job):
    return {key: job[key] for key in ('job_id', 'created_at', 'requested_by', 'spec')}


def job_result(job, download=False):
    payload = safe_job(job)
    dataset = get_item(job['dataset_id'])
    if int(job['expires_at']) <= time.time() or not dataset:
        return {**payload, 'state': 'EXPIRED'}
    payload['state'] = dataset['state']
    if dataset['state'] == 'PROCESSING' and time.time() - int(dataset.get('started_epoch', time.time())) > 960:
        fail_dataset(dataset['job_id'])
        payload['state'] = 'FAILED'
        return payload
    if dataset['state'] != 'READY':
        return payload
    payload.update(dataset['result'])
    if download:
        # A newly issued link must reflect the current review decisions. An
        # already downloaded snapshot retains its declared review revision.
        _, current_revision = review_snapshot(job['spec']['instrument'])
        if current_revision != job['spec']['review_revision']:
            return {**payload, 'state': 'OUTDATED', 'message': 'Review flags changed. Prepare a new export.'}
        payload['url'] = api.s3_client.generate_presigned_url('get_object', Params={
            'Bucket': EXPORT_BUCKET, 'Key': dataset['result']['object_key'],
            'ResponseContentDisposition': f'attachment; filename="{dataset["result"]["filename"]}"'}, ExpiresIn=300)
        payload['expires_in_seconds'] = 300
        db.update_item(Key={'job_id': job['job_id']}, UpdateExpression='SET last_link_at = :now',
                       ExpressionAttributeValues={':now': utc_now()})
    payload.pop('object_key', None)
    return payload


def lambda_handler(event, context):
    try:
        user, denied = identity(event)
        if denied:
            return denied
        method, path = api.get_route(event)
        enabled = ROLE in user['roles']
        if path == f'{ROOT}/access' and method == 'GET':
            return reply(200, {'enabled': enabled, 'role': ROLE, 'contact_email': CONTACT,
                               'viewer': user, 'instruments': api.INSTRUMENT_IDS})
        if not enabled:
            return reply(403, {'error': 'Full-history research access has not been enabled for your account.'})
        if path == f'{ROOT}/exports' and method == 'POST':
            return create_export(event, user)
        if path == f'{ROOT}/exports' and method == 'GET':
            result = db.query(IndexName='owner-created-index', KeyConditionExpression=Key('owner').eq(user['subject']),
                              ScanIndexForward=False, Limit=20)
            return reply(200, {'exports': [job_result(item) for item in result.get('Items', [])]})
        match = re.fullmatch(re.escape(ROOT) + r'/exports/([a-f0-9-]{36})(/download)?', path)
        if match and method == 'GET':
            job = get_item(match.group(1))
            if not job or job.get('owner') != user['subject'] or job.get('kind') != 'request':
                return reply(404, {'error': 'Export not found.'})
            return reply(200, job_result(job, download=bool(match.group(2))))
        return reply(404, {'error': 'Route not found.'})
    except Exception as exc:
        # Neither provider errors nor signed URLs belong in public error text.
        print(json.dumps({'event': 'research_request_failed', 'type': type(exc).__name__}))
        return reply(503, {'error': 'Research exports are temporarily unavailable. Please retry.'})


def clean_annotations(items):
    return [{key: value for key, value in item.items() if not key.startswith('_')} for item in items]


def worker_handler(event, context):
    dataset_id = event.get('dataset_id', '')
    if not re.fullmatch(r'dataset#[a-f0-9]{64}', dataset_id):
        raise ValueError('Invalid dataset identity')
    dataset = get_item(dataset_id)
    if not dataset or dataset.get('kind') != 'dataset' or int(dataset['expires_at']) <= time.time():
        return {'state': 'EXPIRED'}
    try:
        db.update_item(Key={'job_id': dataset_id}, UpdateExpression='SET #state = :processing, started_epoch = :now',
            ConditionExpression='#state = :pending', ExpressionAttributeNames={'#state': 'state'},
            ExpressionAttributeValues={':processing': 'PROCESSING', ':pending': 'PENDING', ':now': int(time.time())})
    except ClientError as exc:
        if exc.response['Error']['Code'] == 'ConditionalCheckFailedException':
            return {'state': 'ALREADY_STARTED'}
        raise
    try:
        source = private_json(dataset['request_key'])
        spec, manifest = source['spec'], source['manifest']
        instrument = spec['instrument']
        if (spec != dataset['spec'] or manifest['silver_etag'] != spec['source_etag']
                or manifest['version'] != spec['reader_version'] or manifest['instrument_id'] != instrument):
            raise ValueError('Snapshot identity does not match')
        if hashlib.sha256(json.dumps(source['reviews'], sort_keys=True).encode()).hexdigest() != spec['review_revision']:
            raise ValueError('Review snapshot identity does not match')
        columns = manifest['columns']
        if set(columns).intersection(ANNOTATIONS):
            raise ValueError('Research annotation column conflict')
        timestamp_index = api.column_index(columns)
        flags, corrections = source['reviews']
        start, end = api.datetime_for_compare(spec['start']), api.datetime_for_compare(spec['end'])
        rows = excluded = included_flagged = 0
        first = last = None
        with tempfile.TemporaryFile() as output:
            with gzip.GzipFile(fileobj=output, mode='wb', mtime=0) as zipped:
                with io.TextIOWrapper(zipped, encoding='utf-8', newline='') as text:
                    writer = csv.writer(text, lineterminator='\n')
                    writer.writerow(columns + ANNOTATIONS)
                    for chunk in manifest['chunks']:
                        low, high = (api.datetime_for_compare(chunk[name]) for name in ('first', 'last'))
                        if start is not None and high < start or end is not None and low > end:
                            continue
                        expected = f"_observations/v1/{instrument}/chunks/{chunk['digest']}.json.gz"
                        if chunk['key'] != expected:
                            raise ValueError('Unexpected source chunk')
                        records = index.read_json(api, expected, index.MAX_BODY_BYTES, chunk['digest'])
                        if len(records) != chunk['count']:
                            raise ValueError('Unexpected source chunk length')
                        for stamp, source_index, raw in records:
                            moment = api.datetime_for_compare(stamp)
                            if moment is None:
                                raise ValueError('Invalid indexed timestamp')
                            if not api.row_matches_time_range(moment, start, end):
                                continue
                            fields = api.split_fields(raw)
                            if len(fields) != len(columns):
                                raise ValueError('Invalid indexed source row')
                            original_time = api.record_timestamp(instrument, columns, fields, timestamp_index)
                            row = api.annotated_silver_record(instrument, columns, fields, source_index, raw,
                                                               original_time, moment, flags, corrections)
                            flagged = bool(row['flags'])
                            if flagged and not spec['include_flagged']:
                                excluded += 1
                                continue
                            writer.writerow(fields + [row['row_key'], row['timestamp_iso'], row['status'],
                                json.dumps(clean_annotations(row['flags']), ensure_ascii=False, separators=(',', ':')),
                                json.dumps(clean_annotations(row['corrections']), ensure_ascii=False, separators=(',', ':'))])
                            rows += 1
                            included_flagged += int(flagged)
                            first = first or row['timestamp_iso']
                            last = row['timestamp_iso']
            size = output.tell()
            output.seek(0)
            checksum = hashlib.sha256()
            while block := output.read(1024 * 1024):
                checksum.update(block)
            output.seek(0)
            digest = dataset_id.split('#')[1]
            object_key = f'exports/{digest}.csv.gz'
            filename = f'{instrument}_research_{digest[:12]}.csv.gz'
            api.s3_client.upload_fileobj(output, EXPORT_BUCKET, object_key, ExtraArgs={
                'ContentType': 'application/gzip', 'ContentDisposition': f'attachment; filename="{filename}"',
                'Metadata': {'instrument': instrument, 'review-revision': spec['review_revision']}})
        result = {'filename': filename, 'object_key': object_key, 'rows': rows, 'bytes': size,
                  'columns': len(columns) + len(ANNOTATIONS), 'excluded_flagged': excluded,
                  'included_flagged': included_flagged, 'start': first, 'end': last,
                  'prepared_at': utc_now(), 'review_revision': spec['review_revision'],
                  'source_etag': spec['source_etag'], 'reader_version': spec['reader_version']}
        result['sha256'] = checksum.hexdigest()
        db.update_item(Key={'job_id': dataset_id}, UpdateExpression='SET #state = :ready, #result = :result',
            ConditionExpression='#state = :processing', ExpressionAttributeNames={'#state': 'state', '#result': 'result'},
            ExpressionAttributeValues={':ready': 'READY', ':processing': 'PROCESSING', ':result': result})
        print(json.dumps({'event': 'research_export_ready', 'dataset_id': dataset_id, 'rows': rows, 'bytes': size}))
        return {'state': 'READY', 'rows': rows}
    except Exception as exc:
        fail_dataset(dataset_id)
        print(json.dumps({'event': 'research_export_failed', 'dataset_id': dataset_id, 'type': type(exc).__name__}))
        raise
