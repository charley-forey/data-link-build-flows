# Semantic model — Data Link Financial Operating System

Direct Lake over `DL_Lakehouse`, star schema, one date table.

## Design principles

**1. One `dim_Date`, marked as the date table.** This single object removes
every date-lookup mechanic from the spreadsheet it replaces and makes time
intelligence — prior period, YTD, fade/gain — one DAX function instead of a
hand-built column per comparison. Auto date/time is **off**.

**2. Star, not snowflake.** Facts join dimensions directly. No dimension joins
another.

**3. All relationships many-to-one, single cross-filter direction.** A
bidirectional filter on a shared dimension produces ambiguous filter paths the
moment a second fact joins it — and every fact here joins `dim_Project`.

**4. Everything numeric stays numeric.** No `TEXT(...) & " / " & TEXT(...)`
tiles, no `"NA"` sentinels. Formatting is a visual concern; a measure returning
text can no longer be summed, compared or conditionally formatted.

**5. `discourageImplicitMeasures`.** Every number on the report comes from a
named measure that is defined once and can be traced to
`docs/04-wip-methodology.md`.

**6. Multi-project from day one.** Every fact carries `ProjectKey`, so
leadership sees a portfolio rather than one job at a time.

**7. The model says when it was last correct.** `meta_PipelineRun` and
`meta_DataQuality` are in the model on purpose — see the liveness measures.

---

## Tables

### Dimensions

| Table | Grain | Key | Notes |
|---|---|---|---|
| `dim_Date` | one day, 2015–2035 | `Date` | Marked as date table. `MonthYearSort` orders text months correctly; `MonthOffset` (0 = current month) makes "last 12 months" a numeric filter that survives export. |
| `dim_Project` | one project | `ProjectKey` | The conformed project. Union of the crosswalk and every observed project id — `IsInCrosswalk` and `IsInProcore` record the difference. Carries `ProcoreProjectId`, `QboCustomerId`, `HubspotDealId` as **attributes**, never join keys. |
| `dim_CostCode` | project × cost code | `CostCodeKey` | Project-scoped: two projects can both have a "16-100" meaning different things, so the key is `project|code`. Key `0` = Unassigned. |
| `dim_Vendor` | one vendor | `VendorKey` | Conformed across Procore and QuickBooks on a normalised name. `SourceSystem` records `procore` / `quickbooks` / `both`. Key `0` = Unassigned. |
| `dim_Account` | one GL account | `AccountKey` | QuickBooks chart of accounts. `IsJobCostAccount` flags COGS and expense types for the GL tie-out. Key `0` = Unassigned. |

Every dimension carries a **key-0 "Unassigned" row**. A fact whose dimension
value cannot be resolved joins to it rather than to nothing — a visible
"Unassigned" bar prompts a question, a silently missing one does not.

### Facts

| Table | Grain | Notes |
|---|---|---|
| `fct_WIP` | project × month | **The deliverable.** The WIP schedule. |
| `fct_BudgetLine` | project × cost code | The detail behind every WIP number — what a PM drills into when they disagree with the roll-up. |
| `fct_ChangeOrder` | one change order | Cumulative roll-up happens in DAX, never in the grain. |
| `fct_CostTransaction` | one cost line | Both sources, tagged by `SourceSystem`. **Never sum across it** — the same invoice is usually in both systems. |
| `fct_Billing` | one billing | Procore payment applications, plus QuickBooks invoices as a check. |

### Metadata

| Table | Purpose |
|---|---|
| `meta_PipelineRun` | Drives `Pipeline Status`. |
| `meta_DataQuality` | The Data Quality report page. |
| `meta_UnmappedProjects` | The Controller's crosswalk to-do list. |

---

## Relationships

All many-to-one, single direction, from fact to dimension.

```
fct_WIP[ProjectKey]              -> dim_Project[ProjectKey]
fct_WIP[MonthStart]              -> dim_Date[Date]
fct_BudgetLine[ProjectKey]       -> dim_Project[ProjectKey]
fct_BudgetLine[CostCodeKey]      -> dim_CostCode[CostCodeKey]
fct_ChangeOrder[ProjectKey]      -> dim_Project[ProjectKey]
fct_ChangeOrder[MonthStart]      -> dim_Date[Date]
fct_CostTransaction[ProjectKey]  -> dim_Project[ProjectKey]
fct_CostTransaction[MonthStart]  -> dim_Date[Date]
fct_CostTransaction[VendorKey]   -> dim_Vendor[VendorKey]
fct_CostTransaction[AccountKey]  -> dim_Account[AccountKey]
fct_Billing[ProjectKey]          -> dim_Project[ProjectKey]
fct_Billing[MonthStart]          -> dim_Date[Date]
meta_UnmappedProjects[ProjectKey]-> dim_Project[ProjectKey]
```

`MonthStart` is set **only** when the date falls inside `dim_Date`'s range.
An unmatched date key raises nothing in a semantic model — it makes every
date-filtered measure come back **blank**, which reads as "no activity this
period" rather than "this row has a broken date". Out-of-range rows are flagged
on the fact (`HasOutOfRangeDate`) and checked by the DQ suite.

---

## Measures

Defined in `powerbi/measures.dax`, all in a `_Measures` table, grouped by
display folder:

| Folder | Contents |
|---|---|
| `01 Contract & Change` | Original / Approved COs / Pending COs / Revised Contract, Contract Growth % |
| `02 Budget & Cost` | Budgets, Committed, Cost to Date, EAC, Cost to Complete, Percent Complete |
| `03 Revenue & Margin` | Earned Revenue, GP @ Completion, GP%, Earned GP, **Fade/Gain** |
| `04 Billing & Backlog` | Billed to Date, Over/Under Billing, Backlog, Backlog Months |
| `05 Reconciliation` | QuickBooks cost, Cost Variance, Projects Unmapped, Unmapped Contract Value |
| `06 Risk & Exceptions` | Projects at Risk, Projects Over EAC, Cost Codes Over EAC, Project Risk Label |
| `07 Pipeline liveness` | Last Successful Run, Pipeline Status, Blocking DQ Failures |

---

## Deployment note

**Generate the TMDL from the live gold schema; do not hand-write it.** A
declared type that disagrees with the Delta table makes Direct Lake drop the
table **silently** — it appears as a missing table, not an error.
`dl_30_build_gold` writes the real schema to `Files/_diag/gold_schema.json` for
exactly this purpose.

Validate after deploying by running a handful of measures through `execute_dax_query`
and tying the totals back to the same aggregation in SQL. A table that failed to
load shows up there as blank, not as an error — so a passing DAX check is the
only proof the model actually bound.
