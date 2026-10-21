"""Enable Cognito's own-account verification scope only. Dry-run by default.

Never sends a code, changes account attributes, invites users, changes roles or
deploys Lambdas. Use before publishing the Team Console self-verification UI.
"""
import argparse
import json
import os
from pathlib import Path
import tempfile

ACCOUNT = "213598695875"
REGION = "us-west-2"
POOL = "us-west-2_oR7CIzxQG"
CLIENT = "5tno54tskp2tk6lhh6c8jh9ev4"
SELF_SCOPE = "aws.cognito.signin.user.admin"
READ_ONLY_FIELDS = {"CreationDate", "LastModifiedDate"}


def scope_update(current, supported_fields):
    if current.get("UserPoolId") != POOL or current.get("ClientId") != CLIENT or current.get("ClientSecret"):
        raise ValueError("Unexpected pool/client or confidential app; no change permitted")
    if current.get("AllowedOAuthFlows") != ["code"] or not current.get("AllowedOAuthFlowsUserPoolClient"):
        raise ValueError("Expected the existing authorization-code public client")
    unknown = set(current) - set(supported_fields) - READ_ONLY_FIELDS
    if unknown:
        raise ValueError("Unrecognized client settings; review before updating")
    # Passing only AllowedOAuthScopes would reset other client settings to
    # defaults. Preserve every returned, supported mutable field instead.
    result = {key: value for key, value in current.items() if key in supported_fields}
    result["AllowedOAuthScopes"] = list(dict.fromkeys([*current.get("AllowedOAuthScopes", []), SELF_SCOPE]))
    return result


def main():
    import boto3
    from botocore.exceptions import ClientError
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials-file", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    credentials, _ = json.JSONDecoder().raw_decode(Path(args.credentials_file).read_text())
    aws = boto3.Session(aws_access_key_id=credentials["aws_access_key_id"], aws_secret_access_key=credentials["aws_secret_access_key"],
        aws_session_token=credentials.get("aws_session_token"), region_name=REGION)
    try:
        if aws.client("sts").get_caller_identity()["Account"] != ACCOUNT:
            raise ValueError("Unexpected AWS account")
        cognito = aws.client("cognito-idp")
        pool = cognito.describe_user_pool(UserPoolId=POOL)["UserPool"]
        if pool.get("MfaConfiguration") != "ON" or not pool.get("AdminCreateUserConfig", {}).get("AllowAdminCreateUserOnly"):
            raise ValueError("Team pool must remain invite-only with required MFA")
        current = cognito.describe_user_pool_client(UserPoolId=POOL, ClientId=CLIENT)["UserPoolClient"]
        fields = cognito.meta.service_model.operation_model("UpdateUserPoolClient").input_shape.members
        update = scope_update(current, fields)
        print("Verified existing account, pool, client, required MFA and invite-only configuration.")
        print("Only planned client change: allow the signed-in user's own Cognito self-service scope.")
        if not args.apply:
            print("DRY RUN: no settings changed, no email sent, no account verified.")
            return
        if SELF_SCOPE in current.get("AllowedOAuthScopes", []):
            print("Self-service scope is already enabled; no change required.")
            return
        backup = Path(tempfile.mkdtemp(prefix="des-moines-team-client-rollback-")) / "previous-client.json"
        with os.fdopen(os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as stream:
            json.dump(current, stream, default=str)
        # No revision guard exists for this API: detect a changed readback
        # immediately before updating; coordinate with other auth operators.
        fresh = cognito.describe_user_pool_client(UserPoolId=POOL, ClientId=CLIENT)["UserPoolClient"]
        if fresh != current:
            raise ValueError("Client changed concurrently; update cancelled")
        cognito.update_user_pool_client(**update)
        result = cognito.describe_user_pool_client(UserPoolId=POOL, ClientId=CLIENT)["UserPoolClient"]
        expected = {key: value for key, value in current.items() if key not in READ_ONLY_FIELDS}
        actual = {key: value for key, value in result.items() if key not in READ_ONLY_FIELDS}
        expected["AllowedOAuthScopes"] = update["AllowedOAuthScopes"]
        # AWS can reorder sets such as scopes, callbacks and providers.
        def normalized(value):
            if isinstance(value, list):
                return sorted((normalized(item) for item in value), key=str)
            if isinstance(value, dict):
                return {key: normalized(item) for key, item in value.items()}
            return value
        if normalized(actual) != normalized(expected):
            raise ValueError("Client readback differs beyond the planned scope; inspect before publishing frontend")
        print(f"Client scope enabled and all other returned settings preserved. Backup: {backup}")
        print("No emails sent, user flags changed, roles granted, IAM policies changed or Lambda deployments performed.")
    except ClientError as error:
        raise SystemExit(f"AWS operation failed: {error.response['Error']['Code']}. No operator permissions were expanded.")


if __name__ == "__main__":
    main()
