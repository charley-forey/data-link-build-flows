# Architecture

## The problem

Three systems of record, no shared key, and a Controller assembling the month-end
reporting by hand:

- **Procore** — projects, budgets, prime contracts, commitments, change orders, direct costs, subcontractor invoices
- **QuickBooks Online** — general ledger, AR/AP, cash, actual cost
- **HubSpot** — CRM pipeline and sales forecast

The requirement is not another application. It is to make the three systems they
already own feed one governed source of truth that produces the weekly and
monthly reporting automatically.

## Shape

```
Procore REST ─┐
QBO REST      ├─► one config-driven extractor per source
HubSpot REST ─┘        │
                       └─► dl_bronze_*   raw payload, unparsed, + audit columns
                                  │
                            dl_silver_*  typed, TRIMmed, validated, rejects logged
                                  │      + dl_silver_project_crosswalk
                            dim_/fct_    star schema
                                  │
                          DQ gate ─┤     blocking failure stops the run
                                  │
                    Semantic model (Direct Lake) ─► Power BI report
```

One lakehouse (`DL_Lakehouse`), layered by table prefix rather than by separate
lakehouses. Three lakehouses would force every cross-layer read through explicit
ABFSS paths for no benefit at this data volume — the prefixes already provide
the separation, and one catalog means unqualified table names resolve everywhere.

## The decisions that shaped it

**Bronze stores the unparsed payload.** Bronze physically cannot drop a column it
never parsed, so a transform bug is a re-run rather than a re-extract. That
matters more than usual here: Procore allows 600 requests per hour, so
re-extracting is genuinely expensive. It also insulates against HubSpot custom
properties, which change without notice.

**One extractor per source, endpoints in YAML.** Adding an endpoint is a config
entry. The alternative — one notebook per entity — produces thirty
near-identical notebooks that each drift in their own direction, and the drift
is invisible until one of them is quietly wrong.

**MERGE on a natural key, never DROP + append.** Re-running is a no-op. That is
what makes the deliberate one-hour watermark overlap safe, and it is why a failed
run resumes rather than restarting.

**Watermarks advance only on success, and reads overlap backwards by an hour.**
Writing the watermark before the load means a crash mid-run silently skips those
rows forever — and nothing ever reports it, because the next run dutifully starts
after the rows it never loaded.

**Transforms are ordered `.sql` files, not dataflows.** Diffable, reviewable in a
pull request, and testable offline in DuckDB. A Power Query step is none of those.

**Gold is rebuilt in full every run.** It is small, and recomputing it is simpler
and self-healing compared with incremental merge logic nobody can debug later.

**The `sv_*` source-view layer sits between silver and gold.** Every gold file
reads `sv_*` and nothing else. That makes gold portable across Spark and DuckDB
(so the real SQL is tested offline), and it makes changing where silver lives a
one-file change. It also let HubSpot be declared as an empty view **with real
types** long before the data existed, so everything downstream compiled and was
tested against it — and going live was a change to that one view, exactly as
intended.

## Where correctness is enforced

| Layer | Mechanism |
|---|---|
| Offline | `scripts/run_tests.py` runs the real silver and gold SQL in DuckDB: 203 assertions covering the WIP identities and the phase 2 facts, plus 45 data-quality expectations executed for real |
| Generation | `scripts/make_notebooks.py` compiles every notebook cell before writing it |
| Ingestion | MERGE keys make re-runs idempotent; run twice, row counts must not move |
| Silver | TRIM everything, floor sentinel dates, reject loudly to `dl_dq_rejects` |
| Gold | `dim_Project` is the union of the crosswalk and every observed project id — referential integrity holds by construction |
| Publish | 53 expectations (35 blocking, 18 warning); blocking failures raise and stop the pipeline |
| Model | Liveness measures state on the report's own face when it was last correct |
| Report | `scripts/make_report.py` validates every projection against the live model schema, and rejects a visual that sorts by a field it does not display — Power BI ignores that sort silently |

## What is deliberately not built

**Real-time.** The reporting cycle is weekly and monthly. A nightly batch matches
it, and streaming would add operational surface for no decision-making benefit.

**Type 2 history on dimensions.** Latest-snapshot-wins is what "the budget as of
the latest pull" means. `fct_WIP` snapshots monthly, which is where history is
actually needed.

**Write-back to source systems.** Read-only in both directions. The platform
reports; it does not become a second place where a number can be edited.

**A separate landing-zone lakehouse.** Raw JSONL lands in `Files/_landing/` when
the local-extraction split is needed; otherwise the payload column is the replay
mechanism.
