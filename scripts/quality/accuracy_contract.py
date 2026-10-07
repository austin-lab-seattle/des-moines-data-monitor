"""CI-only synthetic Silver -> real API contract. Never uploads any data.

The browser consumes actual production handler outputs, not hand-mocked means.
Expected arithmetic is literal and independently specified below. No credentials
or network are used: only the S3 transport is replaced with an in-memory store.
"""
import importlib.util
import io
import json
import sys
import types
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
CASES = {
    "BC-MA200": ("Date / time local,BC1", ["2026-10-07 00:00:00,0", "2026-10-07 00:00:05,120"], "BC1", "120", 60),
    "CO2-LICOR": ("System_Date_(Y-M-D),System_Time_(h:m:s),CO2_(umol_mol-1)", ["2026-10-07,00:00:00,400", "2026-10-07,00:00:05,440"], "CO2_(umol_mol-1)", "440", 420),
    "NEPH-PM25": ("Date_Time,PM2.5 (µg/m³)", ["2026-10-07 00:00:00,2.6", "2026-10-07 00:00:05,5.46"], "PM2.5 (µg/m³)", "5.46", 4.03),
    "NO2-CAPS": ("HHMMSS,Concentration,Timestamp", ["000000,10.123456789,2026-10-07 00:00:00", "000005,11.876543211,2026-10-07 00:00:05"], "Concentration", "11.876543211", 11),
    "SMPS": ("Scan Number,DateTime Sample Start,Total Concentration (#/cm³)", ["1,07/10/2026 00:00:00,1000", "2,07/10/2026 00:00:05,3000"], "Total Concentration (#/cm³)", "3000", 2000),
}


class MemoryS3:
    def get_object(self, *, Bucket, Key):
        instrument = Key.split("/")[0]
        if Key != f"{instrument}/silver/{instrument}_data.csv":
            raise AssertionError(f"Unexpected data source: {Key}")
        header, rows, *_ = CASES[instrument]
        return {"Body": io.BytesIO("\n".join([header, *rows]).encode())}

    def get_paginator(self, _name):
        return types.SimpleNamespace(paginate=lambda **kwargs: [{"Contents": []}])


def build_contract():
    fake = types.SimpleNamespace(client=lambda *args, **kwargs: MemoryS3())
    spec = importlib.util.spec_from_file_location("accuracy_contract_api", ROOT / "lambda_api.py")
    api = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"boto3": fake}):
        spec.loader.exec_module(api)
    contract = {"synthetic_ci_only": True, "instruments": {}}
    for instrument, (_, _, column, latest, mean) in CASES.items():
        def payload(handler, **query):
            result = handler({"queryStringParameters": {"instrument": instrument, **query}})
            assert result["statusCode"] == 200, result
            return json.loads(result["body"])
        observations = payload(api.get_silver_records, limit="8", order="desc")
        latest_response = payload(api.get_silver_records, limit="1", order="desc")
        series = payload(api.get_series)
        assert observations["rows"][0]["values"][column] == latest
        assert latest_response["rows"][0] == observations["rows"][0]
        assert series["measurement"] == column
        assert series["series"] == [{"t": "2026-10-07T07:00:00Z", "v": mean, "n": 2}]
        contract["instruments"][instrument] = {
            "observations": observations, "latest": latest_response, "timeseries": series,
            "column": column, "expectedLatest": latest, "expectedMean": mean,
        }
    return contract


if __name__ == "__main__":
    print(json.dumps(build_contract(), allow_nan=False))
