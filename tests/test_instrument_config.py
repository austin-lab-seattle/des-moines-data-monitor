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
        self.assertEqual([], acquire_serial.validate_config(config))


if __name__ == "__main__":
    unittest.main()
