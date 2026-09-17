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
        self.assertTrue(silver_builder.is_data_row("NEPH-PM25", slash))
        self.assertTrue(silver_builder.is_data_row("NEPH-PM25", hyphen))

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

        self.assertEqual(8, len(silver_builder.split_fields(header)))
        self.assertIn("Scat coefficient", header)
        self.assertEqual(2, total)
        self.assertEqual(2, len(rows))
        self.assertEqual(0, duplicates)
        self.assertEqual(0, mismatches)
        self.assertTrue(any("PuTTY log" in line for line in metadata))


if __name__ == "__main__":
    unittest.main()
