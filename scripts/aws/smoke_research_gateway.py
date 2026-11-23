"""Live gateway export test with an audited, fifteen-minute temporary key.

The raw key exists only in process memory. Always revoke it and verify rejection
in finally, including when preparation, download or integrity validation fails.
No messages are sent; existing keys are preserved.
"""
import argparse
import csv
from datetime import datetime, timedelta, timezone
import gzip
import json
from pathlib import Path
import sys
import tempfile
import time
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.aws.deploy_research_exports import session
from scripts.research.download_data import request, save_download


def run(aws, email, actor_email, instrument, include_flagged=False):
    caller = aws.client('sts').get_caller_identity()
    lam = aws.client('lambda')
    def admin(operation, **options):
        event = {'operation': operation, 'email': email,
                 'actor': {'email': actor_email, 'arn': caller['Arn']}, **options}
        result = lam.invoke(FunctionName='aq-research-key-admin', InvocationType='RequestResponse',
                            Payload=json.dumps(event).encode())
        data = json.loads(result['Payload'].read())
        if result.get('FunctionError') or not data.get('ok'):
            raise RuntimeError(data.get('error', 'Temporary-key operation failed'))
        return data['result']
    def active_ids():
        return {item['key_id'] for item in admin('plan')['keys']}
    def wait_for_key(identifier, present):
        deadline = time.monotonic() + 10
        while True:
            active = active_ids()
            if (identifier in active) is present:
                return active
            if time.monotonic() >= deadline:
                raise RuntimeError('Key metadata index did not converge')
            time.sleep(.5)
    before = active_ids()
    created, raw, identifier = None, None, None
    try:
        created = admin('create-test-key')
        raw, identifier = created['api_key'], created['key_id']
        print('Temporary key created; raw secret will not be printed or saved.', flush=True)
        active = wait_for_key(identifier, True)
        assert before.issubset(active) and identifier in active, 'Existing keys changed unexpectedly'
        access = request(raw, '/access', api_key=True)
        assert access.get('enabled') is True
        print('Gateway accepted the scoped key and current research approval.', flush=True)
        body = {'instrument': instrument, 'include_flagged': include_flagged}
        job = request(raw, '/exports', body, api_key=True)
        assert job['spec']['start'] is None and job['spec']['end'] is None
        print(f'Full-history {instrument} job accepted; CSV preparation runs in AWS.', flush=True)
        deadline = time.monotonic() + min(720, created['expires_at'] - time.time() - 60)
        previous_state, last_notice = None, time.monotonic()
        while time.monotonic() < deadline:
            status = request(raw, f'/exports/{job["job_id"]}', api_key=True)
            state = status['state']
            if state != previous_state or time.monotonic() - last_notice >= 30:
                print(f'AWS export: {state}', flush=True)
                previous_state, last_notice = state, time.monotonic()
            if state == 'READY':
                break
            if state not in {'PENDING', 'PROCESSING'}:
                raise RuntimeError(f'Export failed: {state}')
            time.sleep(5)
        else:
            raise TimeoutError('Export did not become ready before the test deadline')
        result = request(raw, f'/exports/{job["job_id"]}/download', api_key=True)
        assert result['state'] == 'READY' and result.get('url')
        assert result['expires_in_seconds'] == 300
        assert result['spec']['include_flagged'] is include_flagged
        with tempfile.TemporaryDirectory(prefix='research-gateway-smoke-') as directory:
            destination = Path(directory) / 'export.csv.gz'
            save_download(result, destination)  # Verifies compressed-file SHA-256.
            count = flagged = 0
            with gzip.open(destination, 'rt', encoding='utf-8', newline='') as handle:
                reader = csv.DictReader(handle)
                assert len(reader.fieldnames) == result['columns']
                assert all(name in reader.fieldnames for name in ['_research_row_key', '_research_timestamp_utc',
                    '_research_status', '_research_flags', '_research_corrections'])
                for row in reader:
                    count += 1
                    flagged += bool(json.loads(row['_research_flags']))
            assert count == result['rows'] and destination.stat().st_size == result['bytes']
            assert flagged == result['included_flagged']
            if not include_flagged:
                assert flagged == 0
        assert datetime.fromisoformat(result['start']) < datetime.now(timezone.utc) - timedelta(days=14)
        print(json.dumps({'gateway_download_verified': True, 'instrument': instrument,
            'rows': result['rows'], 'bytes': result['bytes'], 'start': result['start'], 'end': result['end'],
            'include_flagged': include_flagged, 'sha256_verified': True,
            'original_keys_preserved': True}), flush=True)
        return result
    finally:
        if identifier:
            admin('revoke-test-key', key_id=identifier)
            try:
                request(raw, '/access', api_key=True)
            except urllib.error.HTTPError as failure:
                if failure.code != 403:
                    raise
            else:
                raise RuntimeError('Revoked temporary key was unexpectedly accepted')
            active = wait_for_key(identifier, False)
            assert identifier not in active and before.issubset(active)
            print('Temporary key revoked; gateway returned 403; existing active keys preserved.', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--aws-creds-file', required=True)
    parser.add_argument('--expected-account', required=True)
    parser.add_argument('--email', required=True)
    parser.add_argument('--actor-email', required=True)
    parser.add_argument('--instrument', choices=['BC-MA200', 'CO2-LICOR', 'NEPH-PM25', 'NO2-CAPS', 'SMPS'], default='NO2-CAPS')
    parser.add_argument('--include-flagged', action='store_true')
    args = parser.parse_args()
    aws = session(args.aws_creds_file)
    if aws.client('sts').get_caller_identity()['Account'] != args.expected_account:
        raise RuntimeError('Account mismatch; no test key created')
    run(aws, args.email, args.actor_email, args.instrument, args.include_flagged)


if __name__ == '__main__':
    main()
