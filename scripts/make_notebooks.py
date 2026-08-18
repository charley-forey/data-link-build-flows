"""Generate the Fabric notebooks as .ipynb from Python source.

NOTEBOOKS ARE GENERATED, NEVER HAND-EDITED. Two reasons:

  * Hand-editing notebook JSON is how you get a notebook that will not open.
  * A hand-edited notebook is overwritten by the next deploy and the change is
    lost SILENTLY - which is the worst way to lose work, because the code looked
    right when it was written and nothing reports that it is gone.

The interesting content is the Python. It belongs under review as Python, not as
escaped strings inside a JSON blob.

    python scripts/make_notebooks.py          # write notebooks/*.ipynb
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "notebooks"

# Every notebook opens with this. The shared library ships as Files/ in the
# lakehouse rather than being pasted into each notebook: one copy, one place to
# fix a bug.
PRELUDE = '''\
import sys
sys.path.insert(0, "/lakehouse/default/Files/lib")

LIB = "/lakehouse/default/Files"
'''


def cell(source: str, kind: str = "code") -> dict:
    # EACH LINE KEEPS ITS TRAILING NEWLINE. The .ipynb format stores `source` as
    # a list of lines that are CONCATENATED verbatim - it does not re-join them
    # with newlines. Splitting on "\n" and dropping the separator produces a
    # cell whose entire body is one line, which fails with a SyntaxError
    # pointing at the first statement rather than at the real problem.
    lines = source.rstrip("\n").split("\n")
    body = [line + "\n" for line in lines[:-1]] + lines[-1:]
    return {
        "cell_type": kind,
        "metadata": {},
        **({"execution_count": None, "outputs": []} if kind == "code" else {}),
        "source": body,
    }


def notebook(cells: list[dict], default_lakehouse: str) -> dict:
    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "language_info": {"name": "python"},
            "kernelspec": {"name": "synapse_pyspark", "display_name": "Synapse PySpark"},
            "microsoft": {"language": "python"},
            "dependencies": {"lakehouse": {"default_lakehouse_name": default_lakehouse}},
        },
        "cells": cells,
    }


# ---------------------------------------------------------------- shared cells

RUN_SQL_HELPER = '''\
import glob
import os
from fabric_common import split_sql_statements, log_run, new_batch_id, utc_now


def run_sql_folder(folder: str, batch_id: str, step: str) -> None:
    """Execute every .sql file in a folder, in FILENAME ORDER.

    Ordering lives in the numeric prefix and the logic lives in version-
    controlled SQL, so a transform can be reviewed in a pull request rather than
    clicked through in a dataflow.
    """
    paths = sorted(glob.glob(os.path.join(folder, "*.sql")))
    if not paths:
        raise RuntimeError(f"no .sql files found in {folder} - is Files/ uploaded?")
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            statements = split_sql_statements(handle.read())
        for statement in statements:
            spark.sql(statement)
        print(f"  {os.path.basename(path):48s} {len(statements)} statement(s)")
        log_run(spark, batch_id, step, os.path.basename(path), len(statements))
'''

DIAG_HELPER = '''\
import json
import os


def write_diag(name: str, payload: dict) -> None:
    """Structured diagnostics to Files/_diag/.

    Fabric's job API gives no per-cell detail - a failed notebook reports
    "Failed" and nothing else. Writing what happened to a file the deploy
    scripts can read back is the difference between debugging this and guessing.
    """
    os.makedirs("/lakehouse/default/Files/_diag", exist_ok=True)
    path = f"/lakehouse/default/Files/_diag/{name}.json"
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, default=str)
    print(f"diagnostics -> {path}")
'''


# ---------------------------------------------------------------- notebooks


def nb_extract_procore() -> dict:
    return notebook(
        [
            cell(
                "# dl_01_extract_procore\n"
                "\n"
                "Pulls every endpoint in `Files/config/procore_endpoints.yml` into bronze.\n"
                "\n"
                "Adding an endpoint is a YAML entry, not a change to this notebook.\n"
                "\n"
                "**Rate limit:** Procore allows 600 requests/hour/client and does *not*\n"
                "send `Retry-After` on a 429 - it sends `X-Rate-Limit-Reset`. The session\n"
                "gates on the remaining-quota header before spending a request.",
                "markdown",
            ),
            cell(
                PRELUDE
                + '''
import requests

import fabric_common as fc
import scope as sc
import procore_extract as px
import watermark as wm
from procore_extract import ToolUnavailable
from ratelimit import RateLimitedSession, QuotaExhausted

CONFIG = f"{LIB}/config/procore_endpoints.yml"
batch_id = fc.new_batch_id()
print(f"batch {batch_id}")
'''
            ),
            cell(
                '''\
endpoints = sc.load_registry(CONFIG)
ordered = sc.resolution_order(endpoints)   # parents before their children
print(f"{len(ordered)} endpoints, resolution order:")
for endpoint in ordered:
    print(f"  {endpoint.name:32s} {endpoint.scope:8s} -> {endpoint.bronze_table}")
'''
            ),
            cell(
                '''\
settings = px.settings_from_secrets(fc.get_secret)
session = RateLimitedSession(requests.Session(), header_units="seconds")

token = px.fetch_token(settings, session)

# ACTIVE PROJECTS ONLY. Looping every project regardless of status is the
# fastest way to spend the hourly quota on jobs that closed three years ago.
projects = list(px.iter_active_projects(session, settings, token))
project_ids = [p["id"] for p in projects]
print(f"{len(project_ids)} active projects")
'''
            ),
            cell(
                '''\
fetched = {}
summary = []

for endpoint in ordered:
    parent_pairs = None
    if endpoint.parent:
        parent_pairs = sc.collect_parent_ids(
            fetched.get(endpoint.parent.endpoint, []), endpoint.parent
        )
        if not parent_pairs:
            # Not an error: a company with no prime contracts has no line items.
            print(f"  {endpoint.name:32s} skipped - parent "
                  f"{endpoint.parent.endpoint!r} returned nothing")
            summary.append((endpoint.name, 0, "skipped"))
            continue

    # Watermark is read BEFORE the pull and written only after it succeeds.
    since = wm.read_since(spark, endpoint.bronze_table, endpoint.name) if endpoint.incremental else None

    headers = px.build_headers(token, settings.company_id, endpoint)
    base_params = px.endpoint_params(endpoint, settings.company_id, since)

    records, rows = [], []
    unavailable = 0
    ingested_at = fc.utc_now()
    try:
        for path, project_id in sc.expand_paths(
            endpoint, settings.company_id, project_ids, parent_pairs
        ):
            params = {**base_params, **px.implicit_params(endpoint, settings.company_id, project_id)}
            try:
                for record in px.iter_records(
                    session, settings.base_url, path, headers, params=params,
                    unwrap=endpoint.unwrap, tolerate_unavailable=True,
                ):
                    # Carry the project id onto the record so a child endpoint can
                    # pair (parent_id, project_id) correctly.
                    record.setdefault("_project_id", project_id)
                    records.append(record)
                    rows.append({
                        **px.to_bronze_row(record, endpoint, project_id, ingested_at),
                        "_batch_id": batch_id,
                        "_row_hash": fc.row_hash(record),
                    })
            except ToolUnavailable:
                # This project does not have the tool enabled. Counted and
                # reported, never fatal - a company where three of ten projects
                # use Financials is entirely normal, and failing the endpoint
                # would lose the seven that do work.
                unavailable += 1
    except QuotaExhausted as exc:
        # Stop cleanly rather than half-loading. Watermarks for endpoints already
        # done have advanced, so the next run resumes rather than restarting.
        print(f"  {endpoint.name:32s} QUOTA EXHAUSTED - {exc}")
        summary.append((endpoint.name, len(rows), "quota_exhausted"))
        fc.log_run(spark, batch_id, "extract_procore", endpoint.bronze_table,
                   len(rows), status="quota_exhausted", message=str(exc))
        break

    fetched[endpoint.name] = records

    if rows:
        df = spark.createDataFrame(rows)
        # MERGE on the composite key, not DROP + append: re-running is a no-op,
        # so the deliberate one-hour watermark overlap cannot duplicate rows.
        fc.merge_delta(spark, df, endpoint.bronze_table, ["_merge_key"])

        high = wm.high_water(records, "updated_at")
        if endpoint.incremental and high:
            wm.write_watermark(spark, endpoint.bronze_table, endpoint.name, high, batch_id)

    fc.log_run(spark, batch_id, "extract_procore", endpoint.bronze_table, len(rows))
    mode = "incremental" if since else "full"
    summary.append((endpoint.name, len(rows), mode))
    note = f"  ({unavailable} project(s) without this tool)" if unavailable else ""
    print(f"  {endpoint.name:32s} {len(rows):7,d} rows  ({mode}){note}")
'''
            ),
            cell(
                DIAG_HELPER
                + '''
empty = [name for name, count, mode in summary if count == 0 and mode != "skipped"]
print(f"\\nrequests made: {session.requests_made}  quota remaining: {session.remaining}")
if empty:
    # On a FULL reload an empty result usually means a permission gap or a tool
    # Data Link does not use - not genuinely zero records. Worth a look either way.
    print(f"empty endpoints ({len(empty)}): {', '.join(empty)}")

write_diag("extract_procore", {
    "batch_id": batch_id,
    "projects": len(project_ids),
    "requests": session.requests_made,
    "quota_remaining": session.remaining,
    "endpoints": [{"name": n, "rows": c, "mode": m} for n, c, m in summary],
})
'''
            ),
        ],
        "DL_Lakehouse",
    )


def nb_extract_qbo() -> dict:
    return notebook(
        [
            cell(
                "# dl_02_extract_qbo\n"
                "\n"
                "Pulls the entities in `Files/config/qbo_entities.yml` into bronze.\n"
                "\n"
                "**The refresh token rotates on every use** and hard-expires at 100 days.\n"
                "The new one is persisted to `dl_meta_token` immediately after the\n"
                "exchange - before any data is pulled - because a crash mid-run must not\n"
                "lose the only credential that still works.",
                "markdown",
            ),
            cell(
                PRELUDE
                + '''
import yaml
import requests

import fabric_common as fc
import qbo_extract as qx
import watermark as wm
from ratelimit import RateLimitedSession

CONFIG = f"{LIB}/config/qbo_entities.yml"
batch_id = fc.new_batch_id()

with open(CONFIG, encoding="utf-8") as handle:
    config = yaml.safe_load(handle)
entities = config["entities"]
reports = config.get("reports", [])
print(f"batch {batch_id}: {len(entities)} entities, {len(reports)} reports")
'''
            ),
            cell(
                '''\
settings = qx.settings_from_secrets(fc.get_secret)
session = RateLimitedSession(requests.Session(), header_units="seconds")

# The stored token beats the one in configuration. QBO invalidates the previous
# refresh token the moment a new one is issued, so the value in .env or Key
# Vault is stale after the very first successful run.
stored = wm.read_token(spark, "quickbooks")
refresh_token = stored[0] if stored else fc.get_secret("QUICKBOOKS_REFRESH_TOKEN")

tokens = qx.refresh_access_token(settings, session, refresh_token)

# PERSIST FIRST, PULL SECOND. If this write is skipped or fails, the rotated
# token is lost and the integration is dead until someone re-consents by hand.
wm.write_token(spark, "quickbooks", tokens.refresh_token, batch_id)
headers = qx.build_headers(tokens.access_token)
print(f"authenticated to realm {settings.realm_id} ({settings.environment})")
'''
            ),
            cell(
                '''\
from datetime import datetime, timedelta, timezone

summary = []

for entry in entities:
    name = entry["name"]
    table = entry["bronze_table"]
    full_reload = entry.get("full_reload", False)

    since = None
    where = None
    if not full_reload:
        since = wm.read_since(spark, table, name)
        if since:
            where = qx.changed_since_where(since)

    records = list(qx.iter_entity(session, settings, headers, name, where=where))
    ingested_at = fc.utc_now()
    rows = [
        {
            **qx.to_bronze_row(record, name, ingested_at),
            "_batch_id": batch_id,
            "_row_hash": fc.row_hash(record),
        }
        for record in records
    ]

    if rows:
        df = spark.createDataFrame(rows)
        fc.merge_delta(spark, df, table, ["_merge_key"])
        high = qx.high_water(records)
        if not full_reload and high:
            wm.write_watermark(spark, table, name, high, batch_id)

    fc.log_run(spark, batch_id, "extract_qbo", table, len(rows))
    mode = "full" if full_reload else ("incremental" if since else "initial")
    summary.append((name, len(rows), mode))
    print(f"  {name:24s} {len(rows):7,d} rows  ({mode})")
'''
            ),
            cell(
                '''\
import json
import re

# Reports return a nested Rows/ColData tree rather than a list, so each is
# stored WHOLE and flattened in silver where the shape is visible in SQL.
for entry in reports:
    name = entry["name"]
    table = entry["bronze_table"]
    payload = qx.fetch_report(session, settings, headers, name, entry.get("params"))
    row = [{
        "_key": name,
        "_project_id": None,
        "_merge_key": f"report|{name}",
        "_source_endpoint": name,
        "_ingested_at": fc.utc_now(),
        "payload": json.dumps(payload, default=str),
        "_batch_id": batch_id,
        "_row_hash": fc.row_hash(payload),
    }]
    fc.merge_delta(spark, spark.createDataFrame(row), table, ["_merge_key"])
    fc.log_run(spark, batch_id, "extract_qbo_report", table, 1)
    print(f"  {name:24s} report captured")
'''
            ),
            cell(
                DIAG_HELPER
                + '''
write_diag("extract_qbo", {
    "batch_id": batch_id,
    "realm": settings.realm_id,
    "environment": settings.environment,
    "refresh_token_rotated": True,
    "entities": [{"name": n, "rows": c, "mode": m} for n, c, m in summary],
})
'''
            ),
        ],
        "DL_Lakehouse",
    )


def nb_bronze_to_silver() -> dict:
    return notebook(
        [
            cell(
                "# dl_10_bronze_to_silver\n"
                "\n"
                "Runs `Files/sql/silver/*.sql` in filename order: parse the raw payloads,\n"
                "type them, TRIM every text value, floor sentinel dates, and log rejects\n"
                "rather than dropping them.",
                "markdown",
            ),
            cell(PRELUDE + "\n" + RUN_SQL_HELPER),
            cell(
                '''\
batch_id = new_batch_id()
print(f"batch {batch_id}")
run_sql_folder(f"{LIB}/sql/silver", batch_id, "bronze_to_silver")
'''
            ),
            cell(
                DIAG_HELPER
                + '''
tables = [row.tableName for row in spark.sql("SHOW TABLES").collect()
          if row.tableName.startswith("dl_silver_")]
counts = {t: spark.table(t).count() for t in sorted(tables)}
for table, count in counts.items():
    print(f"  {table:40s} {count:8,d}")

rejects = spark.table("dl_dq_rejects_silver").count() if "dl_dq_rejects_silver" in tables + [
    r.tableName for r in spark.sql("SHOW TABLES").collect()] else 0
print(f"\\nrejects: {rejects}")
write_diag("bronze_to_silver", {"batch_id": batch_id, "counts": counts, "rejects": rejects})
'''
            ),
        ],
        "DL_Lakehouse",
    )


def nb_build_gold() -> dict:
    return notebook(
        [
            cell(
                "# dl_30_build_gold\n"
                "\n"
                "Runs `Files/sql/gold/*.sql` in filename order. `00_source_views.sql` is\n"
                "the seam: every other file reads `sv_*` and nothing else, which is what\n"
                "lets the same SQL be tested offline in DuckDB by `tests/test_gold.py`.\n"
                "\n"
                "Gold is rebuilt in full every run. It is small, and recomputing it is\n"
                "simpler and self-healing compared with incremental merge logic that\n"
                "nobody can debug six months later.",
                "markdown",
            ),
            cell(PRELUDE + "\n" + RUN_SQL_HELPER),
            cell(
                '''\
batch_id = new_batch_id()
print(f"batch {batch_id}")
run_sql_folder(f"{LIB}/sql/gold", batch_id, "build_gold")
'''
            ),
            cell(
                DIAG_HELPER
                + '''
tables = [row.tableName for row in spark.sql("SHOW TABLES").collect()
          if row.tableName.startswith(("dim_", "fct_", "meta_"))]
counts = {t: spark.table(t).count() for t in sorted(tables)}
for table, count in counts.items():
    print(f"  {table:32s} {count:8,d}")

# The semantic model infers its column types from these tables. A type that
# disagrees with the model makes Direct Lake drop the table SILENTLY, so the
# schema is captured here for deploy_model.py to generate TMDL from.
schema = {
    t: [{"name": f.name, "type": f.dataType.simpleString()} for f in spark.table(t).schema]
    for t in sorted(tables)
}
write_diag("gold_schema", {"batch_id": batch_id, "counts": counts, "schema": schema})
'''
            ),
        ],
        "DL_Lakehouse",
    )


def nb_dq_checks() -> dict:
    return notebook(
        [
            cell(
                "# dl_40_dq_checks\n"
                "\n"
                "The gate. Runs every expectation in `Files/dq/expectations.py`.\n"
                "\n"
                "Blocking failures **raise**, which stops the pipeline before the semantic\n"
                "model refreshes. That is deliberate: a stale report beats a wrong one.\n"
                "Warnings are recorded and the run continues.",
                "markdown",
            ),
            cell(
                PRELUDE
                + '''
sys.path.insert(0, f"{LIB}/dq")

import fabric_common as fc
import dq
from expectations import all_expectations

batch_id = fc.new_batch_id()
suite = all_expectations()
print(f"batch {batch_id}: {len(suite)} expectations")
'''
            ),
            cell(
                '''\
results = dq.run_suite(spark, suite, batch_id)
print(dq.summarise(results))

for result in results:
    if result.passed:
        continue
    flag = "BLOCK" if result.blocking else "warn "
    print(f"  [{flag}] {result.expectation:48s} {result.failing_rows:6,d} rows")
'''
            ),
            cell(
                '''\
# Rebuild the reporting metadata NOW, after the results exist.
#
# Built during the gold step these would always be one run stale: the Data
# Quality page would report "0 warnings" for a run that recorded one, which is
# worse than showing nothing, because it looks authoritative.
import glob
import os

from fabric_common import split_sql_statements

for path in sorted(glob.glob(f"{LIB}/sql/meta/*.sql")):
    with open(path, encoding="utf-8") as handle:
        statements = split_sql_statements(handle.read())
    for statement in statements:
        spark.sql(statement)
    print(f"  {os.path.basename(path):40s} {len(statements)} statement(s)")

print(f"\\nmeta_DataQuality: {spark.table('meta_DataQuality').count()} row(s) from this run")
'''
            ),
            cell(
                DIAG_HELPER
                + '''
write_diag("dq_checks", {
    "batch_id": batch_id,
    "summary": dq.summarise(results),
    "results": [
        {
            "expectation": r.expectation,
            "table": r.table,
            "severity": r.severity,
            "failing_rows": r.failing_rows,
            "passed": r.passed,
        }
        for r in results
    ],
})

# Raise LAST, so the diagnostics are always written even on a blocking failure.
dq.assert_no_blocking(results)
print("\\nno blocking failures - safe to publish")
'''
            ),
        ],
        "DL_Lakehouse",
    )


def nb_bootstrap() -> dict:
    return notebook(
        [
            cell(
                "# dl_00_bootstrap\n"
                "\n"
                "Creates every bronze and control table **empty but correctly typed**,\n"
                "from the same YAML configs the extractors read.\n"
                "\n"
                "Why this exists: without it, nothing downstream can run until every\n"
                "credential is in place and every API has been called successfully. With\n"
                "it, the whole medallion - silver, gold, the data-quality gate and the\n"
                "semantic model - can be deployed and verified in real Spark on day one,\n"
                "with zero data. A schema error then surfaces now rather than at 2am on\n"
                "the first scheduled run.\n"
                "\n"
                "Safe to re-run: it only creates tables that do not already exist, so it\n"
                "never touches loaded data.",
                "markdown",
            ),
            cell(
                PRELUDE
                + '''
import yaml
import fabric_common as fc

# Every bronze table has the same shape, because bronze stores the UNPARSED
# payload plus audit columns. That is what makes a transform bug a re-run
# instead of a re-extract.
BRONZE_SCHEMA = (
    "_key string, _project_id string, _merge_key string, _source_endpoint string, "
    "_ingested_at timestamp, payload string, _batch_id string, _row_hash string"
)

CONTROL_TABLES = {
    "dl_meta_watermark": (
        "table_name string, endpoint string, watermark timestamp, "
        "batch_id string, updated_at timestamp"
    ),
    "dl_meta_run_log": (
        "batch_id string, step string, table_name string, row_count long, "
        "status string, message string, logged_at timestamp"
    ),
    "dl_meta_token": "source string, refresh_token string, obtained_at timestamp, batch_id string",
    "dl_dq_results": (
        "batch_id string, expectation string, table_name string, severity string, "
        "failing_rows long, passed boolean, description string, checked_at timestamp"
    ),
    "dl_dq_rejects": (
        "_dq_expectation string, _dq_reason string, _dq_severity string, "
        "_batch_id string, _row string"
    ),
    # The Controller's manual overrides. Landed from Files/reference/ by
    # dl_06_land_reference; declared here so the crosswalk SQL can run before
    # anyone has uploaded a CSV.
    "dl_bronze_reference_project_crosswalk": (
        "procore_project_id string, qbo_customer_id string, hubspot_deal_id string, "
        "reviewed_by string, active boolean"
    ),
}
'''
            ),
            cell(
                '''\
def bronze_tables_from_config():
    """Every bronze table named by the three source registries.

    Read from the SAME config the extractors read, so a new endpoint cannot be
    added without its table appearing here too.
    """
    tables = set()

    with open(f"{LIB}/config/procore_endpoints.yml", encoding="utf-8") as handle:
        for entry in (yaml.safe_load(handle) or {}).get("endpoints", []):
            tables.add(entry["bronze_table"])

    with open(f"{LIB}/config/qbo_entities.yml", encoding="utf-8") as handle:
        qbo = yaml.safe_load(handle) or {}
        for entry in qbo.get("entities", []) + qbo.get("reports", []):
            tables.add(entry["bronze_table"])

    with open(f"{LIB}/config/hubspot_objects.yml", encoding="utf-8") as handle:
        hubspot = yaml.safe_load(handle) or {}
        for entry in hubspot.get("objects", []) + hubspot.get("reference", []):
            tables.add(entry["bronze_table"])

    return sorted(tables)


created, existing = [], []
for table in bronze_tables_from_config():
    if spark.catalog.tableExists(table):
        existing.append(table)
        continue
    spark.createDataFrame([], BRONZE_SCHEMA).write.format("delta").saveAsTable(table)
    created.append(table)

for table, schema in CONTROL_TABLES.items():
    if spark.catalog.tableExists(table):
        existing.append(table)
        continue
    spark.createDataFrame([], schema).write.format("delta").saveAsTable(table)
    created.append(table)

print(f"created {len(created)} table(s), {len(existing)} already existed")
for table in created:
    print(f"  + {table}")
'''
            ),
            cell(
                DIAG_HELPER
                + '''
write_diag("bootstrap", {"created": created, "existing": existing})
print(f"\\n{len(created) + len(existing)} bronze and control tables ready")
'''
            ),
        ],
        "DL_Lakehouse",
    )


def nb_land_to_bronze() -> dict:
    return notebook(
        [
            cell(
                "# dl_05_land_to_bronze\n"
                "\n"
                "Loads JSONL from `Files/_landing/<batch>/` into the bronze tables.\n"
                "\n"
                "**This notebook holds no credentials and needs none.** That is the whole\n"
                "point of the split: extraction needs an API secret and runs wherever the\n"
                "secret already lives; this half needs Spark and reads files.\n"
                "\n"
                "Use it when the source credentials are not in Key Vault yet. Once they\n"
                "are, `dl_01_extract_procore` writes bronze directly and this becomes a\n"
                "backfill and replay tool rather than the main path.\n"
                "\n"
                "The load is a MERGE on `_merge_key`, so re-running is a no-op and a\n"
                "partially-uploaded batch can simply be uploaded again.",
                "markdown",
            ),
            cell(
                PRELUDE
                + '''
import glob
import json
import os

from pyspark.sql import functions as F

import fabric_common as fc

LANDING = f"{LIB}/_landing"

# Which batch to load. Empty means "the newest folder present", which is what
# you want after uploading one batch; name it explicitly to replay an older one.
BATCH = ""
'''
            ),
            cell(
                '''\
batches = sorted(
    d for d in glob.glob(f"{LANDING}/*") if os.path.isdir(d)
)
if not batches:
    raise RuntimeError(
        f"no batches under {LANDING}. Upload a folder produced by "
        "scripts/extract_local.py before running this."
    )

target = f"{LANDING}/{BATCH}" if BATCH else batches[-1]
files = sorted(glob.glob(f"{target}/*.jsonl"))
print(f"batch {os.path.basename(target)}: {len(files)} file(s)")
if not files:
    raise RuntimeError(f"{target} contains no .jsonl files")
'''
            ),
            cell(
                '''\
batch_id = fc.new_batch_id()
summary = []

for path in files:
    table = os.path.splitext(os.path.basename(path))[0]

    # Read as text and parse per line rather than spark.read.json on the folder:
    # the landing files are small, and this keeps the bronze column order fixed
    # regardless of which keys happen to appear in the first file Spark samples.
    with open(path, encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]

    if not rows:
        print(f"  {table:48s} empty")
        summary.append((table, 0))
        continue

    schema = (
        "_key string, _project_id string, _merge_key string, _source_endpoint string, "
        "_ingested_at timestamp, payload string, _batch_id string, _row_hash string"
    )
    tidy = [
        (
            r.get("_key"),
            r.get("_project_id"),
            r.get("_merge_key"),
            r.get("_source_endpoint"),
            None,  # _ingested_at is set below from the string, via a cast
            r.get("payload"),
            r.get("_batch_id"),
            r.get("_row_hash"),
        )
        for r in rows
    ]
    df = spark.createDataFrame(tidy, schema)
    # The landed timestamp is an ISO string; cast rather than trusting inference.
    df = df.withColumn("_ingested_at", F.to_timestamp(F.lit(rows[0].get("_ingested_at"))))

    written = fc.merge_delta(spark, df, table, ["_merge_key"])
    fc.log_run(spark, batch_id, "land_to_bronze", table, written)
    print(f"  {table:48s} {written:6,d} row(s)")
    summary.append((table, written))
'''
            ),
            cell(
                '''# The Controller's manual crosswalk overrides.
#
# OVERWRITE, not merge. This CSV is the authoritative list of human decisions:
# a row deleted from it means "that mapping was wrong", and merging would keep
# the retracted mapping alive forever.
REFERENCE = f"{LIB}/reference/project_crosswalk.csv"

if os.path.exists(REFERENCE):
    crosswalk = (
        spark.read.option("header", True)
        .schema(
            "procore_project_id string, qbo_customer_id string, "
            "hubspot_deal_id string, reviewed_by string, active boolean"
        )
        .csv(REFERENCE)
    )
    crosswalk.write.format("delta").mode("overwrite").option(
        "overwriteSchema", "true"
    ).saveAsTable("dl_bronze_reference_project_crosswalk")
    print(f"crosswalk overrides: {crosswalk.count()} row(s)")
else:
    print(f"no {REFERENCE} - crosswalk will rely on automatic matching only")
'''
            ),
            cell(
                DIAG_HELPER
                + '''
total = sum(n for _, n in summary)
print(f"\\n{total:,} row(s) merged into bronze across {len(summary)} table(s)")
write_diag("land_to_bronze", {
    "batch_id": batch_id,
    "source_batch": os.path.basename(target),
    "tables": [{"table": t, "rows": n} for t, n in summary],
})
'''
            ),
        ],
        "DL_Lakehouse",
    )


NOTEBOOKS = {
    "dl_00_bootstrap": nb_bootstrap,
    "dl_05_land_to_bronze": nb_land_to_bronze,
    "dl_01_extract_procore": nb_extract_procore,
    "dl_02_extract_qbo": nb_extract_qbo,
    "dl_10_bronze_to_silver": nb_bronze_to_silver,
    "dl_30_build_gold": nb_build_gold,
    "dl_40_dq_checks": nb_dq_checks,
}


# Modules the notebook bodies reach for. A cell that uses one without any cell
# before it importing it raises NameError at runtime - which costs a full Spark
# round trip to discover.
_STDLIB = ("os", "json", "sys", "re", "time", "glob", "yaml", "uuid")


def check(nb: dict, name: str) -> None:
    """Compile every code cell, and verify its imports, before writing.

    Two bugs motivated this, both found the expensive way:

    * A SyntaxError pointing at the notebook's own first line - `source` had
      been split on "\\n" with the separators dropped, and .ipynb CONCATENATES
      that list verbatim rather than re-joining it, so the whole cell arrived as
      one line.
    * A NameError from a shared helper block that called `os.makedirs` without
      importing `os`. Compiling does not catch that; it is a runtime name.

    Both cost several minutes of Spark startup to discover. Checking here costs
    milliseconds. Cells share one kernel, so an import in any earlier cell counts.
    """
    imported: set[str] = set()

    for index, cell_ in enumerate(nb["cells"]):
        if cell_["cell_type"] != "code":
            continue
        source = "".join(cell_["source"])

        try:
            compile(source, f"{name}[cell {index}]", "exec")
        except SyntaxError as exc:
            raise SystemExit(f"{name} cell {index} does not compile: {exc}") from exc

        for module in _STDLIB:
            if re.search(rf"^\s*import\s+{module}\b", source, re.MULTILINE):
                imported.add(module)

        for module in _STDLIB:
            if module in imported:
                continue
            # Ignore matches inside a docstring or comment - a helper's own prose
            # mentioning "os.makedirs" is not a use.
            body = re.sub(r'""".*?"""', "", source, flags=re.DOTALL)
            body = "\n".join(line.split("#", 1)[0] for line in body.splitlines())
            if re.search(rf"(?<![\w.]){module}\.", body):
                raise SystemExit(
                    f"{name} cell {index} uses {module}.* but no cell imports {module}"
                )


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, builder in NOTEBOOKS.items():
        nb = builder()
        check(nb, name)
        path = OUT / f"{name}.ipynb"
        path.write_text(json.dumps(nb, indent=1), encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}")
    print(f"\n{len(NOTEBOOKS)} notebooks generated and syntax-checked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
