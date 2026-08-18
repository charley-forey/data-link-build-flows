"""Capture the live semantic model schema and render the data dictionary.

Two outputs, one source of truth:

  powerbi/model-schema.json    what `make_report.py` validates against
  docs/03-data-dictionary.md   what a human reads

Both are GENERATED from the deployed model rather than maintained by hand. A
hand-written data dictionary is wrong within a week of the first schema change
and nobody notices, because nothing checks it. This one is re-rendered from the
model itself, so "the docs disagree with the model" becomes impossible rather
than merely discouraged.

    python scripts/make_data_dictionary.py            # from the live model
    python scripts/make_data_dictionary.py --offline  # re-render from the JSON

Auth is the Azure CLI; no secret is read or printed.
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
SCHEMA = ROOT / "powerbi" / "model-schema.json"
DICTIONARY = ROOT / "docs" / "03-data-dictionary.md"

WORKSPACE_ID = "2ac993c2-c72f-48d1-9933-93ac189f25bf"
MODEL_ID = "73bd415b-6f03-4bee-b53c-9de36c8e4f9c"
API = "https://api.fabric.microsoft.com/v1"

# What each table is for. The model carries names and types; intent lives here,
# because a column list does not tell you why a table exists.
PURPOSE = {
    "dim_Date": "One row per day, 2015-2035. Marked as the date table.",
    "dim_Project": "One row per project. The union of the crosswalk and every project id observed on a fact.",
    "dim_DealStage": "One row per HubSpot deal stage. Carries the win probability that weighted forecasting depends on.",
    "dim_Owner": "One row per HubSpot owner.",
    "fct_WIP": "Project x month. The WIP schedule - the Controller's deliverable.",
    "fct_BudgetLine": "Project x cost code. The detail behind every WIP number.",
    "fct_ChangeOrder": "One change order. Cumulative roll-up happens in DAX, never in the grain.",
    "fct_Pipeline": "One open deal. Closed deals are excluded - a pipeline is what might still happen.",
    "fct_Aging": "One open document, AR and AP in one table discriminated by Ledger. Amounts are positive in both arms.",
    "fct_CashForecast": "One week x flow. Committed cash only; excludes unbilled backlog.",
    "fct_LabourHours": "One time entry. Cost uses the cost rate, never the billing rate.",
    "_Measures": "Measure anchor. Holds every measure; carries no data.",
    "meta_PipelineRun": "Drives the liveness measures - when the platform last ran.",
    "meta_DataQuality": "The data-quality gate's results, for the report page.",
    "meta_UnmappedProjects": "The Controller's crosswalk to-do list.",
}


def token() -> str:
    exe = shutil.which("az")
    if not exe:
        sys.exit("azure CLI not found on PATH")
    out = subprocess.run(
        [exe, "account", "get-access-token", "--resource",
         "https://api.fabric.microsoft.com", "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit("could not get a Fabric token - run `az login` first")
    return out.stdout.strip()


def call(url: str, tok: str, method: str = "GET", data: bytes | None = None):
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as response:
            raw = response.read().decode()
            return response.status, (json.loads(raw) if raw else {}), response.headers
    except urllib.error.HTTPError as error:
        return error.code, {"error": error.read().decode()[:400]}, error.headers


def fetch_model(tok: str) -> dict:
    """getDefinition is long-running: 202 plus a polling URL, then /result."""
    # format=TMSL asks for a single model.bim. Without it the API returns TMDL
    # - one .tmdl file per table - which is a different parsing job entirely.
    status, body, headers = call(
        f"{API}/workspaces/{WORKSPACE_ID}/semanticModels/{MODEL_ID}"
        f"/getDefinition?format=TMSL",
        tok, "POST", b"{}")
    if status not in (200, 202):
        sys.exit(f"getDefinition failed: {status} {body}")

    if status == 202 or not body.get("definition"):
        location = headers.get("Location")
        for _ in range(40):
            time.sleep(3)
            status, body, _ = call(location, tok)
            state = body.get("status")
            if state in ("Succeeded", "Completed"):
                break
            if state == "Failed":
                sys.exit(f"getDefinition failed: {json.dumps(body)[:400]}")
        status, body, _ = call(location.rstrip("/") + "/result", tok)

    parts = body.get("definition", {}).get("parts", [])
    bim = [p for p in parts if p["path"] == "model.bim"]
    if not bim:
        sys.exit(f"no model.bim in the definition: {[p['path'] for p in parts]}")
    return json.loads(base64.b64decode(bim[0]["payload"]))


def to_schema(bim: dict) -> dict:
    out: dict = {}
    for table in bim["model"]["tables"]:
        out[table["name"]] = {
            "columns": [c["name"] for c in table.get("columns", [])],
            "measures": [m["name"] for m in table.get("measures", [])],
            "column_detail": [
                {
                    "name": c["name"],
                    "dataType": c.get("dataType", ""),
                    "sortByColumn": c.get("sortByColumn"),
                }
                for c in table.get("columns", [])
            ],
            "measure_detail": [
                {
                    "name": m["name"],
                    "description": " ".join(m["description"]).strip()
                    if isinstance(m.get("description"), list)
                    else (m.get("description") or "").strip(),
                    "formatString": m.get("formatString", ""),
                    "displayFolder": m.get("displayFolder", ""),
                }
                for m in table.get("measures", [])
            ],
        }
    return out


def render(schema: dict) -> str:
    lines = [
        "# Data dictionary",
        "",
        "**Generated** by `scripts/make_data_dictionary.py` from the deployed",
        "semantic model. Do not edit by hand — re-run the script instead. A data",
        "dictionary maintained separately from the model is wrong within a week and",
        "nothing catches it.",
        "",
        "Naming: `*Key` is joined on. `*Id` is the source system's own identifier,",
        "carried as an attribute and **never joined across systems**. Columns are",
        "`PascalCase` in gold and `snake_case` in bronze and silver — the change at",
        "the boundary tells you which shape you are looking at.",
        "",
    ]

    dims = sorted(t for t in schema if t.startswith("dim_"))
    facts = sorted(t for t in schema if t.startswith("fct_"))
    meta = sorted(t for t in schema if t.startswith("meta_"))
    other = sorted(t for t in schema if t not in dims + facts + meta)

    for heading, group in (("Dimensions", dims), ("Facts", facts),
                           ("Metadata", meta), ("Measure anchor", other)):
        if not group:
            continue
        lines += [f"## {heading}", ""]
        for name in group:
            table = schema[name]
            lines += [f"### `{name}`", ""]
            if PURPOSE.get(name):
                lines += [PURPOSE[name], ""]
            if table["column_detail"]:
                lines += ["| Column | Type | Sorted by |", "|---|---|---|"]
                for col in table["column_detail"]:
                    sort = f"`{col['sortByColumn']}`" if col["sortByColumn"] else ""
                    lines.append(f"| `{col['name']}` | {col['dataType']} | {sort} |")
                lines.append("")
            if not table["column_detail"] and not table["measure_detail"]:
                lines += ["_No columns._", ""]

    measures = schema.get("_Measures", {}).get("measure_detail", [])
    if measures:
        lines += ["## Measures", "",
                  f"{len(measures)} measures, all on `_Measures`. A measure cannot share a",
                  "name with a column on the same table, and the natural names collide",
                  "immediately — `EAC` and `Backlog` are both columns on `fct_WIP`. Hanging",
                  "every measure off one anchor table avoids that by construction.",
                  ""]
        folders: dict[str, list[dict]] = {}
        for m in measures:
            folders.setdefault(m["displayFolder"] or "General", []).append(m)
        for folder in sorted(folders):
            lines += [f"### {folder}", "", "| Measure | Format | What it is |", "|---|---|---|"]
            for m in folders[folder]:
                desc = m["description"].replace("|", "/").replace("\n", " ")
                if len(desc) > 180:
                    desc = desc[:177] + "..."
                lines.append(f"| `{m['name']}` | `{m['formatString']}` | {desc} |")
            lines.append("")

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true",
                        help="re-render the markdown from the stored JSON")
    args = parser.parse_args()

    if args.offline:
        if not SCHEMA.exists():
            sys.exit(f"{SCHEMA} does not exist - run without --offline first")
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    else:
        schema = to_schema(fetch_model(token()))
        SCHEMA.write_text(json.dumps(schema, indent=1), encoding="utf-8")
        print(f"wrote {SCHEMA.relative_to(ROOT)}")

    DICTIONARY.write_text(render(schema), encoding="utf-8")
    tables = len(schema)
    measures = len(schema.get("_Measures", {}).get("measures", []))
    print(f"wrote {DICTIONARY.relative_to(ROOT)}  ({tables} tables, {measures} measures)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
