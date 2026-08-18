-- HubSpot bronze -> silver.
--
-- Two things about HubSpot's shape drive everything here:
--
--   1. EVERY PROPERTY IS A STRING. HubSpot returns amounts, dates and booleans
--      as JSON strings inside a `properties` object. Nothing is typed until we
--      type it, and a silent cast failure looks like a zero-value deal.
--
--   2. WIN PROBABILITY LIVES ON THE STAGE, NOT THE DEAL. A deal knows which
--      stage it is in; the stage definition knows what that stage is worth.
--      Weighted forecasting is impossible without joining the two, which is why
--      the pipelines endpoint is pulled at all.

-- ---------------------------------------------------------------- deals

CREATE OR REPLACE TABLE dl_silver_hubspot_deals AS
SELECT
    CAST(get_json_object(payload, '$.id') AS STRING)                      AS deal_id,
    TRIM(get_json_object(payload, '$.properties.dealname'))               AS deal_name,
    CAST(get_json_object(payload, '$.properties.pipeline') AS STRING)     AS pipeline_id,
    CAST(get_json_object(payload, '$.properties.dealstage') AS STRING)    AS stage_id,
    CAST(get_json_object(payload, '$.properties.hubspot_owner_id') AS STRING) AS owner_id,
    TRIM(get_json_object(payload, '$.properties.dealtype'))               AS deal_type,
    CAST(COALESCE(get_json_object(payload, '$.properties.amount'), '0') AS DOUBLE) AS amount,

    -- HubSpot's own stage probability, when the portal sets one. Kept separate
    -- from the stage definition's probability so a deal-level override is
    -- visible rather than silently merged.
    CAST(get_json_object(payload, '$.properties.hs_deal_stage_probability') AS DOUBLE)
                                                                          AS deal_probability,

    CASE WHEN CAST(SUBSTR(get_json_object(payload, '$.properties.closedate'), 1, 10) AS DATE)
              < DATE '1990-01-01' THEN NULL
         ELSE CAST(SUBSTR(get_json_object(payload, '$.properties.closedate'), 1, 10) AS DATE) END
                                                                          AS close_date,
    CAST(SUBSTR(get_json_object(payload, '$.properties.createdate'), 1, 10) AS DATE)
                                                                          AS create_date,

    -- Booleans arrive as the STRINGS "true"/"false". A plain CAST to BOOLEAN
    -- returns NULL for those, which would make every closed-won deal look open.
    LOWER(TRIM(get_json_object(payload, '$.properties.hs_is_closed'))) = 'true'
                                                                          AS is_closed,
    LOWER(TRIM(get_json_object(payload, '$.properties.hs_is_closed_won'))) = 'true'
                                                                          AS is_closed_won,
    _ingested_at,
    _batch_id
FROM dl_bronze_hubspot_deals
WHERE get_json_object(payload, '$.id') IS NOT NULL;

-- ---------------------------------------------------------------- stages
--
-- One row per stage, exploded out of the pipeline objects. This is the table
-- that carries win probability.

CREATE OR REPLACE TABLE dl_silver_hubspot_stages AS
SELECT
    CAST(get_json_object(payload, '$.id') AS STRING)          AS pipeline_id,
    TRIM(get_json_object(payload, '$.label'))                 AS pipeline_name,
    CAST(get_json_object(stage, '$.id') AS STRING)            AS stage_id,
    TRIM(get_json_object(stage, '$.label'))                   AS stage_name,
    CAST(COALESCE(get_json_object(stage, '$.displayOrder'), '0') AS INT) AS display_order,
    -- metadata.probability is a string fraction, "0.2" for 20%.
    CAST(COALESCE(get_json_object(stage, '$.metadata.probability'), '0') AS DOUBLE)
                                                              AS win_probability,
    LOWER(TRIM(get_json_object(stage, '$.metadata.isClosed'))) = 'true' AS is_closed_stage,
    _ingested_at,
    _batch_id
FROM dl_bronze_hubspot_pipelines
LATERAL VIEW explode(from_json(get_json_object(payload, '$.stages'), 'array<string>')) AS stage
WHERE get_json_object(payload, '$.id') IS NOT NULL;

-- ---------------------------------------------------------------- owners

CREATE OR REPLACE TABLE dl_silver_hubspot_owners AS
SELECT
    CAST(get_json_object(payload, '$.id') AS STRING)   AS owner_id,
    TRIM(CONCAT(
        COALESCE(get_json_object(payload, '$.firstName'), ''), ' ',
        COALESCE(get_json_object(payload, '$.lastName'), '')))  AS owner_name,
    TRIM(get_json_object(payload, '$.email'))          AS owner_email,
    _ingested_at,
    _batch_id
FROM dl_bronze_hubspot_owners
WHERE get_json_object(payload, '$.id') IS NOT NULL;

-- ---------------------------------------------------------------- companies

CREATE OR REPLACE TABLE dl_silver_hubspot_companies AS
SELECT
    CAST(get_json_object(payload, '$.id') AS STRING)            AS company_id,
    TRIM(get_json_object(payload, '$.properties.name'))         AS company_name,
    TRIM(get_json_object(payload, '$.properties.industry'))     AS industry,
    TRIM(get_json_object(payload, '$.properties.city'))         AS city,
    TRIM(get_json_object(payload, '$.properties.state'))        AS state,
    CAST(get_json_object(payload, '$.properties.hubspot_owner_id') AS STRING) AS owner_id,
    _ingested_at,
    _batch_id
FROM dl_bronze_hubspot_companies
WHERE get_json_object(payload, '$.id') IS NOT NULL;
