import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import lambda_api
from scripts import log_serial_instruments


class SerialLoggerParserTests(unittest.TestCase):
    def test_bronze_file_preserves_raw_payload_with_header(self):
        with TemporaryDirectory() as root:
            cfg = {
                "output_dir": root,
                "filename": "{date}_Neph-serial.csv",
            }
            output = log_serial_instruments.DailyInstrumentRaw(cfg)
            raw = "2026-09-17 13:13:50, 8.174, 25.114, 26.847, 39.478, 1011.836,00,07"
            output.write(datetime(2026, 9, 18, 12, 34, 56, 123000), raw)
            output.handle.close()

            lines = (Path(root) / "2026-09-18_Neph-serial.csv").read_text().splitlines()
            self.assertEqual("PC_Date_Time\tRaw_Line", lines[0])
            self.assertEqual(f"2026-09-18 12:34:56.123\t{raw}", lines[1])

    def test_no2_parser_writes_pipeline_schema(self):
        moment = datetime(2026, 9, 18, 12, 34, 56)
        epoch_seconds = (moment - log_serial_instruments.EPOCH_1904).total_seconds()
        row = log_serial_instruments.parse_no2(
            f"{epoch_seconds},1,2,3,4,5,6,7,8",
            datetime(2026, 9, 18, 12, 35, 1),
        )

        self.assertEqual(len(log_serial_instruments.NO2_COLUMNS), len(row))
        self.assertEqual("123456", row[0])
        self.assertEqual("2026-09-18 12:35:01.000", row[-3])
        self.assertEqual("2026-09-18 12:34:56.000", row[-2])
        self.assertEqual("5", row[-1])
        self.assertTrue(lambda_api.is_data_row("NO2-CAPS", ",".join(row)))

    def test_neph_parser_handles_live_stream(self):
        row = log_serial_instruments.parse_neph(
            "22026-09-17 13:13:50, 8.174, 25.114, 26.847, 39.478, 1011.836,00,07",
            datetime(2026, 9, 17, 13, 14, 0),
        )

        self.assertEqual(len(log_serial_instruments.NEPH_COLUMNS), len(row))
        self.assertEqual("8.174", row[1])
        self.assertEqual("2026-09-17 13:14:00.000", row[0])
        self.assertEqual("2026-09-17 13:13:50.000", row[-2])
        self.assertEqual("10", row[-1])
        self.assertTrue(lambda_api.is_data_row("NEPH-PM25", ",".join(row)))

    def test_licor_parser_maps_xml_to_tabular_schema(self):
        xml = """<li850><data>
          <celltemp>25.1</celltemp><cellpres>101.2</cellpres><co2>421.3</co2>
          <co2abs>0.08</co2abs><h2o>6.8</h2o><h2oabs>0.01</h2oabs>
          <h2odewpoint>1.7</h2odewpoint><ivolt>12.0</ivolt>
          <raw><co2>1</co2><co2ref>2</co2ref><h2o>3</h2o><h2oref>4</h2oref></raw>
          <flowrate>0.75</flowrate>
        </data></li850>"""
        row = log_serial_instruments.parse_licor(
            xml, datetime(2026, 9, 18, 12, 34, 56)
        )

        self.assertEqual(len(log_serial_instruments.LICOR_COLUMNS), len(row))
        self.assertEqual(["2026-09-18", "12:34:56", "421.3"], row[:3])
        self.assertEqual("0.75", row[-1])
        self.assertTrue(lambda_api.is_data_row("CO2-LICOR", "\t".join(row)))


if __name__ == "__main__":
    unittest.main()
