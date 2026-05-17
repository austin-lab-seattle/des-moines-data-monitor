# Des Moines Data Monitor

Air quality data pipeline and monitoring dashboard for the DEOHS research project. Collects data from 5 field instruments, uploads to AWS S3, and provides a **Streamlit dashboard** for real-time pipeline monitoring via CloudWatch.

## Instruments

| ID | Instrument | Status |
|----|-----------|--------|
| BC-MA200 | Black Carbon MA200 | Planned |
| CO2-LICOR | CO2 Li-Cor | Active |
| NEPH-PM25 | Nephelometer PM25 | Planned |
| NO2-CAPS | NO2 CAPS | Active |
| SMPS | SMPS | Planned |

## Architecture

```
Field Laptop                        AWS Cloud
-----------                         ---------
Instruments                         S3: des-moines-test
    |                                   |
schedule_upload_co2.py ----upload----> licor/bronze/{instrument_id}/year=YYYY/month=MM/
    |                                   |
checkpoint + SQLite buffer          Lambda: dq_collector (every 15 min via EventBridge)
                                        |
                                    CloudWatch: AirQuality/Pipeline namespace
                                    (BronzeFiles, BronzeSize, Freshness, LambdaDuration, LambdaSuccess)
                                        |
                                    Streamlit dashboard (local)
```

## Components

### Data Pipeline (field laptop)

- `schedule_upload_co2.py` — Async ingestion pipeline. Reads from sensor files using byte-offset checkpoints, buffers in SQLite, uploads to S3 with retries.
- `instruments_config.json` — Instrument definitions, S3 bucket config, upload interval.
- `aws_creds.json` — AWS credentials (gitignored).

### Cloud Infrastructure

- `lambda/dq_collector.py` — Lambda function triggered by EventBridge (every 15 min). Queries S3 for real batch counts, sizes, freshness per instrument. Publishes metrics to CloudWatch namespace `AirQuality/Pipeline`.
- `athena/create_tables.sql` — DDL for the `des_moines.dq_metrics` Athena table. Partitioned by date.

### Dashboard

`dashboard.py` — Streamlit app that reads directly from CloudWatch and S3 via boto3.

| Section | What it shows |
|---------|--------------|
| Pipeline Health | 4 KPI cards: Worst freshness, Lambda success rate, Lambda errors, MTD cost |
| Instrument Status | Per-instrument cards with Bronze files/size, freshness badge, location |
| Bronze Landing Trend | Time series of Bronze file count per instrument (24h) |
| Lambda Health | Invocation bar chart + duration line chart |

Auto-refreshes every 30 seconds.

## Setup

### 1. Field Laptop (data pipeline)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python schedule_upload_co2.py
```

### 2. Dashboard

```bash
source .venv/bin/activate
streamlit run dashboard.py
```

Opens at `http://localhost:8501`.

### 3. AWS Lambda

1. Create a Lambda function with Python 3.12 runtime.
2. Upload `lambda/dq_collector.py` as the handler (`dq_collector.lambda_handler`).
3. Set environment variables: `S3_BUCKET=des-moines-test`.
4. Attach an IAM role with S3 read/write and CloudWatch put permissions.
5. Create an EventBridge rule to trigger every 15 minutes.

### 4. Athena

Run the DDL in `athena/create_tables.sql` in the Athena console, then:
```sql
MSCK REPAIR TABLE des_moines.dq_metrics;
```

## Security

- `aws_creds.json` is gitignored, never committed.
- Lambda uses its execution role for S3/CloudWatch access.

## Cost Estimate (research scale)

| Service | Monthly Cost |
|---------|-------------|
| S3 storage | < $0.50 |
| Athena queries | < $0.10 |
| Lambda | Free tier |
| **Total** | **< $1/month** |
