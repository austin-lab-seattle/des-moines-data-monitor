import importlib.util
import sys
import types
import unittest
from pathlib import Path


def load_module():
    fake_boto3 = types.SimpleNamespace(client=lambda *args, **kwargs: object())
    previous = sys.modules.get("boto3")
    sys.modules["boto3"] = fake_boto3
    try:
        path = Path(__file__).parents[1] / "lambda" / "silver_builder.py"
        spec = importlib.util.spec_from_file_location("silver_builder", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            del sys.modules["boto3"]
        else:
            sys.modules["boto3"] = previous


silver_builder = load_module()


class SilverBuilderTests(unittest.TestCase):
    def test_neph_accepts_slash_and_hyphen_timestamps(self):
        slash = "2024/06/10 20:01:00,0,8.174,302.087,302.589,27.186,1009.113"
        hyphen = "2026-09-17 13:13:50, 8.174, 25.114, 26.847, 39.478, 1011.836,00,07"
        milliseconds = "2026-09-17 13:13:50.123,8.174,25.114,26.847,39.478,1011.836,00,07"
        self.assertTrue(silver_builder.is_data_row("NEPH-PM25", slash))
        self.assertTrue(silver_builder.is_data_row("NEPH-PM25", hyphen))
        self.assertTrue(silver_builder.is_data_row("NEPH-PM25", milliseconds))

    def test_headerless_neph_capture_uses_defined_schema(self):
        capture = "\n".join([
            "=~=~=~=~=~=~=~=~=~=~= PuTTY log 2026.09.17 13:13:45 =~=~=~=~=~=~=~=~=~=~=",
            "2026-09-17 13:13:50, 8.174, 25.114, 26.847, 39.478, 1011.836,00,07",
            "2026-09-17 13:14:00, 8.200, 25.120, 26.850, 39.500, 1011.830,00,07",
        ])

        header, rows, metadata, total, duplicates, mismatches = silver_builder.consolidate(
            "NEPH-PM25",
            [("NEPH-PM25/bronze/Neph__batch.txt", capture)],
        )

        self.assertEqual(10, len(silver_builder.split_fields(header)))
        self.assertIn("Scat coefficient", header)
        self.assertEqual(2, total)
        self.assertEqual(2, len(rows))
        self.assertEqual(0, duplicates)
        self.assertEqual(0, mismatches)
        self.assertTrue(any("PuTTY log" in line for line in metadata))

    def test_raw_no2_epoch_is_normalized_in_silver(self):
        capture = (
            "3872505678.767,10.599,523.765,751.62,295.89,166273,"
            "1.32319,10104,485.763"
        )
        header, rows, _metadata, total, _duplicates, mismatches = (
            silver_builder.consolidate(
                "NO2-CAPS", [("NO2-CAPS/bronze/no2.txt", capture)]
            )
        )

        self.assertEqual(silver_builder.NO2_CANONICAL_COLUMNS, silver_builder.split_fields(header))
        self.assertEqual(1, total)
        self.assertEqual(0, mismatches)
        values = silver_builder.split_fields(rows[0])
        self.assertEqual("2026-09-17 16:01:18.767", values[9])
        self.assertEqual(values[9], values[10])

    def test_legacy_neph_repairs_clear_twelve_hour_clock_error(self):
        capture = "\n".join([
            "=~=~= PuTTY log 2026.09.17 12:44:50 =~=~=~=",
            "2026-09-17 00:44:50, 9.605, 25.810, 27.790, 37.961, 1012.294,00,07",
            "2026-09-17 00:45:00, 9.561, 25.812, 27.786, 37.956, 1012.294,00,07",
        ])
        _header, rows, _metadata, total, _duplicates, mismatches = (
            silver_builder.consolidate(
                "NEPH-PM25", [("NEPH-PM25/bronze/Neph.txt", capture)]
            )
        )

        self.assertEqual(2, total)
        self.assertEqual(0, mismatches)
        first = silver_builder.split_fields(rows[0])
        self.assertEqual("2026-09-17 12:44:50.000", first[0])
        self.assertEqual("2026-09-17 00:44:50.000", first[-2])
        self.assertEqual("43200", first[-1])

    def test_raw_licor_uses_pc_envelope_timestamp(self):
        xml = (
            "<li850><data><celltemp>27.6</celltemp><cellpres>101.3</cellpres>"
            "<co2>648.2</co2><co2abs>.13</co2abs><h2o>6.79</h2o>"
            "<h2oabs>.06</h2oabs><h2odewpoint>1.59</h2odewpoint><ivolt>11.9</ivolt>"
            "<raw><co2>1</co2><co2ref>2</co2ref><h2o>3</h2o><h2oref>4</h2oref></raw>"
            "<flowrate>.055</flowrate></data></li850>"
        )
        capture = f"PC_Date_Time\tRaw_Line\n2026-09-17 16:26:38.125\t{xml}"
        header, rows, _metadata, total, _duplicates, mismatches = (
            silver_builder.consolidate(
                "CO2-LICOR", [("CO2-LICOR/bronze/co2.txt", capture)]
            )
        )

        self.assertEqual(silver_builder.CO2_CANONICAL_COLUMNS, silver_builder.split_fields(header))
        self.assertEqual(1, total)
        self.assertEqual(0, mismatches)
        values = silver_builder.split_fields(rows[0])
        self.assertEqual(["2026-09-17", "16:26:38", "648.2"], values[:3])
        self.assertEqual("pc_received_at", values[-1])

    def test_legacy_putty_licor_marks_inferred_timestamp(self):
        xml = (
            "<li850><data><celltemp>27.6</celltemp><cellpres>101.3</cellpres>"
            "<co2>648.2</co2><co2abs>.13</co2abs><h2o>6.79</h2o>"
            "<h2oabs>.06</h2oabs><h2odewpoint>1.59</h2odewpoint><ivolt>11.9</ivolt>"
            "<raw><co2>1</co2><co2ref>2</co2ref><h2o>3</h2o><h2oref>4</h2oref></raw>"
            "<flowrate>.055</flowrate></data></li850>"
        )
        capture = "\n".join([
            "=~=~= PuTTY log 2026.09.17 16:26:37 =~=~=~=",
            xml,
            xml.replace("648.2", "649.2"),
        ])
        _header, rows, _metadata, total, _duplicates, mismatches = (
            silver_builder.consolidate(
                "CO2-LICOR", [("CO2-LICOR/bronze/co2.txt", capture)]
            )
        )

        self.assertEqual(2, total)
        self.assertEqual(0, mismatches)
        first = silver_builder.split_fields(rows[0])
        second = silver_builder.split_fields(rows[1])
        self.assertEqual("16:26:37", first[1])
        self.assertEqual("16:26:38", second[1])
        self.assertEqual("inferred_from_putty_start_1s", first[-1])

    def test_neph_silver_adds_corrected_bscat_and_pm25(self):
        header = (
            "Date_Time,Scat coefficient,Sample temperature,Enclosure temperature,"
            "Relative humidity,Atmospheric pressure,Major State,Minor State"
        )
        row = "2026-09-17 13:13:50,8.174,25.114,26.847,39.478,1011.836,00,07"

        transformed_header, transformed_rows = silver_builder.transform_silver(
            "NEPH-PM25", header, [row]
        )

        columns = silver_builder.split_fields(transformed_header)
        values = silver_builder.split_fields(transformed_rows[0])
        self.assertEqual(silver_builder.NEPH_RAW_BSCAT_COLUMN, columns[1])
        self.assertEqual(silver_builder.NEPH_BSCAT_COLUMN, columns[-2])
        self.assertEqual(silver_builder.NEPH_PM25_COLUMN, columns[-1])
        self.assertAlmostEqual(0.08174, float(values[-2]))
        self.assertAlmostEqual(4.937764, float(values[-1]))
        self.assertEqual(len(columns), len(values))

    def test_smps_total_concentration_header_is_canonical(self):
        header = "Scan Number,DateTime Sample Start,Total Concentration (#/cm�)"
        transformed_header, rows = silver_builder.transform_silver(
            "SMPS", header, ["1,10/6/2025 16:25:06,5497.34"]
        )

        self.assertEqual(
            ["Scan Number", "DateTime Sample Start", "Total Concentration (#/cm³)"],
            silver_builder.split_fields(transformed_header),
        )
        self.assertEqual(["1,10/6/2025 16:25:06,5497.34"], rows)

    def test_smps_consolidation_unions_schemas_and_reuses_source_header(self):
        common = ["Scan Number", "DateTime Sample Start", "Total Concentration (#/cm�)"]
        header_a = common + [f"A{i}" for i in range(39)]
        header_b = common + [f"B{i}" for i in range(40)]
        row_a = ["1", "9/1/2026 12:00:00", "100"] + [str(i) for i in range(39)]
        row_b1 = ["1", "9/2/2026 12:00:00", "200"] + [str(i) for i in range(40)]
        row_b2 = ["2", "9/2/2026 12:01:00", "201"] + [str(i + 1) for i in range(40)]
        texts = [
            (
                "SMPS/bronze/A__batch_1.txt",
                "\n".join([silver_builder.csv_line(header_a), silver_builder.csv_line(row_a)]),
            ),
            (
                "SMPS/bronze/B__batch_1.txt",
                "\n".join([silver_builder.csv_line(header_b), silver_builder.csv_line(row_b1)]),
            ),
            ("SMPS/bronze/B__batch_2.txt", silver_builder.csv_line(row_b2)),
        ]

        header, rows, _metadata, total, duplicates, mismatches = (
            silver_builder.consolidate("SMPS", texts)
        )

        columns = silver_builder.split_fields(header)
        self.assertEqual(3, total)
        self.assertEqual(3, len(rows))
        self.assertEqual(0, duplicates)
        self.assertEqual(0, mismatches)
        self.assertEqual(len(set(header_a + header_b)), len(columns))
        self.assertTrue(all(len(silver_builder.split_fields(row)) == len(columns) for row in rows))


if __name__ == "__main__":
    unittest.main()
