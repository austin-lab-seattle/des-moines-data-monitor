"""Explicit API-key authorization for the private research export service."""
import hmac
import os
from botocore.exceptions import ClientError
from datetime import datetime, timezone

import lambda_api as api

SCOPE = 'research:export'
ROOT = '/air-quality/v1/keyed/research'


def authenticate(event, cognito, groups_for):
    # This endpoint never falls back to anonymous access or token claims.
    raw = api.normalized_headers(event).get('x-api-key', '').strip()
    if not raw:
        return None, api.response(401, {'error': 'An approved research API key is required.'})
    match = api.API_KEY_PATTERN.fullmatch(raw)
    if not match:
        return None, api.response(403, {'error': 'Research API access denied.'})
    if not api.API_KEY_HASH_PEPPER:
        raise RuntimeError('API-key validation is not configured')
    key_id = match.group(1)
    item = api.dynamodb_client.get_item(TableName=api.API_KEYS_TABLE,
        Key={'key_id': {'S': key_id}}, ConsistentRead=True).get('Item', {})
    expected = api.ddb_string(item, 'key_hash')
    email = api.normalize_email(api.ddb_string(item, 'email'))
    subject = api.ddb_string(item, 'research_subject')
    if (not expected or not hmac.compare_digest(api.key_hash(raw), expected)
            or api.ddb_string(item, 'status') != 'ACTIVE'
            or SCOPE not in api.ddb_string_list(item, 'scopes') or not email or not subject):
        return None, api.response(403, {'error': 'Research API access denied.'})
    try:
        user = cognito.admin_get_user(UserPoolId=api_research_pool(), Username=email)
    except ClientError as error:
        if error.response['Error']['Code'] == 'UserNotFoundException':
            return None, api.response(403, {'error': 'Research API access denied.'})
        raise
    attrs = {entry['Name']: entry['Value'] for entry in user['UserAttributes']}
    if (not user.get('Enabled') or attrs.get('email_verified') != 'true'
            or attrs.get('email') != email or attrs.get('sub') != subject):
        return None, api.response(403, {'error': 'Research API access denied.'})
    roles = groups_for(user['Username'])
    if 'ResearchExporter' not in roles:
        return None, api.response(403, {'error': 'Research approval is no longer active.'})
    limited = api.enforce_rate_limit(key_id, SCOPE)
    if limited:
        return None, limited
    event['_api_usage'] = {'key_id': key_id, 'email': email, 'scope': SCOPE,
                          'at': datetime.now(timezone.utc).isoformat()}
    return {'subject': subject, 'email': email, 'roles': sorted(roles), 'key_id': key_id}, None


def api_research_pool():
    return os.environ.get('TEAM_USER_POOL_ID', 'us-west-2_oR7CIzxQG')
