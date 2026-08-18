-- dim_Project - the conformed project across Procore, QuickBooks and HubSpot.
--
-- LATE-ARRIVING PROJECTS. Facts routinely reference projects that are not in
-- the crosswalk: a job created in Procore this morning, a project the Controller
-- has not mapped yet, a closed job that still carries cost. Those facts are
-- REAL. Dropping them understates budget, cost and contract value - and does it
-- silently, because a smaller number looks like a smaller number, not an error.
--
-- So the dimension is the UNION of the crosswalk and every project id actually
-- OBSERVED on a fact source, with IsInCrosswalk recording the difference. That
-- makes referential integrity hold BY CONSTRUCTION rather than by hope, and
-- turns "we are missing a mapping" into a visible row on a report page instead
-- of a quietly wrong total.
--
-- ProjectKey is the Procore project id, not an invented surrogate. Procore ids
-- are stable, unique, and already carried on every fact, so a surrogate would
-- add a lookup hop and buy nothing. It also keeps the key debuggable: a wrong
-- number in the report can be pasted straight into Procore's URL bar.

CREATE OR REPLACE TABLE dim_Project AS
WITH observed AS (
    SELECT DISTINCT project_id FROM sv_budget_lines          WHERE project_id IS NOT NULL
    UNION SELECT DISTINCT project_id FROM sv_prime_contracts WHERE project_id IS NOT NULL
    UNION SELECT DISTINCT project_id FROM sv_change_orders   WHERE project_id IS NOT NULL
    UNION SELECT DISTINCT project_id FROM sv_commitments     WHERE project_id IS NOT NULL
    UNION SELECT DISTINCT project_id FROM sv_direct_costs    WHERE project_id IS NOT NULL
    UNION SELECT DISTINCT project_id FROM sv_payment_applications WHERE project_id IS NOT NULL
),
all_projects AS (
    SELECT project_id FROM sv_projects WHERE project_id IS NOT NULL
    UNION SELECT project_id FROM observed
),
contracts AS (
    -- One project can hold several prime contracts. Aggregate to project grain
    -- here so the dimension stays one row per project; the per-contract detail
    -- lives on fct_Billing.
    SELECT
        project_id,
        CAST(SUM(original_contract) AS DOUBLE) AS OriginalContract,
        CAST(SUM(grand_total)       AS DOUBLE) AS ContractGrandTotal,
        MAX(retainage_percent)                 AS RetainagePercent,
        MIN(contract_start_date)               AS ContractStart,
        MAX(contract_finish_date)              AS ContractFinish,
        MAX(CASE WHEN is_executed THEN 1 ELSE 0 END) AS AnyExecuted
    FROM sv_prime_contracts
    GROUP BY project_id
)
SELECT
    a.project_id                                      AS ProjectKey,
    a.project_id                                      AS ProcoreProjectId,
    x.qbo_customer_id                                 AS QboCustomerId,
    x.hubspot_deal_id                                 AS HubspotDealId,
    p.project_number                                  AS ProjectNumber,
    -- A project observed only on a fact has no name row to join to. Naming it
    -- by its id keeps it selectable in a slicer instead of showing as (Blank),
    -- which reads as a bug rather than as a missing mapping.
    COALESCE(p.project_name, CONCAT('Project ', a.project_id)) AS ProjectName,
    COALESCE(p.status,       'Unknown')               AS ProjectStatus,
    COALESCE(p.stage,        'Unknown')               AS ProjectStage,
    COALESCE(p.project_type, 'Unknown')               AS ProjectType,
    COALESCE(p.office_name,  'Unassigned')            AS Office,
    COALESCE(p.region_name,  'Unassigned')            AS Region,
    p.city                                            AS City,
    p.state_code                                      AS StateCode,
    COALESCE(p.is_active, FALSE)                      AS IsActive,
    p.start_date                                      AS ProjectStart,
    p.completion_date                                 AS ProjectFinish,
    x.qbo_fully_qualified_name                        AS QboJobName,
    COALESCE(x.match_method, 'unmatched')             AS CrosswalkMethod,
    COALESCE(x.confidence, 0.00)                      AS CrosswalkConfidence,
    CAST(COALESCE(c.OriginalContract, 0)    AS DOUBLE) AS OriginalContract,
    CAST(COALESCE(c.ContractGrandTotal, 0)  AS DOUBLE) AS ContractGrandTotal,
    CAST(COALESCE(c.RetainagePercent, 0)    AS DOUBLE) AS RetainagePercent,
    c.ContractStart                                   AS ContractStart,
    c.ContractFinish                                  AS ContractFinish,
    CASE WHEN c.project_id IS NULL THEN FALSE ELSE TRUE END       AS HasPrimeContract,
    CASE WHEN COALESCE(c.AnyExecuted, 0) = 1 THEN TRUE ELSE FALSE END AS HasExecutedContract,
    -- The two flags the Unmapped Projects page filters on.
    CASE WHEN p.project_id IS NULL THEN FALSE ELSE TRUE END       AS IsInProcore,
    CASE WHEN COALESCE(x.is_mapped, FALSE) THEN TRUE ELSE FALSE END AS IsInCrosswalk
FROM all_projects a
LEFT JOIN sv_projects  p ON a.project_id = p.project_id
LEFT JOIN sv_crosswalk x ON a.project_id = x.procore_project_id
LEFT JOIN contracts    c ON a.project_id = c.project_id;
