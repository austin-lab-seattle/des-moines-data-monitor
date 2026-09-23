import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.field import copy_to_shared_drive


class SharedDriveCopyTests(unittest.TestCase):
    def test_copy_is_non_destructive_and_skips_current_files(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "local"
            destination = Path(root) / "shared"
            source_file = source / "nephlometer" / "Neph.txt"
            source_file.parent.mkdir(parents=True)
            source_file.write_text("row-one\n")
            destination.mkdir()
            extra = destination / "shared-only.txt"
            extra.write_text("keep me")

            self.assertTrue(copy_to_shared_drive.copy_tree(source, destination))
            self.assertEqual(
                "row-one\n",
                (destination / "nephlometer" / "Neph.txt").read_text(),
            )
            self.assertEqual("keep me", extra.read_text())

            status, detail = copy_to_shared_drive.copy_stable_snapshot(
                source_file, destination / "nephlometer" / "Neph.txt"
            )
            self.assertEqual(("unchanged", None), (status, detail))

    def test_file_that_changes_during_copy_is_deferred(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "live.txt"
            destination = Path(root) / "shared" / "live.txt"
            source.write_text("live row\n")
            with mock.patch.object(
                copy_to_shared_drive,
                "file_signature",
                side_effect=[(9, 1), (10, 2)],
            ):
                status, detail = copy_to_shared_drive.copy_stable_snapshot(
                    source, destination
                )
            self.assertEqual("deferred", status)
            self.assertIn("changed", detail)
            self.assertFalse(destination.exists())
            self.assertEqual([], list(destination.parent.glob("*.partial")))

    def test_destination_cannot_be_inside_source(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "data"
            source.mkdir()
            with self.assertRaisesRegex(ValueError, "cannot be inside"):
                copy_to_shared_drive.validate_roots(source, source / "shared")

    def test_locked_file_is_deferred_after_retries(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "live.txt"
            destination = Path(root) / "shared" / "live.txt"
            source.write_text("live row\n")
            with (
                mock.patch.object(
                    Path,
                    "open",
                    side_effect=PermissionError("file is locked"),
                ),
                mock.patch.object(copy_to_shared_drive.time, "sleep"),
            ):
                status, detail = copy_to_shared_drive.copy_stable_snapshot(
                    source, destination
                )
            self.assertEqual("deferred", status)
            self.assertIn("locked", detail)
            self.assertFalse(destination.exists())
            self.assertEqual([], list(destination.parent.glob("*.partial")))


if __name__ == "__main__":
    unittest.main()
