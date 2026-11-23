"""Grant/revoke one existing key's research scope through the IAM-only service.

Read-only plan by default. Never creates or prints a raw API key.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.aws.deploy_research_exports import session


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--aws-creds-file', required=True)
    parser.add_argument('--expected-account', required=True)
    parser.add_argument('--email', required=True)
    parser.add_argument('--actor-email', required=True)
    parser.add_argument('--key-id')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--revoke', action='store_true')
    args = parser.parse_args()
    aws = session(args.aws_creds_file)
    caller = aws.client('sts').get_caller_identity()
    if caller['Account'] != args.expected_account:
        raise RuntimeError('Account mismatch; no changes made')
    payload = {'operation': 'revoke' if args.revoke else 'grant', 'email': args.email,
               'actor': {'email': args.actor_email, 'arn': caller['Arn']}}
    if not args.apply:
        payload['operation'] = 'plan'
    if args.key_id:
        payload['key_id'] = args.key_id
    response = aws.client('lambda').invoke(FunctionName='aq-research-key-admin',
        InvocationType='RequestResponse', Payload=json.dumps(payload).encode())
    result = json.loads(response['Payload'].read())
    if response.get('FunctionError') or not result.get('ok'):
        raise RuntimeError(result.get('error', 'Operator command failed'))
    print(json.dumps(result['result']))


if __name__ == '__main__':
    main()
