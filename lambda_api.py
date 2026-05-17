import json
import boto3
from datetime import datetime, timedelta, timezone

# Initialize boto3 clients (Lambda execution role provides credentials)
cw = boto3.client('cloudwatch')
s3 = boto3.client('s3')

CW_NAMESPACE = "AirQuality/Pipeline"
INSTRUMENT_IDS = ["BC-MA200", "CO2-LICOR", "NEPH-PM25", "NO2-CAPS", "SMPS"]
BUCKET_NAME = "des-moines-data-pipeline"

def get_cw_stat(metric, stat, dims=None, hours=1, period=300):
    """Helper to fetch a single stat from CloudWatch."""
    try:
        kw = {
            "Namespace": CW_NAMESPACE,
            "MetricName": metric,
            "StartTime": datetime.now(timezone.utc) - timedelta(hours=hours),
            "EndTime": datetime.now(timezone.utc),
            "Period": period,
            "Statistics": [stat]
        }
        if dims:
            kw["Dimensions"] = dims
        pts = cw.get_metric_statistics(**kw).get("Datapoints", [])
        if not pts:
            return 0
        return sorted(pts, key=lambda p: p["Timestamp"])[-1].get(stat, 0)
    except Exception as e:
        print(f"Error fetching CW metric {metric}: {e}")
        return 0

def lambda_handler(event, context):
    """
    HTTP API Gateway entry point.
    Returns JSON payload for the React dashboard.
    """
    now = datetime.now(timezone.utc)
    
    # 1. Pipeline Overview
    lambda_sr = get_cw_stat("LambdaSuccess", "Average", hours=24, period=86400)
    
    # Cost metrics placeholder (if stored in S3)
    mtd_cost = 0.00
    try:
        # Example: Read cost metrics stored as JSON in S3
        # response = s3.get_object(Bucket=BUCKET_NAME, Key='metrics/cost_mtd.json')
        # mtd_cost = json.loads(response['Body'].read().decode('utf-8')).get('total_cost', 0)
        mtd_cost = 12.45 # Placeholder for demo
    except Exception:
        pass

    # 2. Instrument Inventory & Status
    instruments = []
    total_volume = 0
    total_files = 0
    worst_freshness = 0
    
    for iid in INSTRUMENT_IDS:
        dims = [{"Name": "Instrument", "Value": iid}]
        
        b_files = get_cw_stat("BronzeFiles", "Maximum", dims)
        b_size = get_cw_stat("BronzeSize", "Maximum", dims)
        s_files = get_cw_stat("SilverFiles", "Maximum", dims)
        freshness = get_cw_stat("Freshness", "Maximum", dims)
        
        total_volume += b_size
        total_files += b_files
        if freshness > worst_freshness:
            worst_freshness = freshness
            
        # Determine Status
        status = "OK"
        if freshness > 2.0:
            status = "ERROR"
        elif freshness > 0.5:
            status = "DEGRADED"
            
        # Sync calculation
        sync_pct = (s_files / b_files * 100) if b_files > 0 else 100
        
        instruments.append({
            "id": iid,
            "name": iid.replace("-", " "),
            "status": status,
            "bronzeFiles": b_files,
            "silverFiles": s_files,
            "syncStatus": round(sync_pct, 1),
            "freshnessLag": round(freshness, 2),
            "bronzeSize": b_size
        })

    # Prepare final payload
    payload = {
        "lastUpdated": now.isoformat(),
        "kpis": {
            "totalVolumeBytes": total_volume,
            "totalBronzeFiles": total_files,
            "maxFreshnessLag": round(worst_freshness, 2),
            "lambdaSuccessRate": round(lambda_sr * 100, 1),
            "mtdCost": mtd_cost
        },
        "instruments": instruments,
        # Placeholder for 24h trend data for charts
        "trends": {
            "dates": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
            "cost": [1.2, 1.5, 1.3, 1.8, 1.4, 1.6, 2.0]
        }
    }

    return {
        "statusCode": 200,
        "headers": {
            # Required for CORS
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET",
            "Content-Type": "application/json"
        },
        "body": json.dumps(payload)
    }
