-- The calendar.
--
-- One dim_Date, marked as the date table in the semantic model. This single
-- object removes every INDEX/MATCH date mechanic from the spreadsheet it
-- replaces, and it is what makes time intelligence (prior period, YTD,
-- fade/gain) one DAX function instead of a hand-built column per comparison.
--
-- DIALECT: explode(sequence(...)) is Spark. scripts/run_tests.py maps it to a
-- DuckDB generate_series with one macro. Everything else here uses functions
-- both engines share natively (make_date, last_day, quarter, year, month), and
-- MonthOffset is computed from year/month arithmetic rather than
-- months_between() because the two engines spell that differently.
--
-- RANGE: 2015-2035. Wide enough to cover history the Controller might restate
-- and a five-year forward backlog view, narrow enough that a sentinel date from
-- a source system falls OUTSIDE it and gets caught by the fact-level range
-- check rather than quietly extending the axis.

CREATE OR REPLACE TABLE dim_Date AS
WITH days AS (
    SELECT explode(sequence(DATE '2015-01-01', DATE '2035-12-31', INTERVAL 1 DAY)) AS Date
),
enriched AS (
    SELECT
        Date,
        year(Date)    AS Year,
        month(Date)   AS Month,
        quarter(Date) AS Quarter,
        day(Date)     AS DayOfMonth
    FROM days
)
SELECT
    Date                                                        AS Date,
    CAST(Year AS INT)                                           AS Year,
    CAST(Month AS INT)                                          AS Month,
    CAST(Quarter AS INT)                                        AS Quarter,
    CAST(DayOfMonth AS INT)                                     AS DayOfMonth,
    make_date(Year, Month, 1)                                   AS MonthStart,
    last_day(Date)                                              AS MonthEnd,
    date_format(Date, 'MMM')                                    AS MonthShortName,
    date_format(Date, 'MMMM')                                   AS MonthName,
    CONCAT(date_format(Date, 'MMM'), ' ', CAST(Year AS STRING)) AS MonthYear,
    -- Sortable integer so a visual orders "Jan 2026" after "Dec 2025" rather
    -- than alphabetically. Every text month column needs one of these.
    CAST((Year * 100) + Month AS INT)                           AS MonthYearSort,
    CONCAT('Q', CAST(Quarter AS STRING), ' ', CAST(Year AS STRING)) AS QuarterYear,
    CAST((Year * 10) + Quarter AS INT)                          AS QuarterYearSort,
    -- 0 = the current month, negative = past, positive = future. Makes
    -- "last 12 months" a filter on a number rather than a date calculation, and
    -- it is stable when the report is exported.
    CAST((Year * 12 + Month) - (year(CURRENT_DATE) * 12 + month(CURRENT_DATE)) AS INT)
                                                                AS MonthOffset,
    CASE WHEN Date <= CURRENT_DATE THEN TRUE ELSE FALSE END     AS IsPast,
    CASE WHEN make_date(Year, Month, 1) = make_date(year(CURRENT_DATE), month(CURRENT_DATE), 1)
         THEN TRUE ELSE FALSE END                               AS IsCurrentMonth,
    CASE WHEN Year = year(CURRENT_DATE) THEN TRUE ELSE FALSE END AS IsCurrentYear
FROM enriched;
