import json
import logging
import boto3
import time
from datetime import datetime, timezone
import collections

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3_client = boto3.client('s3')
cloudwatch = boto3.client('cloudwatch')

BUCKET = "des-moines-data-pipeline"
INSTRUMENTS = ["BC-MA200", "CO2-LICOR", "NEPH-PM25", "NO2-CAPS", "SMPS"]

def lambda_handler(event, context):
    logger.info("Starting scheduled DQ Collector run")
    start_time = time.time()

    try:
        now = datetime.now(timezone.utc)
        metric_data = []

        for inst in INSTRUMENTS:
            for tier in ["bronze", "silver", "gold"]:
                prefix = f"{inst}/{tier}/"
                response = s3_client.list_objects_v2(Bucket=BUCKET, Prefix=prefix)

                file_count = 0
                total_size = 0
                latest = None

                if 'Contents' in response:
                    for obj in response['Contents']:
                        key = obj['Key']
                        if key.endswith('.keep') or key.endswith('/'):
                            continue
                        file_count += 1
                        total_size += obj['Size']
                        if latest is None or obj['LastModified'] > latest:
                            latest = obj['LastModified']

                metric_data.append({
                    'MetricName': f'{tier.capitalize()}Files',
                    'Dimensions': [{'Name': 'Instrument', 'Value': inst}],
                    'Value': file_count,
                    'Unit': 'Count'
                })
                metric_data.append({
                    'MetricName': f'{tier.capitalize()}Size',
                    'Dimensions': [{'Name': 'Instrument', 'Value': inst}],
                    'Value': total_size,
                    'Unit': 'Bytes'
                })

                if tier == "bronze" and latest:
                    freshness_hours = (now - latest).total_seconds() / 3600
                    metric_data.append({
                        'MetricName': 'Freshness',
                        'Dimensions': [{'Name': 'Instrument', 'Value': inst}],
                        'Value': round(freshness_hours, 2),
                        'Unit': 'None'
                    })

        end_time = time.time()
        duration_ms = (end_time - start_time) * 1000

        metric_data.extend([
            {
                'MetricName': 'LambdaDuration',
                'Value': duration_ms,
                'Unit': 'Milliseconds'
            },
            {
                'MetricName': 'LambdaSuccess',
                'Value': 1,
                'Unit': 'Count'
            }
        ])

        # CloudWatch limits to 20 metrics per API call
        for i in range(0, len(metric_data), 20):
            batch = metric_data[i:i + 20]
            cloudwatch.put_metric_data(
                Namespace='AirQuality/Pipeline',
                MetricData=batch
            )

        logger.info(f"Published {len(metric_data)} metrics to CloudWatch")

        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'DQ Collector run complete',
                'metrics_published': len(metric_data)
            })
        }

    except Exception as e:
        logger.error(f"Error in DQ Collector: {str(e)}")
        cloudwatch.put_metric_data(
            Namespace='AirQuality/Pipeline',
            MetricData=[{
                'MetricName': 'LambdaSuccess',
                'Value': 0,
                'Unit': 'Count'
            }]
        )
        raise e
