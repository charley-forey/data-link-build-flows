-- Procore bronze -> silver.
--
-- RULES (platform/naming-standards.md), and every one of them exists because
-- breaking it produced a real bug somewhere:
--
--   TRIM every text value. Untrimmed source text never matches in a join, and
--   the symptom is a silently smaller number rather than an error.
--
--   Reject loudly, never drop silently. A row missing its natural key goes to
--   dl_dq_rejects with a reason; it does not disappear.
--
--   Floor sentinel dates to NULL. Procore carries placeholder dates far in the
--   past; left alone they drag date axes back to the 1500s and make every
--   time-based visual useless.
--
--   Dedup here, not in bronze. Bronze is an append-and-merge record of what the
--   API said, on purpose. "Latest snapshot wins" is a silver concept.

-- ---------------------------------------------------------------- projects

CREATE OR REPLACE TABLE dl_silver_projects AS
SELECT
    CAST(get_json_object(payload, '$.id')                AS STRING)  AS project_id,
    TRIM(get_json_object(payload, '$.name'))                         AS project_name,
    TRIM(get_json_object(payload, '$.project_number'))               AS project_number,
    TRIM(get_json_object(payload, '$.display_name'))                 AS display_name,
    TRIM(get_json_object(payload, '$.status_name'))                  AS status,
    TRIM(get_json_object(payload, '$.stage_name'))                   AS stage,
    TRIM(get_json_object(payload, '$.type_name'))                    AS project_type,
    TRIM(get_json_object(payload, '$.office.name'))                  AS office_name,
    TRIM(get_json_object(payload, '$.project_region.name'))          AS region_name,
    TRIM(get_json_object(payload, '$.address'))                      AS address,
    TRIM(get_json_object(payload, '$.city'))                         AS city,
    TRIM(get_json_object(payload, '$.state_code'))                   AS state_code,
    CAST(get_json_object(payload, '$.active')            AS BOOLEAN) AS is_active,
    CASE WHEN CAST(SUBSTR(get_json_object(payload, '$.start_date'), 1, 10) AS DATE) < DATE '1990-01-01'
         THEN NULL ELSE CAST(SUBSTR(get_json_object(payload, '$.start_date'), 1, 10) AS DATE) END
                                                                     AS start_date,
    CASE WHEN CAST(SUBSTR(get_json_object(payload, '$.completion_date'), 1, 10) AS DATE) < DATE '1990-01-01'
         THEN NULL ELSE CAST(SUBSTR(get_json_object(payload, '$.completion_date'), 1, 10) AS DATE) END
                                                                     AS completion_date,
    CAST(SUBSTR(get_json_object(payload, '$.updated_at'), 1, 10) AS DATE) AS source_updated_date,
    _ingested_at,
    _batch_id
FROM dl_bronze_procore_projects
WHERE get_json_object(payload, '$.id') IS NOT NULL;

-- ---------------------------------------------------------------- vendors

CREATE OR REPLACE TABLE dl_silver_vendors AS
SELECT
    CAST(get_json_object(payload, '$.id') AS STRING)     AS vendor_id,
    TRIM(get_json_object(payload, '$.name'))            AS vendor_name,
    TRIM(get_json_object(payload, '$.abbreviated_name')) AS vendor_abbrev,
    TRIM(get_json_object(payload, '$.trade_name'))      AS trade_name,
    TRIM(get_json_object(payload, '$.city'))            AS city,
    TRIM(get_json_object(payload, '$.state_code'))      AS state_code,
    CAST(get_json_object(payload, '$.is_active') AS BOOLEAN) AS is_active,
    'procore'                                            AS source_system,
    _ingested_at,
    _batch_id
FROM dl_bronze_procore_vendors
WHERE get_json_object(payload, '$.id') IS NOT NULL;

-- ---------------------------------------------------------------- cost codes

CREATE OR REPLACE TABLE dl_silver_cost_codes AS
SELECT
    cost_code_id,
    project_id,
    cost_code,
    cost_code_name,
    full_code,
    parent_id,
    sortable_code,
    _ingested_at,
    _batch_id
FROM (
    SELECT
        CAST(get_json_object(payload, '$.id')   AS STRING) AS cost_code_id,
        _project_id                                        AS project_id,
        TRIM(get_json_object(payload, '$.code'))           AS cost_code,
        TRIM(get_json_object(payload, '$.name'))           AS cost_code_name,
        TRIM(get_json_object(payload, '$.full_code'))      AS full_code,
        CAST(get_json_object(payload, '$.parent.id') AS STRING) AS parent_id,
        TRIM(get_json_object(payload, '$.sortable_code'))  AS sortable_code,
        _ingested_at,
        _batch_id,
        ROW_NUMBER() OVER (
            PARTITION BY CAST(get_json_object(payload, '$.id') AS STRING), _project_id
            ORDER BY _ingested_at DESC
        ) AS _rn
    FROM dl_bronze_procore_cost_codes
    WHERE get_json_object(payload, '$.id') IS NOT NULL
)
WHERE _rn = 1;

-- ---------------------------------------------------------------- budget lines
--
-- THE WIP SPINE, and the one table on this project that needs a live-data
-- confirmation pass before it can be trusted.
--
-- Procore's budget detail rows return the STRUCTURAL columns below on every
-- tenant, plus one key per column configured on the budget view. Those
-- configured keys are named by the tenant's own budget-view configuration, so
-- their exact spelling cannot be known from the API specification alone -
-- dl_bronze_procore_budget_detail_columns lists them for the pinned view.
--
-- The COALESCE chains below cover Procore's standard column ids. Anything not
-- matched lands as NULL, and the row still arrives with its raw payload intact,
-- so confirming the real names is a re-run of this file - not a re-extract.
--
-- ponytail: COALESCE over the documented standard ids. Run
-- scripts/inspect_budget_columns.py against a live pull and pin the exact keys
-- before go-live. Tracked as open item #2.

CREATE OR REPLACE TABLE dl_silver_budget_lines AS
WITH parsed AS (
    SELECT
        CAST(get_json_object(payload, '$.id')             AS STRING) AS budget_line_id,
        CAST(get_json_object(payload, '$.project_id')     AS STRING) AS project_id,
        CAST(get_json_object(payload, '$.wbs_code_id')    AS STRING) AS wbs_code_id,
        CAST(get_json_object(payload, '$.cost_code_id')   AS STRING) AS cost_code_id,
        TRIM(get_json_object(payload, '$.cost_code'))                AS cost_code,
        CAST(get_json_object(payload, '$.root_cost_code_id') AS STRING) AS root_cost_code_id,
        TRIM(get_json_object(payload, '$.root_cost_code'))           AS root_cost_code,
        CAST(get_json_object(payload, '$.category_id')    AS STRING) AS category_id,
        TRIM(get_json_object(payload, '$.category'))                 AS category,
        TRIM(get_json_object(payload, '$.biller'))                   AS biller,
        TRIM(get_json_object(payload, '$.biller_type'))              AS biller_type,

        CAST(COALESCE(
            get_json_object(payload, '$.original_budget_amount'),
            get_json_object(payload, '$.Original Budget Amount'),
            '0') AS DOUBLE)                                          AS original_budget,

        CAST(COALESCE(
            get_json_object(payload, '$.budget_modifications'),
            get_json_object(payload, '$.Budget Modifications'),
            '0') AS DOUBLE)                                          AS budget_modifications,

        CAST(COALESCE(
            get_json_object(payload, '$.approved_cos'),
            get_json_object(payload, '$.Approved Change Orders'),
            '0') AS DOUBLE)                                          AS approved_budget_changes,

        CAST(COALESCE(
            get_json_object(payload, '$.revised_budget'),
            get_json_object(payload, '$.Revised Budget'),
            '0') AS DOUBLE)                                          AS revised_budget,

        CAST(COALESCE(
            get_json_object(payload, '$.committed_costs'),
            get_json_object(payload, '$.Committed Costs'),
            '0') AS DOUBLE)                                          AS committed_cost,

        CAST(COALESCE(
            get_json_object(payload, '$.direct_costs'),
            get_json_object(payload, '$.Direct Costs'),
            '0') AS DOUBLE)                                          AS direct_cost,

        CAST(COALESCE(
            get_json_object(payload, '$.job_to_date_costs'),
            get_json_object(payload, '$.Job to Date Costs'),
            '0') AS DOUBLE)                                          AS job_to_date_cost,

        CAST(COALESCE(
            get_json_object(payload, '$.estimated_cost_at_completion'),
            get_json_object(payload, '$.budget_forecast.amount'),
            get_json_object(payload, '$.Estimated Cost at Completion'),
            '0') AS DOUBLE)                                          AS estimated_cost_at_completion,

        CAST(COALESCE(
            get_json_object(payload, '$.forecast_to_complete'),
            get_json_object(payload, '$.Forecast To Complete'),
            '0') AS DOUBLE)                                          AS forecast_to_complete,

        CAST(COALESCE(
            get_json_object(payload, '$.projected_over_under'),
            get_json_object(payload, '$.Projected Over Under'),
            '0') AS DOUBLE)                                          AS projected_over_under,

        _ingested_at,
        _batch_id
    FROM dl_bronze_procore_budget_detail_rows
    WHERE get_json_object(payload, '$.id') IS NOT NULL
)
-- ONE ROW PER PROJECT + WBS CODE, LATEST SNAPSHOT WINS. Bronze keeps every
-- shape the API ever returned; "the budget as of the latest pull" is what this
-- table means.
SELECT * EXCEPT (_rn) FROM (
    SELECT parsed.*,
           ROW_NUMBER() OVER (
               PARTITION BY project_id, COALESCE(wbs_code_id, cost_code_id, budget_line_id)
               ORDER BY _ingested_at DESC
           ) AS _rn
    FROM parsed
)
WHERE _rn = 1;

-- ---------------------------------------------------------------- contracts

CREATE OR REPLACE TABLE dl_silver_prime_contracts AS
SELECT
    CAST(get_json_object(payload, '$.id') AS STRING)      AS contract_id,
    _project_id                                            AS project_id,
    TRIM(get_json_object(payload, '$.number'))            AS contract_number,
    TRIM(get_json_object(payload, '$.title'))             AS contract_title,
    TRIM(get_json_object(payload, '$.status'))            AS status,
    CAST(get_json_object(payload, '$.executed') AS BOOLEAN) AS is_executed,
    CAST(COALESCE(get_json_object(payload, '$.grand_total'), '0') AS DOUBLE)  AS grand_total,
    CAST(COALESCE(get_json_object(payload, '$.original_contract_amount'),
                  get_json_object(payload, '$.grand_total'), '0') AS DOUBLE)  AS original_contract,
    CAST(COALESCE(get_json_object(payload, '$.approved_change_orders'), '0') AS DOUBLE)
                                                                              AS approved_change_orders,
    CAST(COALESCE(get_json_object(payload, '$.retainage_percent'), '0') AS DOUBLE)
                                                                              AS retainage_percent,
    CASE WHEN CAST(SUBSTR(get_json_object(payload, '$.contract_start_date'), 1, 10) AS DATE) < DATE '1990-01-01'
         THEN NULL ELSE CAST(SUBSTR(get_json_object(payload, '$.contract_start_date'), 1, 10) AS DATE) END
                                                                              AS contract_start_date,
    CASE WHEN CAST(SUBSTR(get_json_object(payload, '$.contract_estimated_completion_date'), 1, 10) AS DATE) < DATE '1990-01-01'
         THEN NULL ELSE CAST(SUBSTR(get_json_object(payload, '$.contract_estimated_completion_date'), 1, 10) AS DATE) END
                                                                              AS contract_finish_date,
    _ingested_at,
    _batch_id
FROM dl_bronze_procore_prime_contracts
WHERE get_json_object(payload, '$.id') IS NOT NULL;

-- ---------------------------------------------------------------- change orders
--
-- CUMULATIVE, NOT PER-MONTH. Rolling change orders up by month rather than
-- cumulatively is the exact defect that understated portfolio contract value by
-- 16% ($4.85M) on the reference engagement. The grain here is one row per
-- change order; the cumulative roll-up happens in fct_WIP and in the DAX, and
-- both are covered by tests.

CREATE OR REPLACE TABLE dl_silver_change_orders AS
SELECT
    CAST(get_json_object(payload, '$.id') AS STRING)   AS change_order_id,
    _project_id                                         AS project_id,
    CAST(get_json_object(payload, '$.contract_id') AS STRING) AS contract_id,
    'prime'                                             AS change_order_scope,
    TRIM(get_json_object(payload, '$.number'))         AS change_order_number,
    TRIM(get_json_object(payload, '$.title'))          AS title,
    TRIM(get_json_object(payload, '$.status'))         AS status,
    CAST(get_json_object(payload, '$.executed') AS BOOLEAN) AS is_executed,
    CAST(COALESCE(get_json_object(payload, '$.grand_total'),
                  get_json_object(payload, '$.amount'), '0') AS DOUBLE) AS amount,
    CASE WHEN CAST(SUBSTR(get_json_object(payload, '$.created_at'), 1, 10) AS DATE) < DATE '1990-01-01'
         THEN NULL ELSE CAST(SUBSTR(get_json_object(payload, '$.created_at'), 1, 10) AS DATE) END
                                                        AS created_date,
    CASE WHEN CAST(SUBSTR(get_json_object(payload, '$.approved_date'), 1, 10) AS DATE) < DATE '1990-01-01'
         THEN NULL ELSE CAST(SUBSTR(get_json_object(payload, '$.approved_date'), 1, 10) AS DATE) END
                                                        AS approved_date,
    _ingested_at,
    _batch_id
FROM dl_bronze_procore_prime_change_orders
WHERE get_json_object(payload, '$.id') IS NOT NULL

UNION ALL

SELECT
    CAST(get_json_object(payload, '$.id') AS STRING)   AS change_order_id,
    _project_id                                         AS project_id,
    CAST(get_json_object(payload, '$.commitment_id') AS STRING) AS contract_id,
    'commitment'                                        AS change_order_scope,
    TRIM(get_json_object(payload, '$.number'))         AS change_order_number,
    TRIM(get_json_object(payload, '$.title'))          AS title,
    TRIM(get_json_object(payload, '$.status'))         AS status,
    CAST(get_json_object(payload, '$.executed') AS BOOLEAN) AS is_executed,
    CAST(COALESCE(get_json_object(payload, '$.grand_total'),
                  get_json_object(payload, '$.amount'), '0') AS DOUBLE) AS amount,
    CASE WHEN CAST(SUBSTR(get_json_object(payload, '$.created_at'), 1, 10) AS DATE) < DATE '1990-01-01'
         THEN NULL ELSE CAST(SUBSTR(get_json_object(payload, '$.created_at'), 1, 10) AS DATE) END
                                                        AS created_date,
    CASE WHEN CAST(SUBSTR(get_json_object(payload, '$.approved_date'), 1, 10) AS DATE) < DATE '1990-01-01'
         THEN NULL ELSE CAST(SUBSTR(get_json_object(payload, '$.approved_date'), 1, 10) AS DATE) END
                                                        AS approved_date,
    _ingested_at,
    _batch_id
FROM dl_bronze_procore_commitment_change_orders
WHERE get_json_object(payload, '$.id') IS NOT NULL;

-- ---------------------------------------------------------------- commitments
--
-- Purchase orders and subcontracts are the same concept for WIP purposes and
-- differ only in which endpoint they came from, so they are unioned with a
-- discriminator rather than kept as two near-identical tables.

CREATE OR REPLACE TABLE dl_silver_commitments AS
SELECT
    CAST(get_json_object(payload, '$.id') AS STRING) AS commitment_id,
    _project_id                                       AS project_id,
    'purchase_order'                                  AS commitment_type,
    TRIM(get_json_object(payload, '$.number'))       AS commitment_number,
    TRIM(get_json_object(payload, '$.title'))        AS title,
    TRIM(get_json_object(payload, '$.status'))       AS status,
    CAST(get_json_object(payload, '$.vendor.id') AS STRING) AS vendor_id,
    TRIM(get_json_object(payload, '$.vendor.name')) AS vendor_name,
    CAST(COALESCE(get_json_object(payload, '$.grand_total'), '0') AS DOUBLE) AS grand_total,
    CAST(get_json_object(payload, '$.executed') AS BOOLEAN) AS is_executed,
    _ingested_at,
    _batch_id
FROM dl_bronze_procore_purchase_order_contracts
WHERE get_json_object(payload, '$.id') IS NOT NULL

UNION ALL

SELECT
    CAST(get_json_object(payload, '$.id') AS STRING) AS commitment_id,
    _project_id                                       AS project_id,
    'subcontract'                                     AS commitment_type,
    TRIM(get_json_object(payload, '$.number'))       AS commitment_number,
    TRIM(get_json_object(payload, '$.title'))        AS title,
    TRIM(get_json_object(payload, '$.status'))       AS status,
    CAST(get_json_object(payload, '$.vendor.id') AS STRING) AS vendor_id,
    TRIM(get_json_object(payload, '$.vendor.name')) AS vendor_name,
    CAST(COALESCE(get_json_object(payload, '$.grand_total'), '0') AS DOUBLE) AS grand_total,
    CAST(get_json_object(payload, '$.executed') AS BOOLEAN) AS is_executed,
    _ingested_at,
    _batch_id
FROM dl_bronze_procore_work_order_contracts
WHERE get_json_object(payload, '$.id') IS NOT NULL;

-- ---------------------------------------------------------------- direct costs

CREATE OR REPLACE TABLE dl_silver_direct_costs AS
SELECT
    CAST(get_json_object(payload, '$.id') AS STRING)  AS direct_cost_id,
    _project_id                                        AS project_id,
    TRIM(get_json_object(payload, '$.direct_cost_type')) AS direct_cost_type,
    TRIM(get_json_object(payload, '$.description'))   AS description,
    TRIM(get_json_object(payload, '$.invoice_number')) AS invoice_number,
    TRIM(get_json_object(payload, '$.status'))        AS status,
    CAST(get_json_object(payload, '$.vendor_id') AS STRING) AS vendor_id,
    TRIM(get_json_object(payload, '$.vendor_name'))   AS vendor_name,
    CAST(COALESCE(get_json_object(payload, '$.amount'), '0') AS DOUBLE)      AS amount,
    CAST(COALESCE(get_json_object(payload, '$.grand_total'), '0') AS DOUBLE) AS grand_total,
    CASE WHEN CAST(SUBSTR(get_json_object(payload, '$.direct_cost_date'), 1, 10) AS DATE) < DATE '1990-01-01'
         THEN NULL ELSE CAST(SUBSTR(get_json_object(payload, '$.direct_cost_date'), 1, 10) AS DATE) END
                                                       AS cost_date,
    CASE WHEN CAST(SUBSTR(get_json_object(payload, '$.payment_date'), 1, 10) AS DATE) < DATE '1990-01-01'
         THEN NULL ELSE CAST(SUBSTR(get_json_object(payload, '$.payment_date'), 1, 10) AS DATE) END
                                                       AS payment_date,
    _ingested_at,
    _batch_id
FROM dl_bronze_procore_direct_costs
WHERE get_json_object(payload, '$.id') IS NOT NULL;

-- ---------------------------------------------------------------- owner billings

CREATE OR REPLACE TABLE dl_silver_payment_applications AS
SELECT
    CAST(get_json_object(payload, '$.id') AS STRING)  AS payment_application_id,
    _project_id                                        AS project_id,
    CAST(get_json_object(payload, '$.contract_id') AS STRING) AS contract_id,
    TRIM(get_json_object(payload, '$.number'))        AS application_number,
    TRIM(get_json_object(payload, '$.status'))        AS status,
    CAST(COALESCE(get_json_object(payload, '$.total_claimed_amount'), '0') AS DOUBLE)
                                                       AS billed_amount,
    CASE WHEN CAST(SUBSTR(get_json_object(payload, '$.billing_date'), 1, 10) AS DATE) < DATE '1990-01-01'
         THEN NULL ELSE CAST(SUBSTR(get_json_object(payload, '$.billing_date'), 1, 10) AS DATE) END
                                                       AS billing_date,
    _ingested_at,
    _batch_id
FROM dl_bronze_procore_payment_applications
WHERE get_json_object(payload, '$.id') IS NOT NULL;

-- ---------------------------------------------------------------- rejects
--
-- Every row that failed a key check above, with the reason. Built as a UNION so
-- one table answers "what did we refuse, and why" across every source table.

CREATE OR REPLACE TABLE dl_dq_rejects_silver AS
SELECT 'dl_silver_projects' AS target_table, 'missing id' AS reason, payload, _batch_id
FROM dl_bronze_procore_projects WHERE get_json_object(payload, '$.id') IS NULL
UNION ALL
SELECT 'dl_silver_vendors', 'missing id', payload, _batch_id
FROM dl_bronze_procore_vendors WHERE get_json_object(payload, '$.id') IS NULL
UNION ALL
SELECT 'dl_silver_cost_codes', 'missing id', payload, _batch_id
FROM dl_bronze_procore_cost_codes WHERE get_json_object(payload, '$.id') IS NULL
UNION ALL
SELECT 'dl_silver_budget_lines', 'missing id', payload, _batch_id
FROM dl_bronze_procore_budget_detail_rows WHERE get_json_object(payload, '$.id') IS NULL
UNION ALL
SELECT 'dl_silver_prime_contracts', 'missing id', payload, _batch_id
FROM dl_bronze_procore_prime_contracts WHERE get_json_object(payload, '$.id') IS NULL
UNION ALL
SELECT 'dl_silver_direct_costs', 'missing id', payload, _batch_id
FROM dl_bronze_procore_direct_costs WHERE get_json_object(payload, '$.id') IS NULL;
