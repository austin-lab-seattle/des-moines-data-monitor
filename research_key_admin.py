"""IAM-only operator command. This function has no HTTP integration."""
import json
import os
import re
import uuid
from datetime import datetime, timezone

import boto3
import lambda_api as api
from research_keys import SCOPE, api_research_pool

cognito = boto3.client('cognito-idp')
s3 = boto3.client('s3')


def approved_user(email):
    user = cognito.admin_get_user(UserPoolId=api_research_pool(), Username=email)
    attrs = {entry['Name']: entry['Value'] for entry in user['UserAttributes']}
    if not user.get('Enabled') or attrs.get('email') != email or attrs.get('email_verified') != 'true':
        raise ValueError('An existing verified, enabled team account is required')
    groups, token = set(), None
    while True:
        options = {'UserPoolId': api_research_pool(), 'Username': user['Username']}
        if token:
            options['NextToken'] = token
        result = cognito.admin_list_groups_for_user(**options)
        groups.update(item['GroupName'] for item in result.get('Groups', []))
        token = result.get('NextToken')
        if not token:
            break
    return attrs['sub'], 'ResearchExporter' in groups


def active_keys(email):
    result, token = [], None
    while True:
        options = dict(TableName=api.API_KEYS_TABLE, IndexName='status-created-at-index',
            KeyConditionExpression='#status = :active', FilterExpression='email = :email',
            ExpressionAttributeNames={'#status': 'status'},
            ExpressionAttributeValues={':active': {'S': 'ACTIVE'}, ':email': {'S': email}},
            ProjectionExpression='key_id, email, scopes, research_subject')
        if token:
            options['ExclusiveStartKey'] = token
        page = api.dynamodb_client.query(**options)
        result.extend(page.get('Items', []))
        token = page.get('LastEvaluatedKey')
        if not token:
            return result


def command(event, context):
    # API Gateway proxy events cannot enter the operator command. AWS IAM
    # InvokeFunction permission on this separate, un-routed function is required.
    arn = getattr(context, 'invoked_function_arn', '')
    if ('requestContext' in event or 'rawPath' in event or 'body' in event
            or not re.fullmatch(r'arn:aws:lambda:[\w-]+:\d{12}:function:aq-research-key-admin(?::[\w-]+)?', arn)):
        raise ValueError('Use the authenticated IAM operator command')
    operation = event.get('operation')
    email = api.normalize_email(event.get('email'))
    if operation not in {'plan', 'grant', 'revoke'} or not email:
        raise ValueError('A valid operation and email are required')
    subject, approved = approved_user(email)
    if operation == 'grant' and not approved:
        raise ValueError('ResearchExporter approval is required before granting key access')
    keys = active_keys(email)
    if event.get('key_id'):
        keys = [item for item in keys if api.ddb_string(item, 'key_id') == event['key_id']]
    metadata = [{'key_id': api.ddb_string(item, 'key_id'), 'scopes': api.ddb_string_list(item, 'scopes')}
                for item in keys]
    if operation == 'plan':
        return {'email': email, 'keys': metadata, 'research_approved': approved, 'messages_sent': False}
    if len(keys) != 1:
        raise ValueError('Select exactly one existing active key; no key was created or changed')
    key_id = metadata[0]['key_id']
    if not re.fullmatch('[a-z0-9]{16}', key_id):
        raise ValueError('Invalid key identity')
    actor = event.get('actor', {})
    if not api.normalize_email(actor.get('email')) or not actor.get('arn'):
        raise ValueError('Operator identity is required for the audit')
    audit_id = str(uuid.uuid4())
    details = {'audit_id': audit_id, 'email': email, 'key_id': key_id, 'subject': subject,
               'scope': SCOPE, 'operation': operation, 'actor': actor,
               'request_id': getattr(context, 'aws_request_id', ''),
               'created_at': datetime.now(timezone.utc).isoformat()}
    def audit(suffix, result):
        s3.put_object(Bucket=os.environ['RESEARCH_EXPORT_BUCKET'],
            Key=f'audit/research-key-access/{audit_id}/{suffix}.json',
            Body=json.dumps({**details, 'result': result}).encode(),
            ContentType='application/json', CacheControl='no-store', IfNoneMatch='*')
    audit('requested', 'PENDING')
    try:
        options = dict(TableName=api.API_KEYS_TABLE, Key={'key_id': {'S': key_id}},
            ConditionExpression='#status = :active AND email = :email',
            ExpressionAttributeNames={'#status': 'status'},
            ExpressionAttributeValues={':active': {'S': 'ACTIVE'}, ':email': {'S': email}, ':scope': {'SS': [SCOPE]}})
        if operation == 'grant':
            options['ConditionExpression'] += ' AND (attribute_not_exists(research_subject) OR research_subject = :subject)'
            options['ExpressionAttributeValues'][':subject'] = {'S': subject}
            options['UpdateExpression'] = 'SET research_subject = :subject ADD scopes :scope'
        else:
            options['UpdateExpression'] = 'DELETE scopes :scope'
        api.dynamodb_client.update_item(**options)
    except Exception:
        audit('completed', 'FAILED')
        raise
    audit('completed', 'SUCCEEDED')
    return {'email': email, 'key_id': key_id, 'scope': SCOPE,
            'enabled': operation == 'grant', 'audit_id': audit_id, 'messages_sent': False}


def lambda_handler(event, context):
    try:
        return {'ok': True, 'result': command(event, context)}
    except Exception as error:
        print(json.dumps({'event': 'research_key_admin_failed', 'type': type(error).__name__}))
        return {'ok': False, 'error': str(error) if isinstance(error, ValueError) else 'Operator command failed; inspect the private function log.'}
