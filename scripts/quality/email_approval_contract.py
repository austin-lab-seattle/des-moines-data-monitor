"""Render real approval handlers using only offline Moto tables and a fake inbox.

Consumed by Playwright. No real credentials, AWS, email, or data are used.
"""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("offline_approval", ROOT / "tests" / "approval_flow_scenarios.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def build_contract():
    scenario = module.EmailLinkApprovalTests()
    scenario.setUp()
    try:
        scenario.verified()
        links = scenario.links()
        approve = links["elena@example.org", "approve"]
        reject = links["yeon@example.org", "reject"]
        contract = {"synthetic_ci_only": True, "approve_token": approve, "reject_token": reject,
            "approve_preview": scenario.open(approve), "reject_preview": scenario.open(reject),
            "invalid": scenario.open("invalid")}
        contract["approved"] = scenario.open(approve, "POST")
        contract["used"] = scenario.open(approve)
        assert contract["approved"]["statusCode"] == 200
        assert contract["used"]["statusCode"] == 400
        assert len(scenario.keys()) == 1
        return contract
    finally:
        scenario.tearDown()


if __name__ == "__main__":
    print(json.dumps(build_contract()))
