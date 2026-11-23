"""Provision the isolated research export service. Read-only plan by default.

Creates no memberships and sends no messages. Existing public API, Cognito
settings, schedules and source objects are preserved.
"""
import argparse
import io
import json
from pathlib import Path
import time
import zipfile

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[2]
REGION = 'us-west-2'
ROLE = 'AQResearchExportRole'
ADMIN_ROLE = 'AQResearchKeyAdminRole'
ADMIN_FUNCTION = 'aq-research-key-admin'
API_FUNCTION = 'aq-research-export-api'
WORKER = 'aq-research-export-worker'
POOL = 'us-west-2_oR7CIzxQG'
SOURCE_BUCKET = 'des-moines-data-pipeline-austinlab'
INSTRUMENTS = ['BC-MA200', 'CO2-LICOR', 'NEPH-PM25', 'NO2-CAPS', 'SMPS']
ROUTES = ['GET /air-quality/internal/v1/research/access',
          'GET /air-quality/internal/v1/research/exports',
          'POST /air-quality/internal/v1/research/exports',
          'GET /air-quality/internal/v1/research/exports/{job_id}',
          'GET /air-quality/internal/v1/research/exports/{job_id}/download']
KEY_ROUTES = [route.replace('/air-quality/internal/v1/research', '/air-quality/v1/keyed/research') for route in ROUTES]


def session(path):
    creds, _ = json.JSONDecoder().raw_decode(Path(path).read_text())
    return boto3.Session(aws_access_key_id=creds['aws_access_key_id'],
        aws_secret_access_key=creds['aws_secret_access_key'], aws_session_token=creds.get('aws_session_token'), region_name=REGION)


def package():
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as bundle:
        for name in ['lambda_api.py', 'api_usage.py', 'observation_index.py', 'public_data_policy.py', 'research_exports.py', 'research_state.py', 'research_keys.py', 'research_key_admin.py']:
            bundle.write(ROOT / name, name)
    return out.getvalue()


def wait_function(client, name):
    # The operator policy permits GetFunction, which includes configuration,
    # but does not require the separate GetFunctionConfiguration action.
    for _ in range(60):
        config = client.get_function(FunctionName=name)['Configuration']
        if config.get('State') == 'Failed' or config.get('LastUpdateStatus') == 'Failed':
            raise RuntimeError(f'{name} deployment failed')
        if config.get('State') == 'Active' and config.get('LastUpdateStatus', 'Successful') == 'Successful':
            return
        time.sleep(2)
    raise RuntimeError(f'{name} deployment did not become ready')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--aws-creds-file', required=True)
    parser.add_argument('--expected-account', required=True)
    parser.add_argument('--api-id', default='yvhb48sthk')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    aws = session(args.aws_creds_file)
    caller = aws.client('sts').get_caller_identity()
    if caller['Account'] != args.expected_account:
        raise RuntimeError('AWS account mismatch')
    account = caller['Account']
    bucket = f'des-moines-research-{account}-{REGION}'
    role_arn = f'arn:aws:iam::{account}:role/{ROLE}'
    admin_role_arn = f'arn:aws:iam::{account}:role/{ADMIN_ROLE}'
    lam = aws.client('lambda')
    key_config = lam.get_function(FunctionName='aq-dashboard-api')['Configuration'].get('Environment', {}).get('Variables', {})
    if not key_config.get('API_KEY_HASH_PEPPER'):
        raise RuntimeError('Existing API key validation must be configured')
    key_table = key_config.get('API_KEYS_TABLE', 'AQApiKeys')
    rate_table = key_config.get('RATE_LIMITS_TABLE', 'AQApiRateLimits')
    key_arn = f'arn:aws:dynamodb:{REGION}:{account}:table/{key_table}'
    rate_arn = f'arn:aws:dynamodb:{REGION}:{account}:table/{rate_table}'
    gateway = aws.client('apigatewayv2')
    authorizer = next((item for item in gateway.get_authorizers(ApiId=args.api_id).get('Items', [])
                       if item['Name'] == 'UWTeamJwt'), None)
    if not authorizer or authorizer['AuthorizerType'] != 'JWT':
        raise RuntimeError('The verified team JWT authorizer is required')
    if authorizer['JwtConfiguration']['Issuer'] != f'https://cognito-idp.{REGION}.amazonaws.com/{POOL}':
        raise RuntimeError('Unexpected team issuer')
    print(json.dumps({'apply': args.apply, 'bucket': bucket, 'role': ROLE, 'functions': [API_FUNCTION, WORKER, ADMIN_FUNCTION],
                      'routes': ROUTES + KEY_ROUTES, 'membership_changes': False, 'messages_sent': False}), flush=True)
    if not args.apply:
        return
    iam = aws.client('iam')
    trust = {'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow',
        'Principal': {'Service': 'lambda.amazonaws.com'}, 'Action': 'sts:AssumeRole'}]}
    try:
        iam.create_role(RoleName=ROLE, AssumeRolePolicyDocument=json.dumps(trust))
    except iam.exceptions.EntityAlreadyExistsException:
        current = iam.get_role(RoleName=ROLE)['Role']['AssumeRolePolicyDocument']
        if current != trust:
            raise RuntimeError('Existing research role trust differs; inspect before changing it')
    resources = [f'arn:aws:s3:::{SOURCE_BUCKET}/_observations/v1/*']
    for instrument in INSTRUMENTS:
        resources.extend(f'arn:aws:s3:::{SOURCE_BUCKET}/{instrument}/{path}' for path in
                         ['flags/*', 'corrections/*', f'silver/{instrument}_data.csv'])
    policy = {'Version': '2012-10-17', 'Statement': [
        {'Effect': 'Allow', 'Action': ['s3:GetObject'], 'Resource': resources},
        {'Effect': 'Allow', 'Action': ['s3:ListBucket'], 'Resource': f'arn:aws:s3:::{SOURCE_BUCKET}',
         'Condition': {'StringLike': {'s3:prefix': [f'{instrument}/{kind}/*' for instrument in INSTRUMENTS for kind in ['flags', 'corrections']]}}},
        {'Effect': 'Allow', 'Action': ['s3:GetObject', 's3:PutObject', 's3:AbortMultipartUpload', 's3:ListMultipartUploadParts'],
         'Resource': [f'arn:aws:s3:::{bucket}/exports/*', f'arn:aws:s3:::{bucket}/requests/*', f'arn:aws:s3:::{bucket}/state/*']},
        {'Effect': 'Allow', 'Action': ['cognito-idp:AdminListGroupsForUser', 'cognito-idp:AdminGetUser'],
         'Resource': f'arn:aws:cognito-idp:{REGION}:{account}:userpool/{POOL}'},
        {'Effect': 'Allow', 'Action': ['lambda:InvokeFunction'],
         'Resource': f'arn:aws:lambda:{REGION}:{account}:function:{WORKER}'},
    ]}
    policy['Statement'].extend([
        {'Effect': 'Allow', 'Action': ['dynamodb:GetItem'], 'Resource': key_arn},
        {'Effect': 'Allow', 'Action': ['dynamodb:UpdateItem'], 'Resource': key_arn,
         'Condition': {'ForAllValues:StringEquals': {'dynamodb:Attributes': ['key_id', 'last_used_at', 'last_used_at_epoch']}}},
        {'Effect': 'Allow', 'Action': ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem'], 'Resource': rate_arn},
    ])
    try:
        iam.create_role(RoleName=ADMIN_ROLE, AssumeRolePolicyDocument=json.dumps(trust))
    except iam.exceptions.EntityAlreadyExistsException:
        if iam.get_role(RoleName=ADMIN_ROLE)['Role']['AssumeRolePolicyDocument'] != trust:
            raise RuntimeError('Unexpected operator role trust')
    admin_policy = {'Version': '2012-10-17', 'Statement': [
        {'Effect': 'Allow', 'Action': ['dynamodb:Query'], 'Resource': [key_arn, key_arn + '/index/status-created-at-index']},
        {'Effect': 'Allow', 'Action': ['dynamodb:UpdateItem'], 'Resource': key_arn,
         'Condition': {'ForAllValues:StringEquals': {'dynamodb:Attributes': ['key_id', 'status', 'email', 'research_subject', 'scopes']}}},
        {'Effect': 'Allow', 'Action': ['cognito-idp:AdminGetUser', 'cognito-idp:AdminListGroupsForUser'],
         'Resource': f'arn:aws:cognito-idp:{REGION}:{account}:userpool/{POOL}'},
        {'Effect': 'Allow', 'Action': ['s3:PutObject'], 'Resource': f'arn:aws:s3:::{bucket}/audit/research-key-access/*'},
    ]}
    admin_policy['Statement'].extend([
        {'Effect': 'Allow', 'Action': ['dynamodb:PutItem'], 'Resource': key_arn,
         'Condition': {'ForAllValues:StringLike': {'dynamodb:LeadingKeys': ['smok*']}}},
        {'Effect': 'Allow', 'Action': ['dynamodb:UpdateItem'], 'Resource': key_arn,
         'Condition': {'ForAllValues:StringLike': {'dynamodb:LeadingKeys': ['smok*']},
                       'ForAllValues:StringEquals': {'dynamodb:Attributes': ['key_id', 'status', 'email', 'purpose']}}},
    ])
    iam.put_role_policy(RoleName=ADMIN_ROLE, PolicyName='ResearchKeyApprovalOnly', PolicyDocument=json.dumps(admin_policy))
    iam.attach_role_policy(RoleName=ADMIN_ROLE, PolicyArn='arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole')
    iam.put_role_policy(RoleName=ROLE, PolicyName='ResearchExportsOnly', PolicyDocument=json.dumps(policy))
    iam.attach_role_policy(RoleName=ROLE, PolicyArn='arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole')
    s3 = aws.client('s3')
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as exc:
        if exc.response['Error']['Code'] not in ('404', 'NoSuchBucket', 'NotFound'):
            raise
        s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={'LocationConstraint': REGION})
    s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
        key: True for key in ['BlockPublicAcls', 'IgnorePublicAcls', 'BlockPublicPolicy', 'RestrictPublicBuckets']})
    s3.put_bucket_encryption(Bucket=bucket, ServerSideEncryptionConfiguration={'Rules': [
        {'ApplyServerSideEncryptionByDefault': {'SSEAlgorithm': 'AES256'}}]})
    operator = caller['Arn']
    if ':assumed-role/' in operator:
        operator = f'arn:aws:iam::{account}:role/' + operator.split(':assumed-role/')[1].rsplit('/', 1)[0]
    bucket_policy = {'Version': '2012-10-17', 'Statement': [
        {'Sid': 'DenyOtherPrincipals', 'Effect': 'Deny', 'Principal': '*', 'Action': 's3:*',
         'Resource': [f'arn:aws:s3:::{bucket}', f'arn:aws:s3:::{bucket}/*'],
         'Condition': {'ArnNotEquals': {'aws:PrincipalArn': [role_arn, admin_role_arn, operator, f'arn:aws:iam::{account}:root']}}},
        {'Sid': 'RequireTLS', 'Effect': 'Deny', 'Principal': '*', 'Action': 's3:*',
         'Resource': [f'arn:aws:s3:::{bucket}', f'arn:aws:s3:::{bucket}/*'],
         'Condition': {'Bool': {'aws:SecureTransport': 'false'}}},
    ]}
    s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(bucket_policy))
    s3.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={'Rules': [
        {'ID': 'expire-' + prefix.rstrip('/'), 'Status': 'Enabled', 'Filter': {'Prefix': prefix},
         'Expiration': {'Days': days}, 'AbortIncompleteMultipartUpload': {'DaysAfterInitiation': 1}}
        for prefix, days in [('exports/', 1), ('requests/', 1), ('state/', 1), ('audit/', 365)]
    ]})
    cognito = aws.client('cognito-idp')
    try:
        cognito.get_group(UserPoolId=POOL, GroupName='ResearchExporter')
    except cognito.exceptions.ResourceNotFoundException:
        cognito.create_group(UserPoolId=POOL, GroupName='ResearchExporter',
                             Description='Explicitly approved full-history research exports', Precedence=35)
    lam = aws.client('lambda')
    code = package()
    environment = {'RESEARCH_EXPORT_BUCKET': bucket,
        'RESEARCH_EXPORT_WORKER': WORKER, 'TEAM_USER_POOL_ID': POOL, 'S3_BUCKET': SOURCE_BUCKET,
        'DATA_TIMEZONE': 'America/Los_Angeles', 'RESEARCH_CONTACT_EMAIL': 'pavands@uw.edu'}
    api_environment = {**environment, 'API_KEY_HASH_PEPPER': key_config['API_KEY_HASH_PEPPER'],
                       'API_KEYS_TABLE': key_table, 'RATE_LIMITS_TABLE': rate_table}
    for name, handler, memory, timeout in [(WORKER, 'research_exports.worker_handler', 2048, 900),
                                          (API_FUNCTION, 'research_exports.lambda_handler', 512, 20),
                                          (ADMIN_FUNCTION, 'research_key_admin.lambda_handler', 256, 30)]:
        function_role = admin_role_arn if name == ADMIN_FUNCTION else role_arn
        function_environment = api_environment if name == API_FUNCTION else {**environment, 'API_KEYS_TABLE': key_table}
        if name == ADMIN_FUNCTION:
            function_environment['API_KEY_HASH_PEPPER'] = key_config['API_KEY_HASH_PEPPER']
        try:
            current = lam.get_function(FunctionName=name)['Configuration']
        except lam.exceptions.ResourceNotFoundException:
            current = None
        if current:
            if current['Role'] != function_role:
                raise RuntimeError('Existing function uses an unexpected role')
            lam.update_function_code(FunctionName=name, ZipFile=code, RevisionId=current['RevisionId'])
            wait_function(lam, name)
            lam.update_function_configuration(FunctionName=name, Handler=handler, MemorySize=memory,
                Timeout=timeout, Environment={'Variables': function_environment})
        else:
            for attempt in range(12):
                try:
                    lam.create_function(FunctionName=name, Runtime='python3.11', Role=function_role, Handler=handler,
                        Code={'ZipFile': code}, Timeout=timeout, MemorySize=memory,
                        Environment={'Variables': function_environment})
                    break
                except lam.exceptions.InvalidParameterValueException:
                    if attempt == 11:
                        raise
                    time.sleep(3)
            wait_function(lam, name)
        wait_function(lam, name)
        print(json.dumps({'function': name, 'ready': True}), flush=True)
    # Conditional state claims prevent duplicate execution even when Lambda's
    # default async retry policy is used. These operator-level tuning actions
    # are optional and must not require widening deployment permissions.
    for action, options in [
        (lam.put_function_concurrency, {'ReservedConcurrentExecutions': 2}),
        (lam.put_function_event_invoke_config, {'MaximumRetryAttempts': 0, 'MaximumEventAgeInSeconds': 3600}),
    ]:
        try:
            action(FunctionName=WORKER, **options)
        except ClientError as exc:
            if exc.response['Error']['Code'] not in {'AccessDeniedException', 'AccessDenied'}:
                raise
            print(json.dumps({'optional_tuning': action.__name__, 'applied': False}), flush=True)
    function_arn = f'arn:aws:lambda:{REGION}:{account}:function:{API_FUNCTION}'
    integrations = gateway.get_integrations(ApiId=args.api_id).get('Items', [])
    integration = next((item for item in integrations if item.get('IntegrationUri') == function_arn), None)
    if not integration:
        integration = gateway.create_integration(ApiId=args.api_id, IntegrationType='AWS_PROXY',
            IntegrationUri=function_arn, PayloadFormatVersion='2.0', TimeoutInMillis=25000)
    routes = gateway.get_routes(ApiId=args.api_id).get('Items', [])
    for route in ROUTES + KEY_ROUTES:
        existing = next((item for item in routes if item['RouteKey'] == route), None)
        kwargs = {'ApiId': args.api_id, 'Target': f'integrations/{integration["IntegrationId"]}',
                  'AuthorizationType': 'JWT', 'AuthorizerId': authorizer['AuthorizerId']}
        if route in KEY_ROUTES:
            kwargs.update(AuthorizationType='NONE')
            kwargs.pop('AuthorizerId')
            if existing and existing.get('AuthorizationType') not in {None, 'NONE'}:
                raise RuntimeError('Unexpected authorizer on a keyed research route')
        if existing:
            gateway.update_route(RouteId=existing['RouteId'], **kwargs)
        else:
            if route in KEY_ROUTES:
                kwargs.pop('AuthorizerId', None)
            gateway.create_route(RouteKey=route, **kwargs)
    try:
        lam.add_permission(FunctionName=API_FUNCTION, StatementId='ResearchGateway', Action='lambda:InvokeFunction',
            Principal='apigateway.amazonaws.com', SourceArn=f'arn:aws:execute-api:{REGION}:{account}:{args.api_id}/*/*/air-quality/internal/v1/research/*')
    except lam.exceptions.ResourceConflictException:
        pass
    try:
        lam.add_permission(FunctionName=API_FUNCTION, StatementId='ResearchKeyGateway', Action='lambda:InvokeFunction',
            Principal='apigateway.amazonaws.com', SourceArn=f'arn:aws:execute-api:{REGION}:{account}:{args.api_id}/*/*/air-quality/v1/keyed/research/*')
    except lam.exceptions.ResourceConflictException:
        pass
    print(json.dumps({'research_service_ready': True, 'membership_changes': False, 'messages_sent': False}), flush=True)


if __name__ == '__main__':
    main()
