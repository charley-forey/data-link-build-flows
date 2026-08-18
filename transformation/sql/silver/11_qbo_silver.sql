-- QuickBooks Online bronze -> silver.
--
-- The central problem this file solves: QBO transactions are HEADER + LINE
-- documents, and job cost lives on the LINE (via CustomerRef / ClassRef /
-- ProjectRef), not the header. A bill can span four jobs. Summing headers
-- therefore attributes the whole bill to whichever job happens to be named
-- first, which is wrong in a way that still foots to the right company total -
-- so it survives a sanity check and shows up as one project being over budget
-- and another under.
--
-- dl_silver_gl_transactions explodes to LINE grain and is the only place cost
-- is attributed to a job.

-- ---------------------------------------------------------------- customers / jobs
--
-- The Procore crosswalk target. QBO models a job as a Customer with Job=true
-- and a ParentRef; native Projects additionally set IsProject=true.
-- FullyQualifiedName is the "Customer:Job:Sub-job" path and is what a human
-- recognises, so it is carried for the crosswalk review UI.

CREATE OR REPLACE TABLE dl_silver_qbo_customers AS
SELECT
    CAST(get_json_object(payload, '$.Id') AS STRING)          AS qbo_customer_id,
    TRIM(get_json_object(payload, '$.DisplayName'))           AS display_name,
    TRIM(get_json_object(payload, '$.FullyQualifiedName'))    AS fully_qualified_name,
    TRIM(get_json_object(payload, '$.CompanyName'))           AS company_name,
    CAST(get_json_object(payload, '$.ParentRef.value') AS STRING) AS parent_customer_id,
    COALESCE(CAST(get_json_object(payload, '$.Job') AS BOOLEAN), FALSE)       AS is_job,
    COALESCE(CAST(get_json_object(payload, '$.IsProject') AS BOOLEAN), FALSE) AS is_project,
    COALESCE(CAST(get_json_object(payload, '$.Active') AS BOOLEAN), FALSE)    AS is_active,
    CAST(COALESCE(get_json_object(payload, '$.Balance'), '0') AS DOUBLE)      AS balance,
    CAST(COALESCE(get_json_object(payload, '$.BalanceWithJobs'), '0') AS DOUBLE) AS balance_with_jobs,
    _ingested_at,
    _batch_id
FROM dl_bronze_qbo_customers
WHERE get_json_object(payload, '$.Id') IS NOT NULL;

-- Jobs only - the rows that can map to a Procore project. A top-level customer
-- is a client, not a job, and matching one to a project would be wrong.
CREATE OR REPLACE TABLE dl_silver_qbo_jobs AS
SELECT *
FROM dl_silver_qbo_customers
WHERE is_job OR is_project;

-- ---------------------------------------------------------------- lists

CREATE OR REPLACE TABLE dl_silver_qbo_accounts AS
SELECT
    CAST(get_json_object(payload, '$.Id') AS STRING)     AS qbo_account_id,
    TRIM(get_json_object(payload, '$.Name'))             AS account_name,
    TRIM(get_json_object(payload, '$.FullyQualifiedName')) AS account_full_name,
    TRIM(get_json_object(payload, '$.AcctNum'))          AS account_number,
    TRIM(get_json_object(payload, '$.Classification'))   AS classification,
    TRIM(get_json_object(payload, '$.AccountType'))      AS account_type,
    TRIM(get_json_object(payload, '$.AccountSubType'))   AS account_sub_type,
    COALESCE(CAST(get_json_object(payload, '$.Active') AS BOOLEAN), FALSE) AS is_active,
    _ingested_at,
    _batch_id
FROM dl_bronze_qbo_accounts
WHERE get_json_object(payload, '$.Id') IS NOT NULL;

CREATE OR REPLACE TABLE dl_silver_qbo_vendors AS
SELECT
    CAST(get_json_object(payload, '$.Id') AS STRING) AS qbo_vendor_id,
    TRIM(get_json_object(payload, '$.DisplayName'))  AS vendor_name,
    TRIM(get_json_object(payload, '$.CompanyName'))  AS company_name,
    COALESCE(CAST(get_json_object(payload, '$.Active') AS BOOLEAN), FALSE) AS is_active,
    CAST(COALESCE(get_json_object(payload, '$.Balance'), '0') AS DOUBLE)   AS balance,
    'quickbooks'                                      AS source_system,
    _ingested_at,
    _batch_id
FROM dl_bronze_qbo_vendors
WHERE get_json_object(payload, '$.Id') IS NOT NULL;

-- ---------------------------------------------------------------- ledger
--
-- LINE GRAIN. One row per transaction line, unioned across the document types
-- that carry job cost, with a `txn_type` discriminator.
--
-- `posted_amount` is signed so the whole table sums correctly: bills, purchases
-- and journal debits increase cost; vendor credits reduce it. Without the sign
-- convention, a credit memo makes cost go UP and the variance check fires on a
-- correction rather than on a problem.

CREATE OR REPLACE TABLE dl_silver_gl_transactions AS
WITH bills AS (
    SELECT
        'Bill'                                              AS txn_type,
        CAST(get_json_object(payload, '$.Id') AS STRING)     AS txn_id,
        TRIM(get_json_object(payload, '$.DocNumber'))        AS doc_number,
        CAST(SUBSTR(get_json_object(payload, '$.TxnDate'), 1, 10) AS DATE) AS txn_date,
        CAST(get_json_object(payload, '$.VendorRef.value') AS STRING) AS qbo_vendor_id,
        TRIM(get_json_object(payload, '$.VendorRef.name'))   AS vendor_name,
        get_json_object(payload, '$.Line')                   AS lines_json,
        1                                                    AS sign,
        _ingested_at,
        _batch_id
    FROM dl_bronze_qbo_bills
    WHERE get_json_object(payload, '$.Id') IS NOT NULL
),
purchases AS (
    SELECT
        'Purchase',
        CAST(get_json_object(payload, '$.Id') AS STRING),
        TRIM(get_json_object(payload, '$.DocNumber')),
        CAST(SUBSTR(get_json_object(payload, '$.TxnDate'), 1, 10) AS DATE),
        CAST(get_json_object(payload, '$.EntityRef.value') AS STRING),
        TRIM(get_json_object(payload, '$.EntityRef.name')),
        get_json_object(payload, '$.Line'),
        -- A Purchase with Credit=true is a refund: it reduces cost.
        CASE WHEN CAST(get_json_object(payload, '$.Credit') AS BOOLEAN) THEN -1 ELSE 1 END,
        _ingested_at,
        _batch_id
    FROM dl_bronze_qbo_purchases
    WHERE get_json_object(payload, '$.Id') IS NOT NULL
),
vendor_credits AS (
    SELECT
        'VendorCredit',
        CAST(get_json_object(payload, '$.Id') AS STRING),
        TRIM(get_json_object(payload, '$.DocNumber')),
        CAST(SUBSTR(get_json_object(payload, '$.TxnDate'), 1, 10) AS DATE),
        CAST(get_json_object(payload, '$.VendorRef.value') AS STRING),
        TRIM(get_json_object(payload, '$.VendorRef.name')),
        get_json_object(payload, '$.Line'),
        -1,
        _ingested_at,
        _batch_id
    FROM dl_bronze_qbo_vendor_credits
    WHERE get_json_object(payload, '$.Id') IS NOT NULL
),
journal_entries AS (
    SELECT
        'JournalEntry',
        CAST(get_json_object(payload, '$.Id') AS STRING),
        TRIM(get_json_object(payload, '$.DocNumber')),
        CAST(SUBSTR(get_json_object(payload, '$.TxnDate'), 1, 10) AS DATE),
        CAST(NULL AS STRING),
        CAST(NULL AS STRING),
        get_json_object(payload, '$.Line'),
        1,
        _ingested_at,
        _batch_id
    FROM dl_bronze_qbo_journal_entries
    WHERE get_json_object(payload, '$.Id') IS NOT NULL
),
headers AS (
    SELECT * FROM bills
    UNION ALL SELECT * FROM purchases
    UNION ALL SELECT * FROM vendor_credits
    UNION ALL SELECT * FROM journal_entries
),
exploded AS (
    -- from_json + explode rather than a lateral JSON path, so a document with a
    -- single line and one with forty behave identically. A scalar-vs-array
    -- difference here is the classic source of "most bills load, some vanish".
    SELECT
        headers.txn_type,
        headers.txn_id,
        headers.doc_number,
        headers.txn_date,
        headers.qbo_vendor_id,
        headers.vendor_name,
        headers.sign,
        headers._ingested_at,
        headers._batch_id,
        line
    FROM headers
    LATERAL VIEW explode(
        from_json(headers.lines_json, 'array<string>')
    ) AS line
)
SELECT
    txn_type,
    txn_id,
    CAST(get_json_object(line, '$.Id') AS STRING)                       AS line_id,
    CONCAT(txn_type, '|', txn_id, '|', COALESCE(get_json_object(line, '$.Id'), '0'))
                                                                        AS gl_transaction_key,
    doc_number,
    txn_date,
    qbo_vendor_id,
    vendor_name,
    TRIM(get_json_object(line, '$.Description'))                        AS line_description,
    -- Job attribution. AccountBasedExpenseLineDetail and ItemBasedExpenseLineDetail
    -- carry it in different places, and journal entries in a third.
    CAST(COALESCE(
        get_json_object(line, '$.AccountBasedExpenseLineDetail.CustomerRef.value'),
        get_json_object(line, '$.ItemBasedExpenseLineDetail.CustomerRef.value'),
        get_json_object(line, '$.JournalEntryLineDetail.Entity.EntityRef.value')
    ) AS STRING)                                                        AS qbo_customer_id,
    CAST(COALESCE(
        get_json_object(line, '$.AccountBasedExpenseLineDetail.ClassRef.value'),
        get_json_object(line, '$.ItemBasedExpenseLineDetail.ClassRef.value'),
        get_json_object(line, '$.JournalEntryLineDetail.ClassRef.value')
    ) AS STRING)                                                        AS qbo_class_id,
    CAST(COALESCE(
        get_json_object(line, '$.AccountBasedExpenseLineDetail.AccountRef.value'),
        get_json_object(line, '$.JournalEntryLineDetail.AccountRef.value')
    ) AS STRING)                                                        AS qbo_account_id,
    CAST(get_json_object(line, '$.ProjectRef.value') AS STRING)         AS qbo_project_ref,
    CAST(COALESCE(get_json_object(line, '$.Amount'), '0') AS DOUBLE)    AS line_amount,
    -- Journal entries carry direction on the line, not the header.
    CAST(COALESCE(get_json_object(line, '$.Amount'), '0') AS DOUBLE)
        * sign
        * CASE WHEN get_json_object(line, '$.JournalEntryLineDetail.PostingType') = 'Credit'
               THEN -1 ELSE 1 END                                       AS posted_amount,
    _ingested_at,
    _batch_id
FROM exploded
WHERE get_json_object(line, '$.Amount') IS NOT NULL;

-- ---------------------------------------------------------------- AR

CREATE OR REPLACE TABLE dl_silver_qbo_invoices AS
SELECT
    CAST(get_json_object(payload, '$.Id') AS STRING)      AS qbo_invoice_id,
    TRIM(get_json_object(payload, '$.DocNumber'))         AS doc_number,
    CAST(get_json_object(payload, '$.CustomerRef.value') AS STRING) AS qbo_customer_id,
    TRIM(get_json_object(payload, '$.CustomerRef.name'))  AS customer_name,
    CAST(SUBSTR(get_json_object(payload, '$.TxnDate'), 1, 10) AS DATE)  AS invoice_date,
    CAST(SUBSTR(get_json_object(payload, '$.DueDate'), 1, 10) AS DATE)  AS due_date,
    CAST(COALESCE(get_json_object(payload, '$.TotalAmt'), '0') AS DOUBLE) AS total_amount,
    CAST(COALESCE(get_json_object(payload, '$.Balance'), '0') AS DOUBLE)  AS open_balance,
    -- Derived rather than read from a status field: QBO has no single status
    -- attribute on an invoice, and Balance is the fact that matters.
    CASE WHEN CAST(COALESCE(get_json_object(payload, '$.Balance'), '0') AS DOUBLE) <= 0
         THEN TRUE ELSE FALSE END                          AS is_paid,
    _ingested_at,
    _batch_id
FROM dl_bronze_qbo_invoices
WHERE get_json_object(payload, '$.Id') IS NOT NULL;

CREATE OR REPLACE TABLE dl_silver_qbo_ar_open_items AS
SELECT
    qbo_invoice_id,
    doc_number,
    qbo_customer_id,
    customer_name,
    invoice_date,
    due_date,
    total_amount,
    open_balance,
    DATEDIFF(CURRENT_DATE, due_date)              AS days_past_due,
    CASE
        WHEN due_date IS NULL                          THEN 'Unknown'
        WHEN DATEDIFF(CURRENT_DATE, due_date) <= 0     THEN 'Current'
        WHEN DATEDIFF(CURRENT_DATE, due_date) <= 30    THEN '1-30'
        WHEN DATEDIFF(CURRENT_DATE, due_date) <= 60    THEN '31-60'
        WHEN DATEDIFF(CURRENT_DATE, due_date) <= 90    THEN '61-90'
        ELSE '90+'
    END                                            AS aging_bucket,
    _ingested_at,
    _batch_id
FROM dl_silver_qbo_invoices
WHERE NOT is_paid;
