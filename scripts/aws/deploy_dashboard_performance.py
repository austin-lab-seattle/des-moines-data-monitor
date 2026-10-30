"""Deploy only API/builder code, preserving configuration and checking live sources.

Dry-run by default. Explicit AWS account and reviewed baseline are required.
This does not change IAM, schedules, routes, environment variables or memory.
"""
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.request
import zipfile

import boto3

ROOT = Path(__file__).resolve().parents[2]


def prepare(lam, function, source_path, expected_ref, include_api=False):
    live = lam.get_function(FunctionName=function)
    config = live['Configuration']
    with urllib.request.urlopen(live['Code']['Location'], timeout=30) as response:
        previous = response.read()
    assert base64.b64encode(hashlib.sha256(previous).digest()).decode() == config['CodeSha256']
    current = source_path.read_bytes()
    baseline = subprocess.check_output(['git', 'show', f'{expected_ref}:{source_path.relative_to(ROOT)}'], cwd=ROOT)
    name = source_path.name
    candidate = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(previous)) as old:
        deployed = old.read(name)
        if deployed not in (baseline, current):
            raise RuntimeError(f'{function} source differs from the reviewed baseline. Inspect before deploying.')
        replacements = {name: current}
        # Both Lambda packages import the same small usage helper through the
        # API reader. No new table, role or environment setting is required.
        usage = ROOT / 'api_usage.py'
        if usage.exists():
            replacements['api_usage.py'] = usage.read_bytes()
            if 'api_usage.py' in old.namelist():
                baseline_usage = subprocess.check_output(['git', 'show', f'{expected_ref}:api_usage.py'], cwd=ROOT)
                if old.read('api_usage.py') not in (baseline_usage, replacements['api_usage.py']):
                    raise RuntimeError('Usage helper differs from the reviewed baseline. Inspect before deploying.')
        if include_api:
            replacements['lambda_api.py'] = (ROOT / 'lambda_api.py').read_bytes()
            if 'lambda_api.py' in old.namelist():
                baseline_api = subprocess.check_output(['git', 'show', f'{expected_ref}:lambda_api.py'], cwd=ROOT)
                if old.read('lambda_api.py') not in (baseline_api, replacements['lambda_api.py']):
                    raise RuntimeError('Builder API reader differs from the reviewed baseline. Inspect before deploying.')
        with zipfile.ZipFile(candidate, 'w', zipfile.ZIP_DEFLATED) as new:
            for old_name in old.namelist():
                if old_name not in replacements:
                    new.writestr(old_name, old.read(old_name))
            for new_name, body in replacements.items():
                new.writestr(new_name, body)
        unchanged = all(key in old.namelist() and old.read(key) == body for key, body in replacements.items())
    return config, previous, candidate.getvalue(), unchanged


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--aws-creds-file', required=True)
    parser.add_argument('--expected-account', required=True)
    parser.add_argument('--expected-live-ref', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    credentials, _ = json.JSONDecoder().raw_decode(Path(args.aws_creds_file).read_text())
    session = boto3.Session(aws_access_key_id=credentials['aws_access_key_id'],
        aws_secret_access_key=credentials['aws_secret_access_key'],
        aws_session_token=credentials.get('aws_session_token'), region_name='us-west-2')
    if session.client('sts').get_caller_identity()['Account'] != args.expected_account:
        raise RuntimeError('AWS account mismatch. No changes made.')
    lam = session.client('lambda')
    # Prepare and validate both packages before changing either function.
    packages = [(function, prepare(lam, function, path, args.expected_live_ref, include_api)) for function, path, include_api in [
        ('aq-dashboard-api', ROOT / 'lambda_api.py', False),
        ('aq-silver-builder', ROOT / 'lambda' / 'silver_builder.py', True),
    ]]
    for function, (config, previous, candidate, unchanged) in packages:
        if not args.apply or unchanged:
            print(json.dumps({'function': function, 'dry_run': not args.apply, 'already_current': unchanged,
                              'baseline_verified': True, 'configuration_preserved': True}))
            continue
        parent = Path.home() / '.codex' / 'deploy-backups'
        parent.mkdir(parents=True, exist_ok=True)
        backup = Path(tempfile.mkdtemp(prefix='dashboard-cost-', dir=parent)) / f'{function}.zip'
        with os.fdopen(os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'wb') as output:
            output.write(previous)
        lam.update_function_code(FunctionName=function, ZipFile=candidate, RevisionId=config['RevisionId'])
        for _ in range(30):
            fresh = lam.get_function(FunctionName=function)['Configuration']
            if fresh.get('LastUpdateStatus') == 'Successful':
                break
            if fresh.get('LastUpdateStatus') == 'Failed':
                raise RuntimeError(f'{function} code update failed. Backup: {backup}')
            time.sleep(2)
        else:
            raise RuntimeError(f'{function} update did not finish. Check before retrying.')
        for field in ['Environment', 'MemorySize', 'Timeout', 'Role', 'Handler', 'Runtime', 'Architectures']:
            assert fresh.get(field) == config.get(field), f'{function}: {field} changed concurrently'
        assert fresh['CodeSha256'] == base64.b64encode(hashlib.sha256(candidate).digest()).decode()
        print(json.dumps({'function': function, 'deployed': True, 'configuration_preserved': True, 'rollback_zip': str(backup)}))


if __name__ == '__main__':
    main()
