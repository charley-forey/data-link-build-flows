-- Source views: the ONLY thing the gold layer reads.
--
-- Every gold file below reads sv_* and nothing else. Two reasons, and both are
-- load-bearing:
--
-- 1. PORTABILITY. Gold SQL that references silver tables directly picks up
--    their quoting rules, and Spark wants backticks where DuckDB wants double
--    quotes. Renaming once here means every gold file is quote-free and runs
--    unchanged on both - which is what lets `python scripts/run_tests.py`
--    validate the real transform logic offline, with no Fabric and no network.
--
-- 2. A SINGLE SWITCH. These views are the seam between silver and gold. Change
--    where silver lives - a different lakehouse, a different schema, fixture
--    tables during a test - and only this file changes. Every gold file keeps
--    working untouched.
--
-- A view for a source that does not exist yet is declared EMPTY WITH REAL TYPES
-- rather than omitted, so downstream can UNION both arms unconditionally and
-- stay source-agnostic. WHERE 1=0 keeps the column types without reading
-- anything. That is how the HubSpot arm of the model is built and tested before
-- HubSpot ingestion exists.

-- ---------------------------------------------------------------- Procore

CREATE OR REPLACE TEMPORARY VIEW sv_projects AS
SELECT
    CAST(project_id      AS STRING) AS project_id,
    CAST(project_name    AS STRING) AS project_name,
    CAST(project_number  AS STRING) AS project_number,
    CAST(status          AS STRING) AS status,
    CAST(stage           AS STRING) AS stage,
    CAST(project_type    AS STRING) AS project_type,
    CAST(office_name     AS STRING) AS office_name,
    CAST(region_name     AS STRING) AS region_name,
    CAST(city            AS STRING) AS city,
    CAST(state_code      AS STRING) AS state_code,
    CAST(is_active       AS BOOLEAN) AS is_active,
    CAST(start_date      AS DATE)   AS start_date,
    CAST(completion_date AS DATE)   AS completion_date
FROM dl_silver_projects;

CREATE OR REPLACE TEMPORARY VIEW sv_budget_lines AS
SELECT
    CAST(budget_line_id   AS STRING) AS budget_line_id,
    CAST(project_id       AS STRING) AS project_id,
    CAST(cost_code_id     AS STRING) AS cost_code_id,
    CAST(cost_code        AS STRING) AS cost_code,
    CAST(root_cost_code   AS STRING) AS root_cost_code,
    CAST(category         AS STRING) AS category,
    -- Explicit DOUBLE on every money column. DuckDB widens SUM(DOUBLE) to
    -- DECIMAL while Spark keeps DOUBLE, and the semantic model infers its types
    -- from the build. A mismatch makes the table fail to load in Direct Lake
    -- SILENTLY - it appears as a missing table, not an error.
    CAST(original_budget              AS DOUBLE) AS original_budget,
    CAST(approved_budget_changes      AS DOUBLE) AS approved_budget_changes,
    CAST(approved_change_orders       AS DOUBLE) AS approved_change_orders,
    CAST(pending_change_orders        AS DOUBLE) AS pending_change_orders,
    CAST(projected_costs              AS DOUBLE) AS projected_costs,
    CAST(projected_budget             AS DOUBLE) AS projected_budget,
    CAST(budget_modifications         AS DOUBLE) AS budget_modifications,
    CAST(revised_budget               AS DOUBLE) AS revised_budget,
    CAST(committed_cost               AS DOUBLE) AS committed_cost,
    CAST(direct_cost                  AS DOUBLE) AS direct_cost,
    CAST(job_to_date_cost             AS DOUBLE) AS job_to_date_cost,
    CAST(estimated_cost_at_completion AS DOUBLE) AS estimated_cost_at_completion,
    CAST(forecast_to_complete         AS DOUBLE) AS forecast_to_complete,
    CAST(projected_over_under         AS DOUBLE) AS projected_over_under
FROM dl_silver_budget_lines;

CREATE OR REPLACE TEMPORARY VIEW sv_prime_contracts AS
SELECT
    CAST(contract_id            AS STRING) AS contract_id,
    CAST(project_id             AS STRING) AS project_id,
    CAST(contract_number        AS STRING) AS contract_number,
    CAST(status                 AS STRING) AS status,
    CAST(is_executed            AS BOOLEAN) AS is_executed,
    CAST(original_contract      AS DOUBLE) AS original_contract,
    CAST(grand_total            AS DOUBLE) AS grand_total,
    CAST(retainage_percent      AS DOUBLE) AS retainage_percent,
    CAST(contract_start_date    AS DATE)   AS contract_start_date,
    CAST(contract_finish_date   AS DATE)   AS contract_finish_date
FROM dl_silver_prime_contracts;

CREATE OR REPLACE TEMPORARY VIEW sv_change_orders AS
SELECT
    CAST(change_order_id     AS STRING) AS change_order_id,
    CAST(project_id          AS STRING) AS project_id,
    CAST(contract_id         AS STRING) AS contract_id,
    CAST(change_order_scope  AS STRING) AS change_order_scope,
    CAST(change_order_number AS STRING) AS change_order_number,
    CAST(title               AS STRING) AS title,
    CAST(status              AS STRING) AS status,
    CAST(is_executed         AS BOOLEAN) AS is_executed,
    CAST(amount              AS DOUBLE) AS amount,
    CAST(created_date        AS DATE)   AS created_date,
    CAST(approved_date       AS DATE)   AS approved_date
FROM dl_silver_change_orders;

CREATE OR REPLACE TEMPORARY VIEW sv_commitments AS
SELECT
    CAST(commitment_id     AS STRING) AS commitment_id,
    CAST(project_id        AS STRING) AS project_id,
    CAST(commitment_type   AS STRING) AS commitment_type,
    CAST(commitment_number AS STRING) AS commitment_number,
    CAST(status            AS STRING) AS status,
    CAST(vendor_id         AS STRING) AS vendor_id,
    CAST(vendor_name       AS STRING) AS vendor_name,
    CAST(grand_total       AS DOUBLE) AS grand_total,
    CAST(is_executed       AS BOOLEAN) AS is_executed
FROM dl_silver_commitments;

CREATE OR REPLACE TEMPORARY VIEW sv_direct_costs AS
SELECT
    CAST(direct_cost_id   AS STRING) AS direct_cost_id,
    CAST(project_id       AS STRING) AS project_id,
    CAST(direct_cost_type AS STRING) AS direct_cost_type,
    CAST(vendor_id        AS STRING) AS vendor_id,
    CAST(vendor_name      AS STRING) AS vendor_name,
    CAST(status           AS STRING) AS status,
    CAST(amount           AS DOUBLE) AS amount,
    CAST(cost_date        AS DATE)   AS cost_date
FROM dl_silver_direct_costs;

CREATE OR REPLACE TEMPORARY VIEW sv_payment_applications AS
SELECT
    CAST(payment_application_id AS STRING) AS payment_application_id,
    CAST(project_id             AS STRING) AS project_id,
    CAST(contract_id            AS STRING) AS contract_id,
    CAST(application_number     AS STRING) AS application_number,
    CAST(status                 AS STRING) AS status,
    CAST(billed_amount          AS DOUBLE) AS billed_amount,
    CAST(billing_date           AS DATE)   AS billing_date
FROM dl_silver_payment_applications;

CREATE OR REPLACE TEMPORARY VIEW sv_cost_codes AS
SELECT
    CAST(cost_code_id   AS STRING) AS cost_code_id,
    CAST(project_id     AS STRING) AS project_id,
    CAST(cost_code      AS STRING) AS cost_code,
    CAST(cost_code_name AS STRING) AS cost_code_name,
    CAST(full_code      AS STRING) AS full_code
FROM dl_silver_cost_codes;

CREATE OR REPLACE TEMPORARY VIEW sv_procore_vendors AS
SELECT
    CAST(vendor_id   AS STRING) AS vendor_id,
    CAST(vendor_name AS STRING) AS vendor_name,
    CAST(is_active   AS BOOLEAN) AS is_active
FROM dl_silver_vendors;

-- ---------------------------------------------------------------- QuickBooks

CREATE OR REPLACE TEMPORARY VIEW sv_qbo_jobs AS
SELECT
    CAST(qbo_customer_id       AS STRING) AS qbo_customer_id,
    CAST(display_name          AS STRING) AS display_name,
    CAST(fully_qualified_name  AS STRING) AS fully_qualified_name,
    CAST(is_active             AS BOOLEAN) AS is_active
FROM dl_silver_qbo_jobs;

CREATE OR REPLACE TEMPORARY VIEW sv_gl_transactions AS
SELECT
    CAST(gl_transaction_key AS STRING) AS gl_transaction_key,
    CAST(txn_type           AS STRING) AS txn_type,
    CAST(txn_id             AS STRING) AS txn_id,
    CAST(doc_number         AS STRING) AS doc_number,
    CAST(txn_date           AS DATE)   AS txn_date,
    CAST(qbo_customer_id    AS STRING) AS qbo_customer_id,
    CAST(qbo_vendor_id      AS STRING) AS qbo_vendor_id,
    CAST(vendor_name        AS STRING) AS vendor_name,
    CAST(qbo_account_id     AS STRING) AS qbo_account_id,
    CAST(qbo_class_id       AS STRING) AS qbo_class_id,
    CAST(line_description   AS STRING) AS line_description,
    CAST(posted_amount      AS DOUBLE) AS posted_amount
FROM dl_silver_gl_transactions;

CREATE OR REPLACE TEMPORARY VIEW sv_qbo_accounts AS
SELECT
    CAST(qbo_account_id    AS STRING) AS qbo_account_id,
    CAST(account_name      AS STRING) AS account_name,
    CAST(account_full_name AS STRING) AS account_full_name,
    CAST(account_number    AS STRING) AS account_number,
    CAST(classification    AS STRING) AS classification,
    CAST(account_type      AS STRING) AS account_type,
    CAST(account_sub_type  AS STRING) AS account_sub_type
FROM dl_silver_qbo_accounts;

CREATE OR REPLACE TEMPORARY VIEW sv_qbo_vendors AS
SELECT
    CAST(qbo_vendor_id AS STRING) AS vendor_id,
    CAST(vendor_name   AS STRING) AS vendor_name,
    CAST(is_active     AS BOOLEAN) AS is_active
FROM dl_silver_qbo_vendors;

CREATE OR REPLACE TEMPORARY VIEW sv_qbo_invoices AS
SELECT
    CAST(qbo_invoice_id  AS STRING) AS qbo_invoice_id,
    CAST(doc_number      AS STRING) AS doc_number,
    CAST(qbo_customer_id AS STRING) AS qbo_customer_id,
    CAST(customer_name   AS STRING) AS customer_name,
    CAST(invoice_date    AS DATE)   AS invoice_date,
    CAST(due_date        AS DATE)   AS due_date,
    CAST(total_amount    AS DOUBLE) AS total_amount,
    CAST(open_balance    AS DOUBLE) AS open_balance
FROM dl_silver_qbo_invoices;

-- ---------------------------------------------------------------- crosswalk

CREATE OR REPLACE TEMPORARY VIEW sv_crosswalk AS
SELECT
    CAST(ProjectKey               AS STRING) AS project_key,
    CAST(procore_project_id       AS STRING) AS procore_project_id,
    CAST(procore_project_number   AS STRING) AS procore_project_number,
    CAST(procore_project_name     AS STRING) AS procore_project_name,
    CAST(qbo_customer_id          AS STRING) AS qbo_customer_id,
    CAST(qbo_fully_qualified_name AS STRING) AS qbo_fully_qualified_name,
    CAST(hubspot_deal_id          AS STRING) AS hubspot_deal_id,
    CAST(match_method             AS STRING) AS match_method,
    CAST(confidence               AS DOUBLE) AS confidence,
    CAST(is_mapped                AS BOOLEAN) AS is_mapped
FROM dl_silver_project_crosswalk;

-- ---------------------------------------------------------------- HubSpot
--
-- EMPTY BY CONSTRUCTION until phase 2. Declared rather than omitted so the gold
-- files that reference it compile, run and test today, and go live by changing
-- this one view - not by editing every consumer.

CREATE OR REPLACE TEMPORARY VIEW sv_deals AS
SELECT
    CAST(NULL AS STRING) AS deal_id,
    CAST(NULL AS STRING) AS deal_name,
    CAST(NULL AS STRING) AS pipeline_id,
    CAST(NULL AS STRING) AS stage_id,
    CAST(NULL AS STRING) AS owner_id,
    CAST(NULL AS DOUBLE) AS amount,
    CAST(NULL AS DOUBLE) AS stage_probability,
    CAST(NULL AS DATE)   AS close_date,
    CAST(NULL AS DATE)   AS create_date,
    CAST(NULL AS BOOLEAN) AS is_closed,
    CAST(NULL AS BOOLEAN) AS is_closed_won
WHERE 1 = 0;
