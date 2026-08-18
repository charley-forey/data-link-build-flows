-- fct_CostTransaction and fct_Billing.
--
-- These two are where Procore and QuickBooks meet. Cost comes from BOTH
-- systems, tagged with which one it came from, because the reconciliation
-- between them is a deliverable in its own right - the Controller needs to see
-- where the two disagree, not a blended number that hides it.

-- ---------------------------------------------------------------- cost
--
-- Grain: one cost transaction line.
--
-- SourceSystem is the discriminator. Nothing sums across it without saying so:
-- adding Procore direct costs to QuickBooks bills DOUBLE COUNTS, because the
-- same invoice is usually in both. Every measure filters to one source, and the
-- variance measure is the deliberate exception.

CREATE OR REPLACE TABLE fct_CostTransaction AS
-- Procore arm: direct costs, attributed by project directly.
SELECT
    d.project_id                                   AS ProjectKey,
    '0'                                            AS CostCodeKey,
    COALESCE(
        UPPER(REGEXP_REPLACE(COALESCE(d.vendor_name, ''), '[^A-Za-z0-9]', '')),
        '0')                                       AS VendorKey,
    '0'                                            AS AccountKey,
    'procore'                                      AS SourceSystem,
    CONCAT('procore|', d.direct_cost_id)           AS CostTransactionKey,
    COALESCE(d.direct_cost_type, 'Direct Cost')    AS TransactionType,
    CAST(NULL AS STRING)                           AS DocNumber,
    d.cost_date                                    AS TransactionDate,
    CASE WHEN d.cost_date BETWEEN DATE '2015-01-01' AND DATE '2035-12-31'
         THEN make_date(year(d.cost_date), month(d.cost_date), 1) END AS MonthStart,
    CASE WHEN d.cost_date IS NOT NULL
              AND d.cost_date NOT BETWEEN DATE '2015-01-01' AND DATE '2035-12-31'
         THEN TRUE ELSE FALSE END                  AS HasOutOfRangeDate,
    CAST(d.amount AS DOUBLE)                       AS Amount
FROM sv_direct_costs d
WHERE d.project_id IS NOT NULL

UNION ALL

-- QuickBooks arm: GL lines, attributed by the crosswalk.
--
-- A GL line whose customer does not resolve to a project keeps ProjectKey NULL
-- rather than being dropped. It is real company cost that is simply not job
-- costed, and the "unattributed cost" figure it produces is one of the more
-- useful things on the reconciliation page.
SELECT
    x.procore_project_id                           AS ProjectKey,
    '0'                                            AS CostCodeKey,
    COALESCE(
        UPPER(REGEXP_REPLACE(COALESCE(g.vendor_name, ''), '[^A-Za-z0-9]', '')),
        '0')                                       AS VendorKey,
    COALESCE(g.qbo_account_id, '0')                AS AccountKey,
    'quickbooks'                                   AS SourceSystem,
    CONCAT('qbo|', g.gl_transaction_key)           AS CostTransactionKey,
    g.txn_type                                     AS TransactionType,
    g.doc_number                                   AS DocNumber,
    g.txn_date                                     AS TransactionDate,
    CASE WHEN g.txn_date BETWEEN DATE '2015-01-01' AND DATE '2035-12-31'
         THEN make_date(year(g.txn_date), month(g.txn_date), 1) END AS MonthStart,
    CASE WHEN g.txn_date IS NOT NULL
              AND g.txn_date NOT BETWEEN DATE '2015-01-01' AND DATE '2035-12-31'
         THEN TRUE ELSE FALSE END                  AS HasOutOfRangeDate,
    CAST(g.posted_amount AS DOUBLE)                AS Amount
FROM sv_gl_transactions g
LEFT JOIN sv_crosswalk x ON x.qbo_customer_id = g.qbo_customer_id AND x.is_mapped
LEFT JOIN sv_qbo_accounts a ON a.qbo_account_id = g.qbo_account_id
-- Job cost only. Including revenue and balance-sheet lines would make "cost"
-- mean the entire general ledger.
WHERE a.account_type IN ('Cost of Goods Sold', 'Expense', 'Other Expense');

-- ---------------------------------------------------------------- billings
--
-- Grain: one owner billing.
--
-- Procore payment applications are the primary source because they carry the
-- contract linkage and the schedule-of-values detail. QuickBooks invoices are
-- loaded alongside as the CHECK - if the two disagree, someone billed outside
-- the process, and that is worth seeing.

CREATE OR REPLACE TABLE fct_Billing AS
SELECT
    pa.project_id                                  AS ProjectKey,
    'procore'                                      AS SourceSystem,
    CONCAT('procore|', pa.payment_application_id)  AS BillingKey,
    pa.contract_id                                 AS ContractId,
    pa.application_number                          AS DocNumber,
    COALESCE(pa.status, 'Unknown')                 AS Status,
    pa.billing_date                                AS BillingDate,
    CASE WHEN pa.billing_date BETWEEN DATE '2015-01-01' AND DATE '2035-12-31'
         THEN make_date(year(pa.billing_date), month(pa.billing_date), 1) END AS MonthStart,
    CAST(pa.billed_amount AS DOUBLE)               AS BilledAmount,
    CAST(0 AS DOUBLE)                              AS OpenBalance
FROM sv_payment_applications pa
WHERE pa.project_id IS NOT NULL

UNION ALL

SELECT
    x.procore_project_id                           AS ProjectKey,
    'quickbooks'                                   AS SourceSystem,
    CONCAT('qbo|', i.qbo_invoice_id)               AS BillingKey,
    CAST(NULL AS STRING)                           AS ContractId,
    i.doc_number                                   AS DocNumber,
    CASE WHEN i.open_balance <= 0 THEN 'Paid' ELSE 'Open' END AS Status,
    i.invoice_date                                 AS BillingDate,
    CASE WHEN i.invoice_date BETWEEN DATE '2015-01-01' AND DATE '2035-12-31'
         THEN make_date(year(i.invoice_date), month(i.invoice_date), 1) END AS MonthStart,
    CAST(i.total_amount AS DOUBLE)                 AS BilledAmount,
    CAST(i.open_balance AS DOUBLE)                 AS OpenBalance
FROM sv_qbo_invoices i
JOIN sv_crosswalk x ON x.qbo_customer_id = i.qbo_customer_id AND x.is_mapped;
