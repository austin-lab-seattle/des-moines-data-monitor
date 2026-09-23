import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from scripts.field import upload_to_aws as upload_instrument_data


class UploaderSetupTests(unittest.TestCase):
    def config(self, root, pattern):
        config_path = Path(root) / "instruments_config.json"
        config_path.write_text(json.dumps({
            "instruments": [{
                "id": "NO2-CAPS",
                "ingestion_type": "growing_file",
                "data_glob": pattern,
                "active": True,
            }],
            "s3_bucket": "example-bucket",
            "aws_region": "us-west-2",
            "pipeline_status_key": "pipeline_status.json",
        }))
        return str(config_path)

    def test_preflight_passes_without_uploading(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "NO2-CAPS.dat"
            source.write_text("header\nvalue\n")
            config_path = self.config(root, str(source))

            session = mock.Mock()
            session.get_credentials.return_value = object()
            with (
                mock.patch.object(upload_instrument_data, "CONFIG_FILE", config_path),
                mock.patch.object(
                    upload_instrument_data,
                    "boto3",
                    types.SimpleNamespace(Session=mock.Mock(return_value=session)),
                ),
                mock.patch.object(upload_instrument_data, "create_s3_client") as create_client,
            ):
                self.assertTrue(upload_instrument_data.validate_setup())
                create_client.assert_called_once()

    def test_preflight_allows_enabled_instrument_before_data_arrives(self):
        with tempfile.TemporaryDirectory() as root:
            config_path = self.config(root, str(Path(root) / "missing-*.dat"))
            session = mock.Mock()
            session.get_credentials.return_value = object()
            with (
                mock.patch.object(upload_instrument_data, "CONFIG_FILE", config_path),
                mock.patch.object(
                    upload_instrument_data,
                    "boto3",
                    types.SimpleNamespace(Session=mock.Mock(return_value=session)),
                ),
                mock.patch.object(upload_instrument_data, "create_s3_client"),
            ):
                self.assertTrue(upload_instrument_data.validate_setup())

    def test_transient_file_lock_is_retried_without_losing_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "live.txt"
            source.write_bytes(b"first\nsecond\n")
            real_open = open
            attempts = 0

            def flaky_open(path, mode="r", *args, **kwargs):
                nonlocal attempts
                if Path(path) == source and mode == "rb":
                    attempts += 1
                    if attempts < 3:
                        raise PermissionError("sharing violation")
                return real_open(path, mode, *args, **kwargs)

            with (
                mock.patch("builtins.open", side_effect=flaky_open),
                mock.patch.object(upload_instrument_data.time, "sleep"),
            ):
                data, used, new, held, size = upload_instrument_data.read_new_bytes(
                    str(source), 0
                )

            self.assertEqual(3, attempts)
            self.assertEqual("first\nsecond\n", data)
            expected_size = len(source.read_bytes())
            self.assertEqual(
                (0, expected_size, 0, expected_size),
                (used, new, held, size),
            )

    def test_explicit_credentials_file_wins_over_default_chain(self):
        with tempfile.TemporaryDirectory() as root:
            credentials_path = Path(root) / "aws_creds.json"
            credentials_path.write_text(json.dumps({
                "aws_access_key_id": "test-access-key",
                "aws_secret_access_key": "test-secret-key",
                "region": "us-west-2",
            }))
            session = mock.Mock()
            session.get_credentials.return_value = object()
            explicit_client = mock.Mock()
            fake_boto3 = types.SimpleNamespace(
                Session=mock.Mock(return_value=session),
                client=mock.Mock(return_value=explicit_client),
            )

            with (
                mock.patch.object(upload_instrument_data, "boto3", fake_boto3),
                mock.patch.object(
                    upload_instrument_data, "CREDS_FILE", str(credentials_path)
                ),
                mock.patch.object(upload_instrument_data, "PREFER_CREDS_FILE", True),
            ):
                client = upload_instrument_data.create_s3_client({
                    "aws_region": "us-east-1"
                })

            self.assertIs(explicit_client, client)
            fake_boto3.client.assert_called_once_with(
                "s3",
                aws_access_key_id="test-access-key",
                aws_secret_access_key="test-secret-key",
                region_name="us-west-2",
            )
            session.client.assert_not_called()

    def test_missing_explicit_credentials_file_fails_clearly(self):
        with (
            mock.patch.object(upload_instrument_data, "CREDS_FILE", "missing.json"),
            mock.patch.object(upload_instrument_data, "PREFER_CREDS_FILE", True),
        ):
            with self.assertRaisesRegex(FileNotFoundError, "configured AWS credential"):
                upload_instrument_data.create_s3_client({"aws_region": "us-west-2"})


if __name__ == "__main__":
    unittest.main()
