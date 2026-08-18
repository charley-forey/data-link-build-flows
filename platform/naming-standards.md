# Naming standards

One convention, applied everywhere. The value is not the specific choices — it
is that SQL, the semantic model and the DAX all agree without anyone having to
check.

## Table prefixes

| Prefix | Layer | Example |
|---|---|---|
| `dl_bronze_` | Raw payload plus audit columns | `dl_bronze_procore_prime_contracts` |
| `dl_silver_` | Typed, trimmed, validated | `dl_silver_prime_contracts` |
| `dim_` | Gold dimension | `dim_Project` |
| `fct_` | Gold fact | `fct_WIP` |
| `meta_` | Gold, pipeline metadata surfaced into the model | `meta_PipelineRun` |
| `man_` | Gold, manually sourced | `man_Targets` |
| `dl_meta_` | Pipeline state, not modelled | `dl_meta_watermark` |
| `dl_dq_` | Data quality output | `dl_dq_results` |

Bronze tables are named `dl_bronze_<source>_<entity>`. The source segment is
what makes `dl_bronze_procore_vendors` and `dl_bronze_qbo_vendors` distinguishable
at a glance — they are different things and must never be silently unioned.

## Column case

**Bronze and silver are `snake_case`. Gold is `PascalCase`.**

The change at the boundary is deliberate: it makes it immediately obvious
whether you are looking at source-shaped data or model-shaped data. A query
mixing `project_id` and `ProjectKey` is visibly reaching across a layer.

## Keys

| Suffix | Meaning | Rule |
|---|---|---|
| `*Key` | Surrogate or conformed key | The only thing facts join on |
| `*Id` | The source system's own identifier | Carried as an attribute, **never joined across systems** |
| `*Number` | Human-readable reference | For display and for humans to search on |

`ProcoreProjectId`, `QboCustomerId` and `HubspotDealId` all live on `dim_Project`
as attributes. Facts join on `ProjectKey` only. This is what stops someone
"helpfully" joining a QuickBooks customer id to a Procore project id because
both happen to be integers.

`ProjectKey` is the Procore project id rather than an invented surrogate:
Procore ids are stable, unique and already on every fact, so a surrogate would
add a lookup hop and buy nothing. It also keeps the key debuggable — a wrong
number in the report can be pasted straight into Procore's URL bar.

## Audit columns

Every bronze table carries, without exception:

```
_key              natural key from the source
_project_id       owning project, NULL for company-scoped records
_merge_key        _key + _project_id, the MERGE predicate
_source_endpoint  which registry entry produced this row
_ingested_at      when we pulled it
_batch_id         which run - makes a bad run reversible
_row_hash         content hash, independent of JSON key order
payload           the UNPARSED source record
```

`_merge_key` exists because a Delta `MERGE` predicate comparing two NULL
`_project_id` values never matches, so company-scoped endpoints would re-insert
their entire table on every run and grow without bound.

## Fabric items

| Item | Pattern | Example |
|---|---|---|
| Lakehouse | `DL_<Name>_Lakehouse` | `DL_Lakehouse` |
| Notebook | `dl_<nn>_<verb>_<subject>` | `dl_30_build_gold` |
| Pipeline | `DL_<Purpose>_Pipeline` | `DL_Master_Pipeline` |
| Semantic model / Report | Business name, no prefix | `Data Link Financial Operating System` |

The two-digit notebook prefix is the run order, and it is the only place run
order is expressed. `00` bootstrap, `01`–`09` extraction, `10`–`19` silver,
`20`–`39` gold, `40+` quality.

## SQL files

`transformation/sql/<layer>/<nn>_<subject>.sql`, executed in **filename order**.
`00` seeds and source views, `10`–`19` dimensions, `20`–`39` facts, `40+`
metadata.

Ordering lives in the numeric prefix. Logic lives in version-controlled SQL
rather than a dataflow, so a transform can be reviewed in a pull request —
a Power Query step is neither diffable nor testable offline.

## Two non-negotiables

**`TRIM()` every text value on the way in.** Untrimmed source text never matches
in a join, and the symptom is a silently smaller number rather than an error.

**Reject loudly, never drop silently.** A row that fails validation goes to
`dl_dq_rejects` with a reason. It does not disappear.
