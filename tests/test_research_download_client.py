import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts.research.download_data import save_download, request, main, NoApiRedirect


class ResearchDownloadClientTests(unittest.TestCase):
    def test_key_mode_uses_keyed_api_header_without_a_browser_or_credential_redirect(self):
        response = io.BytesIO(b'{"enabled": true}')
        with mock.patch('urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = response
            self.assertTrue(request('CI-only-key', '/access', api_key=True)['enabled'])
            sent = opener.return_value.open.call_args.args[0]
            self.assertIn('/air-quality/v1/keyed/research/access', sent.full_url)
            self.assertEqual('CI-only-key', sent.get_header('X-api-key'))
            self.assertIsNone(sent.get_header('Authorization'))
        with self.assertRaises(RuntimeError):
            NoApiRedirect().redirect_request(None, None, 302, '', {}, 'https://another-host.invalid/')

    def test_key_mode_passes_toggle_and_never_attempts_interactive_sign_in(self):
        with tempfile.TemporaryDirectory() as directory:
            output = str(Path(directory) / 'data.csv.gz')
            with mock.patch.dict('os.environ', {'AQ_API_KEY': 'CI-only-key'}), \
                 mock.patch('sys.argv', ['download_data.py', '--auth', 'api-key', '--instrument', 'BC-MA200', '--include-flagged', '--output', output]), \
                 mock.patch('scripts.research.download_data.sign_in') as sign_in, \
                 mock.patch('scripts.research.download_data.request', side_effect=[{'job_id': 'test'}, {'state': 'READY'}, {'state': 'READY', 'url': 'https://s3.invalid', 'rows': 2}]) as api, \
                 mock.patch('scripts.research.download_data.save_download') as save:
                main()
                sign_in.assert_not_called()
                self.assertEqual({'instrument': 'BC-MA200', 'include_flagged': True}, api.call_args_list[0].args[2])
                self.assertTrue(all(call.kwargs['api_key'] for call in api.call_args_list))
                save.assert_called_once()

    def test_download_is_verified_and_does_not_forward_a_team_token_to_s3(self):
        blob = b'CI-only-export-data'
        result = {'url': 'https://research.s3.us-west-2.amazonaws.com/test.csv.gz',
                  'sha256': hashlib.sha256(blob).hexdigest()}
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'readings.csv.gz'
            with mock.patch('urllib.request.urlopen', return_value=io.BytesIO(blob)) as transport:
                save_download(result, destination)
            self.assertEqual(blob, destination.read_bytes())
            self.assertEqual(result['url'], transport.call_args.args[0])

    def test_bad_digest_never_overwrites_an_existing_download_or_leaves_a_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'readings.csv.gz'
            destination.write_bytes(b'previous-good-export')
            with mock.patch('urllib.request.urlopen', return_value=io.BytesIO(b'corrupted')):
                with self.assertRaises(RuntimeError):
                    save_download({'url': 'https://research.s3.amazonaws.com/data', 'sha256': 'bad'}, destination)
            self.assertEqual(b'previous-good-export', destination.read_bytes())
            self.assertEqual([destination], list(Path(directory).iterdir()))

    def test_non_s3_or_cleartext_downloads_are_rejected_before_the_network(self):
        with mock.patch('urllib.request.urlopen') as transport:
            for url in ['http://research.s3.amazonaws.com/a', 'https://attacker.invalid/a', 'file:///tmp/data']:
                with self.assertRaises(ValueError):
                    save_download({'url': url}, Path('/tmp/unused-ci-download'))
        transport.assert_not_called()
