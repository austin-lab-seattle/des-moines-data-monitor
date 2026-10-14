"""Deploy per-instrument upload/reading alerts, preserving deployed settings."""

import json
import os
import tempfile
import time
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


ROOT = Path(__file__).resolve().parents[2]
CREDS_FILE = ROOT / "aws_creds.json"
REGION = "us-west-2"
ACCOUNT_ID = "213598695875"
BUCKET = "des-moines-data-pipeline-austinlab"
FUNCTION_NAME = "aq-upload-freshness-monitor"
ROLE_NAME = "AQUploadFreshnessMonitorRole"
RULE_NAME = "aq-upload-freshness-hourly"
SOURCE_EMAIL = os.environ.get("UPLOAD_ALERT_FROM_EMAIL")
TO_EMAILS = os.environ.get("UPLOAD_ALERT_TO_EMAILS")
CC_EMAILS = os.environ.get("UPLOAD_ALERT_CC_EMAILS")


def session():
    if os.environ.get("USE_AWS_CREDS_FILE") == "1" and CREDS_FILE.exists():
        creds, _ = json.JSONDecoder().raw_decode(CREDS_FILE.read_text())
        return boto3.Session(
            aws_access_key_id=creds["aws_access_key_id"],
            aws_secret_access_key=creds["aws_secret_access_key"],
            region_name=REGION,
        )
    candidate = boto3.Session(region_name=REGION)
    if candidate.get_credentials() is not None:
        return candidate
    raise RuntimeError("No AWS credentials found")


aws = session()
if aws.client("sts").get_caller_identity()["Account"] != ACCOUNT_ID:
    raise RuntimeError("Refusing to deploy into an unexpected AWS account")

iam = aws.client("iam")
lambda_client = aws.client("lambda")
events = aws.client("events")

try:
    existing_function = lambda_client.get_function(FunctionName=FUNCTION_NAME)["Configuration"]
except lambda_client.exceptions.ResourceNotFoundException:
    existing_function = {}
existing_environment = existing_function.get("Environment", {}).get("Variables", {})
SOURCE_EMAIL = SOURCE_EMAIL or existing_environment.get("ALERT_FROM_EMAIL")
TO_EMAILS = TO_EMAILS or existing_environment.get("ALERT_TO_EMAILS")
CC_EMAILS = CC_EMAILS if CC_EMAILS is not None else existing_environment.get("ALERT_CC_EMAILS", "")
if not SOURCE_EMAIL or not TO_EMAILS:
    raise RuntimeError("UPLOAD_ALERT_FROM_EMAIL and UPLOAD_ALERT_TO_EMAILS are required for initial setup")

assume_policy = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "lambda.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }],
}
try:
    role = iam.get_role(RoleName=ROLE_NAME)["Role"]
except iam.exceptions.NoSuchEntityException:
    role = iam.create_role(
        RoleName=ROLE_NAME,
        Description="Least-privilege role for the Des Moines upload freshness monitor.",
        AssumeRolePolicyDocument=json.dumps(assume_policy),
    )["Role"]
    time.sleep(10)

role_policy = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "ListBronzeUploads",
            "Effect": "Allow",
            "Action": "s3:ListBucket",
            "Resource": f"arn:aws:s3:::{BUCKET}",
            "Condition": {"StringLike": {"s3:prefix": [
                "BC-MA200/bronze/*",
                "CO2-LICOR/bronze/*",
                "NEPH-PM25/bronze/*",
                "NO2-CAPS/bronze/*",
                "SMPS/bronze/*",
                "_monitor/upload_freshness_state.json",
            ]}},
        },
        {
            "Sid": "ReadInstrumentObservationTimes",
            "Effect": "Allow",
            "Action": "s3:GetObject",
            "Resource": [f"arn:aws:s3:::{BUCKET}/{instrument}/silver/{instrument}_data.csv"
                         for instrument in ["BC-MA200", "CO2-LICOR", "NEPH-PM25", "NO2-CAPS", "SMPS"]],
        },
        {
            "Sid": "ReadWriteMonitorState",
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:PutObject"],
            "Resource": f"arn:aws:s3:::{BUCKET}/_monitor/upload_freshness_state.json",
        },
        {
            "Sid": "SendAlertFromLabAddress",
            "Effect": "Allow",
            "Action": "ses:SendEmail",
            "Resource": f"arn:aws:ses:{REGION}:{ACCOUNT_ID}:identity/{SOURCE_EMAIL}",
            "Condition": {"StringEquals": {"ses:FromAddress": SOURCE_EMAIL}},
        },
        {
            "Sid": "WriteOwnLogs",
            "Effect": "Allow",
            "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
            "Resource": f"arn:aws:logs:{REGION}:{ACCOUNT_ID}:log-group:/aws/lambda/{FUNCTION_NAME}:*",
        },
    ],
}
iam.put_role_policy(
    RoleName=ROLE_NAME,
    PolicyName="UploadFreshnessMonitorRuntime",
    PolicyDocument=json.dumps(role_policy),
)

with tempfile.NamedTemporaryFile(suffix=".zip") as archive:
    with zipfile.ZipFile(archive.name, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.write(ROOT / "lambda" / "upload_freshness_monitor.py", "upload_freshness_monitor.py")
        # Reuse the exact timestamp interpretation used by the dashboard.
        bundle.write(ROOT / "lambda_api.py", "lambda_api.py")
    zip_bytes = Path(archive.name).read_bytes()

environment = {
    **existing_environment,
    "S3_BUCKET": BUCKET,
    "STALE_AFTER_HOURS": existing_environment.get("STALE_AFTER_HOURS", "6"),
    "ALERT_FROM_EMAIL": SOURCE_EMAIL,
    "ALERT_TO_EMAILS": TO_EMAILS,
    "ALERT_CC_EMAILS": CC_EMAILS,
}
try:
    function = lambda_client.get_function(FunctionName=FUNCTION_NAME)["Configuration"]
    lambda_client.get_waiter("function_active_v2").wait(FunctionName=FUNCTION_NAME)
    lambda_client.update_function_code(FunctionName=FUNCTION_NAME, ZipFile=zip_bytes,
                                       RevisionId=function["RevisionId"])
    lambda_client.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION_NAME)
    current = lambda_client.get_function(FunctionName=FUNCTION_NAME)["Configuration"]
    if current.get("Environment", {}).get("Variables", {}) != existing_environment:
        raise RuntimeError("Monitor settings changed during deployment; refusing to overwrite them")
    lambda_client.update_function_configuration(
        FunctionName=FUNCTION_NAME,
        RevisionId=current["RevisionId"],
        Role=role["Arn"],
        Description="Independent per-instrument stale upload and observation alerts.",
        Handler="upload_freshness_monitor.lambda_handler",
        Runtime="python3.12",
        Timeout=180,
        MemorySize=256,
        Environment={"Variables": environment},
    )
    lambda_client.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION_NAME)
    function_arn = function["FunctionArn"]
except lambda_client.exceptions.ResourceNotFoundException:
    function = lambda_client.create_function(
        FunctionName=FUNCTION_NAME,
        Description="Independent per-instrument stale upload and observation alerts.",
        Runtime="python3.12",
        Role=role["Arn"],
        Handler="upload_freshness_monitor.lambda_handler",
        Code={"ZipFile": zip_bytes},
        Timeout=180,
        MemorySize=256,
        Environment={"Variables": environment},
    )
    function_arn = function["FunctionArn"]
    lambda_client.get_waiter("function_active_v2").wait(FunctionName=FUNCTION_NAME)

rule = events.put_rule(
    Name=RULE_NAME,
    ScheduleExpression="rate(1 hour)",
    State="ENABLED",
    Description="Checks each Des Moines instrument's upload and reading freshness hourly.",
)
targets = events.put_targets(
    Rule=RULE_NAME,
    Targets=[{"Id": FUNCTION_NAME, "Arn": function_arn}],
)
if targets.get("FailedEntryCount", 0):
    raise RuntimeError(f"Could not attach the monitor to its schedule: {targets['FailedEntries']}")
try:
    lambda_client.add_permission(
        FunctionName=FUNCTION_NAME,
        StatementId=f"eventbridge-{RULE_NAME}",
        Action="lambda:InvokeFunction",
        Principal="events.amazonaws.com",
        SourceArn=rule["RuleArn"],
    )
except lambda_client.exceptions.ResourceConflictException:
    pass

response = lambda_client.invoke(
    FunctionName=FUNCTION_NAME,
    InvocationType="RequestResponse",
    Payload=b'{"dry_run":true}',
)
payload = json.loads(response["Payload"].read().decode())
if response.get("FunctionError"):
    raise RuntimeError(payload)
body = json.loads(payload.get("body", "{}"))
if body.get("status") not in {"waiting-for-first-upload", "healthy", "stale", "unknown"}:
    raise RuntimeError(f"Unexpected initial monitor state: {body}")

print(json.dumps({
    "function": FUNCTION_NAME,
    "rule": RULE_NAME,
    "schedule": "rate(1 hour)",
    "threshold_hours": environment["STALE_AFTER_HOURS"],
    "initial_state": body,
    "to": TO_EMAILS,
    "cc": CC_EMAILS,
}, indent=2))
