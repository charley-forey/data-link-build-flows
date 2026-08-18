-- The three supporting dimensions: cost code, vendor, GL account.
--
-- Each one follows the same two rules, and both exist so that a fact never
-- loses a row to a missing lookup:
--
--   KEY 0 = 'Unassigned'. A fact whose dimension value cannot be resolved joins
--   to key 0 rather than to nothing. A visible "Unassigned" bar on a chart beats
--   a silently missing one - the first prompts a question, the second does not.
--
--   OBSERVED KEYS ARE UNIONED IN, same as dim_Project. If a cost transaction
--   references a cost code that no longer exists in Procore's list, the code is
--   still real and the cost is still real.

-- ---------------------------------------------------------------- cost code
--
-- Cost codes are PROJECT-SCOPED in Procore: two projects can both have a "16-100"
-- meaning different things. So the key is project + code, not code alone -
-- keying on code alone silently merges two projects' budgets under one row.

CREATE OR REPLACE TABLE dim_CostCode AS
WITH observed AS (
    SELECT DISTINCT project_id, cost_code_id, cost_code
    FROM sv_budget_lines
    WHERE cost_code_id IS NOT NULL
),
combined AS (
    SELECT
        project_id,
        cost_code_id,
        cost_code,
        cost_code_name,
        full_code
    FROM sv_cost_codes
    UNION
    SELECT
        o.project_id,
        o.cost_code_id,
        o.cost_code,
        CAST(NULL AS STRING) AS cost_code_name,
        CAST(NULL AS STRING) AS full_code
    FROM observed o
    WHERE NOT EXISTS (
        SELECT 1 FROM sv_cost_codes c
        WHERE c.cost_code_id = o.cost_code_id AND c.project_id = o.project_id
    )
),
deduped AS (
    -- Explicit column list, not `SELECT * EXCEPT (_rn)`: Fabric's Spark rejects
    -- the EXCEPT-star form.
    SELECT project_id, cost_code_id, cost_code, cost_code_name, full_code FROM (
        SELECT c.*, ROW_NUMBER() OVER (
            PARTITION BY project_id, cost_code_id
            -- Prefer the row that actually has a name.
            ORDER BY CASE WHEN cost_code_name IS NULL THEN 1 ELSE 0 END
        ) AS _rn
        FROM combined c
    ) WHERE _rn = 1
)
SELECT
    '0'                          AS CostCodeKey,
    CAST(NULL AS STRING)         AS ProjectKey,
    'Unassigned'                 AS CostCode,
    'Unassigned'                 AS CostCodeName,
    'Unassigned'                 AS FullCode,
    'Unassigned'                 AS RootCostCode
UNION ALL
SELECT
    CONCAT(COALESCE(project_id, 'X'), '|', cost_code_id) AS CostCodeKey,
    project_id                                           AS ProjectKey,
    COALESCE(cost_code, 'Unknown')                       AS CostCode,
    COALESCE(cost_code_name, COALESCE(cost_code, 'Unknown')) AS CostCodeName,
    COALESCE(full_code, cost_code)                       AS FullCode,
    -- Division roll-up: the segment before the first separator. "16-100" rolls
    -- up to "16". Lets the report group by division without a second table.
    SPLIT(COALESCE(cost_code, 'Unknown'), '[-.]')[0]     AS RootCostCode
FROM deduped;

-- ---------------------------------------------------------------- vendor
--
-- CONFORMED ACROSS TWO SYSTEMS. The same subcontractor exists in Procore (with
-- the commitment) and in QuickBooks (with the bill). They have different ids and
-- frequently different spellings.
--
-- Matching is on a normalised name and is deliberately PROPOSED, not asserted:
-- SourceSystem records where each row came from, and a vendor present in both
-- shows as 'both'. Nothing downstream joins on a vendor across systems - cost
-- attribution goes through the PROJECT crosswalk, not the vendor - so a wrong
-- vendor match affects a label, never a number.

CREATE OR REPLACE TABLE dim_Vendor AS
WITH normalised AS (
    SELECT
        vendor_id,
        vendor_name,
        UPPER(REGEXP_REPLACE(COALESCE(vendor_name, ''), '[^A-Za-z0-9]', '')) AS norm_name,
        'procore' AS source_system,
        is_active
    FROM sv_procore_vendors
    UNION ALL
    SELECT
        vendor_id,
        vendor_name,
        UPPER(REGEXP_REPLACE(COALESCE(vendor_name, ''), '[^A-Za-z0-9]', '')),
        'quickbooks',
        is_active
    FROM sv_qbo_vendors
),
grouped AS (
    SELECT
        norm_name,
        MAX(vendor_name) AS VendorName,
        MAX(CASE WHEN source_system = 'procore'    THEN vendor_id END) AS ProcoreVendorId,
        MAX(CASE WHEN source_system = 'quickbooks' THEN vendor_id END) AS QboVendorId,
        MAX(CASE WHEN is_active THEN 1 ELSE 0 END) AS AnyActive,
        COUNT(DISTINCT source_system)              AS SystemCount
    FROM normalised
    WHERE norm_name <> ''
    GROUP BY norm_name
)
SELECT '0' AS VendorKey, 'Unassigned' AS VendorName,
       CAST(NULL AS STRING) AS ProcoreVendorId, CAST(NULL AS STRING) AS QboVendorId,
       'none' AS SourceSystem, FALSE AS IsActive
UNION ALL
SELECT
    norm_name        AS VendorKey,
    VendorName,
    ProcoreVendorId,
    QboVendorId,
    CASE WHEN SystemCount > 1        THEN 'both'
         WHEN ProcoreVendorId IS NOT NULL THEN 'procore'
         ELSE 'quickbooks' END       AS SourceSystem,
    CASE WHEN AnyActive = 1 THEN TRUE ELSE FALSE END AS IsActive
FROM grouped;

-- ---------------------------------------------------------------- GL account

CREATE OR REPLACE TABLE dim_Account AS
SELECT '0' AS AccountKey, 'Unassigned' AS AccountName, 'Unassigned' AS AccountFullName,
       CAST(NULL AS STRING) AS AccountNumber, 'Unassigned' AS Classification,
       'Unassigned' AS AccountType, 'Unassigned' AS AccountSubType,
       FALSE AS IsJobCostAccount
UNION ALL
SELECT
    qbo_account_id                        AS AccountKey,
    COALESCE(account_name, 'Unknown')     AS AccountName,
    COALESCE(account_full_name, account_name) AS AccountFullName,
    account_number                        AS AccountNumber,
    COALESCE(classification, 'Unknown')   AS Classification,
    COALESCE(account_type, 'Unknown')     AS AccountType,
    COALESCE(account_sub_type, 'Unknown') AS AccountSubType,
    -- Which accounts count as job cost for the GL tie-out. Expense and COGS
    -- classifications only: including every account would make the tie-out
    -- compare job cost against the whole trial balance and never reconcile.
    CASE WHEN account_type IN ('Cost of Goods Sold', 'Expense', 'Other Expense')
         THEN TRUE ELSE FALSE END         AS IsJobCostAccount
FROM sv_qbo_accounts;
