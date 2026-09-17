#!/usr/bin/env python3
"""Targeted deployment for the authenticated Team Console backend only."""

import io
import json
import os
import time
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


ROOT = Path(__file__).resolve().parent.parent
CREDS_FILE = ROOT / "aws_creds.json"
REGION = os.environ.get("DEPLOY_AWS_REGION", "us-west-2")
API_ID = "yvhb48sthk"
FUNCTION_NAME = "aq-dashboard-api"
AUDIT_TABLE = "AQAdminAudit"
ISSUER = os.environ.get("TEAM_AUTH_ISSUER")
AUDIENCE = os.environ.get("TEAM_AUTH_AUDIENCE")
ROUTES = [
    "GET /air-quality/internal/v1/api-users",
    "POST /air-quality/internal/v1/api-users/revoke",
    "GET /air-quality/internal/v1/observations",
    "POST /air-quality/internal/v1/flags",
    "POST /air-quality/internal/v1/corrections",
    "GET /air-quality/internal/v1/audit",
]


def session():
    if CREDS_FILE.exists():
        creds, _ = json.JSONDecoder().raw_decode(CREDS_FILE.read_text())
        return boto3.Session(
            aws_access_key_id=creds["aws_access_key_id"],
            aws_secret_access_key=creds["aws_secret_access_key"],
            region_name=creds.get("region") or REGION,
        )
    return boto3.Session(region_name=REGION)


def ensure_audit_table(ddb):
    try:
        ddb.describe_table(TableName=AUDIT_TABLE)
        print(f"Using existing {AUDIT_TABLE} table.")
        return
    except ddb.exceptions.ResourceNotFoundException:
        pass
    ddb.create_table(
        TableName=AUDIT_TABLE,
        AttributeDefinitions=[
            {"AttributeName": "audit_id", "AttributeType": "S"},
            {"AttributeName": "audit_scope", "AttributeType": "S"},
            {"AttributeName": "created_at_epoch", "AttributeType": "N"},
        ],
        KeySchema=[{"AttributeName": "audit_id", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[{
            "IndexName": "scope-created-at-index",
            "KeySchema": [
                {"AttributeName": "audit_scope", "KeyType": "HASH"},
                {"AttributeName": "created_at_epoch", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
        SSESpecification={"Enabled": True},
    )
    ddb.get_waiter("table_exists").wait(TableName=AUDIT_TABLE)
    print(f"Created {AUDIT_TABLE} table.")


def lambda_zip():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(ROOT / "lambda_api.py", "lambda_api.py")
    return output.getvalue()


def wait_for_lambda(client):
    client.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION_NAME)


def deploy_lambda(client):
    current = client.get_function_configuration(FunctionName=FUNCTION_NAME)
    environment = current.get("Environment", {}).get("Variables", {})
    environment["ADMIN_AUDIT_TABLE"] = AUDIT_TABLE
    client.update_function_code(FunctionName=FUNCTION_NAME, ZipFile=lambda_zip(), Publish=False)
    wait_for_lambda(client)
    client.update_function_configuration(
        FunctionName=FUNCTION_NAME,
        Environment={"Variables": environment},
    )
    wait_for_lambda(client)
    print("Updated the API Lambda without changing existing secrets or data settings.")
    return current["FunctionArn"]


def ensure_authorizer(api, lambda_arn):
    integrations = api.get_integrations(ApiId=API_ID).get("Items", [])
    integration = next((item for item in integrations if item.get("IntegrationUri") == lambda_arn), None)
    if not integration:
        raise RuntimeError("The existing API Lambda integration was not found.")

    authorizers = api.get_authorizers(ApiId=API_ID).get("Items", [])
    authorizer = next((item for item in authorizers if item.get("Name") == "UWTeamJwt"), None)
    config = {
        "ApiId": API_ID,
        "AuthorizerType": "JWT",
        "Name": "UWTeamJwt",
        "IdentitySource": ["$request.header.Authorization"],
        "JwtConfiguration": {"Audience": [AUDIENCE], "Issuer": ISSUER},
    }
    if authorizer:
        api.update_authorizer(AuthorizerId=authorizer["AuthorizerId"], **config)
        authorizer_id = authorizer["AuthorizerId"]
        print("Updated the Team Console JWT authorizer.")
    else:
        authorizer_id = api.create_authorizer(**config)["AuthorizerId"]
        print("Created the Team Console JWT authorizer.")

    current_routes = {item["RouteKey"]: item for item in api.get_routes(ApiId=API_ID).get("Items", [])}
    for route_key in ROUTES:
        settings = {
            "ApiId": API_ID,
            "RouteKey": route_key,
            "Target": f"integrations/{integration['IntegrationId']}",
            "AuthorizationType": "JWT",
            "AuthorizerId": authorizer_id,
        }
        if route_key in current_routes:
            api.update_route(RouteId=current_routes[route_key]["RouteId"], **settings)
        else:
            api.create_route(**settings)
        print(f"Protected {route_key}.")

    api.update_api(
        ApiId=API_ID,
        CorsConfiguration={
            "AllowOrigins": ["https://project-kv69p.vercel.app", "http://127.0.0.1:5173", "http://localhost:5173"],
            "AllowMethods": ["GET", "POST", "OPTIONS"],
            "AllowHeaders": ["content-type", "authorization", "x-api-key"],
            "MaxAge": 300,
        },
    )
    print("Updated API CORS for the dashboard, API clients, and Team Console.")


def main():
    if not ISSUER or not AUDIENCE:
        raise SystemExit("Set TEAM_AUTH_ISSUER and TEAM_AUTH_AUDIENCE from provision_team_auth.py.")
    aws = session()
    account_id = aws.client("sts").get_caller_identity()["Account"]
    if account_id != "213598695875":
        raise RuntimeError(f"Refusing to deploy Team Console backend to unexpected AWS account {account_id}.")
    ensure_audit_table(aws.client("dynamodb"))
    lambda_arn = deploy_lambda(aws.client("lambda"))
    ensure_authorizer(aws.client("apigatewayv2"), lambda_arn)
    time.sleep(3)
    print("Team Console backend deployment completed.")


if __name__ == "__main__":
    try:
        main()
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "AWS error")
        message = exc.response.get("Error", {}).get("Message", str(exc))
        raise SystemExit(f"{code}: {message}") from exc
