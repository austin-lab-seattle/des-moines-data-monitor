import boto3
import json
import os
import time
import zipfile
from pathlib import Path

from botocore.exceptions import ClientError

REPO_ROOT = Path(__file__).resolve().parent.parent
CREDS_FILE = REPO_ROOT / "aws_creds.json"
DEFAULT_REGION = "us-west-2"
DEPLOY_REGION = os.environ.get("DEPLOY_AWS_REGION", DEFAULT_REGION)
USE_CREDS_FILE = os.environ.get("USE_AWS_CREDS_FILE") == "1"
ENABLE_COST_KPI = os.environ.get("ENABLE_COST_KPI") == "1"
ENABLE_API_KEY_REGISTRATION = os.environ.get("ENABLE_API_KEY_REGISTRATION") == "1"
PUBLIC_API_KEY_REQUIRED = os.environ.get("PUBLIC_API_KEY_REQUIRED") == "1"
API_KEY_HASH_PEPPER = os.environ.get("API_KEY_HASH_PEPPER")
ACCESS_REQUEST_FROM_EMAIL = os.environ.get("ACCESS_REQUEST_FROM_EMAIL")

print("Starting AWS deployment script...")
print(f"Internal cost KPI: {'enabled' if ENABLE_COST_KPI else 'disabled'}")
print(f"API key registration: {'enabled' if ENABLE_API_KEY_REGISTRATION else 'disabled'}")
print(f"Public API key enforcement: {'enabled' if PUBLIC_API_KEY_REQUIRED else 'disabled'}")

if PUBLIC_API_KEY_REQUIRED and not API_KEY_HASH_PEPPER:
    raise SystemExit(
        "PUBLIC_API_KEY_REQUIRED=1 needs API_KEY_HASH_PEPPER. "
        "Keep this secret in your shell or secret manager, never in the repository."
    )
if ENABLE_API_KEY_REGISTRATION and not ACCESS_REQUEST_FROM_EMAIL:
    raise SystemExit(
        "ENABLE_API_KEY_REGISTRATION=1 needs ACCESS_REQUEST_FROM_EMAIL "
        "for the SES verification email sender."
    )


def build_session():
    """Build a boto3 session.

    Prefers the standard AWS credential chain (environment variables, shared
    config/credentials, or an attached role). Falls back to aws_creds.json only
    when the chain finds nothing.
    """
    if USE_CREDS_FILE and CREDS_FILE.exists():
        with open(CREDS_FILE) as f:
            creds = json.load(f)
        print("Using AWS credentials from aws_creds.json.")
        return boto3.Session(
            aws_access_key_id=creds["aws_access_key_id"],
            aws_secret_access_key=creds["aws_secret_access_key"],
            region_name=DEPLOY_REGION,
        )

    session = boto3.Session(region_name=DEPLOY_REGION)
    if session.get_credentials() is not None:
        region = session.region_name or DEPLOY_REGION
        print(f"Using AWS credentials from the default provider chain (region {region}).")
        return session

    if CREDS_FILE.exists():
        with open(CREDS_FILE) as f:
            creds = json.load(f)
        print("Using AWS credentials from aws_creds.json.")
        return boto3.Session(
            aws_access_key_id=creds["aws_access_key_id"],
            aws_secret_access_key=creds["aws_secret_access_key"],
            region_name=DEPLOY_REGION,
        )

    print("No AWS credentials found. Set env vars / an AWS profile, or create aws_creds.json.")
    raise SystemExit(1)


session = build_session()

iam_client = session.client("iam")
lambda_client = session.client("lambda")
api_gateway_client = session.client("apigatewayv2")
events_client = session.client("events")
s3_client = session.client("s3")
dynamodb_client = session.client("dynamodb")
sts_client = session.client("sts")
account_id = sts_client.get_caller_identity()["Account"]
region = session.region_name

ROLE_NAME = "AQDashboardBackendRole"
API_LAMBDA_NAME = "aq-dashboard-api"
SILVER_LAMBDA_NAME = "aq-silver-builder"
DQ_LAMBDA_NAME = "dq_collector"
API_NAME = "AQDashboardAPI"
BUCKET_NAME = "des-moines-data-pipeline-austinlab"
DQ_RULE_NAME = "dq-collector-hourly"
DQ_SCHEDULE = "rate(1 hour)"
LEGACY_DQ_RULE_NAMES = ["dq-collector-15-minutes"]
ENABLE_REVIEW_API_ROUTES = os.environ.get("ENABLE_REVIEW_API_ROUTES") == "1"
ACCESS_REQUESTS_TABLE_NAME = "AQApiAccessRequests"
API_KEYS_TABLE_NAME = "AQApiKeys"


def read_zip_bytes(zip_name, files):
    with zipfile.ZipFile(zip_name, "w") as z:
        for source, arcname in files:
            z.write(source, arcname=arcname)
    with open(zip_name, "rb") as f:
        return f.read()


def ensure_bucket():
    print("\nChecking S3 bucket...")
    try:
        s3_client.head_bucket(Bucket=BUCKET_NAME)
        print(f"Bucket {BUCKET_NAME} already exists and is accessible.")
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code")
        if error_code in {"403", "AccessDenied"}:
            print(
                f"Bucket {BUCKET_NAME} is not manageable by this identity; "
                "assuming it already exists and continuing."
            )
            return
        print(f"Creating bucket {BUCKET_NAME} in {region}...")
        kwargs = {"Bucket": BUCKET_NAME}
        if region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
        s3_client.create_bucket(**kwargs)

    try:
        s3_client.put_public_access_block(
            Bucket=BUCKET_NAME,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )
        print("Bucket public access block configured.")
    except Exception as e:
        print(f"Could not update bucket public access block; continuing: {e}")

    try:
        s3_client.put_bucket_encryption(
            Bucket=BUCKET_NAME,
            ServerSideEncryptionConfiguration={
                "Rules": [{
                    "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}
                }]
            },
        )
        print("Bucket encryption configured.")
    except Exception as e:
        print(f"Could not update bucket encryption; continuing: {e}")

ensure_bucket()


def ensure_table(name, attribute_definitions, key_schema, indexes=()):
    """Create a pay-per-request DynamoDB table once and keep its TTL enabled."""
    try:
        dynamodb_client.describe_table(TableName=name)
        print(f"DynamoDB table {name} already exists.")
    except dynamodb_client.exceptions.ResourceNotFoundException:
        print(f"Creating DynamoDB table {name}...")
        dynamodb_client.create_table(
            TableName=name,
            AttributeDefinitions=attribute_definitions,
            KeySchema=key_schema,
            GlobalSecondaryIndexes=list(indexes),
            BillingMode="PAY_PER_REQUEST",
            SSESpecification={"Enabled": True},
        )
        dynamodb_client.get_waiter("table_exists").wait(TableName=name)
        print(f"DynamoDB table {name} created.")

    try:
        ttl = dynamodb_client.describe_time_to_live(TableName=name).get("TimeToLiveDescription", {})
        if ttl.get("AttributeName") != "ttl" or ttl.get("TimeToLiveStatus") == "DISABLED":
            dynamodb_client.update_time_to_live(
                TableName=name,
                TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"},
            )
            print(f"DynamoDB TTL enabled for {name}.")
    except Exception as exc:
        print(f"Could not confirm DynamoDB TTL for {name}; continuing: {exc}")


ensure_table(
    ACCESS_REQUESTS_TABLE_NAME,
    attribute_definitions=[
        {"AttributeName": "request_id", "AttributeType": "S"},
        {"AttributeName": "email", "AttributeType": "S"},
        {"AttributeName": "status", "AttributeType": "S"},
        {"AttributeName": "created_at_epoch", "AttributeType": "N"},
    ],
    key_schema=[{"AttributeName": "request_id", "KeyType": "HASH"}],
    indexes=[
        {
            "IndexName": "email-created-at-index",
            "KeySchema": [
                {"AttributeName": "email", "KeyType": "HASH"},
                {"AttributeName": "created_at_epoch", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        },
        {
            "IndexName": "status-created-at-index",
            "KeySchema": [
                {"AttributeName": "status", "KeyType": "HASH"},
                {"AttributeName": "created_at_epoch", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        },
    ],
)
ensure_table(
    API_KEYS_TABLE_NAME,
    attribute_definitions=[
        {"AttributeName": "key_id", "AttributeType": "S"},
        {"AttributeName": "status", "AttributeType": "S"},
        {"AttributeName": "created_at_epoch", "AttributeType": "N"},
    ],
    key_schema=[{"AttributeName": "key_id", "KeyType": "HASH"}],
    indexes=[
        {
            "IndexName": "status-created-at-index",
            "KeySchema": [
                {"AttributeName": "status", "KeyType": "HASH"},
                {"AttributeName": "created_at_epoch", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        },
    ],
)

print("\nChecking IAM role...")
try:
    role = iam_client.get_role(RoleName=ROLE_NAME)
    role_arn = role["Role"]["Arn"]
    print(f"Role {ROLE_NAME} already exists.")
except iam_client.exceptions.NoSuchEntityException:
    print(f"Creating role {ROLE_NAME}...")
    assume_role_policy = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]
    }
    role = iam_client.create_role(
        RoleName=ROLE_NAME,
        AssumeRolePolicyDocument=json.dumps(assume_role_policy),
    )
    role_arn = role["Role"]["Arn"]
    print("Role created. Waiting 10 seconds for IAM propagation...")
    time.sleep(10)

for policy_arn in [
    "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
    "arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess",
]:
    iam_client.attach_role_policy(RoleName=ROLE_NAME, PolicyArn=policy_arn)

# CloudWatch is no longer used by the dashboard; detach the old read access.
try:
    iam_client.detach_role_policy(
        RoleName=ROLE_NAME,
        PolicyArn="arn:aws:iam::aws:policy/CloudWatchReadOnlyAccess",
    )
    print("Detached CloudWatchReadOnlyAccess (no longer needed).")
except Exception:
    pass

inline_policy = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
            "Resource": [
                f"arn:aws:s3:::{BUCKET_NAME}",
                f"arn:aws:s3:::{BUCKET_NAME}/*"
            ]
        }
    ]
}
if ENABLE_COST_KPI:
    inline_policy["Statement"].append({
        "Effect": "Allow",
        "Action": ["ce:GetCostAndUsage"],
        "Resource": "*",
    })
if ENABLE_API_KEY_REGISTRATION or PUBLIC_API_KEY_REQUIRED:
    inline_policy["Statement"].append({
        "Effect": "Allow",
        "Action": [
            "dynamodb:GetItem",
            "dynamodb:PutItem",
            "dynamodb:UpdateItem",
            "dynamodb:DeleteItem",
            "dynamodb:Query",
        ],
        "Resource": [
            f"arn:aws:dynamodb:{region}:{account_id}:table/{ACCESS_REQUESTS_TABLE_NAME}",
            f"arn:aws:dynamodb:{region}:{account_id}:table/{ACCESS_REQUESTS_TABLE_NAME}/index/*",
            f"arn:aws:dynamodb:{region}:{account_id}:table/{API_KEYS_TABLE_NAME}",
            f"arn:aws:dynamodb:{region}:{account_id}:table/{API_KEYS_TABLE_NAME}/index/*",
        ],
    })
if ENABLE_API_KEY_REGISTRATION:
    inline_policy["Statement"].append({
        "Effect": "Allow",
        "Action": ["ses:SendEmail"],
        "Resource": "*",
        "Condition": {"StringEquals": {"ses:FromAddress": ACCESS_REQUEST_FROM_EMAIL}},
    })
iam_client.put_role_policy(
    RoleName=ROLE_NAME,
    PolicyName="AQDashboardRuntimeAccess",
    PolicyDocument=json.dumps(inline_policy),
)
print("IAM role policies configured.")


def wait_for_lambda_update(function_name):
    time.sleep(5)


def run_lambda_update(action_name, update_call):
    for attempt in range(1, 7):
        try:
            return update_call()
        except lambda_client.exceptions.ResourceConflictException:
            if attempt == 6:
                raise
            print(f"Lambda update still in progress during {action_name}; retrying in 5s...")
            time.sleep(5)


def api_lambda_environment(existing_env=None):
    environment = {
        **(existing_env or {}),
        "S3_BUCKET": BUCKET_NAME,
        "ENABLE_COST_KPI": "1" if ENABLE_COST_KPI else "0",
        "ENABLE_API_KEY_REGISTRATION": "1" if ENABLE_API_KEY_REGISTRATION else "0",
        "PUBLIC_API_KEY_REQUIRED": "1" if PUBLIC_API_KEY_REQUIRED else "0",
        "ACCESS_REQUESTS_TABLE": ACCESS_REQUESTS_TABLE_NAME,
        "API_KEYS_TABLE": API_KEYS_TABLE_NAME,
    }
    if API_KEY_HASH_PEPPER:
        environment["API_KEY_HASH_PEPPER"] = API_KEY_HASH_PEPPER
    if ACCESS_REQUEST_FROM_EMAIL:
        environment["ACCESS_REQUEST_FROM_EMAIL"] = ACCESS_REQUEST_FROM_EMAIL
    return environment


def create_or_update_lambda(function_name, handler, runtime, zip_bytes, timeout):
    print(f"\nDeploying Lambda {function_name}...")
    try:
        response = lambda_client.get_function(FunctionName=function_name)
        lambda_arn = response["Configuration"]["FunctionArn"]
        existing_env = (
            response
            .get("Configuration", {})
            .get("Environment", {})
            .get("Variables", {})
        )
        lambda_env = (
            api_lambda_environment(existing_env)
            if function_name == API_LAMBDA_NAME
            else {
                **existing_env,
                "S3_BUCKET": BUCKET_NAME,
                "ENABLE_COST_KPI": "1" if ENABLE_COST_KPI else "0",
            }
        )
        run_lambda_update(
            f"{function_name} code update",
            lambda: lambda_client.update_function_code(FunctionName=function_name, ZipFile=zip_bytes),
        )
        wait_for_lambda_update(function_name)
        run_lambda_update(
            f"{function_name} configuration update",
            lambda: lambda_client.update_function_configuration(
                FunctionName=function_name,
                Runtime=runtime,
                Role=role_arn,
                Handler=handler,
                Timeout=timeout,
                MemorySize=1024,
                Environment={"Variables": lambda_env},
            ),
        )
        wait_for_lambda_update(function_name)
        print(f"Lambda {function_name} updated.")
        return lambda_arn
    except lambda_client.exceptions.ResourceNotFoundException:
        print(f"Creating Lambda {function_name}...")

    for i in range(5):
        try:
            response = lambda_client.create_function(
                FunctionName=function_name,
                Runtime=runtime,
                Role=role_arn,
                Handler=handler,
                Code={"ZipFile": zip_bytes},
                Timeout=timeout,
                MemorySize=1024,
                Environment={
                    "Variables": (
                        api_lambda_environment()
                        if function_name == API_LAMBDA_NAME
                        else {
                            "S3_BUCKET": BUCKET_NAME,
                            "ENABLE_COST_KPI": "1" if ENABLE_COST_KPI else "0",
                        }
                    )
                },
            )
            print(f"Lambda {function_name} created.")
            return response["FunctionArn"]
        except Exception as e:
            if "The role defined for the function cannot be assumed by Lambda" in str(e):
                print("IAM Role propagating, retrying in 5s...")
                time.sleep(5)
            else:
                raise e
    raise RuntimeError(f"Could not create Lambda {function_name}")


print("\nPackaging the dashboard API Lambda...")
api_zip_bytes = read_zip_bytes(
    str(REPO_ROOT / "lambda_api.zip"),
    [(REPO_ROOT / "lambda_api.py", "lambda_api.py")],
)

api_lambda_arn = create_or_update_lambda(
    API_LAMBDA_NAME,
    "lambda_api.lambda_handler",
    "python3.11",
    api_zip_bytes,
    30,
)

silver_zip_bytes = read_zip_bytes(
    str(REPO_ROOT / "silver_builder.zip"),
    [(REPO_ROOT / "lambda" / "silver_builder.py", "silver_builder.py")],
)
silver_lambda_arn = create_or_update_lambda(
    SILVER_LAMBDA_NAME,
    "silver_builder.lambda_handler",
    "python3.12",
    silver_zip_bytes,
    300,
)

print("\nScheduling the Silver builder (daily)...")
SILVER_RULE_NAME = "silver-builder-daily"
silver_rule = events_client.put_rule(
    Name=SILVER_RULE_NAME,
    ScheduleExpression="rate(1 day)",
    State="ENABLED",
    Description="Rebuilds the Silver layer from Bronze once a day.",
)
events_client.put_targets(
    Rule=SILVER_RULE_NAME,
    Targets=[{"Id": SILVER_LAMBDA_NAME, "Arn": silver_lambda_arn}],
)
try:
    lambda_client.add_permission(
        FunctionName=SILVER_LAMBDA_NAME,
        StatementId=f"eventbridge-{SILVER_RULE_NAME}",
        Action="lambda:InvokeFunction",
        Principal="events.amazonaws.com",
        SourceArn=silver_rule["RuleArn"],
    )
except lambda_client.exceptions.ResourceConflictException:
    pass
print(f"EventBridge rule {SILVER_RULE_NAME} configured (daily).")

print("\nRemoving the retired dq_collector (CloudWatch metrics are no longer used)...")
for rule_name in [DQ_RULE_NAME] + LEGACY_DQ_RULE_NAMES:
    try:
        events_client.remove_targets(Rule=rule_name, Ids=[DQ_LAMBDA_NAME], Force=True)
    except Exception:
        pass
    try:
        events_client.delete_rule(Name=rule_name, Force=True)
        print(f"Deleted EventBridge rule {rule_name}.")
    except Exception as e:
        print(f"No EventBridge rule {rule_name} to delete: {e}")

try:
    lambda_client.delete_function(FunctionName=DQ_LAMBDA_NAME)
    print(f"Deleted Lambda {DQ_LAMBDA_NAME}.")
except lambda_client.exceptions.ResourceNotFoundException:
    print(f"Lambda {DQ_LAMBDA_NAME} already removed.")
except Exception as e:
    print(f"Could not delete Lambda {DQ_LAMBDA_NAME}; continuing: {e}")

print("\nConfiguring API Gateway...")
apis = api_gateway_client.get_apis()["Items"]
api = next((item for item in apis if item["Name"] == API_NAME), None)

if api:
    api_id = api["ApiId"]
    api_endpoint = api["ApiEndpoint"]
    print(f"API {API_NAME} already exists ({api_id}).")
else:
    print(f"Creating HTTP API {API_NAME}...")
cors_config = {
    "AllowOrigins": ["*"],
    "AllowMethods": ["GET", "POST", "OPTIONS"],
    "AllowHeaders": ["content-type"],
    "MaxAge": 300,
}

if api:
    api_gateway_client.update_api(ApiId=api_id, CorsConfiguration=cors_config)
    print("API CORS configuration updated.")
else:
    response = api_gateway_client.create_api(
        Name=API_NAME,
        ProtocolType="HTTP",
        CorsConfiguration=cors_config,
    )
    api_id = response["ApiId"]
    api_endpoint = response["ApiEndpoint"]
    print("API created.")

print("Configuring Lambda Integration...")
integrations = api_gateway_client.get_integrations(ApiId=api_id)["Items"]
integration = next((item for item in integrations if item["IntegrationUri"] == api_lambda_arn), None)

if not integration:
    response = api_gateway_client.create_integration(
        ApiId=api_id,
        IntegrationType="AWS_PROXY",
        IntegrationUri=api_lambda_arn,
        PayloadFormatVersion="2.0",
    )
    integration_id = response["IntegrationId"]
else:
    integration_id = integration["IntegrationId"]

routes = api_gateway_client.get_routes(ApiId=api_id)["Items"]
canonical_public_route_keys = [
    "GET /air-quality/v1/summary",
    "GET /air-quality/v1/timeseries",
    "GET /air-quality/v1/observations",
    "GET /air-quality/v1/observations/export",
]
public_access_route_keys = [
    "POST /air-quality/v1/access-requests",
    "GET /air-quality/v1/access-requests/verify",
]
legacy_public_route_keys = [
    "GET /metrics",
    "GET /series",
    "GET /silver-download",
    "GET /silver-records",
]
public_route_keys = canonical_public_route_keys + public_access_route_keys + legacy_public_route_keys
review_route_keys = [
    "GET /record-flags",
    "POST /record-flags",
    "GET /record-corrections",
    "POST /record-corrections",
]
route_keys = public_route_keys + (review_route_keys if ENABLE_REVIEW_API_ROUTES else [])

if not ENABLE_REVIEW_API_ROUTES:
    for route in routes:
        if route["RouteKey"] in review_route_keys:
            api_gateway_client.delete_route(ApiId=api_id, RouteId=route["RouteId"])
            print(f"Deleted non-public route '{route['RouteKey']}'.")
    routes = api_gateway_client.get_routes(ApiId=api_id)["Items"]

existing_route_keys = {route["RouteKey"] for route in routes}
for route_key in route_keys:
    if route_key in existing_route_keys:
        continue
    api_gateway_client.create_route(
        ApiId=api_id,
        RouteKey=route_key,
        Target=f"integrations/{integration_id}",
    )
    print(f"Route '{route_key}' created.")

stages = api_gateway_client.get_stages(ApiId=api_id)["Items"]
if not any(stage["StageName"] == "$default" for stage in stages):
    api_gateway_client.create_stage(
        ApiId=api_id,
        StageName="$default",
        AutoDeploy=True,
    )
    print("Stage '$default' created.")

print("Granting API Gateway permission to invoke Lambda...")
try:
    lambda_client.add_permission(
        FunctionName=API_LAMBDA_NAME,
        StatementId=f"apigateway-{api_id}",
        Action="lambda:InvokeFunction",
        Principal="apigateway.amazonaws.com",
        SourceArn=f"arn:aws:execute-api:{region}:{account_id}:{api_id}/*/*/*",
    )
except lambda_client.exceptions.ResourceConflictException:
    pass

final_url = f"{api_endpoint}/air-quality/v1/summary"
print("\n" + "=" * 50)
print("AWS DEPLOYMENT COMPLETE")
print(f"API URL: {final_url}")
print(f"API base: {api_endpoint}")
print("=" * 50)
print("\nNext Step: Update the VITE_API_URL environment variable in Vercel with this URL.")
