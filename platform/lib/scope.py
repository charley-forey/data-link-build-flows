"""Endpoint registry: config, validation, and parent-child expansion.

Adding a source endpoint is a YAML entry, not a new notebook. That is the whole
point - it is why this project has one extractor per source instead of thirty
near-identical notebooks that each drift in their own direction.

The registry is validated at LOAD time rather than at request time, so a typo in
the config fails in the first second of a run with a message naming the entry,
not forty minutes in with a KeyError.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Iterator, Sequence

SCOPE_COMPANY = "company"
SCOPE_PROJECT = "project"
SCOPE_PARENT = "parent"
VALID_SCOPES = frozenset({SCOPE_COMPANY, SCOPE_PROJECT, SCOPE_PARENT})


@dataclass(frozen=True)
class ParentRef:
    """Which endpoint supplies `{parent_id}`, and the field to read it from.

    `where_field`/`where_value` restrict which parents spawn children. Procore's
    budget detail rows are the motivating case: every project has several budget
    views with DIFFERENT column sets, so pulling all of them produces a table
    whose schema depends on which view happened to be returned first.
    """

    endpoint: str
    field: str = "id"
    where_field: str | None = None
    where_value: str | None = None


@dataclass(frozen=True)
class Endpoint:
    name: str
    path: str
    scope: str
    bronze_table: str
    api_version: str = "1.0"
    incremental: str | None = None
    key: str = "id"
    parent: ParentRef | None = None
    date_range_days: int | None = None
    date_param_prefix: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    unwrap: str | None = None

    def __post_init__(self) -> None:
        if self.scope not in VALID_SCOPES:
            raise ValueError(f"{self.name}: unknown scope {self.scope!r}")
        if self.scope == SCOPE_PARENT and self.parent is None:
            raise ValueError(f"{self.name}: scope 'parent' requires a parent: block")
        if self.scope != SCOPE_PARENT and self.parent is not None:
            raise ValueError(f"{self.name}: parent: is only valid with scope 'parent'")
        if ("{parent_id}" in self.path) != (self.scope == SCOPE_PARENT):
            raise ValueError(f"{self.name}: {{parent_id}} and scope 'parent' must agree")
        if "{project_id}" in self.path and self.scope == SCOPE_COMPANY:
            raise ValueError(f"{self.name}: company scope cannot use {{project_id}}")

    @property
    def needs_company_header(self) -> bool:
        """ALWAYS send Procore-Company-Id, on v1.0 as well as v2.0.

        This contradicts Procore's own documentation and is a measured finding
        carried over from the reference engagement: without the header, v1.0
        project-scoped endpoints return 404 - not 403. A 404 reads as "this
        project does not have that tool enabled", which is why a missing header
        looks for hours like a permissions problem rather than a bug.
        """
        return True


def _parse_endpoint(raw: dict[str, Any]) -> Endpoint:
    parent_raw = raw.get("parent")
    parent = None
    if parent_raw:
        parent = ParentRef(
            endpoint=parent_raw["endpoint"],
            field=parent_raw.get("field", "id"),
            where_field=parent_raw.get("where_field"),
            where_value=parent_raw.get("where_value"),
        )
    return Endpoint(
        name=raw["name"],
        path=raw["path"],
        scope=raw["scope"],
        bronze_table=raw["bronze_table"],
        api_version=str(raw.get("api_version", "1.0")),
        incremental=raw.get("incremental"),
        key=raw.get("key", "id"),
        parent=parent,
        date_range_days=raw.get("date_range_days"),
        date_param_prefix=raw.get("date_param_prefix", ""),
        params=raw.get("params") or {},
        unwrap=raw.get("unwrap"),
    )


def load_registry(path: str) -> list[Endpoint]:
    """Load and validate an endpoints.yml. Raises on any structural error."""
    import yaml  # imported here so the module is usable without PyYAML for tests

    with open(path, encoding="utf-8") as handle:
        document = yaml.safe_load(handle) or {}
    raw_endpoints = document.get("endpoints") or []
    endpoints = [_parse_endpoint(entry) for entry in raw_endpoints]
    validate_registry(endpoints)
    return endpoints


def validate_registry(endpoints: Sequence[Endpoint]) -> None:
    """Catch config mistakes at load time, with a message naming the entry."""
    names = [e.name for e in endpoints]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ValueError(f"duplicate endpoint name(s): {duplicates}")

    tables = [e.bronze_table for e in endpoints]
    dup_tables = sorted({t for t in tables if tables.count(t) > 1})
    if dup_tables:
        raise ValueError(f"duplicate bronze_table(s): {dup_tables}")

    known = set(names)
    for endpoint in endpoints:
        if endpoint.parent and endpoint.parent.endpoint not in known:
            raise ValueError(
                f"{endpoint.name}: parent endpoint "
                f"{endpoint.parent.endpoint!r} is not in the registry"
            )

    resolution_order(endpoints)  # raises on a dependency cycle


def resolution_order(endpoints: Sequence[Endpoint]) -> list[Endpoint]:
    """Parents before their children. Kahn's algorithm, which also detects cycles."""
    by_name = {e.name: e for e in endpoints}
    indegree = {e.name: 0 for e in endpoints}
    children: dict[str, list[str]] = {e.name: [] for e in endpoints}

    for endpoint in endpoints:
        if endpoint.parent:
            children[endpoint.parent.endpoint].append(endpoint.name)
            indegree[endpoint.name] += 1

    queue = [name for name, degree in indegree.items() if degree == 0]
    ordered: list[str] = []
    while queue:
        name = queue.pop(0)
        ordered.append(name)
        for child in children[name]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)

    if len(ordered) != len(endpoints):
        stuck = sorted(set(indegree) - set(ordered))
        raise ValueError(f"cycle in parent references among: {stuck}")
    return [by_name[name] for name in ordered]


def collect_parent_ids(
    parent_records: Iterable[dict],
    ref: ParentRef,
    project_field: str = "_project_id",
) -> list[tuple[Any, Any]]:
    """Return (parent_id, project_id) PAIRS for a parent-scoped endpoint.

    Dedup is on the PAIR, not the id. A company-level budget view carries the
    same view id across every project; deduping on the id alone collapses N
    project-view pairs into one and the child endpoint is then called once
    instead of N times - producing a fraction of the expected rows, silently.
    """
    pairs: list[tuple[Any, Any]] = []
    seen: set[tuple[Any, Any]] = set()
    for record in parent_records:
        if ref.where_field is not None:
            if str(record.get(ref.where_field, "")).strip() != str(ref.where_value).strip():
                continue
        parent_id = record.get(ref.field)
        if parent_id is None:
            continue
        project_id = record.get(project_field)
        pair = (parent_id, project_id)
        if pair in seen:
            continue
        seen.add(pair)
        pairs.append(pair)
    return pairs


def expand_paths(
    endpoint: Endpoint,
    company_id: Any,
    project_ids: Sequence[Any] = (),
    parent_pairs: Sequence[tuple[Any, Any]] | None = None,
) -> Iterator[tuple[str, Any]]:
    """Yield (concrete_path, project_id) for every call this endpoint requires."""
    if endpoint.scope == SCOPE_COMPANY:
        yield endpoint.path.format(company_id=company_id), None
        return

    if endpoint.scope == SCOPE_PROJECT:
        for project_id in project_ids:
            yield endpoint.path.format(company_id=company_id, project_id=project_id), project_id
        return

    for parent_id, project_id in parent_pairs or ():
        yield (
            endpoint.path.format(
                company_id=company_id, project_id=project_id, parent_id=parent_id
            ),
            project_id,
        )


def date_window_params(endpoint: Endpoint, now: datetime | None = None) -> dict[str, str]:
    """Date-window parameters for endpoints that return 0 rows without them.

    Several Procore endpoints (manpower logs, daily logs) answer 200 with an
    empty array unless given an explicit window. That is indistinguishable from
    "no data" and is the reason `date_range_days` exists in the config.
    """
    if not endpoint.date_range_days:
        return {}
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=endpoint.date_range_days)
    prefix = endpoint.date_param_prefix
    start_key = f"{prefix}[start_date]" if prefix else "start_date"
    end_key = f"{prefix}[end_date]" if prefix else "end_date"
    return {start_key: start.strftime("%Y-%m-%d"), end_key: now.strftime("%Y-%m-%d")}
