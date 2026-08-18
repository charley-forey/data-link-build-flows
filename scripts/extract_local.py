"""Run a source extraction locally and land the results as JSONL.

    python scripts/extract_local.py --source procore
    python scripts/extract_local.py --source procore --endpoint budget_detail_rows
    python scripts/extract_local.py --source quickbooks

WHY THIS EXISTS

    Fabric notebooks have no access to a local .env. Until the credentials are
    in Key Vault, extraction runs HERE - where the secret already lives - and
    writes JSONL that a credential-free notebook loads into bronze.

    That split is the point: this half needs a secret and no Spark; the other
    half needs Spark and no secret. It is also the documented fallback for a
    tenant with no Azure subscription.

    It uses the SAME library the notebook uses, so running it exercises the real
    pagination, rate limiting, parent expansion and bronze row shape - not a
    parallel implementation that can drift.

OUTPUT

    .local/_landing/<batch_id>/<bronze_table>.jsonl

    One JSON object per line, already in bronze row shape. Upload the folder to
    the lakehouse under Files/_landing/ and run dl_05_land_to_bronze.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform" / "lib"))

import fabric_common as fc  # noqa: E402

LANDING = ROOT / ".local" / "_landing"


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, default=str) + "\n")


def extract_procore(batch_id: str, only: str | None, max_projects: int | None) -> list[tuple]:
    import requests

    import procore_extract as px
    import scope as sc
    from procore_extract import ToolUnavailable
    from ratelimit import QuotaExhausted, RateLimitedSession

    settings = px.settings_from_secrets(fc.get_secret)
    session = RateLimitedSession(requests.Session(), header_units="seconds")
    token = px.fetch_token(settings, session)

    endpoints = sc.load_registry(str(ROOT / "ingestion/procore/config/endpoints.yml"))
    ordered = sc.resolution_order(endpoints)
    if only:
        # Keep the named endpoint AND its ancestors - a child cannot resolve
        # without the parent that supplies its ids.
        keep, frontier = set(), {only}
        by_name = {e.name: e for e in ordered}
        while frontier:
            name = frontier.pop()
            if name in keep or name not in by_name:
                continue
            keep.add(name)
            parent = by_name[name].parent
            if parent:
                frontier.add(parent.endpoint)
        ordered = [e for e in ordered if e.name in keep]

    projects = list(px.iter_active_projects(session, settings, token))
    project_ids = [p["id"] for p in projects]
    if max_projects:
        project_ids = project_ids[:max_projects]
    print(f"{len(project_ids)} active project(s)\n")

    out = LANDING / batch_id
    fetched: dict[str, list[dict]] = {}
    summary: list[tuple] = []

    for endpoint in ordered:
        parent_pairs = None
        if endpoint.parent:
            parent_pairs = sc.collect_parent_ids(
                fetched.get(endpoint.parent.endpoint, []), endpoint.parent
            )
            if not parent_pairs:
                print(f"  {endpoint.name:32} skipped (parent {endpoint.parent.endpoint!r} empty)")
                summary.append((endpoint.name, 0, "skipped"))
                continue

        headers = px.build_headers(token, settings.company_id, endpoint)
        base_params = px.endpoint_params(endpoint, settings.company_id, None)

        records: list[dict] = []
        rows: list[dict] = []
        ingested_at = fc.utc_now()
        status = "full"
        unavailable = 0

        try:
            for path, project_id in sc.expand_paths(
                endpoint, settings.company_id, project_ids, parent_pairs
            ):
                params = {
                    **base_params,
                    **px.implicit_params(endpoint, settings.company_id, project_id),
                }
                try:
                    for record in px.iter_records(
                        session, settings.base_url, path, headers,
                        params=params, unwrap=endpoint.unwrap,
                        tolerate_unavailable=True,
                    ):
                        record.setdefault("_project_id", project_id)
                        records.append(record)
                        rows.append({
                            **px.to_bronze_row(record, endpoint, project_id, ingested_at),
                            "_batch_id": batch_id,
                            "_row_hash": fc.row_hash(record),
                        })
                except ToolUnavailable:
                    # This project does not have the tool. Counted, not fatal.
                    unavailable += 1
        except QuotaExhausted as exc:
            status = "quota_exhausted"
            print(f"  {endpoint.name:32} QUOTA EXHAUSTED after {len(rows)} row(s) - {exc}")
        except Exception as exc:  # noqa: BLE001
            status = f"error: {type(exc).__name__}"
            print(f"  {endpoint.name:32} ERROR {exc}")

        fetched[endpoint.name] = records
        if rows:
            _write(out / f"{endpoint.bronze_table}.jsonl", rows)

        if unavailable:
            status = "full" if records or unavailable < len(project_ids) else "tool_disabled"
        flag = "" if status == "full" else f"  <- {status}"
        if unavailable:
            flag += f"  ({unavailable} project(s) without this tool)"
        print(f"  {endpoint.name:32} {len(rows):6,d} row(s){flag}")
        summary.append((endpoint.name, len(rows), status))
        if status == "quota_exhausted":
            break

    print(f"\nrequests used: {session.requests_made}   "
          f"quota remaining: {session.remaining}/{session.limit}")
    return summary


def extract_qbo(batch_id: str, only: str | None, _: int | None) -> list[tuple]:
    import requests
    import yaml

    import qbo_extract as qx
    from ratelimit import RateLimitedSession

    settings = qx.settings_from_secrets(fc.get_secret)
    session = RateLimitedSession(requests.Session(), header_units="seconds")
    tokens = qx.refresh_access_token(
        settings, session, fc.get_secret("QUICKBOOKS_REFRESH_TOKEN")
    )
    headers = qx.build_headers(tokens.access_token)

    print("NOTE: the refresh token just ROTATED. The new one is:")
    print(f"      {tokens.refresh_token}")
    print("      Update .env (or dl_meta_token) with it - the old one is dead.\n")

    with open(ROOT / "ingestion/qbo/config/entities.yml", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)

    out = LANDING / batch_id
    summary: list[tuple] = []

    for entry in config["entities"]:
        name = entry["name"]
        if only and name.lower() != only.lower():
            continue
        try:
            records = list(qx.iter_entity(session, settings, headers, name))
        except Exception as exc:  # noqa: BLE001
            print(f"  {name:24} ERROR {exc}")
            summary.append((name, 0, f"error: {type(exc).__name__}"))
            continue
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
            _write(out / f"{entry['bronze_table']}.jsonl", rows)
        print(f"  {name:24} {len(rows):6,d} row(s)")
        summary.append((name, len(rows), "full"))

    for entry in config.get("reports", []):
        name = entry["name"]
        if only and name.lower() != only.lower():
            continue
        try:
            payload = qx.fetch_report(session, settings, headers, name, entry.get("params"))
        except Exception as exc:  # noqa: BLE001
            print(f"  {name:24} ERROR {exc}")
            summary.append((name, 0, f"error: {type(exc).__name__}"))
            continue
        rows = [{
            "_key": name,
            "_project_id": None,
            "_merge_key": f"report|{name}",
            "_source_endpoint": name,
            "_ingested_at": fc.utc_now(),
            "payload": json.dumps(payload, default=str),
            "_batch_id": batch_id,
            "_row_hash": fc.row_hash(payload),
        }]
        _write(out / f"{entry['bronze_table']}.jsonl", rows)
        print(f"  {name:24} report captured")
        summary.append((name, 1, "report"))

    return summary


EXTRACTORS = {"procore": extract_procore, "quickbooks": extract_qbo}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, choices=sorted(EXTRACTORS))
    parser.add_argument("--endpoint", help="only this endpoint (plus its parents)")
    parser.add_argument("--max-projects", type=int, help="limit project fan-out while testing")
    args = parser.parse_args()

    env_path = fc.load_dotenv_upwards(str(ROOT))
    print(f"credentials from: {env_path or '(environment only)'}")

    batch_id = fc.new_batch_id()
    print(f"batch {batch_id}\n")

    summary = EXTRACTORS[args.source](batch_id, args.endpoint, args.max_projects)

    total = sum(count for _, count, _ in summary)
    problems = [n for n, _, s in summary if s not in ("full", "skipped", "report")]
    empty = [n for n, c, s in summary if c == 0 and s == "full"]

    print(f"\n{total:,} row(s) landed in {LANDING / batch_id}")
    if empty:
        # On a FULL pull an empty endpoint is usually a permission gap or a tool
        # this tenant does not use - not genuinely zero records.
        print(f"empty ({len(empty)}): {', '.join(empty)}")
    if problems:
        print(f"PROBLEMS ({len(problems)}): {', '.join(problems)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
