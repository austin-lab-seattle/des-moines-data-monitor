"""Run usage transactions in a separate process to isolate legacy AWS fakes."""
import os,sys,subprocess,unittest
from pathlib import Path
class ApiUsageIntegrationTests(unittest.TestCase):
 def test_offline_usage_transactions(self):
  root=Path(__file__).resolve().parents[1]
  result=subprocess.run([sys.executable,str(root/'tests/usage_scenarios.py')],cwd=root,capture_output=True,text=True,timeout=90)
  self.assertEqual(0,result.returncode,result.stdout+result.stderr)
