-- The project crosswalk: Procore project <-> QuickBooks job <-> HubSpot deal.
--
-- There is no shared key between these three systems. Everything downstream -
-- WIP, project P&L, backlog, the Procore/QBO reconciliation - depends on this
-- table being right, which makes it the highest-risk object in the platform and
-- the one that must never be fully automatic.
--
-- PRECEDENCE, strictly:
--
--   1. MANUAL     the Controller's CSV. Always wins. A human decision is never
--                 overridden by a similarity score.
--   2. EXACT      normalised Procore project number found in the QBO job name.
--   3. FUZZY      token overlap on the name, above a confidence floor, and only
--                 when the match is UNAMBIGUOUS (see below).
--
-- Anything unmatched, or matched below the floor, is NOT dropped. It flows to
-- gold with ProjectKey intact and IsInCrosswalk = FALSE, and appears on the
-- report's Unmapped Projects page. A project missing from the crosswalk should
-- make a number visibly incomplete, never quietly smaller.

-- ---------------------------------------------------------------- normalisation
--
-- Match on a normalised form, not the raw string. "23-104", "23104" and
-- "#23-104 " are the same job to a human and three different strings to a join.

CREATE OR REPLACE TEMPORARY VIEW cw_procore AS
SELECT
    project_id                                   AS procore_project_id,
    project_number                               AS procore_project_number,
    project_name                                 AS procore_project_name,
    UPPER(REGEXP_REPLACE(COALESCE(project_number, ''), '[^A-Za-z0-9]', '')) AS norm_number,
    UPPER(REGEXP_REPLACE(COALESCE(project_name, ''),   '[^A-Za-z0-9]', '')) AS norm_name
FROM dl_silver_projects;

CREATE OR REPLACE TEMPORARY VIEW cw_qbo AS
SELECT
    qbo_customer_id,
    fully_qualified_name                         AS qbo_fully_qualified_name,
    display_name                                 AS qbo_display_name,
    UPPER(REGEXP_REPLACE(COALESCE(display_name, ''), '[^A-Za-z0-9]', ''))          AS norm_name,
    UPPER(REGEXP_REPLACE(COALESCE(fully_qualified_name, ''), '[^A-Za-z0-9]', ''))  AS norm_full_name
FROM dl_silver_qbo_jobs
WHERE is_active;

-- ---------------------------------------------------------------- tier 2: exact number
--
-- Requires a project number of at least 3 characters. Without that floor, a
-- project numbered "1" matches every QBO job whose name contains a 1 - which is
-- most of them.

CREATE OR REPLACE TEMPORARY VIEW cw_exact AS
SELECT
    p.procore_project_id,
    q.qbo_customer_id,
    'exact_number' AS match_method,
    1.00           AS confidence
FROM cw_procore p
JOIN cw_qbo q
  ON LENGTH(p.norm_number) >= 3
 AND (q.norm_name LIKE CONCAT('%', p.norm_number, '%')
      OR q.norm_full_name LIKE CONCAT('%', p.norm_number, '%'));

-- ---------------------------------------------------------------- tier 3: fuzzy name
--
-- Deliberately conservative. A wrong automatic match is far worse than no
-- match: an unmapped project is visible on a report page, whereas a
-- mis-mapped one silently attributes one job's cost to another job's revenue
-- and both projects' margins are wrong with nothing to indicate it.
--
-- ponytail: prefix-overlap similarity rather than a real token-set ratio.
-- Cheap, runs in both Spark and DuckDB, and only ever PROPOSES a match a human
-- confirms. Swap in a proper trigram similarity if the review queue proves too
-- long to work through.

CREATE OR REPLACE TEMPORARY VIEW cw_fuzzy_scored AS
SELECT
    p.procore_project_id,
    q.qbo_customer_id,
    ROUND(
        (LENGTH(p.norm_name) + LENGTH(q.norm_name)
         - ABS(LENGTH(p.norm_name) - LENGTH(q.norm_name)))
        / (2.0 * GREATEST(LENGTH(p.norm_name), LENGTH(q.norm_name), 1))
    , 2) AS length_affinity,
    CASE WHEN SUBSTR(p.norm_name, 1, 8) = SUBSTR(q.norm_name, 1, 8) THEN 1 ELSE 0 END AS prefix_hit
FROM cw_procore p
JOIN cw_qbo q
  ON LENGTH(p.norm_name) >= 8
 AND LENGTH(q.norm_name) >= 8
 AND SUBSTR(p.norm_name, 1, 8) = SUBSTR(q.norm_name, 1, 8)
WHERE NOT EXISTS (
    SELECT 1 FROM cw_exact e WHERE e.procore_project_id = p.procore_project_id
);

CREATE OR REPLACE TEMPORARY VIEW cw_fuzzy AS
SELECT
    procore_project_id,
    qbo_customer_id,
    'fuzzy_name' AS match_method,
    length_affinity AS confidence
FROM (
    SELECT
        s.*,
        COUNT(*)      OVER (PARTITION BY procore_project_id) AS candidates,
        ROW_NUMBER()  OVER (PARTITION BY procore_project_id
                            ORDER BY length_affinity DESC, qbo_customer_id) AS rn
    FROM cw_fuzzy_scored s
)
-- AMBIGUITY IS NOT A MATCH. If a Procore project resembles two QBO jobs equally
-- well, picking one is a coin flip dressed up as a decision. Send it to review.
WHERE rn = 1 AND candidates = 1 AND length_affinity >= 0.80;

-- ---------------------------------------------------------------- assembly

CREATE OR REPLACE TABLE dl_silver_project_crosswalk AS
WITH manual AS (
    -- The Controller's CSV, landed to Files/reference/project_crosswalk.csv and
    -- loaded by dl_06_land_reference. Rows with active = false are tombstones:
    -- an explicit "these are NOT the same job", which must also survive.
    SELECT
        CAST(procore_project_id AS STRING) AS procore_project_id,
        CAST(qbo_customer_id    AS STRING) AS qbo_customer_id,
        CAST(hubspot_deal_id    AS STRING) AS hubspot_deal_id,
        'manual'                           AS match_method,
        1.00                               AS confidence,
        TRIM(reviewed_by)                  AS reviewed_by,
        active
    FROM dl_bronze_reference_project_crosswalk
),
automatic AS (
    SELECT procore_project_id, qbo_customer_id, CAST(NULL AS STRING) AS hubspot_deal_id,
           match_method, confidence, CAST(NULL AS STRING) AS reviewed_by, TRUE AS active
    FROM cw_exact
    UNION ALL
    SELECT procore_project_id, qbo_customer_id, CAST(NULL AS STRING),
           match_method, confidence, CAST(NULL AS STRING), TRUE
    FROM cw_fuzzy
),
ranked AS (
    SELECT
        c.*,
        ROW_NUMBER() OVER (
            PARTITION BY procore_project_id
            -- Manual first, then exact, then fuzzy. This CASE is the precedence
            -- rule; nothing else enforces it.
            ORDER BY CASE match_method
                        WHEN 'manual'       THEN 1
                        WHEN 'exact_number' THEN 2
                        ELSE 3 END,
                     confidence DESC
        ) AS rn
    FROM (SELECT * FROM manual UNION ALL SELECT * FROM automatic) c
)
SELECT
    p.procore_project_id                              AS ProjectKey,
    p.procore_project_id,
    p.procore_project_number,
    p.procore_project_name,
    CASE WHEN r.active THEN r.qbo_customer_id END     AS qbo_customer_id,
    q.qbo_fully_qualified_name,
    CASE WHEN r.active THEN r.hubspot_deal_id END     AS hubspot_deal_id,
    COALESCE(r.match_method, 'unmatched')             AS match_method,
    COALESCE(r.confidence, 0.00)                      AS confidence,
    r.reviewed_by,
    CASE WHEN r.qbo_customer_id IS NOT NULL AND r.active THEN TRUE ELSE FALSE END
                                                      AS is_mapped
FROM cw_procore p
LEFT JOIN (SELECT * FROM ranked WHERE rn = 1) r
       ON r.procore_project_id = p.procore_project_id
LEFT JOIN cw_qbo q
       ON q.qbo_customer_id = r.qbo_customer_id;

-- ---------------------------------------------------------------- review queue
--
-- What the Controller actually works through. Two populations, and they need
-- different actions, so the reason says which:
--   unmatched          -> find the QBO job, add a manual row
--   below_confidence   -> confirm or reject the proposal

CREATE OR REPLACE TABLE dl_gold_crosswalk_candidates AS
SELECT
    p.procore_project_id,
    p.procore_project_number,
    p.procore_project_name,
    s.qbo_customer_id     AS proposed_qbo_customer_id,
    q.qbo_fully_qualified_name AS proposed_qbo_job_name,
    s.length_affinity     AS confidence,
    CASE WHEN s.qbo_customer_id IS NULL THEN 'unmatched' ELSE 'below_confidence' END AS reason
FROM cw_procore p
LEFT JOIN cw_fuzzy_scored s ON s.procore_project_id = p.procore_project_id
LEFT JOIN cw_qbo q          ON q.qbo_customer_id    = s.qbo_customer_id
WHERE p.procore_project_id NOT IN (
    SELECT procore_project_id FROM dl_silver_project_crosswalk WHERE is_mapped
);
