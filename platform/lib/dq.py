"""Data quality engine.

EVERY EXPECTATION IS A SQL PREDICATE THAT RETURNS THE FAILING ROWS.

That single choice is what makes this useful rather than decorative: a failure
is not a boolean, it is a set of rows somebody can open and look at. A red light
that says "referential integrity failed" sends someone hunting; a table of the
14 offending rows tells them which projects to fix.

Two severities, and the distinction is load-bearing:

  ERROR  stops the pipeline. Reserved for things that make a number WRONG rather
         than incomplete - a duplicate dimension key, a fact pointing at a
         project that does not exist, an impossible percentage.

  WARN   records and continues. For things that are true of the real data and
         would be dishonest to hide, but are not defects: a project in Procore
         that the Controller has not yet mapped to a QuickBooks job, a cost
         variance between two systems that genuinely disagree.

The instinct to make everything an ERROR is wrong. A pipeline that blocks on a
real business condition gets muted within a week, and then the blocking checks
stop working too. A STALE REPORT BEATS A WRONG ONE - but only if the thing that
blocks is genuinely a wrongness.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

SEVERITY_ERROR = "error"
SEVERITY_WARN = "warn"

RESULTS_TABLE = "dl_dq_results"
REJECTS_TABLE = "dl_dq_rejects"

_RESULTS_SCHEMA = (
    "batch_id string, expectation string, table_name string, severity string, "
    "failing_rows long, passed boolean, description string, checked_at timestamp"
)


@dataclass(frozen=True)
class Expectation:
    name: str
    table: str
    failing_sql: str
    severity: str = SEVERITY_ERROR
    description: str = ""


@dataclass(frozen=True)
class Result:
    expectation: str
    table: str
    severity: str
    failing_rows: int
    description: str

    @property
    def passed(self) -> bool:
        return self.failing_rows == 0

    @property
    def blocking(self) -> bool:
        return self.severity == SEVERITY_ERROR and not self.passed


# ---------------------------------------------------------------- builders


def not_null(table: str, column: str, severity: str = SEVERITY_ERROR) -> Expectation:
    return Expectation(
        name=f"{table}.{column}.not_null",
        table=table,
        failing_sql=f"SELECT * FROM {table} WHERE `{column}` IS NULL",
        severity=severity,
        description=f"{column} must be present on every row of {table}",
    )


def unique_key(table: str, columns: Sequence[str], severity: str = SEVERITY_ERROR) -> Expectation:
    cols = ", ".join(f"`{c}`" for c in columns)
    return Expectation(
        name=f"{table}.{'_'.join(columns)}.unique",
        table=table,
        failing_sql=(
            f"SELECT {cols}, COUNT(*) AS duplicate_count FROM {table} "
            f"GROUP BY {cols} HAVING COUNT(*) > 1"
        ),
        severity=severity,
        description=f"{', '.join(columns)} uniquely identifies a row in {table}",
    )


def referential(
    table: str,
    column: str,
    parent_table: str,
    parent_column: str,
    severity: str = SEVERITY_ERROR,
) -> Expectation:
    """Orphan check.

    NULL is excluded deliberately: "not yet known" is a different problem from
    "points at something that does not exist", and conflating them means the
    check fires on data that is merely incomplete.
    """
    return Expectation(
        name=f"{table}.{column}.fk_{parent_table}",
        table=table,
        failing_sql=(
            f"SELECT c.* FROM {table} c "
            f"LEFT JOIN {parent_table} p ON c.`{column}` = p.`{parent_column}` "
            f"WHERE c.`{column}` IS NOT NULL AND p.`{parent_column}` IS NULL"
        ),
        severity=severity,
        description=f"every {table}.{column} resolves to {parent_table}.{parent_column}",
    )


def in_range(
    table: str,
    column: str,
    low: float,
    high: float,
    severity: str = SEVERITY_WARN,
) -> Expectation:
    return Expectation(
        name=f"{table}.{column}.range",
        table=table,
        failing_sql=(
            f"SELECT * FROM {table} WHERE `{column}` IS NOT NULL "
            f"AND (`{column}` < {low} OR `{column}` > {high})"
        ),
        severity=severity,
        description=f"{column} between {low} and {high}",
    )


def date_order(
    table: str,
    earlier: str,
    later: str,
    severity: str = SEVERITY_WARN,
) -> Expectation:
    return Expectation(
        name=f"{table}.{earlier}_before_{later}",
        table=table,
        failing_sql=(
            f"SELECT * FROM {table} WHERE `{earlier}` IS NOT NULL "
            f"AND `{later}` IS NOT NULL AND `{later}` < `{earlier}`"
        ),
        severity=severity,
        description=f"{earlier} is not after {later}",
    )


def freshness(
    table: str,
    column: str,
    max_age_hours: int,
    severity: str = SEVERITY_WARN,
) -> Expectation:
    """Catches the silent failure mode where the pipeline stops running and the
    report keeps cheerfully showing last month's numbers."""
    return Expectation(
        name=f"{table}.{column}.freshness",
        table=table,
        failing_sql=(
            f"SELECT MAX(`{column}`) AS newest FROM {table} "
            f"HAVING MAX(`{column}`) < CURRENT_TIMESTAMP() - INTERVAL {max_age_hours} HOURS"
        ),
        severity=severity,
        description=f"{table} has data newer than {max_age_hours}h",
    )


def custom(
    name: str,
    table: str,
    failing_sql: str,
    severity: str = SEVERITY_WARN,
    description: str = "",
) -> Expectation:
    """Escape hatch for business rules the builders do not cover.

    `failing_sql` must select the FAILING rows, same contract as everything else.
    """
    return Expectation(
        name=name,
        table=table,
        failing_sql=failing_sql,
        severity=severity,
        description=description,
    )


# ---------------------------------------------------------------- runner


def _persist_rejects(spark: Any, expectation: Expectation, failing: Any, batch_id: str) -> None:
    """Keep up to 1000 failing rows so someone can actually look at them."""
    from pyspark.sql import functions as F  # type: ignore[import-not-found]

    (
        failing.limit(1000)
        .withColumn("_dq_expectation", F.lit(expectation.name))
        .withColumn("_dq_reason", F.lit(expectation.description or expectation.name))
        .withColumn("_dq_severity", F.lit(expectation.severity))
        .withColumn("_batch_id", F.lit(batch_id))
        .selectExpr(
            "_dq_expectation",
            "_dq_reason",
            "_dq_severity",
            "_batch_id",
            "to_json(struct(*)) AS _row",
        )
        .write.format("delta")
        .mode("append")
        .saveAsTable(REJECTS_TABLE)
    )


def run_suite(spark: Any, expectations: Sequence[Expectation], batch_id: str) -> list[Result]:
    """Run every expectation. Never short-circuits.

    One failure must not hide the other twelve - the point of a run is the whole
    picture, not the first thing that broke.
    """
    results: list[Result] = []
    for expectation in expectations:
        try:
            failing = spark.sql(expectation.failing_sql)
            count = failing.count()
        except Exception as exc:  # noqa: BLE001 - a broken check is itself a failure
            results.append(
                Result(
                    expectation=expectation.name,
                    table=expectation.table,
                    severity=expectation.severity,
                    failing_rows=-1,
                    description=f"expectation failed to run: {exc}",
                )
            )
            continue

        if count:
            _persist_rejects(spark, expectation, failing, batch_id)

        results.append(
            Result(
                expectation=expectation.name,
                table=expectation.table,
                severity=expectation.severity,
                failing_rows=count,
                description=expectation.description,
            )
        )

    _write_results(spark, results, batch_id)
    return results


def _write_results(spark: Any, results: Sequence[Result], batch_id: str) -> None:
    now = datetime.now(timezone.utc)
    rows = [
        (
            batch_id,
            r.expectation,
            r.table,
            r.severity,
            int(r.failing_rows),
            r.passed,
            r.description,
            now,
        )
        for r in results
    ]
    if not rows:
        return
    (
        spark.createDataFrame(rows, _RESULTS_SCHEMA)
        .write.format("delta")
        .mode("append")
        .saveAsTable(RESULTS_TABLE)
    )


def summarise(results: Sequence[Result]) -> str:
    passed = sum(1 for r in results if r.passed)
    warned = sum(1 for r in results if not r.passed and r.severity == SEVERITY_WARN)
    blocked = sum(1 for r in results if r.blocking)
    return f"{len(results)} expectations: {passed} passed, {warned} warned, {blocked} blocking"


def assert_no_blocking(results: Sequence[Result]) -> None:
    """Turn the suite from a report into a gate.

    Raising here is what stops gold rebuilding over bad silver and publishing
    numbers that look current.
    """
    blocking = [r for r in results if r.blocking]
    if not blocking:
        return
    detail = "\n".join(
        f"  {r.expectation} ({r.table}): {r.failing_rows} failing rows - {r.description}"
        for r in blocking
    )
    raise RuntimeError(f"{len(blocking)} blocking data-quality failure(s):\n{detail}")
