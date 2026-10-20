"""Release only the API-request approval workflow. Dry-run unless --apply.

Preserves existing Lambda settings/secrets and never deploys Silver builders,
measurement processing, aircraft routes or public data changes. The supplied
operator credentials must already allow the required Cognito/API operations.
This script never grants permissions to its own AWS identity.
"""
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import urllib.request
import zipfile

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[2]
ACCOUNT = "213598695875"
REGION = "us-west-2"
FUNCTION = "aq-dashboard-api"
API_ID = "yvhb48sthk"
POOL = "us-west-2_oR7CIzxQG"
CLIENT = "5tno54tskp2tk6lhh6c8jh9ev4"
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL}"
APPROVERS = ("elaustin@uw.edu", "jshirai@uw.edu", "yscheong@uw.edu", "pavands@uw.edu")
CONSOLE = "https://deohs-des-moines-air.vercel.app/team"
BASE_API_SHA256 = "6d226903ffee517cae64ac224b227ac6f6bb42db595394ce9db74f6505650ad4"


def session(credentials_file):
    credentials, _ = json.JSONDecoder().raw_decode(Path(credentials_file).read_text())
    return boto3.Session(aws_access_key_id=credentials["aws_access_key_id"], aws_secret_access_key=credentials["aws_secret_access_key"],
        aws_session_token=credentials.get("aws_session_token"), region_name=REGION)


def protected_backup(directory, name, content):
    path = directory / name
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output:
        output.write(content)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials-file", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    aws = session(args.credentials_file)
    if aws.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        raise RuntimeError("Refusing deployment in an unexpected AWS account")
    lam, gateway, cognito = (aws.client(name) for name in ("lambda", "apigatewayv2", "cognito-idp"))
    current = lam.get_function(FunctionName=FUNCTION)
    cfg = current["Configuration"]
    variables = cfg.get("Environment", {}).get("Variables", {})
    if not variables.get("API_KEY_HASH_PEPPER") or not variables.get("ACCESS_REQUEST_FROM_EMAIL"):
        raise RuntimeError("Existing key hashing and verified email sender must already be configured")
    with urllib.request.urlopen(current["Code"]["Location"], timeout=20) as response:
        previous_zip = response.read()
    live_source = zipfile.ZipFile(io.BytesIO(previous_zip)).read("lambda_api.py")
    new_source = (ROOT / "lambda_api.py").read_bytes()
    live_hash = hashlib.sha256(live_source).hexdigest()
    if live_hash not in {BASE_API_SHA256, hashlib.sha256(new_source).hexdigest()}:
        raise RuntimeError("The deployed API differs from the reviewed baseline and this release. Review it before replacing code")
    pool = cognito.describe_user_pool(UserPoolId=POOL)["UserPool"]
    if pool.get("MfaConfiguration") != "ON" or not pool.get("AdminCreateUserConfig", {}).get("AllowAdminCreateUserOnly"):
        raise RuntimeError("Team accounts must remain invite-only with required MFA")
    app = cognito.describe_user_pool_client(UserPoolId=POOL, ClientId=CLIENT)["UserPoolClient"]
    if app.get("ClientSecret") or CONSOLE not in app.get("CallbackURLs", []) or "code" not in app.get("AllowedOAuthFlows", []):
        raise RuntimeError("Unexpected public OAuth client configuration")
    routes = {route["RouteKey"]: route for route in gateway.get_routes(ApiId=API_ID)["Items"]}
    reference = routes["GET /air-quality/internal/v1/api-users"]
    if reference.get("AuthorizationType") != "JWT":
        raise RuntimeError("Existing private API route is not JWT-protected")
    integration = gateway.get_integration(ApiId=API_ID, IntegrationId=reference["Target"].split("/")[-1])
    if integration.get("IntegrationUri") != cfg["FunctionArn"]:
        raise RuntimeError("The route does not target the API function being deployed")
    authorizer = gateway.get_authorizer(ApiId=API_ID, AuthorizerId=reference["AuthorizerId"])
    if authorizer["JwtConfiguration"].get("Issuer") != ISSUER or CLIENT not in authorizer["JwtConfiguration"].get("Audience", []):
        raise RuntimeError("The existing JWT authorizer does not match the team pool/client")
    api = gateway.get_api(ApiId=API_ID)
    cors = api.get("CorsConfiguration", {})
    if "POST" not in cors.get("AllowMethods", []) or "authorization" not in [h.lower() for h in cors.get("AllowHeaders", [])]:
        raise RuntimeError("Existing CORS must permit POST and the Authorization header; no implicit widening was attempted")
    for table in ("AQApiAccessRequests", "AQApiKeys", "AQAdminAudit"):
        aws.client("dynamodb").describe_table(TableName=table)
    print("Verified account, existing API baseline, sender/hash configuration, MFA, invite-only pool and JWT authorizer.")
    print("Approvers: " + ", ".join(APPROVERS))
    print("Plan: add ApiApprover role only, invite missing team accounts, add three guarded routes, update API code and preserve/merge its environment.")
    if not args.apply:
        print("DRY RUN: no resources changed and no emails sent.")
        return
    subprocess.run(["git", "diff", "--exit-code", "HEAD", "--", "lambda_api.py", "scripts/aws/deploy_access_approval.py"], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    backup = Path(tempfile.mkdtemp(prefix="des-moines-approval-rollback-"))
    protected_backup(backup, "previous-api.zip", previous_zip)
    protected_backup(backup, "previous-configuration.json", json.dumps(cfg).encode())
    print(f"Protected operator-only rollback backup: {backup}")
    try:
        cognito.create_group(UserPoolId=POOL, GroupName="ApiApprover", Description="Approve or reject verified researcher API requests only", Precedence=25)
    except cognito.exceptions.GroupExistsException:
        pass
    for email in APPROVERS:
        try:
            user = cognito.admin_get_user(UserPoolId=POOL, Username=email)
            if not user.get("Enabled"):
                raise RuntimeError(f"The team account for {email} is disabled; no automatic reactivation is permitted")
        except cognito.exceptions.UserNotFoundException:
            cognito.admin_create_user(UserPoolId=POOL, Username=email,
                UserAttributes=[{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}], DesiredDeliveryMediums=["EMAIL"])
            print(f"Sent a team-account invitation to {email}; password change and MFA setup remain the recipient's responsibility.")
        cognito.admin_add_user_to_group(UserPoolId=POOL, Username=email, GroupName="ApiApprover")
        print(f"Assigned ApiApprover to {email}; existing roles were preserved.")
    planned_routes = {
        "GET /air-quality/internal/v1/access-requests": True,
        "POST /air-quality/internal/v1/access-requests/decision": True,
        "POST /air-quality/v1/access-requests/verify": False,
    }
    for key, private in planned_routes.items():
        existing = routes.get(key)
        expected_auth = "JWT" if private else "NONE"
        if existing:
            if existing.get("Target") != reference["Target"] or existing.get("AuthorizationType", "NONE") != expected_auth or private and existing.get("AuthorizerId") != reference["AuthorizerId"]:
                raise RuntimeError("An existing approval route has unexpected authorization; refusing to override it")
            continue
        kwargs = {"ApiId": API_ID, "RouteKey": key, "Target": reference["Target"], "AuthorizationType": expected_auth}
        if private:
            kwargs["AuthorizerId"] = reference["AuthorizerId"]
        gateway.create_route(**kwargs)
        print(f"Created route: {key} ({expected_auth})")
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("lambda_api.py", new_source)
    expected_sha = base64.b64encode(hashlib.sha256(package.getvalue()).digest()).decode()
    lam.update_function_code(FunctionName=FUNCTION, ZipFile=package.getvalue(), RevisionId=cfg["RevisionId"])
    lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION, WaiterConfig={"Delay": 2, "MaxAttempts": 30})
    fresh = lam.get_function_configuration(FunctionName=FUNCTION)
    if fresh["CodeSha256"] != expected_sha:
        raise RuntimeError("API code changed concurrently; environment was not modified")
    environment = {**fresh.get("Environment", {}).get("Variables", {}),
        "ACCESS_REQUEST_APPROVERS": ",".join(APPROVERS), "TEAM_CONSOLE_URL": CONSOLE,
        "TEAM_AUTH_ISSUER": ISSUER, "TEAM_AUTH_AUDIENCE": CLIENT, "ENABLE_API_KEY_REGISTRATION": "1"}
    lam.update_function_configuration(FunctionName=FUNCTION, Environment={"Variables": environment}, RevisionId=fresh["RevisionId"])
    lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION, WaiterConfig={"Delay": 2, "MaxAttempts": 30})
    done = lam.get_function_configuration(FunctionName=FUNCTION)
    if done["CodeSha256"] != expected_sha or done.get("Environment", {}).get("Variables", {}) != environment:
        raise RuntimeError("API code/environment readback did not match the release")
    final_routes = {route["RouteKey"]: route for route in gateway.get_routes(ApiId=API_ID)["Items"]}
    for key, private in planned_routes.items():
        if key not in final_routes or final_routes[key].get("Target") != reference["Target"] or final_routes[key].get("AuthorizationType", "NONE") != ("JWT" if private else "NONE") or private and final_routes[key].get("AuthorizerId") != reference["AuthorizerId"]:
            raise RuntimeError("Route readback failed; investigate before publishing frontend")
    print("API approval workflow deployed and read back successfully. No API keys were issued and no instrument/Silver resources were changed.")


if __name__ == "__main__":
    try:
        main()
    except ClientError as error:
        # Do not dump request payloads or configuration containing secrets.
        raise SystemExit(f"AWS operation failed: {error.response.get('Error', {}).get('Code', 'unknown')}. No permissions were escalated.")
