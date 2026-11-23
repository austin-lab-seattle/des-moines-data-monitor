import contextlib
import io
import json
import unittest
import urllib.error
from unittest import mock

from scripts.aws.smoke_research_gateway import run


class ResearchGatewaySmokeTests(unittest.TestCase):
    def test_failure_still_revokes_the_temp_key_checks_rejection_and_preserves_multiple_existing_keys(self):
        aws = mock.Mock()
        sts, lam = mock.Mock(), mock.Mock()
        aws.client.side_effect = lambda name: {'sts': sts, 'lambda': lam}[name]
        sts.get_caller_identity.return_value = {'Arn': 'operator-arn'}
        operations = []
        revoked = False
        def invoke(**options):
            nonlocal revoked
            payload = json.loads(options['Payload'])
            operations.append(payload)
            operation = payload['operation']
            if operation == 'plan':
                result = {'keys': [{'key_id': 'original-one'}, {'key_id': 'original-two'}]}
            elif operation == 'create-test-key':
                result = {'key_id': 'smok-ci-key', 'api_key': 'CI-private-secret'}
            else:
                self.assertEqual('revoke-test-key', operation)
                self.assertEqual('smok-ci-key', payload['key_id'])
                revoked = True
                result = {'active': False}
            # The first post-creation plan includes the extra active key.
            if operation == 'plan' and any(item['operation'] == 'create-test-key' for item in operations) and not revoked:
                result['keys'].append({'key_id': 'smok-ci-key'})
            return {'Payload': io.BytesIO(json.dumps({'ok': True, 'result': result}).encode())}
        lam.invoke.side_effect = invoke
        def gateway(*args, **kwargs):
            if revoked:
                raise urllib.error.HTTPError('https://api.invalid', 403, 'Revoked', {}, None)
            raise RuntimeError('Gateway test failure')
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch('scripts.aws.smoke_research_gateway.request', side_effect=gateway):
            with self.assertRaisesRegex(RuntimeError, 'Gateway test failure'):
                run(aws, 'approved@example.org', 'operator@example.org', 'NO2-CAPS')
        self.assertTrue(revoked)
        self.assertNotIn('CI-private-secret', output.getvalue())
        self.assertIn('gateway returned 403', output.getvalue())
        self.assertEqual(['plan', 'create-test-key', 'plan', 'revoke-test-key', 'plan'],
                         [item['operation'] for item in operations])
