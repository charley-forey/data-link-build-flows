# Data Link Technology Services — Financial Operating System

A Microsoft Fabric platform that connects **Procore**, **QuickBooks Online** and
**HubSpot** into one governed source of truth, and produces the WIP/EAC, project
financial performance, backlog, pipeline forecast, cash forecast, receivables
and capacity reporting that the Controller and Ops team currently assemble by
hand every month.

Built for Data Link Technology Services, a commercial low-voltage contractor
in Arizona.

**Status: both phases built, running end to end on live sandbox data, and
published as an 11-page Power BI report.**

---

## Where things are

**Fabric** — workspace `BuildFlows`, folder `Data Link`.

| Item | Name |
|---|---|
| Lakehouse | `DL_Lakehouse` |
| Notebooks | `dl_00_bootstrap`, `dl_01_extract_procore`, `dl_02_extract_qbo`, `dl_03_extract_hubspot`, `dl_05_land_to_bronze`, `dl_10_bronze_to_silver`, `dl_30_build_gold`, `dl_40_dq_checks` |
| Pipelines | `DL_Ingest_Pipeline` (extraction), `DL_Master_Pipeline` (medallion + gate) |
| Semantic model | `Data Link Financial Operating System` — Direct Lake, 15 tables, 68 measures |
| Report | `Financial Operating System` — 11 pages, 110 visuals |

**This repository** — the source of truth. Fabric is a deployment target; a
mis-created item is fixed by re-running a deploy, not by clicking.

```
docs/            architecture, WIP methodology, runbook, client summary
platform/lib/    the shared library, deployed to Files/lib in the lakehouse
ingestion/       one endpoint registry per source (YAML)
transformation/  the medallion SQL and the data-quality suite
powerbi/         semantic model spec, DAX measures, report spec, theme, PBIR
scripts/         generators and deploy helpers (all dry-run by default)
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
| `dl_bronze_` | Raw API payload **unparsed**, plus audit columns | Never transform here. Bronze cannot drop a column it never parsed, so a transform bug is a re-run, not a re-extract — and re-extracting is what Procore's request quota makes painful. |
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

`dl_40_dq_checks` runs **53 expectations** (35 blocking, 18 warning). **Every
expectation is a SQL predicate that returns the failing rows** — a failure is a
set of rows someone can open, not a red light.

Two severities, and the split is load-bearing:

- **ERROR** stops the pipeline. Reserved for things that make a number *wrong*: a duplicate dimension key, an orphaned fact, an accounting identity that does not hold.
- **WARN** records and continues. For things true of the real data that would be dishonest to hide: an unmapped project, a Procore↔QuickBooks variance, an overdue payable.

The instinct to make everything an ERROR is wrong. A pipeline that blocks on a
real business condition gets muted within a week, and then the blocking checks
stop working too. **A stale report beats a wrong one** — but only when the thing
that blocks is genuinely a wrongness.

---

## Running it

### Offline tests — no network, no Fabric

```bash
python scripts/run_tests.py
```

**203 assertions.** Runs the **real** silver and gold SQL through DuckDB against
fixtures, plus 45 of the data-quality expectations executed for real. The files
under test are the ones that ship.

### Regenerate the notebooks and the report

```bash
python scripts/make_notebooks.py     # .py -> .ipynb, every cell compiled
python scripts/make_report.py        # page spec -> PBIR, every field checked
```

Both are **generated, never hand-edited**. A hand-edited notebook is overwritten
by the next deploy and the change is lost silently.

`make_report.py` validates every field reference against `powerbi/model-schema.json`
before writing. This matters more than it sounds: a mistyped measure name does
not fail at publish time — Power BI renders the visual **empty**, which is
indistinguishable from "no data matched the filter".

### Deploy to Fabric

```bash
python scripts/deploy_files.py --apply    # library, SQL, configs -> Files/
python scripts/deploy_report.py --apply   # PBIR -> the published report
python scripts/make_data_dictionary.py    # live model -> schema + data dictionary
```

Both dry-run without `--apply`. `deploy_files.py` is the command that makes
"what is in Fabric" equal "what is in the repo" — the notebooks are thin, and
run whatever SQL was last uploaded.

Auth is the Azure CLI (`az login`); neither script reads or prints a secret.

---

## Status

**The full medallion runs end to end in Fabric on live sandbox data.**

| Stage | Result |
|---|---|
| `dl_05_land_to_bronze` | JSONL from all three sources landed to bronze |
| `dl_10_bronze_to_silver` | silver tables built, including the crosswalk |
| `dl_30_build_gold` | 7 dimensions, 8 facts, 3 metadata tables |
| `dl_40_dq_checks` | **53 expectations, 0 blocking failures, 3 warnings** |

The three warnings are real conditions in the data, not defects: unattributed
QuickBooks cost (45 rows), unattributed labour hours (5), and overdue payables (4).

**What the report shows, read back out of the published model**

| Measure | Value |
|---|---|
| Revised contract | $1,099,999 |
| Gross profit at completion | $99 — flagged *Watch — thin margin* |
| Backlog | $1,099,999 |
| AR outstanding | $5,281.52 across 20 invoices; $1,525.50 overdue (28.9%) |
| AP outstanding | $1,602.67 across 5 bills, 4 overdue |
| Net working capital | $3,678.85 |
| Weighted pipeline | $0 — correctly zero, the HubSpot portal has no deals |
| Utilisation | 53.3% (10 billable hours of 18.75) |

Sandbox figures. The point is that every one is derived rather than entered, and
every identity holds.

**Source connectivity**

| Source | State |
|---|---|
| **Procore** | connected — 374 rows across 24 endpoints, including the budget-view detail rows that carry EAC |
| **QuickBooks** | connected — chart of accounts, customers and jobs, invoices, bills, purchases, the general ledger, open AR/AP and time activities |
| **HubSpot** | connected — 1 deal pipeline, 7 stages with win probabilities. No deals in the portal yet, so the forecast is wired and reads zero |

### How source data actually reaches bronze — read this

There is **no Key Vault wired to the workspace**, so `get_secret()` raises
inside a notebook and the three extractor notebooks
(`dl_01_extract_procore`, `dl_02_extract_qbo`, `dl_03_extract_hubspot`) **cannot
run in Fabric today**. None of them has ever completed a run there.

That is a deliberate, documented state rather than a defect. Data reaches bronze
through the **landing split**: extraction runs locally where the secret already
lives, and a credential-free notebook loads the result.

```bash
python scripts/extract_local.py --source procore   # -> Files/_landing/<batch>/*.jsonl
```

then `dl_05_land_to_bronze` in Fabric. Everything downstream of bronze — silver,
gold, the gate, the model, the report — is fully automated.

**To close it:** provision a Key Vault, set `DATALINK_KEYVAULT_URL` on the
workspace, and grant the workspace identity read on the secrets. The QuickBooks
refresh token additionally needs write, because it rotates. See
`docs/06-security-findings.md`.

---

## Defects the live data exposed

None of these were reachable by offline testing, and each one produced a number
that looked plausible.

1. **Budget columns named with spaces.** Procore returns `Job to Date Costs`, `Revised Budget`, `Estimated Cost at Completion`. `$.dot` notation cannot parse a name with spaces — it returns NULL, COALESCEs to 0, and yields a WIP schedule where every project shows zero cost, 0% complete and 100% margin. It satisfies every accounting identity and is entirely wrong. Now bracket notation, pinned by `tests/test_silver_keys.py` against a captured payload.

2. **Hours already billed did not count as billable.** QuickBooks records a time entry as `Billable`, `NotBillable` or `HasBeenBilled`. The test matched `LIKE 'BILLABLE%'`, catching the first and silently missing the third — hours already invoiced to a client, the most billable state there is. Utilisation read 26.7% against a true 53.3%. Now matched against the real three-value enum.

3. **Ageing buckets sorted alphabetically.** The chart rendered `1-30, 31-60, 61-90, Current` — `Current` last. A sort by a column the visual does not display is silently ignored. Fixed with `sortByColumn` in the model, and `make_report.py` now refuses to emit a visual that sorts by a field it does not project.

4. **Direct Lake bound six tables to names that did not exist.** Delta directories in OneLake are lowercase; the table-add API passes the given name through verbatim, so `fct_Aging` bound to a path that was not there. The refresh failed outright rather than silently, which is the one mercy here.

5. **One project could take down an entire feed.** Procore answers 403/404 for a project without a given tool enabled, which is normal. That was aborting the whole endpoint and losing every project that *did* have data. Now counted and skipped.

6. **The quality page reported the previous run.** The table feeding it was built one step too early, so it always showed the *last* run's results. Found by querying the live report rather than trusting the pipeline's green tick.

Also measured: Procore's real quota on this tenant is **25 requests per ~10s
window**, not the 600/hour that is widely quoted. The rate-limit reserve now
scales to whatever the API reports.

---

## Operational hazards worth knowing

1. **The QuickBooks refresh token rotates on every use** and hard-expires at 100 days. `dl_02_extract_qbo` persists the new one *before* pulling any data. If that write is ever skipped, the integration works until the access token expires and then fails permanently — an hour after whoever changed it stopped watching. Two expectations watch the token's age (warn at 60 days, block at 85).

2. **Procore does not send `Retry-After` on a 429** — it sends `X-Rate-Limit-Reset`, a Unix epoch. The session gates on the remaining-quota header before spending a request it does not have, and raises `QuotaExhausted` rather than hanging.

3. **`Procore-Company-Id` must be sent on every API version.** Without it, v1.0 project-scoped endpoints return **404**, not 403 — which reads as "this project has no such tool" and looks for hours like a permissions problem.

4. **Procore returns a different column set per budget view.** The registry pins one view by name. Confirm it before the first production run.

5. **A type mismatch makes Direct Lake drop a table silently.** It appears as a missing table, not an error. `dl_30_build_gold` writes the gold schema to `Files/_diag/gold_schema.json` so the model can be generated from what actually exists.

6. **The SQL endpoint lags the lakehouse.** It has reported zero rows for populated tables. Spark is the authority; see the runbook for the trust order.

7. **Spark capacity contention returns HTTP 430**, not a queue. A notebook submitted while another session is still releasing fails immediately with `TooManyRequestsForCapacity`. Retry; it is not a defect.

---

## Known gaps

- **The custom theme does not bind through the REST publish path.** The report renders on the Fabric base theme — a legible, accessibility-tuned palette — and `powerbi/theme.json` applies when the PBIP is opened in Desktop. Cosmetic only: no status is encoded in colour alone anywhere, so the accessibility guarantees hold regardless.
- **Slicers are not synced across pages.** PBIR's sync-group schema could not be verified against a working report, and inventing one produces slicers that look synced and are not. Each page filters correctly on its own; enabling cross-page sync is one setting in Desktop.
- **Labour cost is zero** wherever QuickBooks carries no cost rate, which is everywhere in the sandbox. Labour margin is overstated by exactly that amount until real cost rates exist.
- **The cash forecast excludes unbilled backlog** — deliberately. Turning backlog into expected cash needs a billing schedule and collection assumptions nobody has provided.

---

## Documentation

| File | Contents |
|---|---|
| `docs/00-client-summary.md` | What was built and what it replaces, in business terms |
| `docs/01-architecture.md` | The platform in detail |
| `docs/02-source-mapping.md` | Every model field → system → verified endpoint |
| `docs/03-data-dictionary.md` | Every table, column and measure — **generated from the deployed model** |
| `docs/06-security-findings.md` | What was found, what was done, what is still open |
| `resources/*/endpoints-cheatsheet.md` | Per-source API facts verified against the tenant, not the public docs |
| `docs/04-wip-methodology.md` | Every financial definition, the worked example, and the decisions that are easy to get wrong |
| `docs/05-runbook.md` | Daily operation, failure modes, re-authorisation |
| `powerbi/report-spec.md` | The 11 pages and why each visual is the form it is |
| `powerbi/measures.dax`, `measures-phase2.dax` | The DAX library |
| `platform/naming-standards.md` | Naming conventions |
