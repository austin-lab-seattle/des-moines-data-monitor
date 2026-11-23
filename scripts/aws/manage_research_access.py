"""Explicit, audited research membership changes. No messages are sent."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.aws.deploy_research_exports import session, POOL
import lambda_api as api


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
    api.dynamodb_client = aws.client('dynamodb')
    audit = api.begin_audit_event({'subject': caller['Arn'], 'email': actor_email}, action, email,
                                  {'role': 'ResearchExporter', 'user_pool': POOL})
    if not audit:
        raise RuntimeError('Audit could not be recorded; access was not changed')
    try:
        operation = cognito.admin_remove_user_from_group if args.revoke else cognito.admin_add_user_to_group
        operation(UserPoolId=POOL, Username=user['Username'], GroupName='ResearchExporter')
        api.finish_audit_event(audit, 'SUCCEEDED')
    except Exception:
        api.finish_audit_event(audit, 'FAILED', 'Research membership update failed')
        raise
    print(json.dumps({'updated': True, 'email': email, 'messages_sent': False}), flush=True)


if __name__ == '__main__':
    main()
