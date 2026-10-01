import json
import unittest
from pathlib import Path

from scripts.field.instrument_identity import classify_line, identify_text, inspect_batch


NO2 = "3872505678.767,10.599,523.765,751.62,295.89,166273,1.32319,10104,485.763"
NEPH = "2026-09-17 00:44:50, 9.605, 25.810, 27.790, 37.961, 1012.294,00,07"
LICOR = (
    "<li850><data><celltemp>27.6</celltemp><cellpres>101.3</cellpres>"
    "<co2>648.2</co2><co2abs>0.13</co2abs><h2o>6.8</h2o>"
    "<h2oabs>0.06</h2oabs><h2odewpoint>1.6</h2odewpoint>"
    "<ivolt>11.9</ivolt><raw><co2>1</co2><co2ref>2</co2ref>"
    "<h2o>3</h2o><h2oref>4</h2oref></raw><flowrate>0.05</flowrate>"
    "</data></li850>"
)
SMPS = "1,17/09/2026 12:22:09," + ",".join(str(value) for value in range(45))


class InstrumentIdentityTests(unittest.TestCase):
    def test_tracked_signature_files_identify_all_instruments(self):
        root = Path(__file__).parents[1] / "sample-data"
        expected = {
            "black-carbon-ma200.csv": "BC-MA200",
            "co2-licor.xml": "CO2-LICOR",
            "neph-pm25.txt": "NEPH-PM25",
            "no2-caps.txt": "NO2-CAPS",
            "smps.csv": "SMPS",
        }
        for filename, instrument_id in expected.items():
            with self.subTest(filename=filename):
                self.assertEqual(
                    instrument_id,
                    identify_text((root / filename).read_text(encoding="utf-8")),
                )
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(set(expected.values()), set(manifest))
        self.assertEqual(
            {name for name in expected},
            {entry["file"] for entry in manifest.values()},
        )

    def test_classifies_raw_and_pc_enveloped_serial_rows(self):
        self.assertEqual("NO2-CAPS", classify_line(NO2))
        self.assertEqual("NEPH-PM25", classify_line(NEPH))
        self.assertEqual("CO2-LICOR", classify_line(LICOR))
        self.assertEqual(
            "NEPH-PM25",
            classify_line(f"2026-09-17 00:45:01.123\t{NEPH}"),
        )
        self.assertEqual(
            "NEPH-PM25",
            classify_line(NEPH.replace("2026-", "22026-", 1)),
        )

    def test_wrong_neph_data_is_blocked_from_smps_bronze(self):
        result = inspect_batch("SMPS", NEPH + "\n" + NEPH)
        self.assertFalse(result["valid"])
        self.assertIn("detected NEPH-PM25", result["reason"])

    def test_mixed_expected_and_foreign_rows_are_blocked(self):
        result = inspect_batch("SMPS", SMPS + "\n" + NEPH)
        self.assertFalse(result["valid"])
        self.assertEqual(1, result["counts"]["SMPS"])
        self.assertEqual(1, result["counts"]["NEPH-PM25"])

    def test_correct_batch_with_metadata_is_allowed(self):
        result = inspect_batch("NEPH-PM25", "startup metadata\n" + NEPH)
        self.assertTrue(result["valid"])
        self.assertEqual(1, result["unknown_lines"])

    def test_one_valid_row_does_not_hide_mostly_garbled_content(self):
        text = NEPH + "\n" + "\n".join(f"garbled-{index}" for index in range(6))
        result = inspect_batch("NEPH-PM25", text)
        self.assertFalse(result["valid"])
        self.assertIn("6 unrecognized", result["reason"])

    def test_unrecognized_payload_fails_closed(self):
        result = inspect_batch("NO2-CAPS", "garbled data from wrong baud\n")
        self.assertFalse(result["valid"])
        self.assertIn("no recognizable NO2-CAPS", result["reason"])

    def test_split_licor_xml_is_recognized_as_a_batch(self):
        split = LICOR.replace("><", ">\n<")
        result = inspect_batch("CO2-LICOR", split)
        self.assertTrue(result["valid"])
        self.assertEqual("CO2-LICOR", identify_text(split))


if __name__ == "__main__":
    unittest.main()
