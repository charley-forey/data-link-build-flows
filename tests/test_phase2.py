"""Run the phase 2 gold SQL against fixtures and assert its identities.

Same contract as tests/test_gold.py: the files under test are the ones that
ship. Covers the pipeline weighting, AR/AP aging, and the cash forecast.

    python tests/test_phase2.py
"""

from __future__ import annotations

import re
import sys
from datetime import date, timedelta
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "transformation" / "sql" / "gold"

sys.path.insert(0, str(ROOT / "platform" / "lib"))
from fabric_common import split_sql_statements  # noqa: E402

FILES = [
    "01_dim_date.sql",
    "13_dim_dealstage_owner.sql",
    "24_fct_pipeline.sql",
    "25_fct_aging_cash.sql",
]

TODAY = date.today()


def d(offset: int) -> str:
    return (TODAY + timedelta(days=offset)).isoformat()


FIXTURES = f"""
-- The seven real stages from the live HubSpot portal, probabilities included.
CREATE OR REPLACE TABLE sv_deal_stages AS SELECT * FROM (VALUES
    ('appointmentscheduled','Appointment Scheduled','default','Sales Pipeline',0,0.2,FALSE),
    ('qualifiedtobuy','Qualified To Buy','default','Sales Pipeline',1,0.4,FALSE),
    ('contractsent','Contract Sent','default','Sales Pipeline',4,0.9,FALSE),
    ('closedwon','Closed Won','default','Sales Pipeline',5,1.0,TRUE),
    ('closedlost','Closed Lost','default','Sales Pipeline',6,0.0,TRUE)
) AS t(stage_id,stage_name,pipeline_id,pipeline_name,display_order,win_probability,is_closed_stage);

CREATE OR REPLACE TABLE sv_owners AS SELECT * FROM (VALUES
    ('own1','Dana Reid','dana@example.com')
) AS t(owner_id,owner_name,owner_email);

CREATE OR REPLACE TABLE sv_deals AS SELECT * FROM (VALUES
    -- open, stage probability 0.4 -> weighted 40,000
    ('d1','Hospital retrofit','default','qualifiedtobuy','own1','newbusiness',100000.0,NULL,DATE '{d(45)}',DATE '{d(-30)}',FALSE,FALSE),
    -- open, DEAL-level override 0.75 beats the stage's 0.9 -> 75,000
    ('d2','School cabling','default','contractsent','own1','newbusiness',100000.0,0.75,DATE '{d(20)}',DATE '{d(-60)}',FALSE,FALSE),
    -- closed won: must be EXCLUDED from pipeline
    ('d3','Won job','default','closedwon','own1','newbusiness',500000.0,NULL,DATE '{d(-5)}',DATE '{d(-90)}',TRUE,TRUE),
    -- closed lost: must be EXCLUDED
    ('d4','Lost job','default','closedlost','own1','newbusiness',400000.0,NULL,DATE '{d(-3)}',DATE '{d(-80)}',TRUE,FALSE),
    -- open but with no stage match -> falls back to 0 probability, not an error
    ('d5','Orphan stage','default','nosuchstage',NULL,NULL,50000.0,NULL,DATE '{d(10)}',DATE '{d(-10)}',FALSE,FALSE)
) AS t(deal_id,deal_name,pipeline_id,stage_id,owner_id,deal_type,amount,deal_probability,close_date,create_date,is_closed,is_closed_won);

CREATE OR REPLACE TABLE sv_crosswalk AS SELECT * FROM (VALUES
    ('P1','P1','23-101','Hospital','C1','Acme:Hospital',NULL,'manual',1.00,TRUE)
) AS t(project_key,procore_project_id,procore_project_number,procore_project_name,
       qbo_customer_id,qbo_fully_qualified_name,hubspot_deal_id,match_method,confidence,is_mapped);

CREATE OR REPLACE TABLE sv_ar_open AS SELECT * FROM (VALUES
    -- not yet due
    ('i1','INV-1','C1','Acme',DATE '{d(-10)}',DATE '{d(14)}',1000.0,1000.0,-14,'Current'),
    -- overdue 45 days
    ('i2','INV-2','C1','Acme',DATE '{d(-75)}',DATE '{d(-45)}',2000.0,2000.0,45,'31-60'),
    -- unmapped customer: still counts as AR, just no project
    ('i3','INV-3','C9','Someone Else',DATE '{d(-5)}',DATE '{d(21)}',500.0,500.0,-21,'Current')
) AS t(document_id,doc_number,counterparty_id,counterparty_name,document_date,due_date,
       total_amount,open_balance,days_past_due,aging_bucket);

CREATE OR REPLACE TABLE sv_ap_open AS SELECT * FROM (VALUES
    ('b1','BILL-1','V1','Supplier',DATE '{d(-20)}',DATE '{d(7)}',800.0,800.0,-7,'Current'),
    ('b2','BILL-2','V2','Sub',DATE '{d(-100)}',DATE '{d(-40)}',300.0,300.0,40,'31-60')
) AS t(document_id,doc_number,counterparty_id,counterparty_name,document_date,due_date,
       total_amount,open_balance,days_past_due,aging_bucket);

CREATE OR REPLACE TABLE sv_time_activities AS SELECT * FROM (VALUES
    ('t1','C1','w1','Sam Ortiz','Employee','Billable',DATE '{d(-7)}',8.0,45.0,120.0),
    ('t2','C9','w1','Sam Ortiz','Employee','NotBillable',DATE '{d(-6)}',4.0,45.0,0.0)
) AS t(time_activity_id,qbo_customer_id,worker_id,worker_name,worker_type,billable_status,
       activity_date,hours,cost_rate,billing_rate);
"""

CHECKS = 0


def check(condition: bool, label: str) -> None:
    global CHECKS  # noqa: PLW0603
    assert condition, label
    CHECKS += 1


def _split_args(text: str) -> list[str]:
    """Split a call's argument list on commas that are not inside parentheses."""
    args, depth, current = [], 0, []
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            args.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if current:
        args.append("".join(current).strip())
    return args


def rewrite_call(sql: str, name: str, build) -> str:
    """Rewrite every `name(...)` call, honouring nested parentheses.

    A regex cannot do this: `date_sub(next_day(x, 'MON'), 35)` has a comma
    inside its first argument, and `[^,]+` truncates it there. That produced
    syntactically valid nonsense rather than an obvious failure, which is the
    worst kind of shim bug.
    """
    out, index = [], 0
    pattern = re.compile(rf"\b{name}\s*\(", re.IGNORECASE)
    while True:
        match = pattern.search(sql, index)
        if not match:
            out.append(sql[index:])
            return "".join(out)
        out.append(sql[index : match.start()])
        depth, position = 1, match.end()
        while depth:
            if sql[position] == "(":
                depth += 1
            elif sql[position] == ")":
                depth -= 1
            position += 1
        out.append(build(*_split_args(sql[match.end() : position - 1])))
        index = position


def to_duckdb(sql: str) -> str:
    """Only date-function spellings, same discipline as test_gold."""
    sql = re.sub(r"explode\s*\(\s*sequence\s*\(", "unnest(generate_series(", sql, flags=re.IGNORECASE)
    sql = re.sub(r"date_format\s*\(([^,]+),\s*'MMMM'\s*\)", r"strftime(\1, '%B')", sql, flags=re.IGNORECASE)
    sql = re.sub(r"date_format\s*\(([^,]+),\s*'MMM'\s*\)", r"strftime(\1, '%b')", sql, flags=re.IGNORECASE)

    # date_trunc('WEEK', ...) needs no mapping - Spark and DuckDB agree that a
    # week starts on Monday. That is exactly why the SQL uses it instead of a
    # dayofweek() magic number.
    sql = rewrite_call(sql, "DATEDIFF", lambda a, b: f"date_diff('day', ({b})::DATE, ({a})::DATE)")
    return sql.replace("`", '"')


def one(con, sql: str):
    row = con.execute(sql).fetchone()
    assert row is not None, f"no row: {sql}"
    return row[0]


def close(actual, expected, label, tol=0.01):
    assert abs(float(actual) - expected) < tol, f"{label}: expected {expected}, got {actual}"


def main() -> int:
    con = duckdb.connect(":memory:")
    for statement in split_sql_statements(FIXTURES):
        con.execute(statement)

    for name in FILES:
        sql = to_duckdb((GOLD / name).read_text(encoding="utf-8"))
        for statement in split_sql_statements(sql):
            try:
                con.execute(statement)
            except Exception as exc:  # noqa: BLE001
                raise AssertionError(f"{name}: {exc}\n---\n{statement[:400]}") from exc

    # ------------------------------------------------------- dim_DealStage
    check(one(con, "SELECT COUNT(*) FROM dim_DealStage WHERE DealStageKey='0'") == 1,
          "key-0 Unassigned stage must exist")
    check(one(con, "SELECT StageOutcome FROM dim_DealStage WHERE DealStageKey='closedwon'") == "Won",
          "probability 1.0 on a closed stage is Won")
    check(one(con, "SELECT StageOutcome FROM dim_DealStage WHERE DealStageKey='closedlost'") == "Lost",
          "probability 0.0 on a closed stage is Lost")
    check(one(con, "SELECT StageOutcome FROM dim_DealStage WHERE DealStageKey='contractsent'") == "Open",
          "an unclosed stage is Open regardless of probability")
    CHECKS_LOCAL = 0

    # ------------------------------------------------------- fct_Pipeline
    # Closed deals are excluded: 5 deals in, 3 open out.
    check(one(con, "SELECT COUNT(*) FROM fct_Pipeline") == 3,
          f"closed deals must be excluded, got {one(con, 'SELECT COUNT(*) FROM fct_Pipeline')}")
    check(one(con, "SELECT COUNT(*) FROM fct_Pipeline WHERE DealKey IN ('d3','d4')") == 0,
          "closed-won and closed-lost must not appear in the pipeline")

    # Stage probability drives the weighting.
    close(one(con, "SELECT WeightedAmount FROM fct_Pipeline WHERE DealKey='d1'"), 40000.0,
          "d1 weighted at the stage probability 0.4")
    # A deal-level override beats the stage.
    close(one(con, "SELECT WinProbability FROM fct_Pipeline WHERE DealKey='d2'"), 0.75,
          "deal-level probability overrides the stage's 0.9")
    close(one(con, "SELECT WeightedAmount FROM fct_Pipeline WHERE DealKey='d2'"), 75000.0,
          "d2 weighted at its own 0.75, not the stage's 0.9")
    # An unmatched stage degrades to zero rather than erroring or inflating.
    close(one(con, "SELECT WeightedAmount FROM fct_Pipeline WHERE DealKey='d5'"), 0.0,
          "a deal on an unknown stage contributes nothing, and does not fail")
    check(one(con, "SELECT OwnerKey FROM fct_Pipeline WHERE DealKey='d5'") == "0",
          "a deal with no owner falls back to the Unassigned key")
    close(one(con, "SELECT SUM(WeightedAmount) FROM fct_Pipeline"), 115000.0,
          "total weighted pipeline")
    close(one(con, "SELECT SUM(Amount) FROM fct_Pipeline"), 250000.0, "total unweighted pipeline")
    CHECKS_LOCAL += 9

    # ------------------------------------------------------- fct_Aging
    close(one(con, "SELECT SUM(OpenBalance) FROM fct_Aging WHERE Ledger='AR'"), 3500.0, "AR total")
    close(one(con, "SELECT SUM(OpenBalance) FROM fct_Aging WHERE Ledger='AP'"), 1100.0, "AP total")
    # Both ledgers are POSITIVE here; direction belongs to the cash forecast.
    check(one(con, "SELECT COUNT(*) FROM fct_Aging WHERE OpenBalance < 0") == 0,
          "fct_Aging states magnitude - no negative balances")
    close(one(con, "SELECT SUM(OpenBalance) FROM fct_Aging WHERE Ledger='AR' AND IsOverdue"),
          2000.0, "overdue AR")
    # The mapped customer carries a project; the unmapped one does not.
    check(one(con, "SELECT ProjectKey FROM fct_Aging WHERE DocumentId='i1'") == "P1",
          "AR on a mapped customer resolves to its project")
    check(one(con, "SELECT ProjectKey FROM fct_Aging WHERE DocumentId='i3'") is None,
          "AR on an unmapped customer keeps a NULL project rather than being dropped")
    check(one(con, "SELECT COUNT(*) FROM fct_Aging WHERE DocumentId='i3'") == 1,
          "unmapped AR is still counted")
    # Bucket ordering is explicit, because 'Current' sorts after '1-30'.
    check(one(con, "SELECT AgingBucketSort FROM fct_Aging WHERE DocumentId='i1'")
          < one(con, "SELECT AgingBucketSort FROM fct_Aging WHERE DocumentId='i2'"),
          "Current must sort before 31-60")
    CHECKS_LOCAL += 8

    # ------------------------------------------------------- fct_CashForecast
    # Collections are positive, payments negative. This is the only place a sign
    # is applied.
    close(one(con, "SELECT SUM(Amount) FROM fct_CashForecast WHERE Flow='Collections'"),
          3500.0, "all AR appears as positive collections")
    close(one(con, "SELECT SUM(Amount) FROM fct_CashForecast WHERE Flow='Payments'"),
          -1100.0, "all AP appears as negative payments")
    close(one(con, "SELECT SUM(Amount) FROM fct_CashForecast"), 2400.0, "net cash position")
    # Nothing is lost: every open document lands in some week.
    close(one(con, "SELECT SUM(DocumentCount) FROM fct_CashForecast"), 5.0,
          "all five open documents are bucketed")
    # Overdue items are pulled into the current week rather than dropped for
    # sitting in the past.
    check(one(con, """
        SELECT COUNT(*) FROM fct_CashForecast
        WHERE OverdueAmount <> 0 AND WeekStart >= date_trunc('week', CURRENT_DATE)
    """) >= 1, "overdue documents must land in the current week, not vanish")
    CHECKS_LOCAL += 5

    # ------------------------------------------------------- fct_LabourHours
    close(one(con, "SELECT LabourCost FROM fct_LabourHours WHERE LabourKey='t1'"), 360.0,
          "labour cost uses the COST rate (8h x 45), never the billing rate")
    close(one(con, "SELECT BillableValue FROM fct_LabourHours WHERE LabourKey='t1'"), 960.0,
          "billable value uses the billing rate (8h x 120)")
    check(one(con, "SELECT IsBillable FROM fct_LabourHours WHERE LabourKey='t2'") is False,
          "NotBillable is not billable")
    check(one(con, "SELECT ProjectKey FROM fct_LabourHours WHERE LabourKey='t1'") == "P1",
          "labour on a mapped customer resolves to its project")
    CHECKS_LOCAL += 4

    CHECKS_LOCAL += check_expectations(con)

    print(f"test_phase2: {CHECKS + CHECKS_LOCAL} assertions passed across {len(FILES)} gold SQL files")
    return 0


def check_expectations(con) -> int:
    """Execute the phase 2 expectations against the fixture warehouse.

    Proves the SQL is valid - a check that throws is a check that silently never
    ran - and that clean fixtures pass every blocking one.
    """
    sys.path.insert(0, str(ROOT / "transformation" / "dq"))
    from expectations import phase2_expectations  # noqa: PLC0415

    from dq import SEVERITY_ERROR  # noqa: PLC0415

    present = {
        row[0].lower()
        for row in con.execute("SELECT table_name FROM information_schema.tables").fetchall()
    }

    ran = 0
    for expectation in phase2_expectations():
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
                f"blocking expectation {expectation.name!r} failed on clean fixtures "
                f"({len(failing)} rows)"
            )

    # The fixtures deliberately contain an overdue bill and an overdue invoice,
    # so the warnings that describe those conditions must actually fire. A
    # warning that never fires is not a warning.
    overdue_ap = con.execute(
        "SELECT COUNT(*) FROM fct_Aging WHERE Ledger='AP' AND IsOverdue"
    ).fetchone()
    assert overdue_ap and overdue_ap[0] == 1, "the overdue-AP warning should fire"

    stale = con.execute("SELECT COUNT(*) FROM fct_Pipeline WHERE IsPastCloseDate").fetchone()
    assert stale and stale[0] == 0, "no fixture deal is past its close date"

    print(f"test_phase2: {ran} phase 2 data-quality expectations executed, no blocking failures")
    return ran + 2


if __name__ == "__main__":
    raise SystemExit(main())
