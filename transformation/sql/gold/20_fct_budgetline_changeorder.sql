-- fct_BudgetLine and fct_ChangeOrder.
--
-- MONTHSTART IS ONLY SET WHEN THE DATE FALLS INSIDE dim_Date. An unmatched date
-- key does not raise an error in a semantic model - it makes every measure
-- filtered by date come back BLANK for those rows. A blank measure looks like
-- "no data for this period" rather than "this row has a broken date", so the
-- out-of-range case is flagged explicitly on the fact instead.

-- ---------------------------------------------------------------- budget lines
--
-- Grain: project x cost code, as of the latest budget snapshot. This is the
-- detail behind every WIP number - the table a project manager drills into when
-- they disagree with the roll-up.

CREATE OR REPLACE TABLE fct_BudgetLine AS
SELECT
    b.project_id                                          AS ProjectKey,
    CONCAT(COALESCE(b.project_id, 'X'), '|', COALESCE(b.cost_code_id, '0')) AS CostCodeKey,
    b.budget_line_id                                      AS BudgetLineKey,
    COALESCE(b.cost_code, 'Unknown')                      AS CostCode,
    COALESCE(b.category, 'Unassigned')                    AS Category,
    CAST(b.original_budget              AS DOUBLE)        AS OriginalBudget,
    CAST(b.approved_budget_changes      AS DOUBLE)        AS ApprovedBudgetChanges,
    -- Revised budget is taken from Procore where present, and derived where the
    -- budget view does not expose it. Deriving unconditionally would override
    -- Procore's own arithmetic, which includes modifications this sum does not.
    CAST(CASE WHEN b.revised_budget <> 0 THEN b.revised_budget
              ELSE b.original_budget + b.approved_budget_changes + b.budget_modifications
         END AS DOUBLE)                                   AS RevisedBudget,
    CAST(b.committed_cost               AS DOUBLE)        AS CommittedCost,
    CAST(b.direct_cost                  AS DOUBLE)        AS DirectCost,
    CAST(b.job_to_date_cost             AS DOUBLE)        AS JobToDateCost,
    CAST(b.estimated_cost_at_completion AS DOUBLE)        AS EstimatedCostAtCompletion,
    CAST(b.forecast_to_complete         AS DOUBLE)        AS ForecastToComplete,
    CAST(b.projected_over_under         AS DOUBLE)        AS ProjectedOverUnder,
    -- Over-EAC at the cost-code level is the earliest visible signal that a job
    -- is going wrong, and it is exactly what gets lost in a project-level
    -- roll-up where one under-running code masks another that is over.
    CASE WHEN b.job_to_date_cost > b.estimated_cost_at_completion
              AND b.estimated_cost_at_completion > 0
         THEN TRUE ELSE FALSE END                         AS IsOverEac
FROM sv_budget_lines b
WHERE b.project_id IS NOT NULL;

-- ---------------------------------------------------------------- change orders
--
-- Grain: one row per change order. The CUMULATIVE roll-up happens at query
-- time, not here.
--
-- Rolling change orders up PER MONTH rather than cumulatively is the exact
-- defect that understated portfolio contract value by 16% ($4.85M) on the
-- reference engagement: each month showed only that month's changes, and the
-- contract value silently reverted to near-original whenever a month was quiet.
-- The grain below plus a running total in DAX is what prevents that shape of
-- bug from being expressible.

CREATE OR REPLACE TABLE fct_ChangeOrder AS
SELECT
    c.project_id                                       AS ProjectKey,
    c.change_order_id                                  AS ChangeOrderKey,
    c.contract_id                                      AS ContractId,
    c.change_order_scope                               AS ChangeOrderScope,
    c.change_order_number                              AS ChangeOrderNumber,
    c.title                                            AS Title,
    COALESCE(c.status, 'Unknown')                      AS Status,
    CAST(c.amount AS DOUBLE)                           AS Amount,
    c.created_date                                     AS CreatedDate,
    c.approved_date                                    AS ApprovedDate,
    -- Approval drives contract value, so the effective date is the approval
    -- date. Falling back to created_date for approved-but-undated rows keeps
    -- them on the timeline instead of dropping them out of every period.
    CASE
        WHEN c.approved_date IS NOT NULL THEN c.approved_date
        ELSE c.created_date
    END                                                AS EffectiveDate,
    CASE
        WHEN COALESCE(
                CASE WHEN c.approved_date IS NOT NULL THEN c.approved_date ELSE c.created_date END,
                DATE '1900-01-01') BETWEEN DATE '2015-01-01' AND DATE '2035-12-31'
        THEN make_date(
                year(CASE WHEN c.approved_date IS NOT NULL THEN c.approved_date ELSE c.created_date END),
                month(CASE WHEN c.approved_date IS NOT NULL THEN c.approved_date ELSE c.created_date END),
                1)
    END                                                AS MonthStart,
    CASE
        WHEN COALESCE(
                CASE WHEN c.approved_date IS NOT NULL THEN c.approved_date ELSE c.created_date END,
                DATE '1900-01-01') NOT BETWEEN DATE '2015-01-01' AND DATE '2035-12-31'
        THEN TRUE ELSE FALSE END                       AS HasOutOfRangeDate,
    -- Approval is derived from the data rather than from status TEXT, which
    -- varies by Procore configuration - one tenant's "Approved" is another's
    -- "Executed". `is_executed` and the presence of an approval date are facts.
    CASE WHEN c.is_executed OR c.approved_date IS NOT NULL
         THEN TRUE ELSE FALSE END                      AS IsApproved
FROM sv_change_orders c
WHERE c.project_id IS NOT NULL;
