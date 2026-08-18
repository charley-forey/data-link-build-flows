-- Reporting metadata, rebuilt AFTER the quality gate has written its results.
--
-- These two tables are what let the report state its own trustworthiness, so
-- they must reflect the run that just happened. Built during the gold step they
-- would always be one run stale - the Data Quality page would report "0
-- warnings" for a run that recorded one, which is worse than showing nothing.

CREATE OR REPLACE TABLE meta_PipelineRun AS
SELECT
    batch_id                     AS BatchId,
    MAX(logged_at)               AS RunAt,
    COUNT(*)                     AS StepCount,
    SUM(row_count)               AS RowsWritten,
    MAX(CASE WHEN status <> 'ok' THEN 1 ELSE 0 END) = 0 AS Succeeded
FROM dl_meta_run_log
GROUP BY batch_id;

-- Only the most recent run. History stays in dl_dq_results; a page listing
-- every failure ever recorded is a page nobody reads.
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
    -- readers, and this report goes to a CEO and a Controller.
    CASE WHEN r.passed THEN 3
         WHEN r.severity = 'error' THEN 1
         ELSE 2 END              AS SeveritySort,
    CASE WHEN r.passed THEN 'Passed'
         WHEN r.severity = 'error' THEN 'Blocking'
         ELSE 'Warning' END      AS StatusLabel
FROM dl_dq_results r
CROSS JOIN latest l
WHERE r.checked_at = l.checked_at;
