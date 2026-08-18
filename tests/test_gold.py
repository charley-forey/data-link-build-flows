"""Run the REAL gold SQL against fixtures and assert the WIP identities.

This is the check that matters. Every number the Controller will look at comes
out of transformation/sql/gold/30_fct_wip.sql, and the whole point of testing it
here is that the file under test is the one that ships - not a Python
re-implementation of it that can drift.

Offline: no Fabric, no network, no Spark. DuckDB executes the same statements
Spark will, which verifies the LOGIC - joins, key resolution, the arithmetic,
the cumulative change-order roll-up. It does not verify Spark dialect edge
cases; those surface on the first real run and are caught by the DQ suite.

The gold files deliberately avoid Spark-only syntax so this works with almost no
compatibility shim. Every shim is a place where the test and production diverge,
so they are kept to date-function spellings only - never to anything that could
let a statement pass here and fail in Spark.

    python tests/test_gold.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "transformation" / "sql" / "gold"

sys.path.insert(0, str(ROOT / "platform" / "lib"))
from fabric_common import split_sql_statements  # noqa: E402

# Gold files exercised here. 11_dim_costcode_vendor_account is skipped: it uses
# Spark's SPLIT(...)[0] for the division roll-up, which is not needed by any WIP
# identity and would cost more shim than it is worth.
FILES = [
    "01_dim_date.sql",
    "10_dim_project.sql",
    "20_fct_budgetline_changeorder.sql",
    "22_fct_cost_billing.sql",
    "30_fct_wip.sql",
]

FIXTURES = """
CREATE OR REPLACE TABLE sv_projects AS SELECT * FROM (VALUES
    ('P1','Hospital Fitout','23-101','Active','Construction','Commercial','Phoenix','AZ','Phoenix','AZ',TRUE,DATE '2026-01-05',DATE '2026-12-01'),
    ('P2','School Cabling','23-102','Active','Construction','Education','Tucson','AZ','Tucson','AZ',TRUE,DATE '2026-02-01',DATE '2026-09-01'),
    ('P3','Retail Security','23-103','Active','Construction','Retail','Mesa','AZ','Mesa','AZ',TRUE,DATE '2026-03-01',DATE '2026-11-01')
) AS t(project_id,project_name,project_number,status,stage,project_type,office_name,region_name,city,state_code,is_active,start_date,completion_date);

-- P9 appears ONLY on a budget line. It must still reach dim_Project.
CREATE OR REPLACE TABLE sv_budget_lines AS SELECT * FROM (VALUES
    ('B1','P1','C10','16-100','16','Labor',  30000.0, 5000.0, 0.0, 35000.0, 20000.0, 10000.0, 25000.0, 50000.0, 25000.0, 0.0),
    ('B2','P1','C20','16-200','16','Material',20000.0, 5000.0, 0.0, 25000.0, 15000.0,  5000.0, 20000.0, 40000.0, 20000.0, 0.0),
    ('B3','P2','C10','16-100','16','Labor',  40000.0,    0.0, 0.0, 40000.0, 30000.0, 18000.0, 48000.0, 45000.0,     0.0, 0.0),
    -- P3 has revised_budget = 0 as well as EAC = 0, so the fallback chain is
    -- exercised end to end rather than short-circuiting on a populated budget.
    ('B4','P3','C10','16-100','16','Labor',   8000.0,    0.0, 0.0,     0.0,  1000.0,  1000.0,  2000.0,     0.0,  6000.0, 0.0),
    ('B5','P9','C10','16-100','16','Labor',   1000.0,    0.0, 0.0,  1000.0,     0.0,     0.0,   500.0,  1000.0,   500.0, 0.0)
) AS t(budget_line_id,project_id,cost_code_id,cost_code,root_cost_code,category,
       original_budget,approved_budget_changes,budget_modifications,revised_budget,
       committed_cost,direct_cost,
       job_to_date_cost,estimated_cost_at_completion,forecast_to_complete,projected_over_under);

CREATE OR REPLACE TABLE sv_prime_contracts AS SELECT * FROM (VALUES
    ('K1','P1','PC-001','Executed',TRUE,100000.0,100000.0,10.0,DATE '2026-01-05',DATE '2026-12-01'),
    ('K2','P2','PC-002','Executed',TRUE, 50000.0, 50000.0, 5.0,DATE '2026-02-01',DATE '2026-09-01'),
    ('K3','P3','PC-003','Executed',TRUE, 10000.0, 10000.0, 0.0,DATE '2026-03-01',DATE '2026-11-01')
) AS t(contract_id,project_id,contract_number,status,is_executed,original_contract,grand_total,
       retainage_percent,contract_start_date,contract_finish_date);

-- P1: one approved CO (counts) and one pending (must NOT count).
CREATE OR REPLACE TABLE sv_change_orders AS SELECT * FROM (VALUES
    ('CO1','P1','K1','prime','001','Added devices','Approved',TRUE, 20000.0,DATE '2026-03-01',DATE '2026-03-15'),
    ('CO2','P1','K1','prime','002','Pending scope','Pending', FALSE, 5000.0,DATE '2026-04-01',NULL),
    ('CO3','P1','K1','commitment','C-01','Sub change','Approved',TRUE, 9999.0,DATE '2026-03-01',DATE '2026-03-20')
) AS t(change_order_id,project_id,contract_id,change_order_scope,change_order_number,title,
       status,is_executed,amount,created_date,approved_date);

CREATE OR REPLACE TABLE sv_commitments AS SELECT * FROM (VALUES
    ('M1','P1','subcontract','SC-1','Approved','V1','Ace Electric',15000.0,TRUE)
) AS t(commitment_id,project_id,commitment_type,commitment_number,status,vendor_id,vendor_name,grand_total,is_executed);

CREATE OR REPLACE TABLE sv_direct_costs AS SELECT * FROM (VALUES
    ('D1','P1','Invoice','V1','Ace Electric','Approved',1000.0,DATE '2026-04-10')
) AS t(direct_cost_id,project_id,direct_cost_type,vendor_id,vendor_name,status,amount,cost_date);

CREATE OR REPLACE TABLE sv_payment_applications AS SELECT * FROM (VALUES
    ('PA1','P1','K1','1','Approved',70000.0,DATE '2026-04-30'),
    ('PA2','P2','K2','1','Approved',30000.0,DATE '2026-04-30')
) AS t(payment_application_id,project_id,contract_id,application_number,status,billed_amount,billing_date);

CREATE OR REPLACE TABLE sv_cost_codes AS SELECT * FROM (VALUES
    ('C10','P1','16-100','Labor','16-100')
) AS t(cost_code_id,project_id,cost_code,cost_code_name,full_code);

CREATE OR REPLACE TABLE sv_procore_vendors AS SELECT * FROM (VALUES
    ('V1','Ace Electric',TRUE)
) AS t(vendor_id,vendor_name,is_active);

CREATE OR REPLACE TABLE sv_qbo_vendors AS SELECT * FROM (VALUES
    ('QV1','Ace Electric',TRUE)
) AS t(vendor_id,vendor_name,is_active);

CREATE OR REPLACE TABLE sv_qbo_jobs AS SELECT * FROM (VALUES
    ('Q1','23-101 Hospital','Acme:23-101 Hospital',TRUE)
) AS t(qbo_customer_id,display_name,fully_qualified_name,is_active);

CREATE OR REPLACE TABLE sv_qbo_accounts AS SELECT * FROM (VALUES
    ('A1','Job Materials','Job Expenses:Job Materials','5010','Expense','Cost of Goods Sold','Supplies'),
    ('A2','Sales','Income:Sales','4010','Revenue','Income','SalesOfProductIncome')
) AS t(qbo_account_id,account_name,account_full_name,account_number,classification,account_type,account_sub_type);

-- QuickBooks says 40,000 of cost on P1 where Procore says 45,000. The 5,000
-- difference is the whole point of the reconciliation column.
-- The revenue line must be excluded: it is not job cost.
CREATE OR REPLACE TABLE sv_gl_transactions AS SELECT * FROM (VALUES
    ('G1','Bill','T1','B-100',DATE '2026-04-01','Q1','QV1','Ace Electric','A1','CL1','Wire',40000.0),
    ('G2','Invoice','T2','I-1',DATE '2026-04-02','Q1','QV1','Ace Electric','A2','CL1','Billing',70000.0)
) AS t(gl_transaction_key,txn_type,txn_id,doc_number,txn_date,qbo_customer_id,qbo_vendor_id,
       vendor_name,qbo_account_id,qbo_class_id,line_description,posted_amount);

CREATE OR REPLACE TABLE sv_qbo_invoices AS SELECT * FROM (VALUES
    ('I1','INV-1','Q1','Acme:23-101 Hospital',DATE '2026-04-02',DATE '2026-05-02',70000.0,70000.0)
) AS t(qbo_invoice_id,doc_number,qbo_customer_id,customer_name,invoice_date,due_date,total_amount,open_balance);

CREATE OR REPLACE TABLE sv_crosswalk AS SELECT * FROM (VALUES
    ('P1','P1','23-101','Hospital Fitout','Q1','Acme:23-101 Hospital',NULL,'manual',1.00,TRUE),
    ('P2','P2','23-102','School Cabling',NULL,NULL,NULL,'unmatched',0.00,FALSE),
    ('P3','P3','23-103','Retail Security',NULL,NULL,NULL,'unmatched',0.00,FALSE)
) AS t(project_key,procore_project_id,procore_project_number,procore_project_name,
       qbo_customer_id,qbo_fully_qualified_name,hubspot_deal_id,match_method,confidence,is_mapped);
"""


def to_duckdb(sql: str) -> str:
    """Map the handful of Spark spellings used by the gold files and dq.py.

    Gold SQL is deliberately quote-free so it needs almost nothing. The DQ
    builders quote identifiers with backticks (Spark); DuckDB wants double
    quotes. CURRENT_TIMESTAMP() is a Spark function call, a bare keyword in
    DuckDB.
    """
    # NOTE: there is deliberately no `SELECT * EXCEPT (...)` -> EXCLUDE mapping.
    # DuckDB accepts the EXCEPT-star form and Fabric's Spark rejects it, so a
    # shim here would let a statement pass every offline test and then fail in
    # production. It did exactly that once. The SQL now spells columns out.
    sql = re.sub(r"CURRENT_TIMESTAMP\s*\(\s*\)", "CURRENT_TIMESTAMP", sql, flags=re.IGNORECASE)
    # dim_Date only: Spark generates a date range with explode(sequence(...)) and
    # formats month names with Java patterns. DuckDB spells both differently.
    sql = re.sub(r"explode\s*\(\s*sequence\s*\(", "unnest(generate_series(", sql, flags=re.IGNORECASE)
    sql = re.sub(r"date_format\s*\(([^,]+),\s*'MMMM'\s*\)", r"strftime(\1, '%B')", sql, flags=re.IGNORECASE)
    sql = re.sub(r"date_format\s*\(([^,]+),\s*'MMM'\s*\)", r"strftime(\1, '%b')", sql, flags=re.IGNORECASE)
    return sql.replace("`", '"')


def run_gold(con: duckdb.DuckDBPyConnection) -> None:
    for name in FILES:
        sql = to_duckdb((GOLD / name).read_text(encoding="utf-8"))
        for statement in split_sql_statements(sql):
            try:
                con.execute(statement)
            except Exception as exc:  # noqa: BLE001
                raise AssertionError(f"{name}: {exc}\n---\n{statement[:400]}") from exc


def one(con: duckdb.DuckDBPyConnection, sql: str):
    row = con.execute(sql).fetchone()
    assert row is not None, f"no row: {sql}"
    return row[0]


def close(actual: float, expected: float, label: str, tol: float = 0.01) -> None:
    assert abs(actual - expected) < tol, f"{label}: expected {expected}, got {actual}"


def main() -> int:
    con = duckdb.connect(":memory:")
    con.execute("SET TimeZone='UTC'")
    for statement in split_sql_statements(FIXTURES):
        con.execute(statement)

    run_gold(con)
    checks = 0

    # ---------------------------------------------------------- dim_Project
    # A project seen only on a budget line must still exist, flagged.
    assert one(con, "SELECT COUNT(*) FROM dim_Project WHERE ProjectKey='P9'") == 1, \
        "late-arriving project P9 was dropped - facts would be silently understated"
    assert one(con, "SELECT IsInProcore FROM dim_Project WHERE ProjectKey='P9'") is False
    assert one(con, "SELECT IsInCrosswalk FROM dim_Project WHERE ProjectKey='P1'") is True
    assert one(con, "SELECT IsInCrosswalk FROM dim_Project WHERE ProjectKey='P2'") is False
    assert one(con, "SELECT ProjectName FROM dim_Project WHERE ProjectKey='P9'") == "Project P9"
    checks += 5

    # ---------------------------------------------------------- change orders
    # Approved prime only: the pending 5,000 and the commitment 9,999 must not
    # reach contract value.
    close(one(con, "SELECT ApprovedChangeOrders FROM fct_WIP WHERE ProjectKey='P1'"),
          20000.0, "P1 approved change orders")
    close(one(con, "SELECT PendingChangeOrders FROM fct_WIP WHERE ProjectKey='P1'"),
          5000.0, "P1 pending change orders")
    checks += 2

    # ---------------------------------------------------------- P1 identities
    # Contract 100,000 + approved CO 20,000 = 120,000
    # EAC 90,000, cost to date 45,000 -> 50% complete
    p1 = con.execute("""
        SELECT RevisedContract, EAC, CostToDate, PercentComplete, EarnedRevenue,
               CostToComplete, GrossProfitAtCompletion, GrossProfitPctAtCompletion,
               EarnedGrossProfit, BilledToDate, OverBilling, UnderBilling, Backlog,
               CostToDateQbo, CostVariance
        FROM fct_WIP WHERE ProjectKey='P1'
    """).fetchone()
    assert p1 is not None, "P1 missing from fct_WIP"
    (revised, eac, ctd, pct, earned, ctc, gp, gp_pct,
     earned_gp, billed, over, under, backlog, qbo_cost, variance) = p1

    close(revised, 120000.0, "P1 revised contract")
    close(eac, 90000.0, "P1 EAC")
    close(ctd, 45000.0, "P1 cost to date")
    close(pct, 0.50, "P1 percent complete")
    close(earned, 60000.0, "P1 earned revenue")
    close(ctc, 45000.0, "P1 cost to complete")
    close(gp, 30000.0, "P1 gross profit at completion")
    close(gp_pct, 0.25, "P1 GP% at completion")
    close(earned_gp, 15000.0, "P1 earned gross profit")
    close(billed, 70000.0, "P1 billed to date")
    close(over, 10000.0, "P1 over-billing")
    close(under, 0.0, "P1 under-billing")
    close(backlog, 60000.0, "P1 backlog")
    # The revenue GL line must be excluded from cost.
    close(qbo_cost, 40000.0, "P1 QuickBooks cost (revenue line must be excluded)")
    close(variance, 5000.0, "P1 Procore-QuickBooks cost variance")
    checks += 15

    # ---------------------------------------------------------- P2: over-running
    # Cost 48,000 against EAC 45,000. Percent complete must CAP at 1.0 for
    # revenue while the raw value stays visible.
    p2 = con.execute("""
        SELECT PercentComplete, PercentCompleteRaw, EarnedRevenue, EarnedGrossProfit,
               OverBilling, UnderBilling, Backlog
        FROM fct_WIP WHERE ProjectKey='P2'
    """).fetchone()
    assert p2 is not None, "P2 missing from fct_WIP"
    close(p2[0], 1.0, "P2 percent complete is capped")
    assert p2[1] > 1.0, "P2 raw percent complete must stay above 1.0 so over-run is visible"
    close(p2[2], 50000.0, "P2 earned revenue cannot exceed the contract")
    close(p2[3], 2000.0, "P2 earned gross profit")
    close(p2[4], 0.0, "P2 over-billing")
    close(p2[5], 20000.0, "P2 under-billing")
    close(p2[6], 0.0, "P2 backlog")
    checks += 7

    # ---------------------------------------------------------- P3: EAC fallback
    # The budget view reports EAC = 0. Falling back to the revised budget stops
    # a divide-by-zero making the job look complete with no cost.
    p3 = con.execute("""
        SELECT EAC, PercentComplete, EarnedRevenue FROM fct_WIP WHERE ProjectKey='P3'
    """).fetchone()
    assert p3 is not None, "P3 missing from fct_WIP"
    close(p3[0], 8000.0, "P3 EAC falls back to revised budget when Procore reports 0")
    close(p3[1], 0.25, "P3 percent complete")
    close(p3[2], 2500.0, "P3 earned revenue")
    checks += 3

    # ---------------------------------------------------------- invariants
    # These must hold for every row, and are the same assertions the DQ suite
    # runs against live data.
    assert one(con, """
        SELECT COUNT(*) FROM fct_WIP
        WHERE ABS(RevisedContract - (OriginalContract + ApprovedChangeOrders)) > 0.01
    """) == 0, "revised contract must equal original plus approved change orders"

    assert one(con, "SELECT COUNT(*) FROM fct_WIP WHERE PercentComplete < 0 OR PercentComplete > 1.0") == 0
    assert one(con, "SELECT COUNT(*) FROM fct_WIP WHERE OverBilling > 0 AND UnderBilling > 0") == 0, \
        "a project cannot be both over- and under-billed"
    assert one(con, """
        SELECT COUNT(*) FROM fct_WIP
        WHERE ABS((EarnedRevenue - CostToDate) - EarnedGrossProfit) > 0.01
    """) == 0, "earned gross profit must equal earned revenue less cost to date"
    assert one(con, """
        SELECT COUNT(*) FROM fct_WIP w
        LEFT JOIN dim_Project p ON p.ProjectKey = w.ProjectKey
        WHERE p.ProjectKey IS NULL
    """) == 0, "every WIP row must resolve to a project"
    checks += 5

    # P9 has a budget but no contract, so it is not a work in progress.
    assert one(con, "SELECT COUNT(*) FROM fct_WIP WHERE ProjectKey='P9'") == 1, \
        "P9 has a revised budget, so it belongs on the schedule"
    checks += 1

    checks += check_expectations(con)

    print(f"test_gold: {checks} assertions passed across {len(FILES)} gold SQL files")
    return 0


def check_expectations(con: duckdb.DuckDBPyConnection) -> int:
    """Execute the real DQ expectations against the fixture warehouse.

    Two things are proved here, and the second is the one that bites in
    production: that every expectation's SQL is VALID (a check that throws is a
    check that silently never ran), and that clean data passes the blocking
    gate. An expectation nobody has executed is a comment.

    Only expectations whose tables exist in the fixture set are run - the bronze
    freshness and token-age checks need ingestion, which is not what this file
    tests.
    """
    sys.path.insert(0, str(ROOT / "transformation" / "dq"))
    from expectations import all_expectations  # noqa: PLC0415

    from dq import SEVERITY_ERROR  # noqa: PLC0415

    present = {
        row[0].lower()
        for row in con.execute(
            "SELECT table_name FROM information_schema.tables"
        ).fetchall()
    }

    ran = 0
    for expectation in all_expectations():
        if expectation.table.lower() not in present:
            continue
        try:
            failing = con.execute(to_duckdb(expectation.failing_sql)).fetchall()
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(
                f"expectation {expectation.name!r} has invalid SQL: {exc}"
            ) from exc
        ran += 1
        if expectation.severity == SEVERITY_ERROR:
            assert not failing, (
                f"blocking expectation {expectation.name!r} failed on clean fixture "
                f"data ({len(failing)} rows) - either the gold build or the "
                f"expectation is wrong"
            )

    # P2 over-runs its EAC on purpose, so the two warnings that describe that
    # condition must actually fire. A warning that never fires is not a warning.
    overrun = con.execute(
        "SELECT COUNT(*) FROM fct_WIP WHERE PercentCompleteRaw > 1.0"
    ).fetchone()
    assert overrun and overrun[0] == 1, "the EAC over-run warning should fire for P2"

    # P2 and P3 are unmapped and hold contracts, so the crosswalk warning fires.
    unmapped = con.execute(
        "SELECT COUNT(*) FROM dim_Project WHERE NOT IsInCrosswalk AND OriginalContract > 0"
    ).fetchone()
    assert unmapped and unmapped[0] == 2, "the unmapped-project warning should fire for P2 and P3"

    print(f"test_gold: {ran} data-quality expectations executed, no blocking failures")
    return ran + 2


if __name__ == "__main__":
    raise SystemExit(main())
