"""Explicit, audited research membership changes. No messages are sent."""
import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.aws.deploy_research_exports import session, POOL
import lambda_api as api


def change_membership(cognito, s3, bucket, user, actor, email, revoke=False):
    """Record intent before mutation and retain an independent final result."""
    audit_id = str(uuid.uuid4())
    prefix = f'audit/research-access/{audit_id}/'
    event = {'audit_id': audit_id, 'actor': actor, 'email': email,
        'action': 'REVOKE_RESEARCH_ACCESS' if revoke else 'GRANT_RESEARCH_ACCESS',
        'role': 'ResearchExporter', 'user_pool': POOL,
        'created_at': datetime.now(timezone.utc).isoformat()}
    def record(name, result):
        s3.put_object(Bucket=bucket, Key=prefix + name + '.json',
            Body=json.dumps({**event, 'result': result,
                'recorded_at': datetime.now(timezone.utc).isoformat()}).encode(),
            ContentType='application/json', CacheControl='no-store', IfNoneMatch='*')
    record('requested', 'PENDING')  # Fail closed if durable audit is unavailable.
    try:
        operation = cognito.admin_remove_user_from_group if revoke else cognito.admin_add_user_to_group
        operation(UserPoolId=POOL, Username=user['Username'], GroupName='ResearchExporter')
    except Exception:
        record('completed', 'FAILED')
        raise
    record('completed', 'SUCCEEDED')
    return audit_id


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--aws-creds-file', required=True)
    parser.add_argument('--expected-account', required=True)
    parser.add_argument('--email', required=True)
    parser.add_argument('--actor-email', required=True)
    parser.add_argument('--revoke', action='store_true')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    email = api.normalize_email(args.email)
    actor_email = api.normalize_email(args.actor_email)
    if not email or not actor_email:
        raise ValueError('Valid account and actor emails are required')
    aws = session(args.aws_creds_file)
    caller = aws.client('sts').get_caller_identity()
    if caller['Account'] != args.expected_account:
        raise RuntimeError('Account mismatch')
    cognito = aws.client('cognito-idp')
    user = cognito.admin_get_user(UserPoolId=POOL, Username=email)
    attrs = {item['Name']: item['Value'] for item in user['UserAttributes']}
    if not user['Enabled'] or attrs.get('email') != email or attrs.get('email_verified') != 'true':
        raise RuntimeError('The existing verified, enabled team account is required; no account was created')
    action = 'REVOKE_RESEARCH_ACCESS' if args.revoke else 'GRANT_RESEARCH_ACCESS'
    print(json.dumps({'email': email, 'role': 'ResearchExporter', 'action': action,
                      'apply': args.apply, 'messages_sent': False}), flush=True)
    if not args.apply:
        return
    bucket = f'des-moines-research-{caller["Account"]}-us-west-2'
    audit_id = change_membership(cognito, aws.client('s3'), bucket, user,
        {'arn': caller['Arn'], 'email': actor_email}, email, args.revoke)
    print(json.dumps({'updated': True, 'email': email, 'audit_id': audit_id, 'messages_sent': False}), flush=True)


if __name__ == '__main__':
    main()
