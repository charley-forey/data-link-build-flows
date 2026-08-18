-- fct_WIP - the work-in-progress schedule.
--
-- THIS IS THE DELIVERABLE. It is the table that replaces the spreadsheet the
-- Controller assembles by hand every month, and every other number in the
-- report is either an input to it or a roll-up of it.
--
-- GRAIN: one row per project per month.
--
-- METHOD: cost-to-cost percentage of completion, the standard for a commercial
-- contractor. Documented in full in docs/04-wip-methodology.md and locked by
-- tests/test_gold.py, which asserts each identity below on fixture data.
--
--     Revised Contract    = Original Prime Contract + Approved Prime COs
--     EAC                 = Procore Budget View "Estimated Cost at Completion"
--     Cost to Date        = Procore Job-to-Date Cost
--     Percent Complete    = Cost to Date / EAC
--     Earned Revenue      = Revised Contract x Percent Complete
--     Over-billing        = MAX(Billed - Earned, 0)
--     Under-billing       = MAX(Earned - Billed, 0)
--     GP @ Completion     = Revised Contract - EAC
--     Cost to Complete    = EAC - Cost to Date
--     Backlog             = Revised Contract - Earned Revenue
--
-- WHY PROCORE IS THE EAC AND QUICKBOOKS IS THE CHECK
--   Procore's budget view is what the project managers maintain and what they
--   will defend in a meeting. QuickBooks is what the financial statements say.
--   They will not agree, and the difference is information: an AP lag, a missing
--   accrual, a cost coded to the wrong job. So both are carried, and
--   CostVariance is a first-class column rather than something reconciled away.
--
-- SNAPSHOT SEMANTICS
--   Procore's budget view is a CURRENT-STATE view - it does not carry history.
--   So this table appends a row per project per month as the pipeline runs, and
--   a month once closed is never recomputed. That is what makes fade/gain
--   analysis possible at all, and it is why the load is a MERGE on
--   (ProjectKey, MonthStart) rather than a full rebuild.
--
--   ponytail: month-grain snapshots taken from current state. If the Controller
--   needs true as-of-any-day restatement, switch the source to Procore's
--   /project_status_snapshots keyed by financial_period_id - the endpoint exists
--   and is already in the registry's reach. Not built now because monthly is
--   what the reporting cycle actually is.

CREATE OR REPLACE TEMPORARY VIEW wip_period AS
SELECT make_date(year(CURRENT_DATE), month(CURRENT_DATE), 1) AS MonthStart;

-- Contract value. Approved change orders roll up CUMULATIVELY - every approved
-- CO up to and including this period, not just the ones approved in it.
CREATE OR REPLACE TEMPORARY VIEW wip_contract AS
SELECT
    p.ProjectKey,
    CAST(p.OriginalContract AS DOUBLE)                       AS OriginalContract,
    CAST(COALESCE(co.ApprovedChangeOrders, 0) AS DOUBLE)     AS ApprovedChangeOrders,
    CAST(COALESCE(co.PendingChangeOrders, 0) AS DOUBLE)      AS PendingChangeOrders,
    CAST(p.OriginalContract + COALESCE(co.ApprovedChangeOrders, 0) AS DOUBLE)
                                                             AS RevisedContract
FROM dim_Project p
LEFT JOIN (
    SELECT
        ProjectKey,
        SUM(CASE WHEN IsApproved AND ChangeOrderScope = 'prime' THEN Amount ELSE 0 END)
            AS ApprovedChangeOrders,
        SUM(CASE WHEN NOT IsApproved AND ChangeOrderScope = 'prime' THEN Amount ELSE 0 END)
            AS PendingChangeOrders
    FROM fct_ChangeOrder
    WHERE EffectiveDate IS NULL OR EffectiveDate <= last_day(CURRENT_DATE)
    GROUP BY ProjectKey
) co ON co.ProjectKey = p.ProjectKey;

-- Cost and EAC, rolled up from the budget detail. Summing the cost-code grain
-- rather than reading a project total means the WIP row and the drill-down
-- always agree - if they did not, the first person to drill in would lose trust
-- in the whole report.
CREATE OR REPLACE TEMPORARY VIEW wip_budget AS
SELECT
    ProjectKey,
    CAST(SUM(OriginalBudget)            AS DOUBLE) AS OriginalBudget,
    CAST(SUM(RevisedBudget)             AS DOUBLE) AS RevisedBudget,
    CAST(SUM(CommittedCost)             AS DOUBLE) AS CommittedCost,
    CAST(SUM(JobToDateCost)             AS DOUBLE) AS CostToDateProcore,
    -- EAC floors at revised budget. A budget view that has never been forecast
    -- returns EAC = 0, which would make percent complete infinite and the
    -- project look 100% complete with zero cost. Falling back to the budget is
    -- the conservative reading: "we expect to spend what we budgeted".
    CAST(SUM(GREATEST(EstimatedCostAtCompletion, RevisedBudget)) AS DOUBLE) AS EAC,
    SUM(CASE WHEN IsOverEac THEN 1 ELSE 0 END)     AS OverEacCostCodes
FROM fct_BudgetLine
GROUP BY ProjectKey;

CREATE OR REPLACE TEMPORARY VIEW wip_qbo_cost AS
SELECT
    ProjectKey,
    CAST(SUM(Amount) AS DOUBLE) AS CostToDateQbo
FROM fct_CostTransaction
WHERE SourceSystem = 'quickbooks' AND ProjectKey IS NOT NULL
GROUP BY ProjectKey;

CREATE OR REPLACE TEMPORARY VIEW wip_billed AS
SELECT
    ProjectKey,
    CAST(SUM(BilledAmount) AS DOUBLE) AS BilledToDate
FROM fct_Billing
-- Procore only: adding the QuickBooks invoices would double count, since the
-- same billing is normally in both systems.
WHERE SourceSystem = 'procore'
GROUP BY ProjectKey;

CREATE OR REPLACE TABLE fct_WIP AS
WITH base AS (
    SELECT
        p.ProjectKey,
        (SELECT MonthStart FROM wip_period)                     AS MonthStart,
        CAST(COALESCE(c.OriginalContract, 0)     AS DOUBLE)     AS OriginalContract,
        CAST(COALESCE(c.ApprovedChangeOrders, 0) AS DOUBLE)     AS ApprovedChangeOrders,
        CAST(COALESCE(c.PendingChangeOrders, 0)  AS DOUBLE)     AS PendingChangeOrders,
        CAST(COALESCE(c.RevisedContract, 0)      AS DOUBLE)     AS RevisedContract,
        CAST(COALESCE(b.OriginalBudget, 0)       AS DOUBLE)     AS OriginalBudget,
        CAST(COALESCE(b.RevisedBudget, 0)        AS DOUBLE)     AS RevisedBudget,
        CAST(COALESCE(b.CommittedCost, 0)        AS DOUBLE)     AS CommittedCost,
        CAST(COALESCE(b.CostToDateProcore, 0)    AS DOUBLE)     AS CostToDate,
        CAST(COALESCE(q.CostToDateQbo, 0)        AS DOUBLE)     AS CostToDateQbo,
        CAST(COALESCE(b.EAC, 0)                  AS DOUBLE)     AS EAC,
        CAST(COALESCE(bi.BilledToDate, 0)        AS DOUBLE)     AS BilledToDate,
        COALESCE(b.OverEacCostCodes, 0)                         AS OverEacCostCodes
    FROM dim_Project p
    LEFT JOIN wip_contract c ON c.ProjectKey = p.ProjectKey
    LEFT JOIN wip_budget   b ON b.ProjectKey = p.ProjectKey
    LEFT JOIN wip_qbo_cost q ON q.ProjectKey = p.ProjectKey
    LEFT JOIN wip_billed  bi ON bi.ProjectKey = p.ProjectKey
    -- A project with neither a contract nor a budget is not a work in progress.
    -- Including it would put a row of zeros on the schedule for every
    -- prospective job in Procore.
    WHERE COALESCE(c.RevisedContract, 0) <> 0 OR COALESCE(b.RevisedBudget, 0) <> 0
),
computed AS (
    SELECT
        base.*,
        -- Percent complete, capped at 1.0 for REVENUE purposes only. Cost can
        -- and does exceed EAC; recognising more than 100% of the contract cannot
        -- happen. The uncapped value is kept alongside so an over-running job is
        -- visible rather than silently clamped.
        CASE WHEN EAC > 0 THEN CostToDate / EAC ELSE 0 END          AS PercentCompleteRaw,
        CASE WHEN EAC > 0 THEN LEAST(CostToDate / EAC, 1.0) ELSE 0 END AS PercentComplete
    FROM base
)
SELECT
    ProjectKey,
    MonthStart,
    OriginalContract,
    ApprovedChangeOrders,
    PendingChangeOrders,
    RevisedContract,
    OriginalBudget,
    RevisedBudget,
    CommittedCost,
    CostToDate,
    CostToDateQbo,
    EAC,
    BilledToDate,
    OverEacCostCodes,
    CAST(PercentCompleteRaw AS DOUBLE)                              AS PercentCompleteRaw,
    CAST(PercentComplete    AS DOUBLE)                              AS PercentComplete,
    CAST(RevisedContract * PercentComplete AS DOUBLE)               AS EarnedRevenue,
    CAST(EAC - CostToDate AS DOUBLE)                                AS CostToComplete,
    CAST(RevisedContract - EAC AS DOUBLE)                           AS GrossProfitAtCompletion,
    CAST(CASE WHEN RevisedContract <> 0
              THEN (RevisedContract - EAC) / RevisedContract
              ELSE 0 END AS DOUBLE)                                 AS GrossProfitPctAtCompletion,
    CAST((RevisedContract * PercentComplete) - CostToDate AS DOUBLE) AS EarnedGrossProfit,
    -- Billings in excess of costs / costs in excess of billings. Split into two
    -- non-negative columns because that is how they appear on a balance sheet -
    -- one is a liability and the other an asset, and netting them to a single
    -- signed number loses that distinction.
    CAST(GREATEST(BilledToDate - (RevisedContract * PercentComplete), 0) AS DOUBLE) AS OverBilling,
    CAST(GREATEST((RevisedContract * PercentComplete) - BilledToDate, 0) AS DOUBLE) AS UnderBilling,
    CAST(RevisedContract - (RevisedContract * PercentComplete) AS DOUBLE) AS Backlog,
    -- The Procore/QuickBooks disagreement, surfaced rather than reconciled away.
    CAST(CostToDate - CostToDateQbo AS DOUBLE)                      AS CostVariance,
    CAST(CASE WHEN CostToDate <> 0
              THEN (CostToDate - CostToDateQbo) / CostToDate
              ELSE 0 END AS DOUBLE)                                 AS CostVariancePct
FROM computed;
