#!/usr/bin/env python3
"""Provision the invite-only Cognito foundation for the private Team Console.

This creates no public data routes. The pool starts with Cognito-hosted team
accounts and can later add UW's SAML identity provider without changing the
application's authorization model.
"""

import json
import os
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


ROOT = Path(__file__).resolve().parent.parent
CREDS_FILE = ROOT / "aws_creds.json"
HOSTED_UI_CSS_FILE = ROOT / "infra" / "cognito" / "team-login.css"
HOSTED_UI_LOGO_FILE = ROOT / "infra" / "cognito" / "team-login-logo.png"
REGION = os.environ.get("DEPLOY_AWS_REGION", "us-west-2")
POOL_NAME = "DesMoinesAirTeam"
DOMAIN_PREFIX = "des-moines-air-team-213598695875"
CLIENT_NAME = "DesMoinesAirTeamConsole"
INITIAL_ADMIN = os.environ.get("TEAM_INITIAL_ADMIN", "pavands@uw.edu").strip().lower()
CALLBACK_URLS = [
    "https://deohs-des-moines-air.vercel.app/team",
    "https://project-kv69p.vercel.app/team",
    "http://127.0.0.1:5173/team",
    "http://localhost:5173/team",
]
GROUPS = [
    ("Admin", "Full Team Console administration", 0),
    ("Reviewer", "Review and flag environmental observations", 10),
    ("AccessManager", "View and revoke researcher API keys", 20),
    ("Viewer", "Read-only access to reviewed observations", 30),
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


def existing_pool(client):
    token = None
    while True:
        kwargs = {"MaxResults": 60}
        if token:
            kwargs["NextToken"] = token
        result = client.list_user_pools(**kwargs)
        pool = next((item for item in result.get("UserPools", []) if item["Name"] == POOL_NAME), None)
        if pool:
            return pool["Id"]
        token = result.get("NextToken")
        if not token:
            return None


def ensure_pool(client):
    pool_id = existing_pool(client)
    if pool_id:
        print(f"Using existing Cognito user pool {POOL_NAME} ({pool_id}).")
        return pool_id
    result = client.create_user_pool(
        PoolName=POOL_NAME,
        UsernameAttributes=["email"],
        AutoVerifiedAttributes=["email"],
        MfaConfiguration="OFF",
        AdminCreateUserConfig={
            "AllowAdminCreateUserOnly": True,
            "InviteMessageTemplate": {
                "EmailSubject": "Your Des Moines Air Team Console account",
                "EmailMessage": (
                    "Your private Team Console username is {username} and temporary "
                    "password is {####}. Sign in and set a permanent password."
                ),
            },
        },
        Policies={
            "PasswordPolicy": {
                "MinimumLength": 14,
                "RequireUppercase": True,
                "RequireLowercase": True,
                "RequireNumbers": True,
                "RequireSymbols": True,
                "TemporaryPasswordValidityDays": 7,
            }
        },
        DeletionProtection="ACTIVE",
    )
    pool_id = result["UserPool"]["Id"]
    print(f"Created Cognito user pool {POOL_NAME} ({pool_id}).")
    return pool_id


def ensure_mfa(client, pool_id):
    client.set_user_pool_mfa_config(
        UserPoolId=pool_id,
        MfaConfiguration="ON",
        SoftwareTokenMfaConfiguration={"Enabled": True},
    )
    print("Required authenticator-app MFA for team accounts.")


def ensure_domain(client, pool_id):
    try:
        details = client.describe_user_pool_domain(Domain=DOMAIN_PREFIX).get("DomainDescription", {})
        if details.get("UserPoolId") == pool_id:
            print(f"Using existing hosted sign-in domain {DOMAIN_PREFIX}.")
            return
        if details.get("UserPoolId"):
            raise RuntimeError("The planned Cognito domain prefix belongs to another user pool.")
    except client.exceptions.ResourceNotFoundException:
        pass
    client.create_user_pool_domain(Domain=DOMAIN_PREFIX, UserPoolId=pool_id)
    print(f"Created hosted sign-in domain {DOMAIN_PREFIX}.")


def ensure_groups(client, pool_id):
    current = {item["GroupName"] for item in client.list_groups(UserPoolId=pool_id).get("Groups", [])}
    for name, description, precedence in GROUPS:
        if name in current:
            continue
        client.create_group(
            GroupName=name,
            UserPoolId=pool_id,
            Description=description,
            Precedence=precedence,
        )
        print(f"Created team role {name}.")


def ensure_client(client, pool_id):
    clients = client.list_user_pool_clients(UserPoolId=pool_id, MaxResults=60).get("UserPoolClients", [])
    found = next((item for item in clients if item["ClientName"] == CLIENT_NAME), None)
    settings = {
        "UserPoolId": pool_id,
        "ClientName": CLIENT_NAME,
        "RefreshTokenValidity": 1,
        "AccessTokenValidity": 60,
        "IdTokenValidity": 60,
        "TokenValidityUnits": {
            "AccessToken": "minutes",
            "IdToken": "minutes",
            "RefreshToken": "days",
        },
        "ExplicitAuthFlows": ["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
        "SupportedIdentityProviders": ["COGNITO"],
        "CallbackURLs": CALLBACK_URLS,
        "LogoutURLs": CALLBACK_URLS,
        "AllowedOAuthFlows": ["code"],
        "AllowedOAuthScopes": ["openid", "email", "profile"],
        "AllowedOAuthFlowsUserPoolClient": True,
        "PreventUserExistenceErrors": "ENABLED",
        "EnableTokenRevocation": True,
    }
    if found:
        client.update_user_pool_client(ClientId=found["ClientId"], **settings)
        print(f"Updated app client {CLIENT_NAME}.")
        return found["ClientId"]
    result = client.create_user_pool_client(GenerateSecret=False, **settings)
    client_id = result["UserPoolClient"]["ClientId"]
    print(f"Created app client {CLIENT_NAME}.")
    return client_id


def ensure_hosted_ui_branding(client, pool_id, client_id):
    client.set_ui_customization(
        UserPoolId=pool_id,
        ClientId=client_id,
        CSS=HOSTED_UI_CSS_FILE.read_text(),
        ImageFile=HOSTED_UI_LOGO_FILE.read_bytes(),
    )
    print("Applied Des Moines Air branding to the hosted team sign-in page.")


def ensure_admin(client, pool_id):
    try:
        client.admin_get_user(UserPoolId=pool_id, Username=INITIAL_ADMIN)
        print(f"Using existing initial administrator {INITIAL_ADMIN}.")
    except client.exceptions.UserNotFoundException:
        client.admin_create_user(
            UserPoolId=pool_id,
            Username=INITIAL_ADMIN,
            UserAttributes=[
                {"Name": "email", "Value": INITIAL_ADMIN},
                {"Name": "email_verified", "Value": "true"},
            ],
            DesiredDeliveryMediums=["EMAIL"],
        )
        print(f"Invited initial administrator {INITIAL_ADMIN}.")
    client.admin_add_user_to_group(UserPoolId=pool_id, Username=INITIAL_ADMIN, GroupName="Admin")
    print("Initial administrator belongs to Admin.")


def main():
    aws = session()
    account_id = aws.client("sts").get_caller_identity()["Account"]
    if account_id != "213598695875":
        raise RuntimeError(f"Refusing to provision Team Console auth in unexpected AWS account {account_id}.")
    client = aws.client("cognito-idp")
    pool_id = ensure_pool(client)
    ensure_mfa(client, pool_id)
    ensure_domain(client, pool_id)
    ensure_groups(client, pool_id)
    client_id = ensure_client(client, pool_id)
    ensure_hosted_ui_branding(client, pool_id, client_id)
    ensure_admin(client, pool_id)
    issuer = f"https://cognito-idp.{REGION}.amazonaws.com/{pool_id}"
    authority = issuer
    print("\nTeam authentication foundation is ready.")
    print(f"TEAM_AUTH_ISSUER={issuer}")
    print(f"TEAM_AUTH_AUDIENCE={client_id}")
    print(f"VITE_TEAM_AUTHORITY={authority}")
    print(f"VITE_TEAM_CLIENT_ID={client_id}")
    print(f"Hosted sign-in: https://{DOMAIN_PREFIX}.auth.{REGION}.amazoncognito.com")
    print("The pool is invite-only, TOTP MFA is required, and self-registration is disabled.")


if __name__ == "__main__":
    try:
        main()
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "AWS error")
        message = exc.response.get("Error", {}).get("Message", str(exc))
        raise SystemExit(f"{code}: {message}") from exc
