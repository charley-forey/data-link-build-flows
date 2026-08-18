"""QuickBooks Online extraction engine.

The important difference from Procore: QBO uses an authorization-code grant with
a REFRESH TOKEN THAT ROTATES ON EVERY USE.

    access token   3,600 s
    refresh token  8,640,000 s (100 days) hard expiry, and a NEW one is returned
                   every single time you refresh

If the rotated token is not persisted, the integration works until the current
access token expires and then fails permanently, roughly an hour after whoever
built it stopped watching. Persisting it is therefore not an optimisation, it is
the difference between a pipeline and a demo. `refresh_access_token` returns the
new refresh token and REFUSES to let the caller ignore it.

Query language is SQL-ish over `/v3/company/{realm}/query`, paged with
STARTPOSITION/MAXRESULTS (1-based, max 1000). Incremental loading uses `/cdc`,
which looks back at most 30 days - so a periodic full reconcile is required and
is not optional.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Sequence

from ratelimit import request_with_retry

TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
PROD_BASE = "https://quickbooks.api.intuit.com"
SANDBOX_BASE = "https://sandbox-quickbooks.api.intuit.com"
MINOR_VERSION = "75"

MAX_RESULTS = 1000
CDC_MAX_LOOKBACK_DAYS = 30

# QBO hard-expires a refresh token at 100 days. Warn well before, because
# recovering needs a human to click through an OAuth consent screen.
REFRESH_TOKEN_WARN_DAYS = 60
REFRESH_TOKEN_ERROR_DAYS = 85


@dataclass(frozen=True)
class Settings:
    client_id: str
    client_secret: str
    realm_id: str
    environment: str = "sandbox"

    @property
    def base_url(self) -> str:
        return SANDBOX_BASE if self.environment == "sandbox" else PROD_BASE

    @property
    def company_url(self) -> str:
        return f"{self.base_url}/v3/company/{self.realm_id}"


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    refresh_token: str
    obtained_at: datetime

    @property
    def refresh_token_age_days(self) -> float:
        return (datetime.now(timezone.utc) - self.obtained_at).total_seconds() / 86400


def settings_from_secrets(get_secret: Callable[..., str]) -> Settings:
    return Settings(
        client_id=get_secret("QUICKBOOKS_CLIENT_ID"),
        client_secret=get_secret("QUICKBOOKS_CLIENT_SECRET"),
        realm_id=get_secret("QUICKBOOKS_REALM_ID"),
        environment=get_secret("QUICKBOOKS_ENVIRONMENT", default="sandbox"),
    )


def _basic_auth(settings: Settings) -> str:
    raw = f"{settings.client_id}:{settings.client_secret}".encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def refresh_access_token(settings: Settings, session: Any, refresh_token: str) -> TokenSet:
    """Exchange a refresh token for an access token AND A NEW REFRESH TOKEN.

    The caller MUST persist `TokenSet.refresh_token`. The old one is dead the
    moment this returns; storing it is how the integration silently expires.
    """
    response = session.post(
        TOKEN_URL,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {_basic_auth(settings)}",
        },
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        timeout=30,
    )
    response.raise_for_status()
    body = response.json()
    return TokenSet(
        access_token=body["access_token"],
        refresh_token=body["refresh_token"],
        obtained_at=datetime.now(timezone.utc),
    )


def build_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


# ---------------------------------------------------------------- query API


def _escape(value: str) -> str:
    """QBO query strings are single-quoted; a literal quote is doubled."""
    return value.replace("'", "''")


def build_query(
    entity: str,
    start_position: int,
    max_results: int = MAX_RESULTS,
    where: str | None = None,
) -> str:
    clause = f" WHERE {where}" if where else ""
    return (
        f"SELECT * FROM {entity}{clause} "
        f"STARTPOSITION {start_position} MAXRESULTS {max_results}"
    )


def changed_since_where(since: datetime) -> str:
    """Incremental predicate for the query API (as opposed to /cdc)."""
    stamp = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S-00:00")
    return f"Metadata.LastUpdatedTime >= '{_escape(stamp)}'"


def iter_entity(
    session: Any,
    settings: Settings,
    headers: dict[str, str],
    entity: str,
    where: str | None = None,
    max_results: int = MAX_RESULTS,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[dict]:
    """Page through one QBO entity.

    STARTPOSITION is 1-BASED, not 0-based. Starting at 0 makes QBO return the
    first page twice and then skip a record at every page boundary - which looks
    like a duplicate-key problem rather than a paging bug.
    """
    url = f"{settings.company_url}/query"
    position = 1

    while True:
        query = build_query(entity, position, max_results, where)
        response = request_with_retry(
            session,
            url,
            {**headers, "Content-Type": "application/text"},
            {"query": query, "minorversion": MINOR_VERSION},
            sleep=sleep,
        )
        body = response.json().get("QueryResponse", {}) or {}
        rows = body.get(entity) or []
        if not rows:
            return

        yield from rows

        if len(rows) < max_results:
            return
        position += len(rows)


def iter_cdc(
    session: Any,
    settings: Settings,
    headers: dict[str, str],
    entities: Sequence[str],
    changed_since: datetime,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[tuple[str, dict]]:
    """Change-data-capture across several entities in one call.

    Look-back is capped at 30 days by QBO. We clamp rather than let the API
    reject the request, and the caller schedules a periodic full reconcile
    because anything older than the window is invisible here.
    """
    floor = datetime.now(timezone.utc) - timedelta(days=CDC_MAX_LOOKBACK_DAYS - 1)
    if changed_since < floor:
        changed_since = floor

    response = request_with_retry(
        session,
        f"{settings.company_url}/cdc",
        headers,
        {
            "entities": ",".join(entities),
            "changedSince": changed_since.astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S-00:00"
            ),
            "minorversion": MINOR_VERSION,
        },
        sleep=sleep,
    )
    for group in response.json().get("CDCResponse", []) or []:
        for query_response in group.get("QueryResponse", []) or []:
            for entity, rows in query_response.items():
                if not isinstance(rows, list):
                    continue  # skip the paging scalars (startPosition, maxResults)
                for row in rows:
                    yield entity, row


def fetch_report(
    session: Any,
    settings: Settings,
    headers: dict[str, str],
    report: str,
    params: dict[str, Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Fetch one QBO report (GeneralLedger, ProfitAndLossDetail, ARAgingDetail...).

    Reports come back as a nested Rows/ColData tree rather than a flat list, so
    they are stored whole in bronze and flattened in silver where the shape is
    visible in SQL and diffable in review.
    """
    response = request_with_retry(
        session,
        f"{settings.company_url}/reports/{report}",
        headers,
        {**(params or {}), "minorversion": MINOR_VERSION},
        sleep=sleep,
    )
    return response.json()


# ---------------------------------------------------------------- bronze shape


def to_bronze_row(
    record: dict,
    entity: str,
    ingested_at: datetime,
    key_field: str = "Id",
) -> dict[str, Any]:
    key = str(record.get(key_field, ""))
    return {
        "_key": key,
        "_project_id": None,
        "_merge_key": f"{entity}|{key}",
        "_source_endpoint": entity,
        "_ingested_at": ingested_at,
        "payload": json.dumps(record, default=str),
    }


def high_water(records: Sequence[dict]) -> datetime | None:
    """Newest Metadata.LastUpdatedTime in a batch, or None if empty."""
    stamps: list[datetime] = []
    for record in records:
        raw = (record.get("MetaData") or {}).get("LastUpdatedTime")
        if not raw:
            continue
        text = str(raw)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            continue
        stamps.append(parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc))
    return max(stamps) if stamps else None
