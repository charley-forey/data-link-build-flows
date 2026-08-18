# Data Link Technology Services — Financial Operating System

A Microsoft Fabric platform that connects **Procore**, **QuickBooks Online** and
**HubSpot** into one governed source of truth, and produces the WIP/EAC, project
financial performance and backlog reporting that the Controller and Ops team
currently assemble by hand every month.

Built for Data Link Technology Services, a commercial low-voltage contractor
in Arizona.

---

## Where things are

**Fabric** — workspace `BuildFlows`, folder `Data Link`.

| Item | Name |
|---|---|
| Lakehouse | `DL_Lakehouse` |
| Notebooks | `dl_00_bootstrap`, `dl_01_extract_procore`, `dl_02_extract_qbo`, `dl_10_bronze_to_silver`, `dl_30_build_gold`, `dl_40_dq_checks` |
| Pipelines | `DL_Ingest_Pipeline` (extraction), `DL_Master_Pipeline` (medallion + gate) |

**This repository** — the source of truth. Fabric is a deployment target; a
mis-created item is fixed by re-running a deploy, not by clicking.

```
docs/            architecture, WIP methodology, runbook
platform/lib/    the shared library, deployed to Files/lib in the lakehouse
ingestion/       one endpoint registry per source (YAML)
transformation/  the medallion SQL and the data-quality suite
powerbi/         semantic model spec, DAX measures, report spec, theme
scripts/         notebook generator, QBO authorisation, deploy helpers
tests/           offline tests - no network, no Fabric, no Spark
```

---

## Architecture

```
Procore REST ─┐
QBO REST      ├─► one config-driven extractor per source
HubSpot REST ─┘        │
                       └─► dl_bronze_*   raw payload, unparsed, + audit columns
                                  │
                            dl_silver_*  typed, TRIMmed, validated, rejects logged
                                  │      + dl_silver_project_crosswalk
                            dim_/fct_    the star schema
                                  │
                          DQ gate ─┤     blocking failure stops the run
                                  │
                    Semantic model (Direct Lake) ─► Power BI report
```

### The layer contract

| Prefix | Holds | Rule |
|---|---|---|
| `dl_bronze_` | Raw API payload **unparsed**, plus audit columns | Never transform here. Bronze cannot drop a column it never parsed, so a transform bug is a re-run, not a re-extract — and re-extracting is what Procore's 600 requests/hour makes painful. |
| `dl_silver_` | Typed, TRIMmed, validated | Rejected rows are logged with a reason, **never dropped**. |
| `dim_` / `fct_` / `meta_` | The star schema | Column names match the semantic model exactly; the DAX reads them by name. |
| `dl_meta_` | Pipeline state | watermarks, run log, token store |
| `dl_dq_` | Data quality output | results and the failing rows themselves |

Columns are `snake_case` through silver and `PascalCase` in gold. The change at
the boundary is deliberate: it makes it visually obvious whether you are looking
at source-shaped or model-shaped data.

Keys: `*Key` is joined on. `*Id` is the source system's own identifier — carried
as an attribute, **never joined across systems**. `ProcoreProjectId`,
`QboCustomerId` and `HubspotDealId` all live on `dim_Project`; facts join on
`ProjectKey` only.

---

## The two things that make this work

### The crosswalk

There is no shared key between Procore, QuickBooks and HubSpot. Everything
downstream depends on `dl_silver_project_crosswalk`, which resolves in strict
precedence: **manual** (the Controller's CSV, always wins) → **exact** project
number → **fuzzy** name, only when unambiguous and above a confidence floor.

Ambiguity is not a match. If a project resembles two QuickBooks jobs equally
well, picking one is a coin flip dressed up as a decision — it goes to review.

`dim_Project` is the **union of the crosswalk and every project id observed on a
fact**, with `IsInCrosswalk` recording the difference. Referential integrity
therefore holds by construction, and a missing mapping becomes a visible row on
a report page rather than a quietly smaller total.

### The gate

`dl_40_dq_checks` runs 34 expectations. **Every expectation is a SQL predicate
that returns the failing rows** — a failure is a set of rows someone can open,
not a red light.

Two severities, and the split is load-bearing:

- **ERROR** stops the pipeline. Reserved for things that make a number *wrong*: a duplicate dimension key, an orphaned fact, an accounting identity that does not hold.
- **WARN** records and continues. For things true of the real data that would be dishonest to hide: an unmapped project, a Procore↔QuickBooks variance.

The instinct to make everything an ERROR is wrong. A pipeline that blocks on a
real business condition gets muted within a week, and then the blocking checks
stop working too. **A stale report beats a wrong one** — but only when the thing
that blocks is genuinely a wrongness.

---

## Running it

### Offline tests — no network, no Fabric

```bash
python tests/test_gold.py
```

Runs the **real** gold SQL through DuckDB against fixtures: 66 assertions
covering every WIP identity, plus 26 of the data-quality expectations executed
for real. The file under test is the one that ships.

### Regenerate the notebooks

```bash
python scripts/make_notebooks.py
```

Notebooks are **generated, never hand-edited** — a hand-edited notebook is
overwritten by the next deploy and the change is lost silently. Every code cell
is compiled before it is written.

### Deploy to Fabric

The library, SQL and configs live in the lakehouse `Files/` area
(`lib/`, `sql/`, `config/`, `dq/`, `reference/`) so notebooks stay thin: one copy
of the code, one place to fix a bug. Re-upload after changing anything under
`platform/`, `transformation/` or `ingestion/`.

---

## Status

**Verified running in Fabric**

The complete medallion has been executed end to end in the real Spark runtime:

| Stage | Result |
|---|---|
| `dl_00_bootstrap` | 52 bronze + 6 control tables created, correctly typed |
| `dl_10_bronze_to_silver` | 17 silver tables built, including the crosswalk |
| `dl_30_build_gold` | 5 dimensions, 4 facts, 3 metadata tables |
| `dl_40_dq_checks` | **34 expectations, 34 passed, 0 warnings, 0 blocking** |

`dim_Date` generated correctly (7,670 days, 2015–2035, `MonthOffset` −139 to
+112). Every money column in `fct_WIP` landed as `float` — no DECIMAL
contamination, which is the thing that silently breaks Direct Lake.

- WIP arithmetic locked by tests: 66 assertions on the real gold SQL.
- Both pipelines wired with `Succeeded` dependencies.
- Semantic model built over the gold schema with relationships.

**Live source verification**

| Source | State | Evidence |
|---|---|---|
| **Procore** | connected | 374 rows across 24 endpoints, landed to bronze. Includes budget-view detail rows (the EAC spine). |
| **HubSpot** | connected | Authenticates on the `pat-` service token; 1 deal pipeline, 7 stages. Confirms the `2026-03` API path. |
| **QuickBooks** | blocked | Needs one interactive OAuth consent: `python scripts/qbo_authorize.py` |

```bash
python scripts/smoke_test.py            # one read-only call per source
python scripts/extract_local.py --source procore   # full registry -> JSONL
```

`smoke_test.py` exists because a credential problem found locally costs seconds,
while the same problem found inside a Fabric notebook costs a Spark session
startup and a trip through the driver logs.

**Four defects the live APIs exposed** (none of which offline tests could catch):

1. `PROCORE_SANDBOX_URL` held a *browser* URL, so every request was built under `/company/home`. `normalise_base_url()` now reduces any input to a bare origin.
2. The vendors path was wrong — `/rest/v1.0/vendors` (query-scoped), not `/companies/{id}/vendors`.
3. A project without a tool enabled answers 403/404, and that was aborting the whole endpoint for every other project. Now `ToolUnavailable`: counted, skipped, reported.
4. **The budget money columns are named with spaces** (`Job to Date Costs`). `$.dot` notation cannot parse those — it returns NULL, COALESCEs to 0, and yields a WIP schedule that passes every accounting identity while being entirely wrong. Now bracket notation, pinned by `tests/test_silver_keys.py` against a captured payload.

Also measured: Procore's real quota on this tenant is **25 requests per ~10s window**, not the 600/hour that is widely quoted. The rate-limit reserve now scales to the limit the API reports.

**Not built yet (phase 2)**

- HubSpot ingestion. The extractor and config exist; the gold arm is declared as an empty typed view (`sv_deals`) so everything downstream compiles and tests today, and goes live by changing that one view.
- Cash forecast, AR/collections, capacity planning.
- The Power BI report. The semantic model is buildable programmatically; the report is authored as a PBIP project and published — the Fabric MCP server has no report-creation tool.

---

## Operational hazards worth knowing

1. **The QuickBooks refresh token rotates on every use** and hard-expires at 100 days. `dl_02_extract_qbo` persists the new one to `dl_meta_token` *before* pulling any data. If that write is ever skipped, the integration works until the access token expires and then fails permanently — an hour after whoever changed it stopped watching. Two expectations watch the token's age (warn at 60 days, block at 85).

2. **Procore allows 600 requests/hour** and does **not** send `Retry-After` on a 429 — it sends `X-Rate-Limit-Reset`, a Unix epoch. The session gates on the remaining-quota header before spending a request it does not have, and raises `QuotaExhausted` rather than hanging.

3. **`Procore-Company-Id` must be sent on every API version.** Without it, v1.0 project-scoped endpoints return **404**, not 403 — which reads as "this project has no such tool" and looks for hours like a permissions problem.

4. **Procore returns a different column set per budget view.** The registry pins one view by name. Confirm it before the first production run.

5. **A type mismatch makes Direct Lake drop a table silently.** It appears as a missing table, not an error. `dl_30_build_gold` writes the gold schema to `Files/_diag/gold_schema.json` so the semantic model can be generated from what actually exists.

---

## Documentation

| File | Contents |
|---|---|
| `docs/04-wip-methodology.md` | Every financial definition, the worked example, and the seven decisions that are easy to get wrong |
| `docs/05-runbook.md` | Daily operation, failure modes, re-authorisation |
| `powerbi/measures.dax` | The DAX library |
| `platform/naming-standards.md` | Naming conventions |
