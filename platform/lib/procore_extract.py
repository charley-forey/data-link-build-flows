"""Procore extraction engine.

Imported by both the Fabric notebook and the local runner, so there is one
implementation of auth, pagination and retry rather than one per notebook.

Measured constraints this encodes (see resources/procore/README.md):

  * 600 requests/hour per client. A full unthrottled run over every project and
    every endpoint cannot finish inside that, which is why scoping to ACTIVE
    projects and incremental watermarks are not optimisations here - they are
    what makes the run possible at all.
  * Procore-Company-Id must be sent on every version. Without it, v1.0
    project-scoped endpoints answer 404, not 403.
  * per_page caps at 1000. Terminate on the response, never on a page count.
  * Client-credentials grant, not authorization_code: a user-based token expires
    and breaks the pipeline unattended at the worst possible moment.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Sequence

from ratelimit import request_with_retry
from scope import Endpoint, date_window_params

MAX_PER_PAGE = 1000
MAX_PAGES = 1000


def normalise_base_url(raw: str) -> str:
    """Reduce whatever was configured to a bare API origin.

    People configure this by copying the address bar, which yields something
    like `https://sandbox.procore.com/4265679/company/home`. Left alone, every
    request is then built as `.../company/home/rest/v1.0/...` and the failure is
    a 404 that looks like a bad endpoint rather than a bad base URL.

    Keeping only scheme://host makes that class of mistake impossible.
    """
    from urllib.parse import urlparse

    text = (raw or "").strip().rstrip("/")
    if not text:
        return "https://sandbox.procore.com"
    if "//" not in text:
        text = f"https://{text}"
    parsed = urlparse(text)
    if not parsed.netloc:
        return "https://sandbox.procore.com"
    return f"{parsed.scheme or 'https'}://{parsed.netloc}"


@dataclass(frozen=True)
class Settings:
    """Connection settings. Never holds a token - tokens are fetched, not stored."""

    base_url: str
    client_id: str
    client_secret: str
    company_id: str

    @property
    def token_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/oauth/token"


def settings_from_secrets(get_secret: Callable[..., str]) -> Settings:
    return Settings(
        base_url=normalise_base_url(
            get_secret("PROCORE_BASE_URL", default="https://sandbox.procore.com")
        ),
        client_id=get_secret("PROCORE_CLIENT_ID"),
        client_secret=get_secret("PROCORE_CLIENT_SECRET"),
        company_id=get_secret("PROCORE_COMPANY_ID"),
    )


def fetch_token(settings: Settings, session: Any) -> str:
    """Client-credentials grant."""
    response = session.post(
        settings.token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": settings.client_id,
            "client_secret": settings.client_secret,
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def build_headers(token: str, company_id: Any, endpoint: Endpoint) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if endpoint.needs_company_header:
        headers["Procore-Company-Id"] = str(company_id)
    return headers


# ---------------------------------------------------------------- watermarks


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def updated_at_filter(since: datetime, until: datetime | None = None) -> str:
    """Procore's range syntax: 2026-07-01T00:00:00Z...2026-07-31T23:59:59Z"""
    until = until or datetime.now(timezone.utc)
    return f"{_iso(since)}...{_iso(until)}"


def watermark_params(endpoint: Endpoint, last_ingested: datetime | None) -> dict[str, str]:
    """Filter params for an incremental pull, or {} for a full reload.

    A full reload is still idempotent - the load is a MERGE on the natural key.
    """
    if not endpoint.incremental or last_ingested is None:
        return {}
    since = last_ingested - timedelta(hours=1)
    return {endpoint.incremental: updated_at_filter(since)}


# ---------------------------------------------------------------- pagination


def _int_or_none(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def unwrap_payload(payload: Any, key: str | None = None) -> list[dict]:
    """Most list endpoints return a bare array; a few wrap it in an object."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    if key and isinstance(payload.get(key), list):
        return payload[key]
    for candidate in ("data", "items", "results"):
        if isinstance(payload.get(candidate), list):
            return payload[candidate]
    return [payload] if payload else []


# A Procore project that does not have a tool enabled answers 403 or 404 for
# that tool's endpoints. It is a statement about configuration, not an error:
# a company with ten projects where three use Financials is entirely normal.
# Treating it as fatal means one such project takes down the whole endpoint for
# every other project too.
TOOL_UNAVAILABLE_STATUS = frozenset({403, 404})


def status_of(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) if response is not None else None


def iter_records(
    session: Any,
    base_url: str,
    path: str,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
    per_page: int = MAX_PER_PAGE,
    unwrap: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
    tolerate_unavailable: bool = False,
) -> Iterator[dict]:
    """Page through a list endpoint.

    Termination is driven by the RESPONSE - a short page, an empty page, or the
    `Total` header being reached. Never by an assumed page count, which silently
    truncates the moment the data grows.

    With `tolerate_unavailable`, a 403/404 yields nothing instead of raising, so
    a project without the tool is skipped rather than failing the endpoint. The
    caller is expected to COUNT those skips and report them - silently swallowing
    them would turn a permissions problem into "no data", which is exactly the
    failure mode this codebase refuses everywhere else.
    """
    url = f"{base_url.rstrip('/')}{path}"
    seen = 0
    total: int | None = None

    for page in range(1, MAX_PAGES + 1):
        page_params = dict(params or {})
        page_params.update({"page": page, "per_page": per_page})

        try:
            response = request_with_retry(session, url, headers, page_params, sleep=sleep)
        except Exception as exc:  # noqa: BLE001
            if tolerate_unavailable and status_of(exc) in TOOL_UNAVAILABLE_STATUS:
                raise ToolUnavailable(path, status_of(exc)) from exc
            raise

        rows = unwrap_payload(response.json(), unwrap)
        if not rows:
            return

        yield from rows
        seen += len(rows)

        if total is None:
            total = _int_or_none(response.headers.get("Total"))
        if total is not None and seen >= total:
            return
        if len(rows) < per_page:
            return


class ToolUnavailable(RuntimeError):
    """This project does not have the tool behind that endpoint enabled."""

    def __init__(self, path: str, status: int | None) -> None:
        self.path = path
        self.status = status
        super().__init__(f"{status} on {path} - tool not enabled for this project")


def iter_active_projects(
    session: Any,
    settings: Settings,
    token: str,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[dict]:
    """Active projects only.

    Looping every project regardless of status is the fastest way to blow the
    600/hour quota on jobs that closed three years ago.
    """
    endpoint = Endpoint(
        name="projects",
        path="/rest/v1.0/companies/{company_id}/projects",
        scope="company",
        bronze_table="dl_bronze_procore_projects",
    )
    headers = build_headers(token, settings.company_id, endpoint)
    yield from iter_records(
        session,
        settings.base_url,
        endpoint.path.format(company_id=settings.company_id),
        headers,
        params={"filters[by_status]": "Active"},
        sleep=sleep,
    )


# ---------------------------------------------------------------- bronze shape


def to_bronze_row(
    record: dict,
    endpoint: Endpoint,
    project_id: Any,
    ingested_at: datetime,
) -> dict[str, Any]:
    """Wrap a raw Procore record for the bronze layer.

    The full payload is kept as an UNPARSED JSON string. Bronze physically
    cannot drop a column it never parsed, so a transform bug is a re-run rather
    than a re-extract - and re-extracting is what the rate limit makes painful.

    `_merge_key` combines the natural key with the project id because a Delta
    MERGE predicate comparing two NULL `_project_id` values never matches, which
    would make every company-scoped endpoint re-insert its whole table each run.
    """
    key = str(record.get(endpoint.key, ""))
    return {
        "_key": key,
        "_project_id": str(project_id) if project_id is not None else None,
        "_merge_key": f"{key}|{project_id if project_id is not None else ''}",
        "_source_endpoint": endpoint.name,
        "_ingested_at": ingested_at,
        "payload": json.dumps(record, default=str),
    }


def endpoint_params(
    endpoint: Endpoint,
    company_id: Any,
    since: datetime | None,
) -> dict[str, Any]:
    """All query parameters for one endpoint: static, window and watermark."""
    params: dict[str, Any] = dict(endpoint.params)
    params.update(date_window_params(endpoint))
    params.update(watermark_params(endpoint, since))
    return params


def implicit_params(endpoint: Endpoint, company_id: Any, project_id: Any) -> dict[str, Any]:
    """Scope parameters for endpoints that scope by QUERY STRING, not by path.

    Procore is inconsistent about this: `/rest/v1.0/prime_contracts` is
    project-scoped via `?project_id=`, while
    `/rest/v1.0/projects/{project_id}/direct_costs` puts it in the path. Rather
    than encoding the difference in every config entry, infer it - if the scope
    placeholder is absent from the path, it belongs in the query string.
    """
    params: dict[str, Any] = {}
    if "{company_id}" not in endpoint.path:
        params["company_id"] = company_id
    if endpoint.scope != "company" and project_id is not None and "{project_id}" not in endpoint.path:
        params["project_id"] = project_id
    return params
