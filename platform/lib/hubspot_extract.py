"""HubSpot CRM extraction engine (phase 2).

Differences from the other two sources that the code has to encode:

  * Auth is a private-app bearer token. No refresh, no expiry to manage - the
    simplest of the three.
  * Paging is an opaque `after` CURSOR, not a page number. There is no total and
    no page count; you stop when `paging.next.after` is absent.
  * Properties are NOT returned by default. Ask for nothing and you get an
    object with an id and almost nothing else - which looks like empty data
    rather than a missing parameter.
  * Incremental loading uses the SEARCH endpoint (a POST) filtered on
    `hs_lastmodifieddate`. Search caps at 200 per page and 10,000 total results
    per query, so a large incremental window must be split by date.
  * `Retry-After` on a 429 is in MILLISECONDS. Treating it as seconds sleeps
    1000x too long and the run looks hung rather than throttled.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Sequence

from ratelimit import request_with_retry

BASE_URL = "https://api.hubapi.com"
API_VERSION = "2026-03"

LIST_PAGE_SIZE = 100
SEARCH_PAGE_SIZE = 200
SEARCH_RESULT_CAP = 10_000


@dataclass(frozen=True)
class ObjectSpec:
    """One CRM object type to pull, and the properties we actually need.

    Properties are explicit rather than "everything" because HubSpot portals
    accumulate hundreds of custom properties, and a wildcard pull is both slow
    and a schema that changes without anyone deciding it should.
    """

    name: str
    object_type: str
    bronze_table: str
    properties: Sequence[str] = field(default_factory=tuple)
    associations: Sequence[str] = field(default_factory=tuple)


def build_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _objects_url(object_type: str) -> str:
    return f"{BASE_URL}/crm/objects/{API_VERSION}/{object_type}"


def iter_objects(
    session: Any,
    headers: dict[str, str],
    spec: ObjectSpec,
    page_size: int = LIST_PAGE_SIZE,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[dict]:
    """Full listing of one object type, following the `after` cursor."""
    url = _objects_url(spec.object_type)
    after: str | None = None

    while True:
        params: dict[str, Any] = {"limit": page_size}
        if spec.properties:
            params["properties"] = ",".join(spec.properties)
        if spec.associations:
            params["associations"] = ",".join(spec.associations)
        if after:
            params["after"] = after

        response = request_with_retry(
            session, url, headers, params, header_units="milliseconds", sleep=sleep
        )
        body = response.json()
        rows = body.get("results") or []
        if not rows:
            return
        yield from rows

        after = ((body.get("paging") or {}).get("next") or {}).get("after")
        if not after:
            return


def search_since(
    session: Any,
    headers: dict[str, str],
    spec: ObjectSpec,
    since: datetime,
    page_size: int = SEARCH_PAGE_SIZE,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[dict]:
    """Incremental pull via the search endpoint.

    Sorted ascending by `hs_lastmodifieddate` and paged by cursor. If a window
    exceeds HubSpot's 10,000-result cap this raises rather than silently
    returning a truncated set - a quiet truncation here is missing pipeline
    data that nothing downstream can detect.
    """
    url = f"{_objects_url(spec.object_type)}/search"
    since_ms = int(since.astimezone(timezone.utc).timestamp() * 1000)
    after: str | None = None
    seen = 0

    while True:
        body: dict[str, Any] = {
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "hs_lastmodifieddate",
                            "operator": "GTE",
                            "value": str(since_ms),
                        }
                    ]
                }
            ],
            "sorts": [{"propertyName": "hs_lastmodifieddate", "direction": "ASCENDING"}],
            "limit": page_size,
        }
        if spec.properties:
            body["properties"] = list(spec.properties)
        if after:
            body["after"] = after

        response = request_with_retry(
            session,
            url,
            headers,
            None,
            header_units="milliseconds",
            sleep=sleep,
            method="search",
            json_body=body,
        )
        payload = response.json()
        rows = payload.get("results") or []
        if not rows:
            return

        yield from rows
        seen += len(rows)
        if seen >= SEARCH_RESULT_CAP:
            raise RuntimeError(
                f"{spec.name}: incremental window returned the {SEARCH_RESULT_CAP}-result "
                "search cap. Narrow the window (split by date) - continuing would "
                "silently drop records."
            )

        after = ((payload.get("paging") or {}).get("next") or {}).get("after")
        if not after:
            return


def fetch_pipelines(
    session: Any,
    headers: dict[str, str],
    object_type: str = "deals",
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict]:
    """Deal pipelines with their stages and win probabilities.

    Needed for weighted pipeline forecasting: the probability lives on the
    STAGE definition, not on the deal.
    """
    response = request_with_retry(
        session,
        f"{BASE_URL}/crm/pipelines/{API_VERSION}/{object_type}",
        headers,
        None,
        header_units="milliseconds",
        sleep=sleep,
    )
    return response.json().get("results") or []


def fetch_owners(
    session: Any,
    headers: dict[str, str],
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict]:
    response = request_with_retry(
        session,
        f"{BASE_URL}/crm/owners/{API_VERSION}",
        headers,
        {"limit": LIST_PAGE_SIZE},
        header_units="milliseconds",
        sleep=sleep,
    )
    return response.json().get("results") or []


# ---------------------------------------------------------------- bronze shape


def to_bronze_row(record: dict, spec: ObjectSpec, ingested_at: datetime) -> dict[str, Any]:
    key = str(record.get("id", ""))
    return {
        "_key": key,
        "_project_id": None,
        "_merge_key": f"{spec.object_type}|{key}",
        "_source_endpoint": spec.name,
        "_ingested_at": ingested_at,
        "payload": json.dumps(record, default=str),
    }


def high_water(records: Sequence[dict]) -> datetime | None:
    """Newest hs_lastmodifieddate in a batch, or None if empty."""
    stamps: list[datetime] = []
    for record in records:
        raw = (record.get("properties") or {}).get("hs_lastmodifieddate") or record.get(
            "updatedAt"
        )
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
