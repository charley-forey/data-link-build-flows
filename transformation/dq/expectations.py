"""The data-quality suite for Data Link.

Severity philosophy, because it decides whether this suite is useful in six
months or muted in one:

  ERROR stops the pipeline. Reserved for things that make a number WRONG rather
  than incomplete - a duplicate dimension key that doubles a total, a fact
  pointing at a project that does not exist, a percentage above 100%, an
  accounting identity that does not hold.

  WARN records and continues. For things that are true of the real data and
  would be dishonest to hide, but are not defects: a Procore project the
  Controller has not mapped to a QuickBooks job yet, a cost variance between two
  systems that genuinely disagree, a change order with no approval date.

THE INSTINCT TO MAKE EVERYTHING AN ERROR IS WRONG. A pipeline that blocks on a
real business condition gets muted within a week - and once someone has learned
to ignore the alert, the blocking checks stop working too. A STALE REPORT BEATS
A WRONG ONE, but only when the thing that blocks is genuinely a wrongness.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "platform" / "lib"))

from dq import (  # noqa: E402
    SEVERITY_ERROR,
    SEVERITY_WARN,
    Expectation,
    custom,
    freshness,
    in_range,
    not_null,
    referential,
    unique_key,
)

# How far Procore and QuickBooks may disagree on a project's cost before it is
# worth a human's attention. Confirm with the Controller - these are placeholders
# chosen to be roughly one day of a crew's time, not a policy. Open item #5.
COST_VARIANCE_ABS = 5000.0
COST_VARIANCE_PCT = 0.10

# Source freshness. 30 hours rather than 24 so a single missed nightly run
# warns, but a run that starts an hour late does not.
FRESHNESS_HOURS = 30


def dimension_expectations() -> list[Expectation]:
    """A duplicate dimension key silently doubles every measure joined to it,
    and it does so without any error - which is why these are all ERROR."""
    return [
        unique_key("dim_Project", ["ProjectKey"]),
        unique_key("dim_Date", ["Date"]),
        unique_key("dim_CostCode", ["CostCodeKey"]),
        unique_key("dim_Vendor", ["VendorKey"]),
        unique_key("dim_Account", ["AccountKey"]),
        not_null("dim_Project", "ProjectKey"),
        not_null("dim_Project", "ProjectName"),
        not_null("dim_Date", "Date"),
    ]


def referential_expectations() -> list[Expectation]:
    """Orphaned facts.

    dim_Project is built as the UNION of the crosswalk and every observed
    project id precisely so these cannot fail. If one does, the dimension build
    is broken - which is exactly the sort of thing worth stopping for.
    """
    return [
        referential("fct_WIP", "ProjectKey", "dim_Project", "ProjectKey"),
        referential("fct_BudgetLine", "ProjectKey", "dim_Project", "ProjectKey"),
        referential("fct_ChangeOrder", "ProjectKey", "dim_Project", "ProjectKey"),
        referential("fct_Billing", "ProjectKey", "dim_Project", "ProjectKey"),
        referential("fct_CostTransaction", "ProjectKey", "dim_Project", "ProjectKey"),
    ]


def date_key_expectations() -> list[Expectation]:
    """MonthStart must resolve to dim_Date.

    An unmatched date key does not raise anything in a semantic model - it makes
    every date-filtered measure come back BLANK for those rows. Blank reads as
    "no activity this period", so this failure mode is invisible unless it is
    checked for explicitly.
    """
    return [
        referential("fct_WIP", "MonthStart", "dim_Date", "Date"),
        referential("fct_ChangeOrder", "MonthStart", "dim_Date", "Date"),
        referential("fct_Billing", "MonthStart", "dim_Date", "Date"),
        referential("fct_CostTransaction", "MonthStart", "dim_Date", "Date"),
        custom(
            name="fct_ChangeOrder.out_of_range_dates",
            table="fct_ChangeOrder",
            failing_sql="SELECT * FROM fct_ChangeOrder WHERE HasOutOfRangeDate",
            severity=SEVERITY_WARN,
            description="change orders dated outside 2015-2035 (sentinel or typo)",
        ),
        custom(
            name="fct_CostTransaction.out_of_range_dates",
            table="fct_CostTransaction",
            failing_sql="SELECT * FROM fct_CostTransaction WHERE HasOutOfRangeDate",
            severity=SEVERITY_WARN,
            description="cost transactions dated outside 2015-2035",
        ),
    ]


def accounting_expectations() -> list[Expectation]:
    """The identities that define the WIP schedule.

    These are ERROR because a WIP schedule that does not satisfy them is not a
    WIP schedule - it is a table of numbers that happens to look like one. The
    same identities are asserted offline in tests/test_gold.py; these catch the
    case where real data breaks an assumption the fixtures did not.
    """
    return [
        custom(
            name="fct_WIP.revised_contract_identity",
            table="fct_WIP",
            failing_sql=(
                "SELECT * FROM fct_WIP "
                "WHERE ABS(RevisedContract - (OriginalContract + ApprovedChangeOrders)) > 0.01"
            ),
            severity=SEVERITY_ERROR,
            description="revised contract = original contract + approved change orders",
        ),
        custom(
            name="fct_WIP.earned_gp_identity",
            table="fct_WIP",
            failing_sql=(
                "SELECT * FROM fct_WIP "
                "WHERE ABS((EarnedRevenue - CostToDate) - EarnedGrossProfit) > 0.01"
            ),
            severity=SEVERITY_ERROR,
            description="earned gross profit = earned revenue - cost to date",
        ),
        custom(
            name="fct_WIP.billing_exclusivity",
            table="fct_WIP",
            failing_sql="SELECT * FROM fct_WIP WHERE OverBilling > 0 AND UnderBilling > 0",
            severity=SEVERITY_ERROR,
            description="a project cannot be both over-billed and under-billed",
        ),
        in_range("fct_WIP", "PercentComplete", 0.0, 1.0, severity=SEVERITY_ERROR),
        custom(
            name="fct_WIP.eac_below_cost",
            table="fct_WIP",
            failing_sql="SELECT * FROM fct_WIP WHERE EAC < CostToDate",
            severity=SEVERITY_WARN,
            description=(
                "estimated cost at completion is below cost already spent - the "
                "forecast needs updating by the project manager"
            ),
        ),
        custom(
            name="fct_WIP.negative_margin",
            table="fct_WIP",
            failing_sql="SELECT * FROM fct_WIP WHERE GrossProfitAtCompletion < 0",
            severity=SEVERITY_WARN,
            description="project forecast to complete at a loss",
        ),
        custom(
            name="fct_WIP.overrun_projects",
            table="fct_WIP",
            failing_sql="SELECT * FROM fct_WIP WHERE PercentCompleteRaw > 1.0",
            severity=SEVERITY_WARN,
            description="cost to date exceeds EAC - revenue recognition is capped",
        ),
    ]


def reconciliation_expectations() -> list[Expectation]:
    """Procore vs QuickBooks.

    WARN, always. A variance here is a FINDING, not necessarily a bug: an AP
    invoice not yet entered, a cost coded to the wrong job, an accrual Procore
    does not know about. Blocking the pipeline on it would stop the reporting
    that is needed to investigate it, which is exactly backwards.

    Only mapped projects are checked - an unmapped project has no QuickBooks
    cost by definition, and flagging it here would duplicate the crosswalk
    warning with a much scarier number.
    """
    return [
        custom(
            name="fct_WIP.procore_qbo_cost_variance",
            table="fct_WIP",
            failing_sql=(
                "SELECT w.* FROM fct_WIP w "
                "JOIN dim_Project p ON p.ProjectKey = w.ProjectKey "
                "WHERE p.IsInCrosswalk "
                f"  AND ABS(w.CostVariance) > {COST_VARIANCE_ABS} "
                f"  AND ABS(w.CostVariancePct) > {COST_VARIANCE_PCT}"
            ),
            severity=SEVERITY_WARN,
            description=(
                f"Procore and QuickBooks job cost differ by more than "
                f"${COST_VARIANCE_ABS:,.0f} and {COST_VARIANCE_PCT:.0%}"
            ),
        ),
        custom(
            name="dim_Project.unmapped_with_contract",
            table="dim_Project",
            failing_sql=(
                "SELECT * FROM dim_Project "
                "WHERE NOT IsInCrosswalk AND OriginalContract > 0"
            ),
            severity=SEVERITY_WARN,
            description=(
                "project has a contract but no QuickBooks job mapping - its cost "
                "cannot be reconciled until the Controller maps it"
            ),
        ),
        custom(
            name="fct_CostTransaction.unattributed_qbo_cost",
            table="fct_CostTransaction",
            failing_sql=(
                "SELECT * FROM fct_CostTransaction "
                "WHERE SourceSystem = 'quickbooks' AND ProjectKey IS NULL"
            ),
            severity=SEVERITY_WARN,
            description="QuickBooks job-cost lines not attributable to any project",
        ),
    ]


def freshness_expectations() -> list[Expectation]:
    """Staleness, including the one that kills QuickBooks integrations.

    The refresh-token check is the important one. QuickBooks rotates its refresh
    token on every use and hard-expires it at 100 days. If the rotated token is
    ever not persisted, everything keeps working until it abruptly does not -
    and the failure looks like an auth error weeks after the actual mistake.
    Warning at 60 days leaves time to re-consent without an emergency.
    """
    return [
        freshness("dl_bronze_procore_projects", "_ingested_at", FRESHNESS_HOURS),
        freshness("dl_bronze_procore_budget_detail_rows", "_ingested_at", FRESHNESS_HOURS),
        freshness("dl_bronze_qbo_bills", "_ingested_at", FRESHNESS_HOURS),
        custom(
            name="dl_meta_token.qbo_refresh_token_age",
            table="dl_meta_token",
            failing_sql=(
                "SELECT * FROM dl_meta_token WHERE source = 'quickbooks' "
                "AND obtained_at < CURRENT_TIMESTAMP() - INTERVAL 60 DAYS"
            ),
            severity=SEVERITY_WARN,
            description=(
                "QuickBooks refresh token is over 60 days old (hard expiry at 100). "
                "Re-run scripts/qbo_authorize.py before it lapses."
            ),
        ),
        custom(
            name="dl_meta_token.qbo_refresh_token_critical",
            table="dl_meta_token",
            failing_sql=(
                "SELECT * FROM dl_meta_token WHERE source = 'quickbooks' "
                "AND obtained_at < CURRENT_TIMESTAMP() - INTERVAL 85 DAYS"
            ),
            severity=SEVERITY_ERROR,
            description=(
                "QuickBooks refresh token expires in under 15 days. Re-consent now "
                "or the integration stops without further warning."
            ),
        ),
    ]


def phase2_expectations() -> list[Expectation]:
    """Pipeline, receivables, payables, cash and labour.

    The accounting identities here are ERROR because they define what the
    numbers mean; everything describing the state of the business is WARN.
    """
    return [
        unique_key("dim_DealStage", ["DealStageKey"]),
        unique_key("dim_Owner", ["OwnerKey"]),
        unique_key("fct_Pipeline", ["DealKey"]),
        unique_key("fct_Aging", ["AgingKey"]),
        referential("fct_Pipeline", "DealStageKey", "dim_DealStage", "DealStageKey"),
        referential("fct_Pipeline", "OwnerKey", "dim_Owner", "OwnerKey"),
        referential("fct_Pipeline", "MonthStart", "dim_Date", "Date"),

        custom(
            name="fct_Pipeline.weighted_identity",
            table="fct_Pipeline",
            failing_sql=(
                "SELECT * FROM fct_Pipeline "
                "WHERE ABS(WeightedAmount - (Amount * WinProbability)) > 0.01"
            ),
            severity=SEVERITY_ERROR,
            description="weighted amount = amount x win probability",
        ),
        in_range("fct_Pipeline", "WinProbability", 0.0, 1.0, severity=SEVERITY_ERROR),
        custom(
            name="fct_Pipeline.closed_deals_excluded",
            table="fct_Pipeline",
            failing_sql=(
                "SELECT p.* FROM fct_Pipeline p "
                "JOIN dim_DealStage s ON s.DealStageKey = p.DealStageKey "
                "WHERE s.IsClosedStage"
            ),
            severity=SEVERITY_ERROR,
            description=(
                "a closed deal must never sit in the pipeline - closed-won especially, "
                "because it makes a pipeline look healthy while it is emptying"
            ),
        ),
        custom(
            name="fct_Pipeline.past_close_date",
            table="fct_Pipeline",
            failing_sql="SELECT * FROM fct_Pipeline WHERE IsPastCloseDate",
            severity=SEVERITY_WARN,
            description="open deals whose close date has already passed - the forecast is stale",
        ),

        # ---------------------------------------------------------- cash
        custom(
            name="fct_Aging.no_negative_balances",
            table="fct_Aging",
            failing_sql="SELECT * FROM fct_Aging WHERE OpenBalance < 0",
            severity=SEVERITY_ERROR,
            description=(
                "fct_Aging states magnitude; direction is applied in the cash "
                "forecast. A negative here means a sign was baked in twice."
            ),
        ),
        custom(
            name="fct_CashForecast.collections_positive",
            table="fct_CashForecast",
            failing_sql="SELECT * FROM fct_CashForecast WHERE Flow = 'Collections' AND Amount < 0",
            severity=SEVERITY_ERROR,
            description="money coming in is positive",
        ),
        custom(
            name="fct_CashForecast.payments_negative",
            table="fct_CashForecast",
            failing_sql="SELECT * FROM fct_CashForecast WHERE Flow = 'Payments' AND Amount > 0",
            severity=SEVERITY_ERROR,
            description="money going out is negative",
        ),
        custom(
            name="fct_Aging.ar_over_90",
            table="fct_Aging",
            failing_sql="SELECT * FROM fct_Aging WHERE Ledger = 'AR' AND AgingBucket = '90+'",
            severity=SEVERITY_WARN,
            description="receivables over 90 days - collection risk",
        ),
        custom(
            name="fct_Aging.ap_overdue",
            table="fct_Aging",
            failing_sql="SELECT * FROM fct_Aging WHERE Ledger = 'AP' AND IsOverdue",
            severity=SEVERITY_WARN,
            description="bills already past due",
        ),
        custom(
            name="fct_Aging.missing_due_date",
            table="fct_Aging",
            failing_sql="SELECT * FROM fct_Aging WHERE DueDate IS NULL",
            severity=SEVERITY_WARN,
            description=(
                "no due date, so this document cannot be placed in the cash "
                "forecast at all - it is invisible rather than merely late"
            ),
        ),

        # ---------------------------------------------------------- labour
        custom(
            name="fct_LabourHours.cost_not_billing_rate",
            table="fct_LabourHours",
            failing_sql=(
                "SELECT * FROM fct_LabourHours "
                "WHERE Hours > 0 AND LabourCost > 0 AND BillableValue > 0 "
                "  AND LabourCost > BillableValue"
            ),
            severity=SEVERITY_WARN,
            description=(
                "labour costing more than it bills - either a loss-making rate or "
                "the billing rate has been used as cost somewhere"
            ),
        ),
        custom(
            name="fct_LabourHours.unattributed_hours",
            table="fct_LabourHours",
            failing_sql="SELECT * FROM fct_LabourHours WHERE ProjectKey IS NULL",
            severity=SEVERITY_WARN,
            description="hours not attributable to any project - capacity that cannot be costed",
        ),
    ]


def all_expectations() -> Sequence[Expectation]:
    return [
        *dimension_expectations(),
        *referential_expectations(),
        *date_key_expectations(),
        *accounting_expectations(),
        *reconciliation_expectations(),
        *freshness_expectations(),
        *phase2_expectations(),
    ]


if __name__ == "__main__":
    suite = all_expectations()
    blocking = sum(1 for e in suite if e.severity == SEVERITY_ERROR)
    print(f"{len(suite)} expectations: {blocking} blocking, {len(suite) - blocking} warning")
    for expectation in suite:
        print(f"  [{expectation.severity:5s}] {expectation.name}")
