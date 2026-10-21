"""Stage email-link approval without account changes or emails. Dry-run default.

Use --apply to stage the API and routes, publish the frontend rewrite, then
--apply --enable to turn on links in future approver notifications. Existing
Team Console approval and all measurement processing remain unchanged.
"""
import argparse
import base64
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import urllib.error
import urllib.request
import zipfile

from botocore.exceptions import ClientError

from deploy_access_approval import ACCOUNT, API_ID, APPROVERS, CONSOLE, FUNCTION, ISSUER, POOL, ROOT, protected_backup, session

BASE_API_SHA256 = "cb90345d1a7491200e4bddc8f9e7d6d3ab6bd6ef73bf9de396ffe48bc7c15629"
PATH = "/air-quality/v1/access-requests/email-decision"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials-file", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--enable", action="store_true", help="Enable links only after the production frontend rewrite is live")
    args = parser.parse_args()
    aws = session(args.credentials_file)
    if aws.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        raise RuntimeError("Unexpected AWS account")
    lam, gateway = aws.client("lambda"), aws.client("apigatewayv2")
    current = lam.get_function(FunctionName=FUNCTION)
    cfg = current["Configuration"]
    env = cfg.get("Environment", {}).get("Variables", {})
    if (set(env.get("ACCESS_REQUEST_APPROVERS", "").split(",")) != set(APPROVERS)
            or env.get("TEAM_CONSOLE_URL") != CONSOLE or env.get("TEAM_AUTH_ISSUER") != ISSUER
            or env.get("ENABLE_API_KEY_REGISTRATION") != "1" or not env.get("API_KEY_HASH_PEPPER")
            or not env.get("ACCESS_REQUEST_FROM_EMAIL")):
        raise RuntimeError("Unexpected approval configuration; refusing to change recipients, identities or secrets")
    pool = aws.client("cognito-idp").describe_user_pool(UserPoolId=POOL)["UserPool"]
    if pool.get("MfaConfiguration") != "ON" or not pool.get("AdminCreateUserConfig", {}).get("AllowAdminCreateUserOnly"):
        raise RuntimeError("Existing Team Console security settings changed")
    with urllib.request.urlopen(current["Code"]["Location"], timeout=20) as result:
        previous_zip = result.read()
    old_archive = zipfile.ZipFile(io.BytesIO(previous_zip))
    new_source = (ROOT / "lambda_api.py").read_bytes()
    source_hash = hashlib.sha256(new_source).hexdigest()
    live_hash = hashlib.sha256(old_archive.read("lambda_api.py")).hexdigest()
    if live_hash not in {BASE_API_SHA256, source_hash}:
        raise RuntimeError("Deployed source differs from the reviewed baseline/release; review before replacement")
    routes = {route["RouteKey"]: route for route in gateway.get_routes(ApiId=API_ID)["Items"]}
    reference = routes["GET /air-quality/internal/v1/access-requests"]
    if reference.get("AuthorizationType") != "JWT":
        raise RuntimeError("Existing private request queue is not JWT-protected")
    integration = gateway.get_integration(ApiId=API_ID, IntegrationId=reference["Target"].split("/")[-1])
    if integration.get("IntegrationUri") != cfg["FunctionArn"]:
        raise RuntimeError("Unexpected integration target")
    for method in ("GET", "POST"):
        existing = routes.get(f"{method} {PATH}")
        if existing and (existing.get("Target") != reference["Target"] or existing.get("AuthorizationType", "NONE") != "NONE"):
            raise RuntimeError("Unexpected existing email-decision route")
    if args.enable:
        # This harmless GET verifies the public rewrite without a token,
        # account session, requester, email, audit event or approval mutation.
        try:
            with urllib.request.urlopen(CONSOLE.removesuffix("/team") + PATH, timeout=20) as result:
                page = result.read().decode()
                status = result.status
        except urllib.error.HTTPError as result:
            page, status = result.read().decode(), result.code
        if status not in {400, 503} or "API access decision" not in page or 'form method="post"' in page:
            raise RuntimeError("Production email-decision rewrite is not ready; stage API and publish frontend first")
    print("Verified account, reviewed API baseline, three approvers (Jeff excluded), existing MFA and private queue.")
    print("Plan: two token-guarded public routes, API code only, preserve all environment values" + (", enable email links." if args.enable else "; do not enable links yet."))
    if not args.apply:
        print("DRY RUN: no changes, emails or decisions.")
        return
    subprocess.run(["git", "diff", "--exit-code", "HEAD", "--", "lambda_api.py", "scripts/aws/deploy_email_approvals.py"], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    backup = Path(tempfile.mkdtemp(prefix="des-moines-email-approval-rollback-"))
    protected_backup(backup, "previous-api.zip", previous_zip)
    protected_backup(backup, "previous-configuration.json", json.dumps(cfg).encode())
    print(f"Operator-only rollback backup: {backup}")
    if live_hash != source_hash:
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as archive:
            for item in old_archive.infolist():
                archive.writestr(item, new_source if item.filename == "lambda_api.py" else old_archive.read(item.filename))
        lam.update_function_code(FunctionName=FUNCTION, ZipFile=package.getvalue(), RevisionId=cfg["RevisionId"])
        lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION, WaiterConfig={"Delay": 2, "MaxAttempts": 30})
        expected_code_sha = base64.b64encode(hashlib.sha256(package.getvalue()).digest()).decode()
    else:
        expected_code_sha = cfg["CodeSha256"]
    fresh = lam.get_function_configuration(FunctionName=FUNCTION)
    if fresh["CodeSha256"] != expected_code_sha or fresh.get("Environment", {}).get("Variables", {}) != env:
        raise RuntimeError("API code/environment changed concurrently; no environment update attempted")
    desired = env | {"ENABLE_EMAIL_LINK_APPROVAL": "1" if args.enable else env.get("ENABLE_EMAIL_LINK_APPROVAL", "0")}
    if desired != env:
        lam.update_function_configuration(FunctionName=FUNCTION, Environment={"Variables": desired}, RevisionId=fresh["RevisionId"])
        lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION, WaiterConfig={"Delay": 2, "MaxAttempts": 30})
    for method in ("GET", "POST"):
        key = f"{method} {PATH}"
        if key not in routes:
            gateway.create_route(ApiId=API_ID, RouteKey=key, Target=reference["Target"], AuthorizationType="NONE")
            print(f"Created {key}: bearer link guard in API, no team session required")
    done = lam.get_function_configuration(FunctionName=FUNCTION)
    final_routes = {route["RouteKey"]: route for route in gateway.get_routes(ApiId=API_ID)["Items"]}
    if done["CodeSha256"] != expected_code_sha or done.get("Environment", {}).get("Variables", {}) != desired:
        raise RuntimeError("API readback mismatch")
    for key in (f"GET {PATH}", f"POST {PATH}"):
        if final_routes[key].get("Target") != reference["Target"] or final_routes[key].get("AuthorizationType", "NONE") != "NONE":
            raise RuntimeError("Email-decision route readback mismatch")
    for key in ("GET /air-quality/internal/v1/access-requests", "POST /air-quality/internal/v1/access-requests/decision"):
        if final_routes[key] != routes[key] or final_routes[key].get("AuthorizationType") != "JWT":
            raise RuntimeError("Private approval route changed")
    print("Readback passed. No email, key, account, role, IAM, instrument or Silver changes performed.")


if __name__ == "__main__":
    try:
        main()
    except ClientError as error:
        raise SystemExit(f"AWS operation failed: {error.response.get('Error', {}).get('Code', 'unknown')}. No permissions were escalated.")
