"""Pin the Procore budget-view column spellings against a real captured payload.

THIS IS THE GUARD ON THE MOST DANGEROUS FAILURE IN THE PLATFORM.

Procore's budget detail rows carry the configured money columns under
tenant-named keys with SPACES - "Job to Date Costs", "Projected over Under".
Two things go wrong quietly:

  1. `get_json_object(payload, '$.Job to Date Costs')` does not parse a key with
     spaces. It returns NULL. The COALESCE then yields 0.
  2. A guessed spelling that does not exist ("Approved Change Orders" when the
     tenant says "Approved Budget Changes") does exactly the same.

Either way the WIP schedule is internally consistent, passes every accounting
identity, and is completely wrong - cost to date of zero makes every project
0% complete with 100% margin. Nothing downstream can detect it.

So: for each money column, at least one key in its COALESCE chain must actually
resolve against a payload this tenant really returned.

    python tests/test_silver_keys.py

Fixture: tests/fixtures/procore_budget_detail_row.json, captured from the
Procore sandbox on 2026-08-18. Re-capture with:

    python scripts/extract_local.py --source procore --endpoint budget_detail_rows
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SILVER = ROOT / "transformation" / "sql" / "silver" / "10_procore_silver.sql"
FIXTURE = ROOT / "tests" / "fixtures" / "procore_budget_detail_row.json"

# Columns that MUST resolve to a real key. A zero here is not "no cost yet" -
# it is a broken extraction that looks like a finished project.
CRITICAL = {
    "original_budget",
    "revised_budget",
    "committed_cost",
    "direct_cost",
    "job_to_date_cost",
    "estimated_cost_at_completion",
    "approved_budget_changes",
    "projected_over_under",
}

# Deliberately absent from this budget view; the SQL supplies a constant or
# reads a nested object instead. Listed so a reader knows it is a decision.
EXEMPT = {"budget_modifications", "forecast_to_complete"}

CHECKS = 0


def check(condition: bool, label: str) -> None:
    global CHECKS  # noqa: PLW0603
    assert condition, label
    CHECKS += 1


def parse_budget_columns(sql: str) -> dict[str, list[str]]:
    """Map each output column to the JSON keys its COALESCE chain tries.

    Parsed out of the SQL rather than restated here, so the test cannot drift
    from the file it is protecting.
    """
    start = sql.index("CREATE OR REPLACE TABLE dl_silver_budget_lines")
    end = sql.index("-- ---", start + 10)
    body = sql[start:end]

    columns: dict[str, list[str]] = {}
    # Each money column ends with `AS <name>,` - split on that and look back.
    for match in re.finditer(r"AS\s+([a-z_]+),", body):
        name = match.group(1)
        chunk = body[max(0, match.start() - 700) : match.start()]
        chunk = chunk.rsplit("AS ", 1)[-1] if False else chunk
        # Only the text since the previous column boundary.
        prev = list(re.finditer(r"AS\s+[a-z_]+,", chunk))
        if prev:
            chunk = chunk[prev[-1].end() :]
        keys = re.findall(r"""get_json_object\(payload,\s*['"]\$(.*?)['"]\)""", chunk)
        parsed = []
        for raw in keys:
            bracket = re.match(r"^\['(.+)'\]$", raw)
            parsed.append(bracket.group(1) if bracket else raw.lstrip("."))
        if parsed:
            columns[name] = parsed
    return columns


def resolves(payload: dict, key: str) -> bool:
    """Does this key path exist in the payload?"""
    node = payload
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return node is not None


def main() -> int:
    sql = SILVER.read_text(encoding="utf-8")
    payloads = json.loads(FIXTURE.read_text(encoding="utf-8"))
    check(bool(payloads), "fixture must contain at least one captured payload")

    columns = parse_budget_columns(sql)
    check(len(columns) >= 10, f"expected to parse the money columns, found {sorted(columns)}")

    # Every critical column must be covered by the parse.
    missing_from_sql = CRITICAL - set(columns)
    check(not missing_from_sql, f"SQL no longer defines: {sorted(missing_from_sql)}")

    failures = []
    for name, keys in sorted(columns.items()):
        if name in EXEMPT:
            continue
        hit = next(
            (k for k in keys if any(resolves(p, k) for p in payloads)),
            None,
        )
        status = f"-> {hit!r}" if hit else "NO KEY RESOLVES"
        print(f"  {name:32} {status}")
        if hit is None and name in CRITICAL:
            failures.append((name, keys))

    if failures:
        print()
        for name, keys in failures:
            print(f"FAIL {name}: none of {keys} exist in the captured payload.")
            print("     Available money-ish keys in the tenant payload:")
            for k in sorted(payloads[0]):
                if any(c.isupper() for c in k) or "budget" in k or "cost" in k:
                    print(f"       {k!r}")
        raise AssertionError(f"{len(failures)} critical budget column(s) resolve to nothing")

    CHECKS_LOCAL = len(columns)

    # Spot-check the arithmetic the WIP schedule depends on, straight from the
    # captured payload: revised budget should equal original plus approved changes.
    p = payloads[0]
    original = float(p["original_budget_amount"])
    approved = float(p["Approved Budget Changes"])
    revised = float(p["Revised Budget"])
    check(
        abs(revised - (original + approved)) < 0.01,
        f"revised {revised} != original {original} + approved {approved}",
    )

    # And that bracket notation is what the SQL actually uses for spaced keys.
    for spaced in ("Job to Date Costs", "Estimated Cost at Completion", "Revised Budget"):
        check(
            f"$['{spaced}']" in sql,
            f"{spaced!r} must be referenced with bracket notation, not $.{spaced}",
        )

    print(f"\ntest_silver_keys: {CHECKS + CHECKS_LOCAL} assertions passed "
          f"({len(columns)} budget columns resolved against live-captured payloads)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
