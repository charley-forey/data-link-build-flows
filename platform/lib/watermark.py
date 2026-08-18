"""Incremental-load watermarks.

Two rules, and both exist because breaking either loses rows silently:

1. THE WATERMARK ADVANCES ONLY ON SUCCESS. Writing it before the load means a
   crash mid-run skips those rows forever - and nothing ever reports it, because
   the next run dutifully starts after the rows it never loaded.

2. READS OVERLAP BACKWARDS BY AN HOUR. Source `updated_at` fields have second
   granularity, and clock skew between an API's servers and ours is real. A
   record written in the same second the watermark was taken is otherwise
   invisible. Re-pulling an hour costs nothing because the load is a MERGE on
   the natural key, not an append.

An empty batch must NOT advance the watermark: `high_water` returns None for an
empty record list, and the caller skips the write.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

WATERMARK_TABLE = "dl_meta_watermark"
OVERLAP = timedelta(hours=1)

_SCHEMA = (
    "table_name string, endpoint string, watermark timestamp, "
    "batch_id string, updated_at timestamp"
)


def _parse(value: Any) -> datetime | None:
    """Parse an API timestamp. Tolerates `Z`, offsets, and naive strings."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def apply_overlap(mark: datetime | None) -> datetime | None:
    """Pure function so the overlap rule is testable without Spark."""
    if mark is None:
        return None
    if mark.tzinfo is None:
        mark = mark.replace(tzinfo=timezone.utc)
    return mark - OVERLAP


def high_water(records: Iterable[dict], field: str = "updated_at") -> datetime | None:
    """Newest source timestamp in a batch.

    Returns None for an empty batch, which correctly leaves the watermark
    untouched - an empty pull must not advance it.
    """
    stamps = [_parse(record.get(field)) for record in records]
    stamps = [stamp for stamp in stamps if stamp is not None]
    return max(stamps) if stamps else None


# ---------------------------------------------------------------- storage


def ensure_table(spark: Any, table: str = WATERMARK_TABLE) -> None:
    if not spark.catalog.tableExists(table):
        spark.createDataFrame([], _SCHEMA).write.format("delta").saveAsTable(table)


def read_watermark(spark: Any, table_name: str, endpoint: str) -> datetime | None:
    ensure_table(spark)
    rows = (
        spark.sql(
            f"SELECT watermark FROM {WATERMARK_TABLE} "
            f"WHERE table_name = '{table_name}' AND endpoint = '{endpoint}'"
        )
        .limit(1)
        .collect()
    )
    return rows[0]["watermark"] if rows else None


def read_since(spark: Any, table_name: str, endpoint: str) -> datetime | None:
    """The value to filter the API on: last watermark, less the overlap."""
    return apply_overlap(read_watermark(spark, table_name, endpoint))


def write_watermark(
    spark: Any,
    table_name: str,
    endpoint: str,
    mark: datetime,
    batch_id: str,
) -> None:
    """Record a new high-water mark. Call this only after the load succeeded."""
    ensure_table(spark)
    now = datetime.now(timezone.utc)
    spark.createDataFrame(
        [(table_name, endpoint, mark, batch_id, now)], _SCHEMA
    ).createOrReplaceTempView("_wm_src")
    spark.sql(
        f"MERGE INTO {WATERMARK_TABLE} AS t USING _wm_src AS s "
        f"ON t.table_name <=> s.table_name AND t.endpoint <=> s.endpoint "
        f"WHEN MATCHED THEN UPDATE SET * "
        f"WHEN NOT MATCHED THEN INSERT *"
    )
    spark.catalog.dropTempView("_wm_src")


# ---------------------------------------------------------------- token store

TOKEN_TABLE = "dl_meta_token"
_TOKEN_SCHEMA = "source string, refresh_token string, obtained_at timestamp, batch_id string"


def read_token(spark: Any, source: str) -> tuple[str, datetime] | None:
    """Read the stored refresh token and when it was last rotated.

    Used when Key Vault write-back is unavailable. QuickBooks rotates its
    refresh token on every use and hard-expires it at 100 days, so the token
    that works is the one from the LAST run, not the one in .env.
    """
    if not spark.catalog.tableExists(TOKEN_TABLE):
        return None
    rows = (
        spark.sql(
            f"SELECT refresh_token, obtained_at FROM {TOKEN_TABLE} "
            f"WHERE source = '{source}' ORDER BY obtained_at DESC"
        )
        .limit(1)
        .collect()
    )
    return (rows[0]["refresh_token"], rows[0]["obtained_at"]) if rows else None


def write_token(spark: Any, source: str, refresh_token: str, batch_id: str) -> None:
    """Persist a rotated refresh token. Append-only, so a bad write is recoverable."""
    if not spark.catalog.tableExists(TOKEN_TABLE):
        spark.createDataFrame([], _TOKEN_SCHEMA).write.format("delta").saveAsTable(TOKEN_TABLE)
    row = [(source, refresh_token, datetime.now(timezone.utc), batch_id)]
    (
        spark.createDataFrame(row, _TOKEN_SCHEMA)
        .write.format("delta")
        .mode("append")
        .saveAsTable(TOKEN_TABLE)
    )
