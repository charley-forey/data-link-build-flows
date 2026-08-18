-- QuickBooks: accounts payable and labour hours.
--
-- Phase 2 additions. Separate from 11_qbo_silver.sql because they serve
-- different reports - cash forecasting and capacity planning - and keeping them
-- apart means a change to one cannot break the WIP spine.

-- ---------------------------------------------------------------- AP
--
-- The other half of the cash picture. Same aging shape as AR so the two can sit
-- side by side on a page. Sign convention is deliberately NOT applied here:
-- this table states what is OWED as a positive number, and the cash forecast
-- negates it. Baking the sign in makes every ad-hoc query a guess about which
-- direction the number points.

CREATE OR REPLACE TABLE dl_silver_qbo_bills_header AS
SELECT
    CAST(get_json_object(payload, '$.Id') AS STRING)      AS qbo_bill_id,
    TRIM(get_json_object(payload, '$.DocNumber'))         AS doc_number,
    CAST(get_json_object(payload, '$.VendorRef.value') AS STRING) AS qbo_vendor_id,
    TRIM(get_json_object(payload, '$.VendorRef.name'))    AS vendor_name,
    CAST(SUBSTR(get_json_object(payload, '$.TxnDate'), 1, 10) AS DATE) AS bill_date,
    CAST(SUBSTR(get_json_object(payload, '$.DueDate'), 1, 10) AS DATE) AS due_date,
    CAST(COALESCE(get_json_object(payload, '$.TotalAmt'), '0') AS DOUBLE) AS total_amount,
    CAST(COALESCE(get_json_object(payload, '$.Balance'), '0') AS DOUBLE) AS open_balance,
    _ingested_at,
    _batch_id
FROM dl_bronze_qbo_bills
WHERE get_json_object(payload, '$.Id') IS NOT NULL;

CREATE OR REPLACE TABLE dl_silver_qbo_ap_open_items AS
SELECT
    qbo_bill_id,
    doc_number,
    qbo_vendor_id,
    vendor_name,
    bill_date,
    due_date,
    total_amount,
    open_balance,
    DATEDIFF(CURRENT_DATE, due_date)              AS days_past_due,
    CASE
        WHEN due_date IS NULL                          THEN 'Unknown'
        WHEN DATEDIFF(CURRENT_DATE, due_date) <= 0     THEN 'Current'
        WHEN DATEDIFF(CURRENT_DATE, due_date) <= 30    THEN '1-30'
        WHEN DATEDIFF(CURRENT_DATE, due_date) <= 60    THEN '31-60'
        WHEN DATEDIFF(CURRENT_DATE, due_date) <= 90    THEN '61-90'
        ELSE '90+'
    END                                            AS aging_bucket,
    _ingested_at,
    _batch_id
FROM dl_silver_qbo_bills_header
WHERE open_balance > 0;

-- ---------------------------------------------------------------- labour
--
-- Hours, for capacity planning.
--
-- QuickBooks records two rates and they are not interchangeable: CostRate is
-- what the hour costs the company, HourlyRate is what the client is billed.
-- Only the cost rate belongs in a margin or capacity calculation; using the
-- billing rate inflates cost by the markup and makes every job look worse.

CREATE OR REPLACE TABLE dl_silver_qbo_time_activities AS
SELECT
    CAST(get_json_object(payload, '$.Id') AS STRING)      AS time_activity_id,
    CAST(get_json_object(payload, '$.CustomerRef.value') AS STRING) AS qbo_customer_id,
    CAST(COALESCE(
        get_json_object(payload, '$.EmployeeRef.value'),
        get_json_object(payload, '$.VendorRef.value')) AS STRING)   AS worker_id,
    TRIM(COALESCE(
        get_json_object(payload, '$.EmployeeRef.name'),
        get_json_object(payload, '$.VendorRef.name')))              AS worker_name,
    -- NameOf distinguishes an employee from a subcontractor, which is the
    -- difference between owned capacity and bought capacity.
    TRIM(get_json_object(payload, '$.NameOf'))            AS worker_type,
    TRIM(get_json_object(payload, '$.BillableStatus'))    AS billable_status,
    CASE WHEN CAST(SUBSTR(get_json_object(payload, '$.TxnDate'), 1, 10) AS DATE) < DATE '1990-01-01'
         THEN NULL ELSE CAST(SUBSTR(get_json_object(payload, '$.TxnDate'), 1, 10) AS DATE) END
                                                          AS activity_date,
    -- Hours and Minutes are separate fields; a 90-minute entry is Hours=1,
    -- Minutes=30. Reading Hours alone silently loses a third of the time.
    CAST(COALESCE(get_json_object(payload, '$.Hours'), '0') AS DOUBLE)
        + CAST(COALESCE(get_json_object(payload, '$.Minutes'), '0') AS DOUBLE) / 60.0
                                                          AS hours,
    CAST(COALESCE(get_json_object(payload, '$.CostRate'), '0') AS DOUBLE)   AS cost_rate,
    CAST(COALESCE(get_json_object(payload, '$.HourlyRate'), '0') AS DOUBLE) AS billing_rate,
    _ingested_at,
    _batch_id
FROM dl_bronze_qbo_time_activities
WHERE get_json_object(payload, '$.Id') IS NOT NULL;
