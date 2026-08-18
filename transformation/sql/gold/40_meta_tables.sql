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

-- meta_DataQuality and meta_PipelineRun are NOT built here.
--
-- They are derived from dl_dq_results and dl_meta_run_log, which the quality
-- gate writes AFTER this file runs. Building them here would publish a Data
-- Quality page that always shows the PREVIOUS run - a report that says
-- "0 warnings" while the run that just finished recorded one. Worse than no
-- page at all, because it looks authoritative.
--
-- dl_40_dq_checks rebuilds both once its own results exist. See
-- transformation/sql/meta/50_meta_reporting.sql.

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
