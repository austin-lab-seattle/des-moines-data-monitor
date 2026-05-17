-- Athena DDL for Des Moines Data Monitor
-- Run these in the Athena console or via AWS CLI.
-- Database: des_moines
-- All tables read from S3 directly. No ETL required.

-- =============================================================
-- 1. Create database
-- =============================================================
CREATE DATABASE IF NOT EXISTS des_moines
COMMENT 'DEOHS air quality monitoring data'
LOCATION 's3://des-moines-test/';


-- =============================================================
-- 2. Data quality metrics (written by Lambda)
-- =============================================================
CREATE EXTERNAL TABLE IF NOT EXISTS des_moines.dq_metrics (
    collection_time        STRING    COMMENT 'UTC timestamp of metrics collection',
    instrument_id          STRING    COMMENT 'Unique instrument identifier',
    display_name           STRING    COMMENT 'Human-readable instrument name',
    location               STRING    COMMENT 'Deployment site',
    is_active              BOOLEAN   COMMENT 'Whether instrument is currently active',
    s3_batch_count         INT       COMMENT 'Number of batch files in S3 for this instrument',
    s3_total_bytes         BIGINT    COMMENT 'Total bytes stored in S3 for this instrument',
    latest_batch_time      STRING    COMMENT 'Timestamp of most recent batch upload',
    pipeline_status        STRING    COMMENT 'ok, error, degraded, inactive, or unknown',
    bronze_row_estimate    BIGINT    COMMENT 'Estimated row count in bronze layer',
    gold_row_count         BIGINT    COMMENT 'Row count in gold layer (0 until gold is built)',
    data_freshness_hours   DOUBLE    COMMENT 'Hours since last batch upload',
    checkpoint_offset      BIGINT    COMMENT 'Current byte offset in source file',
    rows_uploaded_last_run INT       COMMENT 'Rows uploaded in the most recent pipeline run',
    pending_uploads        INT       COMMENT 'Batches waiting to be uploaded'
)
PARTITIONED BY (dt STRING)
ROW FORMAT SERDE 'org.openx.data.jsonserde.JsonSerDe'
WITH SERDEPROPERTIES ('ignore.malformed.json' = 'true')
STORED AS TEXTFILE
LOCATION 's3://des-moines-test/licor/dq_metrics/'
TBLPROPERTIES ('has_encrypted_data' = 'false');

-- After creating the table, load partitions:
-- MSCK REPAIR TABLE des_moines.dq_metrics;


-- =============================================================
-- 3. Bronze layer batch inventory
--    Lists all raw batch files uploaded by the pipeline.
--    This uses S3 inventory, not the file contents.
-- =============================================================
-- Note: Athena cannot directly list S3 objects as a table.
-- Use the dq_metrics table for batch counts instead.
-- If you need per-file metadata, enable S3 Inventory on the bucket
-- and create an Athena table over the inventory output.


-- =============================================================
-- Useful queries for Grafana panels
-- =============================================================

-- Latest metrics snapshot (most recent collection)
-- SELECT * FROM des_moines.dq_metrics
-- WHERE dt = (SELECT MAX(dt) FROM des_moines.dq_metrics)
-- AND collection_time = (
--     SELECT MAX(collection_time) FROM des_moines.dq_metrics
--     WHERE dt = (SELECT MAX(dt) FROM des_moines.dq_metrics)
-- );

-- Active instruments count
-- SELECT COUNT(*) as active_count
-- FROM des_moines.dq_metrics
-- WHERE dt = (SELECT MAX(dt) FROM des_moines.dq_metrics)
-- AND collection_time = (SELECT MAX(collection_time) FROM des_moines.dq_metrics WHERE dt = (SELECT MAX(dt) FROM des_moines.dq_metrics))
-- AND is_active = true;

-- Batches over time per instrument
-- SELECT collection_time, instrument_id, s3_batch_count
-- FROM des_moines.dq_metrics
-- WHERE dt >= date_format(current_date - interval '7' day, '%Y-%m-%d')
-- ORDER BY collection_time;

-- Data freshness per instrument
-- SELECT instrument_id, display_name, data_freshness_hours
-- FROM des_moines.dq_metrics
-- WHERE dt = (SELECT MAX(dt) FROM des_moines.dq_metrics)
-- AND collection_time = (SELECT MAX(collection_time) FROM des_moines.dq_metrics WHERE dt = (SELECT MAX(dt) FROM des_moines.dq_metrics))
-- AND is_active = true;


-- =============================================================
-- 4. AWS Cost Metrics (written by Lambda via Cost Explorer API)
-- =============================================================
CREATE EXTERNAL TABLE IF NOT EXISTS des_moines.cost_metrics (
    date               STRING    COMMENT 'Date of the cost record (YYYY-MM-DD)',
    service            STRING    COMMENT 'AWS service name (e.g., Amazon S3, AWS Lambda)',
    cost_usd           DOUBLE    COMMENT 'Unblended cost in USD',
    collection_time    STRING    COMMENT 'UTC timestamp when cost data was collected'
)
PARTITIONED BY (dt STRING)
ROW FORMAT SERDE 'org.openx.data.jsonserde.JsonSerDe'
WITH SERDEPROPERTIES ('ignore.malformed.json' = 'true')
STORED AS TEXTFILE
LOCATION 's3://des-moines-test/licor/cost_metrics/'
TBLPROPERTIES ('has_encrypted_data' = 'false');

-- After creating: MSCK REPAIR TABLE des_moines.cost_metrics;


-- =============================================================
-- Cost queries for Grafana
-- =============================================================

-- Daily total cost (last 14 days)
-- SELECT date, SUM(cost_usd) AS total_cost
-- FROM des_moines.cost_metrics
-- WHERE dt = (SELECT MAX(dt) FROM des_moines.cost_metrics)
-- GROUP BY date ORDER BY date;

-- Cost breakdown by service
-- SELECT service, SUM(cost_usd) AS total_cost
-- FROM des_moines.cost_metrics
-- WHERE dt = (SELECT MAX(dt) FROM des_moines.cost_metrics)
-- GROUP BY service ORDER BY total_cost DESC;

-- Month-to-date total
-- SELECT ROUND(SUM(cost_usd), 2) AS mtd_cost
-- FROM des_moines.cost_metrics
-- WHERE dt = (SELECT MAX(dt) FROM des_moines.cost_metrics);
