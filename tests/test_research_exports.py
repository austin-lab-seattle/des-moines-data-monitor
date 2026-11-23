"""Isolate Moto from legacy test modules that install global AWS fakes."""
import subprocess
import sys
import unittest
from pathlib import Path


class ResearchExportIntegrationTests(unittest.TestCase):
    def test_offline_research_export_security_and_accuracy(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, str(root / 'tests/research_export_scenarios.py')],
                                cwd=root, capture_output=True, text=True, timeout=90)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
