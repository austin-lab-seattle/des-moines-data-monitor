import boto3
import json
import time
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CREDS_FILE = REPO_ROOT / "aws_creds.json"
DEFAULT_REGION = "us-west-2"

print("Starting AWS deployment script...")


def build_session():
    """Build a boto3 session.

    Prefers the standard AWS credential chain (environment variables, shared
    config/credentials, or an attached role). Falls back to aws_creds.json only
    when the chain finds nothing.
    """
    session = boto3.Session()
    if session.get_credentials() is not None:
        region = session.region_name or DEFAULT_REGION
        print(f"Using AWS credentials from the default provider chain (region {region}).")
        return boto3.Session(region_name=region)

    if CREDS_FILE.exists():
        with open(CREDS_FILE) as f:
            creds = json.load(f)
        print("Using AWS credentials from aws_creds.json.")
        return boto3.Session(
            aws_access_key_id=creds["aws_access_key_id"],
            aws_secret_access_key=creds["aws_secret_access_key"],
            region_name=creds.get("region", DEFAULT_REGION),
        )

    print("No AWS credentials found. Set env vars / an AWS profile, or create aws_creds.json.")
    raise SystemExit(1)


session = build_session()

iam_client = session.client("iam")
lambda_client = session.client("lambda")
api_gateway_client = session.client("apigatewayv2")
events_client = session.client("events")
s3_client = session.client("s3")
sts_client = session.client("sts")
account_id = sts_client.get_caller_identity()["Account"]
region = session.region_name

ROLE_NAME = "AQDashboardBackendRole"
API_LAMBDA_NAME = "aq-dashboard-api"
DQ_LAMBDA_NAME = "dq_collector"
API_NAME = "AQDashboardAPI"
BUCKET_NAME = "des-moines-data-pipeline-austinlab"
DQ_RULE_NAME = "dq-collector-hourly"
DQ_SCHEDULE = "rate(1 hour)"
LEGACY_DQ_RULE_NAMES = ["dq-collector-15-minutes"]


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
    except Exception:
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
            "Action": ["ce:GetCostAndUsage"],
            "Resource": "*"
        },
        {
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:ListBucket"],
            "Resource": [
                f"arn:aws:s3:::{BUCKET_NAME}",
                f"arn:aws:s3:::{BUCKET_NAME}/*"
            ]
        }
    ]
}
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


def create_or_update_lambda(function_name, handler, runtime, zip_bytes, timeout):
    print(f"\nDeploying Lambda {function_name}...")
    try:
        response = lambda_client.get_function(FunctionName=function_name)
        lambda_arn = response["Configuration"]["FunctionArn"]
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
                Environment={"Variables": {"S3_BUCKET": BUCKET_NAME}},
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
                Environment={"Variables": {"S3_BUCKET": BUCKET_NAME}},
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
        "AllowMethods": ["GET", "OPTIONS"],
        "AllowHeaders": ["content-type"],
        "MaxAge": 300,
    }
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
route_key = "GET /metrics"
if not any(route["RouteKey"] == route_key for route in routes):
    api_gateway_client.create_route(
        ApiId=api_id,
        RouteKey=route_key,
        Target=f"integrations/{integration_id}",
    )
    print("Route 'GET /metrics' created.")

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

final_url = f"{api_endpoint}/metrics"
print("\n" + "=" * 50)
print("AWS DEPLOYMENT COMPLETE")
print(f"API URL: {final_url}")
print("=" * 50)
print("\nNext Step: Update the VITE_API_URL environment variable in Vercel with this URL.")
