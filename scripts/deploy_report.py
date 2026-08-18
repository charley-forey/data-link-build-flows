"""Publish the PBIR report to Fabric.

The Fabric MCP server has no report-creation tool, so this is the one item
deployed over raw REST. Dry-run by default, like every other deploy script here.

    python scripts/deploy_report.py            show what would be sent
    python scripts/deploy_report.py --apply    create or update the report

Auth comes from the Azure CLI (`az account get-access-token`), so no secret is
read, written or printed by this script.
"""

from __future__ import annotations

import argparse
import base64
import json
import pathlib
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPORT_NAME = "Financial Operating System"
MODEL_NAME = "Data Link Financial Operating System"
WORKSPACE = "BuildFlows"
WORKSPACE_ID = "2ac993c2-c72f-48d1-9933-93ac189f25bf"
MODEL_ID = "73bd415b-6f03-4bee-b53c-9de36c8e4f9c"
FOLDER_ID = "3ffcd477-0703-4081-a9f5-6b2a3d8fbe55"
SRC = ROOT / "powerbi" / f"{REPORT_NAME}.Report"
API = "https://api.fabric.microsoft.com/v1"

# Published reports bind to the model by CONNECTION. The local definition.pbir
# uses byPath so the folder still opens in Desktop as a PBIP; the two are not
# interchangeable and the swap happens here, at the boundary.
CONNECTION = (
    f"Data Source=powerbi://api.powerbi.com/v1.0/myorg/{WORKSPACE};"
    f'initial catalog="{MODEL_NAME}";'
    f"integrated security=ClaimsToken;semanticmodelid={MODEL_ID}"
)


def token() -> str:
    # On Windows `az` is a .cmd shim, which CreateProcess will not resolve from
    # a bare name - which is why this looks it up rather than trusting PATH.
    exe = shutil.which("az")
    if not exe:
        sys.exit("azure CLI not found on PATH - install it, or run `az login`")
    out = subprocess.run(
        [exe, "account", "get-access-token", "--resource",
         "https://api.fabric.microsoft.com", "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, shell=False,
    )
    if out.returncode != 0:
        sys.exit("could not get a Fabric token - run `az login` first")
    return out.stdout.strip()


def call(method: str, url: str, body: dict | None = None, tok: str = ""):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {tok}")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request) as response:
            raw = response.read().decode()
            return response.status, (json.loads(raw) if raw else {}), response.headers
    except urllib.error.HTTPError as error:
        raw = error.read().decode()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"raw": raw}
        return error.code, parsed, error.headers


def parts() -> list[dict]:
    """Every file in the .Report folder, base64'd, with POSIX relative paths."""
    out = []
    for path in sorted(SRC.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(SRC).as_posix()
        text = path.read_text(encoding="utf-8")
        if relative == "definition.pbir":
            pbir = json.loads(text)
            pbir["datasetReference"] = {"byConnection": {"connectionString": CONNECTION}}
            text = json.dumps(pbir, indent=2)
        out.append({
            "path": relative,
            "payload": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            "payloadType": "InlineBase64",
        })
    return out


def find_report(tok: str) -> str | None:
    status, body, _ = call("GET", f"{API}/workspaces/{WORKSPACE_ID}/reports", tok=tok)
    if status != 200:
        sys.exit(f"could not list reports: {status} {body}")
    for item in body.get("value", []):
        if item.get("displayName") == REPORT_NAME:
            return item["id"]
    return None


def wait(headers, tok: str) -> None:
    """Long-running operations return 202 and a polling URL."""
    location = headers.get("Location")
    if not location:
        return
    for _ in range(60):
        time.sleep(3)
        status, body, _ = call("GET", location, tok=tok)
        state = (body or {}).get("status")
        if state in ("Succeeded", "Completed"):
            return
        if state == "Failed":
            sys.exit(f"operation failed: {json.dumps(body)[:800]}")
    sys.exit("operation did not finish in time")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="actually publish")
    args = parser.parse_args()

    if not SRC.exists():
        sys.exit(f"{SRC} does not exist - run scripts/make_report.py first")

    payload = parts()
    pages = sum(1 for p in payload if p["path"].endswith("/page.json"))
    visuals = sum(1 for p in payload if p["path"].endswith("/visual.json"))
    size = sum(len(p["payload"]) for p in payload)
    print(f"{REPORT_NAME}: {len(payload)} parts, {pages} pages, {visuals} visuals, "
          f"{size / 1024:.0f} KB encoded")
    print(f"binds to semantic model {MODEL_ID} in {WORKSPACE}")

    if not args.apply:
        print("\ndry run - nothing sent. re-run with --apply to publish.")
        return 0

    tok = token()
    existing = find_report(tok)
    definition = {"parts": payload}

    if existing:
        print(f"updating existing report {existing}")
        status, body, headers = call(
            "POST", f"{API}/workspaces/{WORKSPACE_ID}/reports/{existing}/updateDefinition",
            {"definition": definition}, tok)
        if status not in (200, 202):
            sys.exit(f"update failed: {status} {json.dumps(body)[:1200]}")
        wait(headers, tok)
        print(f"updated {REPORT_NAME}")
    else:
        print("creating report")
        status, body, headers = call(
            "POST", f"{API}/workspaces/{WORKSPACE_ID}/reports",
            {"displayName": REPORT_NAME, "definition": definition,
             "folderId": FOLDER_ID}, tok)
        if status not in (200, 201, 202):
            sys.exit(f"create failed: {status} {json.dumps(body)[:1200]}")
        wait(headers, tok)
        print(f"created {REPORT_NAME}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
