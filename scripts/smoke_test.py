"""Prove each source API actually returns data, before anything touches Fabric.

    python scripts/smoke_test.py                 # all configured sources
    python scripts/smoke_test.py --source procore

This is the cheapest possible failure. A credential problem found here costs
seconds; the same problem found inside a Fabric notebook costs a Spark session
startup and a trip through the driver logs to read the traceback.

NOTHING IS WRITTEN. Read-only calls, no Delta tables, no watermarks advanced.

Secret VALUES are never printed - only whether a secret is present, and what the
API returned.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform" / "lib"))

import fabric_common as fc  # noqa: E402

OK = "  ok  "
FAIL = " FAIL "
SKIP = " skip "


def _mask(value: str) -> str:
    """Enough to confirm the right credential is loaded, not enough to use it."""
    if not value:
        return "(empty)"
    return f"{value[:4]}...{value[-4:]} ({len(value)} chars)"


# ---------------------------------------------------------------- procore


def smoke_procore() -> bool:
    """Client-credentials grant, then two read-only calls.

    Client credentials need no human, which is why this one can run unattended
    and QuickBooks cannot.
    """
    import requests

    import procore_extract as px
    from ratelimit import RateLimitedSession
    from scope import Endpoint

    try:
        settings = px.settings_from_secrets(fc.get_secret)
    except RuntimeError as exc:
        print(f"[{SKIP}] procore: {exc}")
        return True

    print(f"[      ] procore: {settings.base_url}, company {settings.company_id}")
    print(f"[      ] procore: client_id {_mask(settings.client_id)}")

    session = RateLimitedSession(requests.Session(), header_units="seconds")

    try:
        token = px.fetch_token(settings, session)
    except Exception as exc:  # noqa: BLE001
        print(f"[{FAIL}] procore: token exchange failed - {exc}")
        _procore_hint(exc)
        return False
    print(f"[{OK}] procore: token acquired ({len(token)} chars)")

    # Company-scoped projects list. This is also the call that decides how much
    # of the hourly quota the real extractor will spend.
    endpoint = Endpoint(
        name="projects",
        path="/rest/v1.0/companies/{company_id}/projects",
        scope="company",
        bronze_table="dl_bronze_procore_projects",
    )
    headers = px.build_headers(token, settings.company_id, endpoint)

    try:
        rows = list(
            px.iter_records(
                session,
                settings.base_url,
                endpoint.path.format(company_id=settings.company_id),
                headers,
                per_page=100,
            )
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[{FAIL}] procore: project list failed - {exc}")
        _procore_hint(exc)
        return False

    print(f"[{OK}] procore: {len(rows)} project(s) returned")
    for row in rows[:5]:
        print(
            f"          - {str(row.get('id')):>10}  "
            f"{str(row.get('name'))[:44]:<44} {row.get('status_name') or ''}"
        )

    if not rows:
        print("          (no projects: an empty sandbox, or the token's company is wrong)")
        return True

    # The budget views on the first project. This is the WIP spine, and it is
    # where the tenant-specific budget-view NAME has to be confirmed.
    project_id = rows[0].get("id")
    bv = Endpoint(
        name="budget_views",
        path="/rest/v1.0/budget_views",
        scope="project",
        bronze_table="dl_bronze_procore_budget_views",
    )
    try:
        views = list(
            px.iter_records(
                session,
                settings.base_url,
                bv.path,
                px.build_headers(token, settings.company_id, bv),
                params={"project_id": project_id},
                per_page=100,
            )
        )
        print(f"[{OK}] procore: {len(views)} budget view(s) on project {project_id}")
        for view in views:
            print(f"          - {view.get('name')!r}")
        if views:
            print(
                "          ^ pin ONE of these names into "
                "ingestion/procore/config/endpoints.yml (budget_detail_rows.parent.where_value)"
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[  warn] procore: budget views unavailable - {exc}")

    print(f"[      ] procore: {session.requests_made} request(s) used, "
          f"quota remaining {session.remaining}")
    return True


def _procore_hint(exc: Exception) -> None:
    text = str(exc)
    if "401" in text or "invalid_client" in text:
        print("          hint: client id/secret rejected. Confirm the app is authorised "
              "for this company, and that the base URL matches the credentials "
              "(sandbox creds do not work against api.procore.com).")
    elif "404" in text:
        print("          hint: a 404 here is usually the missing Procore-Company-Id "
              "header or a company id the token cannot see - not a bad path.")
    elif "403" in text:
        print("          hint: the app lacks permission on this company or tool.")


# ---------------------------------------------------------------- quickbooks


def smoke_qbo() -> bool:
    import requests

    import qbo_extract as qx
    from ratelimit import RateLimitedSession

    missing = [
        name
        for name in ("QUICKBOOKS_CLIENT_ID", "QUICKBOOKS_CLIENT_SECRET",
                     "QUICKBOOKS_REALM_ID", "QUICKBOOKS_REFRESH_TOKEN")
        if not _present(name)
    ]
    if missing:
        print(f"[{SKIP}] quickbooks: not authorised yet - missing {', '.join(missing)}")
        print("          run: python scripts/qbo_authorize.py")
        return True

    settings = qx.settings_from_secrets(fc.get_secret)
    session = RateLimitedSession(requests.Session(), header_units="seconds")
    print(f"[      ] quickbooks: realm {settings.realm_id} ({settings.environment})")

    try:
        tokens = qx.refresh_access_token(
            settings, session, fc.get_secret("QUICKBOOKS_REFRESH_TOKEN")
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[{FAIL}] quickbooks: token refresh failed - {exc}")
        print("          hint: the refresh token ROTATES on every use. If a pipeline run "
              "has already consumed it, the live value is in dl_meta_token, not .env. "
              "If it is over 100 days old, re-run scripts/qbo_authorize.py.")
        return False

    print(f"[{OK}] quickbooks: access token acquired")

    # PERSIST THE ROTATED TOKEN. QuickBooks invalidates the old one, so a
    # diagnostic that refreshes without saving the replacement would leave .env
    # holding a dead credential - i.e. checking the connection would break it.
    env_path = fc.find_dotenv(str(ROOT))
    if env_path:
        fc.update_dotenv(env_path, "QUICKBOOKS_REFRESH_TOKEN", tokens.refresh_token)
        print(f"[{OK}] quickbooks: rotated refresh token saved to {env_path}")
    else:
        print(f"[  warn] quickbooks: token rotated to {_mask(tokens.refresh_token)} "
              "but no .env to save it to - the previous value is now stale")

    headers = qx.build_headers(tokens.access_token)

    for entity in ("CompanyInfo", "Customer", "Account", "Bill"):
        try:
            rows = list(qx.iter_entity(session, settings, headers, entity, max_results=5))
        except Exception as exc:  # noqa: BLE001
            print(f"[{FAIL}] quickbooks: {entity} query failed - {exc}")
            return False
        label = ""
        if entity == "CompanyInfo" and rows:
            label = f"  ({rows[0].get('CompanyName', '')})"
        elif entity == "Customer":
            jobs = sum(1 for r in rows if r.get("Job") or r.get("IsProject"))
            label = f"  ({jobs} of these are jobs/projects)"
        print(f"[{OK}] quickbooks: {entity:<12} {len(rows)} row(s){label}")

    return True


# ---------------------------------------------------------------- hubspot


def smoke_hubspot() -> bool:
    import requests

    import hubspot_extract as hx
    from ratelimit import RateLimitedSession

    if not _present("HUBSPOT_PRIVATE_APP_TOKEN"):
        print(f"[{SKIP}] hubspot: no token configured")
        return True

    token = fc.get_secret("HUBSPOT_PRIVATE_APP_TOKEN")

    # A shape warning is not a verdict - try the call and report what HubSpot
    # actually says. Refusing on a prefix would be guessing.
    problem = hx.check_token_shape(token)
    if problem:
        print(f"[  warn] hubspot: {problem}")

    headers = hx.build_headers(token)
    session = RateLimitedSession(requests.Session(), header_units="milliseconds")

    spec = hx.ObjectSpec(
        name="deals",
        object_type="deals",
        bronze_table="dl_bronze_hubspot_deals",
        properties=("dealname", "amount", "dealstage", "closedate"),
    )
    try:
        rows = []
        for row in hx.iter_objects(session, headers, spec, page_size=5):
            rows.append(row)
            if len(rows) >= 5:
                break
    except Exception as exc:  # noqa: BLE001
        print(f"[{FAIL}] hubspot: deals query failed - {exc}")
        print("          hint: a 401 means the private-app token is wrong; a 403 means "
              "the app is missing the crm.objects.deals.read scope.")
        return False

    print(f"[{OK}] hubspot: {len(rows)} deal(s) returned")

    try:
        pipelines = hx.fetch_pipelines(session, headers)
        stages = sum(len(p.get("stages") or []) for p in pipelines)
        print(f"[{OK}] hubspot: {len(pipelines)} pipeline(s), {stages} stage(s)")
        print("          (win probability lives on the STAGE, not the deal)")
    except Exception as exc:  # noqa: BLE001
        print(f"[  warn] hubspot: pipelines unavailable - {exc}")

    return True


# ---------------------------------------------------------------- runner


def _present(name: str) -> bool:
    try:
        fc.get_secret(name)
    except RuntimeError:
        return False
    return True




SOURCES = {"procore": smoke_procore, "quickbooks": smoke_qbo, "hubspot": smoke_hubspot}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=sorted(SOURCES), help="test one source only")
    args = parser.parse_args()

    env_path = fc.load_dotenv_upwards(str(ROOT))
    print(f"credentials from: {env_path or '(environment only)'}\n")

    chosen = [args.source] if args.source else list(SOURCES)
    failures = []
    for name in chosen:
        print(f"--- {name} ---")
        try:
            if not SOURCES[name]():
                failures.append(name)
        except Exception as exc:  # noqa: BLE001
            print(f"[{FAIL}] {name}: unexpected error - {type(exc).__name__}: {exc}")
            failures.append(name)
        print()

    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("all configured sources responded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
