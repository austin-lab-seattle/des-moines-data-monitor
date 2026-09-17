import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from scripts import upload_instrument_data


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


if __name__ == "__main__":
    unittest.main()
