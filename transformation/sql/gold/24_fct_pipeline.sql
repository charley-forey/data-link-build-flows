-- fct_Pipeline - the sales pipeline, weighted.
--
-- Grain: one row per open deal.
--
-- WEIGHTED VALUE = AMOUNT x PROBABILITY, and the probability comes from the
-- STAGE, not the deal. HubSpot lets a portal override probability per deal;
-- where it has, that wins, because someone made a deliberate call about that
-- specific opportunity. Otherwise the stage definition applies.
--
-- CLOSED DEALS ARE EXCLUDED. A pipeline is what might still happen. Won deals
-- become revenue and belong to Procore and the WIP schedule; lost deals belong
-- to a win-rate analysis, not a forecast. Including either inflates the number
-- the CEO is trying to plan against - and closed-won is the more dangerous of
-- the two, because it makes the pipeline look healthy while it is actually
-- emptying.

CREATE OR REPLACE TABLE fct_Pipeline AS
SELECT
    d.deal_id                                    AS DealKey,
    COALESCE(d.stage_id, '0')                    AS DealStageKey,
    COALESCE(d.owner_id, '0')                    AS OwnerKey,
    d.deal_name                                  AS DealName,
    COALESCE(d.deal_type, 'Unspecified')         AS DealType,
    CAST(d.amount AS DOUBLE)                     AS Amount,

    -- Deal-level override first, then the stage definition.
    CAST(COALESCE(d.deal_probability, s.win_probability, 0) AS DOUBLE) AS WinProbability,
    CAST(d.amount * COALESCE(d.deal_probability, s.win_probability, 0) AS DOUBLE)
                                                 AS WeightedAmount,

    d.close_date                                 AS CloseDate,
    d.create_date                                AS CreateDate,

    -- Only set when the date lands inside dim_Date. An unmatched date key does
    -- not error in a semantic model - it makes every date-filtered measure come
    -- back blank, which reads as "no pipeline this month".
    CASE WHEN d.close_date BETWEEN DATE '2015-01-01' AND DATE '2035-12-31'
         THEN make_date(year(d.close_date), month(d.close_date), 1) END AS MonthStart,
    CASE WHEN d.close_date IS NOT NULL
              AND d.close_date NOT BETWEEN DATE '2015-01-01' AND DATE '2035-12-31'
         THEN TRUE ELSE FALSE END                AS HasOutOfRangeDate,

    -- Age is measured to the reporting date, not to a hard-coded today, so an
    -- exported report keeps saying the same thing tomorrow.
    CASE WHEN d.create_date IS NOT NULL
         THEN DATEDIFF(CURRENT_DATE, d.create_date) END AS DaysOpen,
    CASE WHEN d.close_date IS NOT NULL AND d.close_date < CURRENT_DATE
         THEN TRUE ELSE FALSE END                AS IsPastCloseDate
FROM sv_deals d
LEFT JOIN sv_deal_stages s ON s.stage_id = d.stage_id
WHERE NOT COALESCE(d.is_closed, FALSE);
