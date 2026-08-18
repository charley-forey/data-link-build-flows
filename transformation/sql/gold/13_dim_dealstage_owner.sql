-- Phase 2 dimensions: deal stage and owner.
--
-- Same two rules as every other dimension here: a key-0 "Unassigned" row so a
-- fact never loses a row to a missing lookup, and observed keys unioned in so
-- referential integrity holds by construction.

-- ---------------------------------------------------------------- deal stage
--
-- THE STAGE CARRIES THE WIN PROBABILITY. A deal knows which stage it sits in;
-- only the stage definition knows what that stage is worth. Weighted pipeline
-- forecasting is a join, not a column on the deal - which is why the pipelines
-- endpoint is pulled at all.

CREATE OR REPLACE TABLE dim_DealStage AS
WITH defined AS (
    SELECT
        s.stage_id,
        COALESCE(s.stage_name, 'Unknown')    AS stage_name,
        COALESCE(s.pipeline_name, 'Unknown') AS pipeline_name,
        s.display_order,
        s.win_probability,
        s.is_closed_stage
    FROM sv_deal_stages s
),
observed AS (
    -- Stages that deals actually reference but the pipeline definition no
    -- longer contains - a stage renamed or deleted after deals moved through
    -- it. The deals are real, so the stage has to exist for them to join to.
    -- Same construction as dim_Project: union the observed keys and let
    -- referential integrity hold by construction rather than by hope.
    --
    -- Probability 0 is the conservative reading: an unknown stage contributes
    -- nothing to a weighted forecast rather than inventing a value.
    SELECT DISTINCT
        d.stage_id,
        CONCAT('Unknown stage (', d.stage_id, ')') AS stage_name,
        'Unknown'                                  AS pipeline_name,
        CAST(998 AS INT)                           AS display_order,
        CAST(0 AS DOUBLE)                          AS win_probability,
        FALSE                                      AS is_closed_stage
    FROM sv_deals d
    WHERE d.stage_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM defined s WHERE s.stage_id = d.stage_id)
)
SELECT
    '0'                          AS DealStageKey,
    'Unassigned'                 AS StageName,
    'Unassigned'                 AS PipelineName,
    CAST(999 AS INT)             AS DisplayOrder,
    CAST(0 AS DOUBLE)            AS WinProbability,
    FALSE                        AS IsClosedStage,
    'Unknown'                    AS StageOutcome
UNION ALL
SELECT
    s.stage_id                   AS DealStageKey,
    s.stage_name                 AS StageName,
    s.pipeline_name              AS PipelineName,
    s.display_order              AS DisplayOrder,
    s.win_probability            AS WinProbability,
    s.is_closed_stage            AS IsClosedStage,
    -- Derived from the probability rather than from the label, because stage
    -- names are free text a sales admin can rename at any time. 1.0 is won,
    -- 0.0 on a closed stage is lost, anything else is still open.
    CASE
        WHEN NOT s.is_closed_stage      THEN 'Open'
        WHEN s.win_probability >= 1.0   THEN 'Won'
        ELSE 'Lost'
    END                          AS StageOutcome
FROM (SELECT * FROM defined UNION ALL SELECT * FROM observed) s;

-- ---------------------------------------------------------------- owner

CREATE OR REPLACE TABLE dim_Owner AS
SELECT '0' AS OwnerKey, 'Unassigned' AS OwnerName, CAST(NULL AS STRING) AS OwnerEmail
UNION ALL
SELECT
    o.owner_id                            AS OwnerKey,
    COALESCE(NULLIF(TRIM(o.owner_name), ''), o.owner_email, 'Unknown') AS OwnerName,
    o.owner_email                         AS OwnerEmail
FROM sv_owners o;
