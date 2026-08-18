-- fct_Aging and fct_CashForecast.
--
-- Receivables, payables, and the near-term cash position they imply.

-- ---------------------------------------------------------------- aging
--
-- ONE TABLE, TWO LEDGERS, discriminated by Ledger. AR and AP have identical
-- shape and are always read together, so two near-identical tables would mean
-- every cash question is a UNION written by hand at query time.
--
-- Amounts are POSITIVE in both arms. This table states magnitude: what is owed
-- to us, and what we owe. Direction is applied where direction is meant - in
-- the cash forecast below. Signing it here would make "total aging" a
-- meaningless number that happens to compute.

CREATE OR REPLACE TABLE fct_Aging AS
SELECT
    'AR'                                        AS Ledger,
    CONCAT('AR|', a.document_id)                AS AgingKey,
    a.document_id                               AS DocumentId,
    a.doc_number                                AS DocNumber,
    a.counterparty_id                           AS CounterpartyId,
    COALESCE(a.counterparty_name, 'Unknown')    AS CounterpartyName,
    x.procore_project_id                        AS ProjectKey,
    a.document_date                             AS DocumentDate,
    a.due_date                                  AS DueDate,
    CAST(a.total_amount AS DOUBLE)              AS TotalAmount,
    CAST(a.open_balance AS DOUBLE)              AS OpenBalance,
    a.days_past_due                             AS DaysPastDue,
    a.aging_bucket                              AS AgingBucket,
    -- Sort key, because "1-30" sorts before "Current" alphabetically and a
    -- bucket chart in the wrong order is worse than no chart.
    CASE a.aging_bucket
        WHEN 'Current' THEN 1 WHEN '1-30' THEN 2 WHEN '31-60' THEN 3
        WHEN '61-90'   THEN 4 WHEN '90+'  THEN 5 ELSE 9 END AS AgingBucketSort,
    CASE WHEN a.days_past_due > 0 THEN TRUE ELSE FALSE END  AS IsOverdue,
    CASE WHEN a.due_date BETWEEN DATE '2015-01-01' AND DATE '2035-12-31'
         THEN make_date(year(a.due_date), month(a.due_date), 1) END AS MonthStart
FROM sv_ar_open a
LEFT JOIN sv_crosswalk x ON x.qbo_customer_id = a.counterparty_id AND x.is_mapped

UNION ALL

SELECT
    'AP',
    CONCAT('AP|', p.document_id),
    p.document_id,
    p.doc_number,
    p.counterparty_id,
    COALESCE(p.counterparty_name, 'Unknown'),
    CAST(NULL AS STRING),   -- bills are vendor-scoped; the job link is on the line, not the header
    p.document_date,
    p.due_date,
    CAST(p.total_amount AS DOUBLE),
    CAST(p.open_balance AS DOUBLE),
    p.days_past_due,
    p.aging_bucket,
    CASE p.aging_bucket
        WHEN 'Current' THEN 1 WHEN '1-30' THEN 2 WHEN '31-60' THEN 3
        WHEN '61-90'   THEN 4 WHEN '90+'  THEN 5 ELSE 9 END,
    CASE WHEN p.days_past_due > 0 THEN TRUE ELSE FALSE END,
    CASE WHEN p.due_date BETWEEN DATE '2015-01-01' AND DATE '2035-12-31'
         THEN make_date(year(p.due_date), month(p.due_date), 1) END
FROM sv_ap_open p;

-- ---------------------------------------------------------------- cash forecast
--
-- Grain: one row per week, per flow.
--
-- WHAT THIS IS: committed cash movement only - invoices already raised and
-- bills already received, placed in the week they fall due. Nothing here is
-- modelled or predicted.
--
-- WHAT THIS IS NOT: a forecast of work not yet billed. Turning backlog into
-- expected cash needs a billing schedule and collection assumptions the
-- Controller has not given us, and inventing them would produce a confident
-- line on a chart with nothing behind it. Backlog is reported separately, and
-- the gap between the two is the honest answer.
--
-- ponytail: due-date bucketing, no collection-probability curve. A real
-- contractor collects late and the pattern is measurable from payment history -
-- add that once there is enough history to measure, not before.

CREATE OR REPLACE TABLE fct_CashForecast AS
WITH weeks AS (
    -- 26 weeks forward and 4 back. Overdue items are real cash that has not
    -- arrived, so they belong on the chart rather than being dropped for
    -- sitting in the past.
    --
    -- Weeks are identified with date_trunc('WEEK', ...) rather than a
    -- dayofweek() comparison. Spark numbers Sunday as 1 and DuckDB numbers it
    -- as 0, so `dayofweek(d) = 2` means Monday in one engine and Tuesday in the
    -- other - a magic number that silently shifts every bucket by a day
    -- depending on where it runs. date_trunc starts the week on Monday in both.
    SELECT Date AS WeekStart
    FROM dim_Date
    WHERE Date = date_trunc('WEEK', Date)
      AND Date BETWEEN date_trunc('WEEK', CURRENT_DATE) - INTERVAL 28 DAYS
                   AND date_trunc('WEEK', CURRENT_DATE) + INTERVAL 182 DAYS
),
flows AS (
    SELECT
        'Collections'                       AS Flow,
        CAST(a.OpenBalance AS DOUBLE)       AS Amount,
        a.DueDate                           AS DueDate,
        a.IsOverdue                         AS IsOverdue
    FROM fct_Aging a
    WHERE a.Ledger = 'AR' AND a.DueDate IS NOT NULL

    UNION ALL

    SELECT
        'Payments',
        -- NEGATIVE here, and only here. fct_Aging states magnitude; this is
        -- where money leaving becomes a negative number.
        CAST(-a.OpenBalance AS DOUBLE),
        a.DueDate,
        a.IsOverdue
    FROM fct_Aging a
    WHERE a.Ledger = 'AP' AND a.DueDate IS NOT NULL
),
bucketed AS (
    SELECT
        -- Anything already overdue lands in the CURRENT week: it is due now,
        -- and burying it in a past week takes real unpaid cash off the chart.
        GREATEST(
            date_trunc('WEEK', f.DueDate),
            date_trunc('WEEK', CURRENT_DATE)
        )                                                     AS WeekStart,
        f.Flow,
        f.Amount,
        f.IsOverdue
    FROM flows f
)
SELECT
    w.WeekStart                                       AS WeekStart,
    COALESCE(b.Flow, 'Collections')                   AS Flow,
    CAST(COALESCE(SUM(b.Amount), 0) AS DOUBLE)        AS Amount,
    CAST(COALESCE(SUM(CASE WHEN b.IsOverdue THEN b.Amount ELSE 0 END), 0) AS DOUBLE)
                                                      AS OverdueAmount,
    COUNT(b.Amount)                                   AS DocumentCount,
    CASE WHEN w.WeekStart < CURRENT_DATE THEN TRUE ELSE FALSE END AS IsPast
FROM weeks w
LEFT JOIN bucketed b ON b.WeekStart = w.WeekStart
GROUP BY w.WeekStart, COALESCE(b.Flow, 'Collections'),
         CASE WHEN w.WeekStart < CURRENT_DATE THEN TRUE ELSE FALSE END;

-- ---------------------------------------------------------------- labour
--
-- Grain: one time entry.
--
-- Cost uses CostRate, never HourlyRate. HourlyRate is what the client is
-- billed; using it as cost inflates every job by the markup.

CREATE OR REPLACE TABLE fct_LabourHours AS
SELECT
    t.time_activity_id                          AS LabourKey,
    x.procore_project_id                        AS ProjectKey,
    COALESCE(t.worker_id, '0')                  AS WorkerKey,
    COALESCE(t.worker_name, 'Unknown')          AS WorkerName,
    COALESCE(t.worker_type, 'Unknown')          AS WorkerType,
    COALESCE(t.billable_status, 'Unknown')      AS BillableStatus,
    t.activity_date                             AS ActivityDate,
    CASE WHEN t.activity_date BETWEEN DATE '2015-01-01' AND DATE '2035-12-31'
         THEN make_date(year(t.activity_date), month(t.activity_date), 1) END AS MonthStart,
    CAST(t.hours AS DOUBLE)                     AS Hours,
    CAST(t.hours * t.cost_rate AS DOUBLE)       AS LabourCost,
    CAST(t.hours * t.billing_rate AS DOUBLE)    AS BillableValue,
    CASE WHEN UPPER(COALESCE(t.billable_status, '')) LIKE 'BILLABLE%'
         THEN TRUE ELSE FALSE END               AS IsBillable
FROM sv_time_activities t
LEFT JOIN sv_crosswalk x ON x.qbo_customer_id = t.qbo_customer_id AND x.is_mapped;
