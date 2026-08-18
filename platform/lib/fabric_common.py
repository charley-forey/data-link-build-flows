"""Shared helpers for every Data Link notebook.

Three things live here because every extractor and every transform needs them
and there must be exactly one implementation of each:

    get_secret()   one way to obtain a credential, Key Vault or environment
    merge_delta()  idempotent load, so re-running is a no-op
    log_run()      append-only run log that never takes down the caller

The important function is `merge_delta`. The naive pattern is:

    spark.sql("DROP TABLE IF EXISTS dl_bronze_procore_projects")
    df.write.format("delta").mode("append").saveAsTable("dl_bronze_procore_projects")

which destroys the table on every run, loses all history, and leaves a window
where a concurrent reader sees nothing. `merge_delta` is idempotent instead,
which is what makes the deliberate one-hour watermark overlap safe.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

VAULT_ENV = "DATALINK_KEYVAULT_URL"

AUDIT_COLUMNS = ("_ingested_at", "_source_endpoint", "_batch_id", "_row_hash")
RUN_LOG_TABLE = "dl_meta_run_log"

# The .env this project shipped with uses a few names that do not match the
# canonical ones (a "SANBOX" typo, and HUBSPOT_API_KEY for what is really a
# private-app token). Aliasing here is cheaper and safer than asking someone to
# rename live credentials, and it keeps one canonical name in the code.
_ENV_ALIASES: dict[str, tuple[str, ...]] = {
    "PROCORE_CLIENT_ID": ("PROCORE_SANDBOX_CLIENT_ID", "PROCORE_SANBOX_CLIENT_ID"),
    "PROCORE_CLIENT_SECRET": ("PROCORE_SANDBOX_CLIENT_SECRET", "PROCORE_SANBOX_CLIENT_SECRET"),
    "PROCORE_COMPANY_ID": ("PROCORE_SANDBOX_COMPANY_ID",),
    "PROCORE_BASE_URL": ("PROCORE_SANDBOX_URL",),
    # HubSpot issues three different credentials and only one of them works
    # against the CRM REST APIs. Aliased in preference order so whichever is
    # present is found, and check_token_shape() explains the difference.
    "HUBSPOT_PRIVATE_APP_TOKEN": (
        "HUBSPOT_ACCESS_TOKEN",
        "HUBSPOT_SERVICE_KEY",
        "HUBSPOT_PERSONAL_ACCESS_KEY",
        "HUBSPOT_API_KEY",
        "HUBSPOT_DEVELOPER_API_KEY",
    ),
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- secrets


def _secret_to_env(name: str) -> str:
    """`procore-client-id` and `PROCORE_CLIENT_ID` name the same secret."""
    return name.upper().replace("-", "_")


def get_secret(name: str, vault_env: str = VAULT_ENV, default: str | None = None) -> str:
    """Key Vault inside Fabric, environment variable locally.

    One function produces a credential, so there is no second place for one to
    hide. The vault URL is itself an environment variable - nothing here
    hardcodes a vault, so pointing at a different tenant is a config change.

    Raises RuntimeError naming the fix when the secret is absent, rather than
    returning None and failing later somewhere less obvious.
    """
    vault = os.environ.get(vault_env)
    if vault:
        try:
            import notebookutils  # type: ignore[import-not-found]

            return notebookutils.credentials.getSecret(vault, name)
        except ImportError:
            pass  # not running inside Fabric; fall through to the environment

    env_name = _secret_to_env(name)
    for candidate in (env_name, *_ENV_ALIASES.get(env_name, ())):
        value = os.environ.get(candidate)
        if value:
            return value

    if default is not None:
        return default
    raise RuntimeError(
        f"Secret {name!r} not found. Set {vault_env} to the Key Vault URL inside "
        f"Fabric, or export {env_name} locally. See docs/05-runbook.md."
    )


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader so local runs need no dependency.

    Does not overwrite anything already exported - a real environment variable
    always beats a file, which is what makes CI and production behave.
    """
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def find_dotenv(start: str | None = None, filename: str = ".env") -> str | None:
    """Walk up from `start` looking for a .env, and return the first hit.

    A fixed relative path breaks the moment the code runs from somewhere other
    than the repo root - a git worktree sits several levels below the checkout
    that actually holds the credentials, and CI runs from somewhere else again.
    Walking up finds it without anyone having to know the depth.
    """
    here = os.path.abspath(start or os.getcwd())
    while True:
        candidate = os.path.join(here, filename)
        if os.path.exists(candidate):
            return candidate
        parent = os.path.dirname(here)
        if parent == here:  # reached the filesystem root
            return None
        here = parent


def load_dotenv_upwards(start: str | None = None) -> str | None:
    """Find and load the nearest .env above `start`. Returns the path used."""
    path = find_dotenv(start)
    if path:
        load_dotenv(path)
    return path


# ---------------------------------------------------------------- batch identity


def new_batch_id() -> str:
    """One id per pipeline run.

    Stamped on every row written. Makes a bad run reversible: the rows to remove
    are exactly `WHERE _batch_id = '<id>'`.
    """
    return f"{utc_now():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"


def row_hash(payload: Any) -> str:
    """Stable content hash of a record.

    sort_keys makes it independent of JSON key ordering, so an API that returns
    the same record with reshuffled keys does not look like a change.
    """
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# ---------------------------------------------------------------- idempotent load


def merge_sql(
    table: str,
    source_view: str,
    key_columns: Sequence[str],
    columns: Iterable[str],
) -> str:
    """Build a MERGE statement. Pure string work, so it is testable without Spark.

    Keys are compared with `<=>` (null-safe equality) rather than `=`. A plain
    `t.col = s.col` never matches when both sides are NULL, so company-scoped
    endpoints - where `_project_id` is legitimately NULL - would re-insert every
    row on every run and the table would grow without bound.
    """
    keys = list(key_columns)
    cols = list(columns)
    if not keys:
        raise ValueError("merge requires at least one key column")
    if not cols:
        raise ValueError("merge requires at least one column")
    missing = [k for k in keys if k not in cols]
    if missing:
        raise ValueError(f"key column(s) not present in source: {missing}")

    on = " AND ".join(f"t.`{k}` <=> s.`{k}`" for k in keys)
    updates = ", ".join(f"t.`{c}` = s.`{c}`" for c in cols if c not in keys)
    insert_cols = ", ".join(f"`{c}`" for c in cols)
    insert_vals = ", ".join(f"s.`{c}`" for c in cols)
    matched = f"WHEN MATCHED THEN UPDATE SET {updates}\n" if updates else ""
    return (
        f"MERGE INTO {table} AS t\n"
        f"USING {source_view} AS s\n"
        f"ON {on}\n"
        f"{matched}"
        f"WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals})"
    )


def merge_delta(spark: Any, df: Any, table: str, key_columns: Sequence[str]) -> int:
    """Idempotent upsert into a Delta table. Creates it on first run."""
    keys = list(key_columns)
    if not spark.catalog.tableExists(table):
        df.write.format("delta").saveAsTable(table)
        return df.count()

    view = f"_src_{uuid.uuid4().hex[:8]}"
    df.createOrReplaceTempView(view)
    try:
        spark.sql(merge_sql(table, view, keys, df.columns))
    finally:
        spark.catalog.dropTempView(view)
    return df.count()


# ---------------------------------------------------------------- run log


def log_run(
    spark: Any,
    batch_id: str,
    step: str,
    table: str,
    row_count: int,
    status: str = "ok",
    message: str = "",
) -> None:
    """Append one row to the run log.

    Deliberately append-only, and deliberately never raises: a logging failure
    must not take down a pipeline that otherwise succeeded.
    """
    try:
        row = [(batch_id, step, table, int(row_count), status, message, utc_now())]
        schema = (
            "batch_id string, step string, table_name string, row_count long, "
            "status string, message string, logged_at timestamp"
        )
        (
            spark.createDataFrame(row, schema)
            .write.format("delta")
            .mode("append")
            .saveAsTable(RUN_LOG_TABLE)
        )
    except Exception as exc:  # noqa: BLE001 - logging must never be fatal
        print(f"[warn] run log write failed: {exc}")


# ---------------------------------------------------------------- sql helpers


def split_sql_statements(sql: str) -> list[str]:
    """Split a .sql file into executable statements.

    Comments are stripped BEFORE splitting - a `;` inside a `--` comment is
    otherwise read as a statement boundary and tears the statement in half.

    ponytail: line-wise comment strip, then split on `;`. Breaks if a string
    literal ever contains `--` or `;`. None do today. Reach for sqlglot if that
    changes.
    """
    body = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    return [statement.strip() for statement in body.split(";") if statement.strip()]
