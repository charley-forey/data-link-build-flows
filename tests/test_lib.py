"""Unit tests for the platform library. No network, no Spark, no framework.

Covers the logic that is easy to get wrong and expensive to get wrong:
the MERGE predicate, watermark arithmetic, endpoint-registry validation, and
the three APIs' differing rate-limit conventions.

    python tests/test_lib.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform" / "lib"))

import ratelimit  # noqa: E402
import watermark as wm  # noqa: E402
from fabric_common import merge_sql, row_hash, split_sql_statements  # noqa: E402
from scope import (  # noqa: E402
    Endpoint,
    ParentRef,
    collect_parent_ids,
    date_window_params,
    expand_paths,
    resolution_order,
    validate_registry,
)

CHECKS = 0


def check(condition: bool, label: str) -> None:
    global CHECKS  # noqa: PLW0603
    assert condition, label
    CHECKS += 1


# ---------------------------------------------------------------- merge


def test_merge_sql() -> None:
    sql = merge_sql("t_bronze", "src", ["_merge_key"], ["_merge_key", "payload", "_batch_id"])

    # NULL-SAFE EQUALITY IS THE POINT. `t.col = s.col` never matches when both
    # sides are NULL, so a company-scoped endpoint with a NULL _project_id would
    # re-insert its entire table on every run and grow without bound.
    check("<=>" in sql, "merge predicate must use null-safe equality")
    check("=" in sql and "WHEN MATCHED THEN UPDATE SET" in sql, "matched clause present")
    check("WHEN NOT MATCHED THEN INSERT" in sql, "insert clause present")
    # The key must not appear in the SET list - updating a key to itself is noise.
    set_clause = sql.split("UPDATE SET")[1].split("WHEN NOT MATCHED")[0]
    check("_merge_key" not in set_clause, "key column must not be in the UPDATE SET list")

    # Structural errors must be caught at build time, not at 3am in Spark.
    for bad_args, label in [
        ((("t", "s", [], ["a"])), "empty key list"),
        ((("t", "s", ["k"], [])), "empty column list"),
        ((("t", "s", ["missing"], ["a"])), "key not present in source columns"),
    ]:
        try:
            merge_sql(*bad_args)
        except ValueError:
            CHECKS_OK = True  # noqa: N806
        else:
            raise AssertionError(f"merge_sql should reject: {label}")
        check(CHECKS_OK, label)


def test_row_hash_is_key_order_independent() -> None:
    a = row_hash({"id": 1, "name": "x"})
    b = row_hash({"name": "x", "id": 1})
    check(a == b, "row hash must not depend on JSON key ordering")
    check(a != row_hash({"id": 2, "name": "x"}), "row hash must change with content")


def test_split_sql_statements() -> None:
    sql = "SELECT 1; -- a comment with ; inside\nSELECT 2;"
    statements = split_sql_statements(sql)
    check(len(statements) == 2, f"a ; inside a comment must not split a statement: {statements}")


# ---------------------------------------------------------------- watermarks


def test_watermark_overlap() -> None:
    mark = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)
    since = wm.apply_overlap(mark)
    check(since == mark - timedelta(hours=1), "reads must overlap one hour backwards")
    check(wm.apply_overlap(None) is None, "no watermark means a full pull")

    # A naive timestamp must be treated as UTC, not as local time - guessing the
    # zone here shifts the window by hours and silently skips records.
    naive = wm.apply_overlap(datetime(2026, 8, 17, 12, 0, 0))
    check(naive.tzinfo is not None, "naive timestamps must be normalised to UTC")


def test_high_water() -> None:
    # AN EMPTY BATCH MUST NOT ADVANCE THE WATERMARK. Returning "now" here would
    # skip every record written while the run was in flight, forever, silently.
    check(wm.high_water([]) is None, "an empty batch must not advance the watermark")

    records = [
        {"updated_at": "2026-08-17T10:00:00Z"},
        {"updated_at": "2026-08-17T12:00:00Z"},
        {"updated_at": None},
        {"other": "no timestamp"},
    ]
    high = wm.high_water(records)
    check(high == datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc), f"got {high}")

    check(wm.high_water([{"updated_at": "not a date"}]) is None, "unparseable dates are ignored")


# ---------------------------------------------------------------- registry


def _endpoint(name: str, **kwargs) -> Endpoint:
    defaults = {
        "path": f"/rest/v1.0/{name}",
        "scope": "company",
        "bronze_table": f"dl_bronze_{name}",
    }
    defaults.update(kwargs)
    return Endpoint(name=name, **defaults)


def test_registry_validation() -> None:
    # A parent-scoped endpoint whose path lacks {parent_id} is a config error.
    try:
        Endpoint(name="x", path="/rest/v1.0/x", scope="parent", bronze_table="t",
                 parent=ParentRef(endpoint="y"))
    except ValueError:
        check(True, "scope 'parent' requires {parent_id} in the path")
    else:
        raise AssertionError("should reject parent scope without {parent_id}")

    try:
        validate_registry([_endpoint("a"), _endpoint("a")])
    except ValueError:
        check(True, "duplicate endpoint names rejected")
    else:
        raise AssertionError("should reject duplicate names")

    try:
        validate_registry([_endpoint("a"), _endpoint("b", bronze_table="dl_bronze_a")])
    except ValueError:
        check(True, "duplicate bronze tables rejected")
    else:
        raise AssertionError("should reject duplicate bronze tables")

    dangling = Endpoint(
        name="child", path="/rest/v1.0/x/{parent_id}/y", scope="parent",
        bronze_table="t", parent=ParentRef(endpoint="nope"),
    )
    try:
        validate_registry([dangling])
    except ValueError:
        check(True, "dangling parent reference rejected")
    else:
        raise AssertionError("should reject a parent that is not in the registry")


def test_resolution_order() -> None:
    parent = _endpoint("parents")
    child = Endpoint(
        name="children", path="/rest/v1.0/parents/{parent_id}/children", scope="parent",
        bronze_table="dl_bronze_children", parent=ParentRef(endpoint="parents"),
    )
    order = [e.name for e in resolution_order([child, parent])]
    check(order.index("parents") < order.index("children"), f"parents must come first: {order}")


def test_collect_parent_ids_dedups_on_the_pair() -> None:
    # A company-level budget view carries the SAME view id across every project.
    # Deduping on the id alone collapses N project-view pairs into one, and the
    # child endpoint is then called once instead of N times - producing a
    # fraction of the expected rows, with nothing to indicate it.
    records = [
        {"id": 42, "_project_id": "p1"},
        {"id": 42, "_project_id": "p2"},
        {"id": 42, "_project_id": "p3"},
    ]
    pairs = collect_parent_ids(records, ParentRef(endpoint="budget_views", field="id"))
    check(len(pairs) == 3, f"must dedup on (parent, project) pairs, got {pairs}")

    # And a genuine duplicate pair collapses to one.
    pairs = collect_parent_ids(records + [{"id": 42, "_project_id": "p1"}],
                               ParentRef(endpoint="budget_views", field="id"))
    check(len(pairs) == 3, "an exact duplicate pair collapses")


def test_collect_parent_ids_where_filter() -> None:
    # Procore returns a different column set per budget view, so only one named
    # view may spawn children.
    records = [
        {"id": 1, "name": "Data Link Standard Budget View", "_project_id": "p1"},
        {"id": 2, "name": "Someone Else's View", "_project_id": "p1"},
    ]
    ref = ParentRef(
        endpoint="budget_views", field="id",
        where_field="name", where_value="Data Link Standard Budget View",
    )
    pairs = collect_parent_ids(records, ref)
    check(pairs == [(1, "p1")], f"only the pinned view spawns children, got {pairs}")


def test_expand_paths() -> None:
    company = _endpoint("projects", path="/rest/v1.0/companies/{company_id}/projects")
    check(list(expand_paths(company, 99)) == [("/rest/v1.0/companies/99/projects", None)],
          "company scope fills company_id")

    project = _endpoint("direct_costs", scope="project",
                        path="/rest/v1.1/projects/{project_id}/direct_costs")
    out = list(expand_paths(project, 99, ["a", "b"]))
    check(len(out) == 2 and out[0][1] == "a", f"project scope fans out per project: {out}")


def test_date_window_params() -> None:
    plain = _endpoint("a")
    check(date_window_params(plain) == {}, "no window unless date_range_days is set")

    # Some Procore endpoints answer 200 with zero rows unless given a window,
    # which is indistinguishable from "no data".
    windowed = _endpoint("logs", scope="project",
                         path="/rest/v1.0/projects/{project_id}/logs",
                         date_range_days=30, date_param_prefix="filters")
    params = date_window_params(windowed, now=datetime(2026, 8, 17, tzinfo=timezone.utc))
    check(params["filters[start_date]"] == "2026-07-18", params)
    check(params["filters[end_date]"] == "2026-08-17", params)


# ---------------------------------------------------------------- rate limits


class _Response:
    def __init__(self, headers: dict) -> None:
        self.headers = headers


def test_retry_delay_units() -> None:
    # HubSpot sends Retry-After in MILLISECONDS. Treating it as seconds sleeps
    # 1000x too long and the run looks hung rather than throttled.
    ms = ratelimit.retry_delay(_Response({"Retry-After": "2000"}), 0, "milliseconds")
    check(abs(ms - 2.0) < 0.001, f"milliseconds must convert to seconds, got {ms}")

    secs = ratelimit.retry_delay(_Response({"Retry-After": "2"}), 0, "seconds")
    check(abs(secs - 2.0) < 0.001, f"seconds pass through, got {secs}")

    # Procore sends NO Retry-After. It sends X-Rate-Limit-Reset, a Unix epoch.
    future = datetime.now(timezone.utc).timestamp() + 30
    reset = ratelimit.retry_delay(_Response({"X-Rate-Limit-Reset": str(future)}), 0)
    check(25 < reset < 35, f"reset epoch drives the delay, got {reset}")

    # Nothing useful in the headers falls back to exponential backoff.
    check(ratelimit.retry_delay(_Response({}), 3) == 8.0, "exponential fallback")


def test_quota_gate() -> None:
    slept: list[float] = []
    session = ratelimit.RateLimitedSession(
        session=None, reserve=20, wait=True, sleep=slept.append
    )

    session.remaining = 500
    session._gate()
    check(not slept, "plenty of quota means no wait")

    # At the reserve floor it must wait for the reset rather than spend a
    # request it does not have - the reserve leaves room for a retry to land.
    session.remaining = 5
    session.reset_epoch = datetime.now(timezone.utc).timestamp() + 10
    session._gate()
    check(len(slept) == 1 and slept[0] > 9, f"must wait for the reset window, got {slept}")
    check(session.remaining is None, "after waiting, re-learn quota from the next response")

    # If waiting is not allowed, raise with the reset time rather than hang.
    strict = ratelimit.RateLimitedSession(session=None, reserve=20, wait=False)
    strict.remaining = 0
    strict.reset_epoch = datetime.now(timezone.utc).timestamp() + 10
    try:
        strict._gate()
    except ratelimit.QuotaExhausted as exc:
        check(exc.remaining == 0, "QuotaExhausted carries the remaining count")
    else:
        raise AssertionError("should raise QuotaExhausted when waiting is disabled")


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"test_lib: {CHECKS} assertions passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
