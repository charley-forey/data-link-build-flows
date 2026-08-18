-- Pipeline metadata surfaced INTO the model.
--
-- The spreadsheet this platform replaces cannot say when it was last correct.
-- A dashboard that silently shows three-week-old numbers is worse than one that
-- is obviously broken, because nobody stops trusting it. These tables are what
-- let the report say "STALE - these numbers may be weeks old" on its own face.

-- A single-row anchor table that exists ONLY to hold measures.
--
-- Measures cannot share a name with a column on the same table, and the natural
-- measure names collide immediately: `EAC` and `Backlog` are both columns on
-- fct_WIP. Renaming the measures to avoid it would put "EAC Total" in front of
-- the CEO, which is a worse outcome than one hidden table.
--
-- It also gives the report author one place to find every measure, grouped by
-- display folder, instead of hunting across five fact tables.
CREATE OR REPLACE TABLE meta_Measures AS
SELECT CAST(1 AS INT) AS Anchor;

CREATE OR REPLACE TABLE meta_PipelineRun AS
SELECT
    batch_id                     AS BatchId,
    MAX(logged_at)               AS RunAt,
    COUNT(*)                     AS StepCount,
    SUM(row_count)               AS RowsWritten,
    MAX(CASE WHEN status <> 'ok' THEN 1 ELSE 0 END) = 0 AS Succeeded
FROM dl_meta_run_log
GROUP BY batch_id;

-- The DQ result set, shaped for the report's Data Quality page. Only the most
-- recent run: history lives in dl_dq_results, but a page showing every failure
-- ever recorded is a page nobody reads.
CREATE OR REPLACE TABLE meta_DataQuality AS
WITH latest AS (
    SELECT MAX(checked_at) AS checked_at FROM dl_dq_results
)
SELECT
    r.expectation                AS Expectation,
    r.table_name                 AS TableName,
    r.severity                   AS Severity,
    r.failing_rows               AS FailingRows,
    r.passed                     AS Passed,
    r.description                AS Description,
    r.checked_at                 AS CheckedAt,
    -- Sort order plus a text label, so the page never encodes status in colour
    -- alone. Red and green are not distinguishable for a meaningful share of
    -- readers, and this report goes to a CEO and a Controller, not a colour
    -- vision panel.
    CASE WHEN r.passed THEN 3
         WHEN r.severity = 'error' THEN 1
         ELSE 2 END              AS SeveritySort,
    CASE WHEN r.passed THEN 'Passed'
         WHEN r.severity = 'error' THEN 'Blocking'
         ELSE 'Warning' END      AS StatusLabel
FROM dl_dq_results r
CROSS JOIN latest l
WHERE r.checked_at >= l.checked_at - INTERVAL 1 HOURS;

-- The crosswalk review queue, promoted to gold so it can be a report page. This
-- is the Controller's actual to-do list.
CREATE OR REPLACE TABLE meta_UnmappedProjects AS
SELECT
    p.ProjectKey,
    p.ProjectNumber,
    p.ProjectName,
    p.ProjectStatus,
    p.OriginalContract,
    p.CrosswalkMethod,
    p.CrosswalkConfidence,
    c.proposed_qbo_customer_id   AS ProposedQboCustomerId,
    c.proposed_qbo_job_name      AS ProposedQboJobName,
    COALESCE(c.reason, 'unmatched') AS Reason
FROM dim_Project p
LEFT JOIN dl_gold_crosswalk_candidates c
       ON c.procore_project_id = p.ProjectKey
WHERE NOT p.IsInCrosswalk;
