import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts.research.download_data import save_download


class ResearchDownloadClientTests(unittest.TestCase):
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
