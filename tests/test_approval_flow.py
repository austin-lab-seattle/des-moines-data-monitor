"""Run real transaction/condition checks in an offline, isolated AWS emulator."""
import os
from pathlib import Path
import subprocess
import sys
import unittest


class ApprovalIntegrationTests(unittest.TestCase):
    def test_offline_dynamodb_approval_scenarios(self):
        root = Path(__file__).resolve().parents[1]
        env = {key: value for key, value in os.environ.items() if not key.startswith("AWS_")}
        result = subprocess.run([sys.executable, str(root / "tests" / "approval_flow_scenarios.py")], cwd=root, env=env, capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
