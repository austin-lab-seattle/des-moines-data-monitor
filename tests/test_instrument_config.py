import json
import unittest
from pathlib import Path

from scripts.field import acquire_serial


class InstrumentConfigTests(unittest.TestCase):
    def test_unified_example_covers_all_instruments_and_three_serial_sources(self):
        path = Path(__file__).parents[1] / "config" / "instruments.example.json"
        config = json.loads(path.read_text(encoding="utf-8"))

        instruments = config["instruments"]
        self.assertEqual(
            {"BC-MA200", "CO2-LICOR", "NEPH-PM25", "NO2-CAPS", "SMPS"},
            {item["id"] for item in instruments},
        )
        serial = acquire_serial.serial_instrument_configs(config)
        self.assertEqual(
            {"CO2-LICOR", "NEPH-PM25", "NO2-CAPS"},
            {item["id"] for item in serial},
        )
        self.assertTrue(all(item["baud"] == 38400 for item in serial))
        for instrument in instruments:
            patterns = instrument["data_glob"]
            if isinstance(patterns, str):
                patterns = [patterns]
            self.assertTrue(
                all(pattern.startswith("C:/des_moines/data/") for pattern in patterns)
            )
        self.assertEqual(
            {"NEPH-PM25", "NO2-CAPS"},
            {item["id"] for item in serial if item["active"]},
        )
        self.assertEqual([], acquire_serial.validate_config(config))

    def test_pre_flag_config_defaults_only_confirmed_sources_to_enabled(self):
        config = {
            "instruments": [
                {"id": instrument_id, "active": True, "serial": {
                    "name": parser,
                    "port": port,
                    "baud": 38400,
                    "parser": parser,
                    "output_dir": "data",
                    "filename": f"{parser}.txt",
                }}
                for instrument_id, parser, port in (
                    ("NO2-CAPS", "no2", "COM7"),
                    ("NEPH-PM25", "neph", "COM8"),
                    ("CO2-LICOR", "licor", "COM9"),
                )
            ]
        }
        serial = acquire_serial.serial_instrument_configs(config)
        self.assertEqual(
            {"NO2-CAPS", "NEPH-PM25"},
            {item["id"] for item in serial if item["active"]},
        )


if __name__ == "__main__":
    unittest.main()
