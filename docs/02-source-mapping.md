# Source mapping

Every model field traced to the system and endpoint it comes from. This is the
artefact that makes the endpoint registry non-speculative: nothing is pulled
because it sounded useful, and nothing on the report is unsourced.

Verified against the local API mirrors in `api_docs/`:
Procore OpenAPI (2,131 paths), QuickBooks Online (74 entities),
HubSpot (2,168 endpoints).

---

## Procore — the WIP spine

| Model field | Endpoint | Source field | Notes |
|---|---|---|---|
| `dim_Project[ProjectKey]` | `/rest/v1.0/companies/{company_id}/projects` | `id` | Also the join key on every fact |
| `dim_Project[ProjectNumber]` | same | `project_number` | The crosswalk's exact-match candidate |
| `dim_Project[ProjectName]` | same | `name` | |
| `dim_Project[ProjectStatus]` | same | `status_name` | |
| `dim_Project[Office]` / `[Region]` | same | `office.name`, `project_region.name` | |
| `dim_Project[OriginalContract]` | `/rest/v1.0/prime_contracts` | `original_contract_amount`, falling back to `grand_total` | Summed per project — one project can hold several contracts |
| `dim_Project[RetainagePercent]` | same | `retainage_percent` | Carried but **not** currently deducted from billings — open item |
| `fct_ChangeOrder[Amount]` | `/rest/v1.0/projects/{id}/prime_change_orders` | `grand_total` or `amount` | |
| `fct_ChangeOrder[IsApproved]` | same | `executed`, or `approved_date` present | Derived from data, **not** from status text — status vocabularies vary by tenant configuration |
| `fct_Billing[BilledAmount]` | `/rest/v1.0/prime_contracts/{id}/payment_applications` | `total_claimed_amount` | Owner billings; the "billed to date" in the WIP schedule |
| `fct_BudgetLine[OriginalBudget]` | `/rest/v1.0/budget_views/{id}/detail_rows` | `original_budget_amount` | ⚠ see below |
| `fct_BudgetLine[JobToDateCost]` | same | `job_to_date_costs` | ⚠ |
| `fct_BudgetLine[EstimatedCostAtCompletion]` | same | `estimated_cost_at_completion` / `budget_forecast.amount` | ⚠ **The EAC.** |
| `fct_BudgetLine[CommittedCost]` | same | `committed_costs` | ⚠ |
| `dim_CostCode[CostCode]` | `/rest/v1.0/cost_codes` | `code`, `name` | Project-scoped via query string |
| `fct_CostTransaction` (Procore arm) | `/rest/v1.1/projects/{id}/direct_costs` | `amount`, `direct_cost_date` | |
| `dim_Vendor` (Procore arm) | `/rest/v1.0/companies/{id}/vendors` | `id`, `name` | |

> ⚠ **Budget detail row columns are tenant-specific.** Procore returns the
> structural fields on every tenant plus **one key per column configured on the
> budget view** — and those keys are named by the tenant's own configuration, so
> they cannot be known from the API specification alone.
>
> `dl_silver_budget_lines` matches Procore's standard column ids through a
> COALESCE chain; anything unmatched lands NULL and the raw payload is still
> intact, so pinning the real keys is a re-run of one SQL file, not a re-extract.
>
> `dl_bronze_procore_budget_detail_columns` lists the actual keys for the pinned
> view after the first run. **Pin them before go-live.**

### Not pulled, and why

| Endpoint | Reason |
|---|---|
| RFIs, submittals, daily logs, punch | Operational, not financial. Phase 1 is WIP and project P&L. |
| Drawings, photos, documents | No reporting value; large payloads against a 600/hour quota. |
| Timecards / manpower | Not pulled. Capacity currently rests on QuickBooks `TimeActivity`; Procore timecards would deepen it considerably. |
| `/budget_line_items` (v1.0, v1.1) | POST only — there is no GET. The read path is the budget view. |

---

## QuickBooks Online — the financial truth

| Model field | Entity | Source field | Notes |
|---|---|---|---|
| `dim_Project[QboCustomerId]` | `Customer` | `Id` where `Job=true` or `IsProject=true` | Via the crosswalk. A **top-level customer is a client, not a job** — matching one to a project would be wrong |
| `dim_Project[QboJobName]` | `Customer` | `FullyQualifiedName` | `Customer:Job:Sub-job`; what a human recognises in the review queue |
| `fct_CostTransaction` (QBO arm) | `Bill`, `Purchase`, `VendorCredit`, `JournalEntry` | `Line[].Amount` | **Line grain, not header.** Job cost lives on the line via `CustomerRef`; a bill can span four jobs, and summing headers attributes the whole bill to whichever job is named first |
| `fct_CostTransaction[ProjectKey]` | same | `Line[].AccountBasedExpenseLineDetail.CustomerRef.value` (or `ItemBased…`, or `JournalEntryLineDetail.Entity.EntityRef.value`) | Three different paths for the same concept |
| `fct_CostTransaction[Amount]` | same | signed | Bills and journal debits increase cost; vendor credits and `Credit=true` purchases reduce it. Without the sign convention a correction makes cost go **up** |
| `dim_Account[IsJobCostAccount]` | `Account` | `AccountType` in COGS / Expense / Other Expense | Bounds the GL tie-out. Including every account would compare job cost against the whole trial balance |
| `fct_Billing` (QBO arm) | `Invoice` | `TotalAmt`, `Balance` | The **check** on Procore billings, never summed with them |
| `dl_silver_qbo_ar_open_items` | `Invoice` | `Balance > 0` | Aging buckets derived from `DueDate`; QBO has no single invoice status field |
| Tie-out | `ProfitAndLossDetail` report | nested Rows/ColData | Stored whole, flattened in silver where the shape is visible in SQL |

Accrual basis throughout. Cash basis would understate both cost and revenue and
the WIP schedule would not tie to the financial statements.

---

## HubSpot — pipeline

| Model field | Object | Source property |
|---|---|---|
| `fct_Pipeline[Amount]` | `deals` | `amount` |
| `fct_Pipeline[CloseDate]` | `deals` | `closedate` |
| `fct_Pipeline[Probability]` | `/crm/pipelines/2026-03/deals` | stage `probability` |
| `dim_DealStage` | pipelines | stage id, label, display order |
| `dim_Owner` | `/crm/owners/2026-03` | `id`, name, email |
| `dim_Project[HubspotDealId]` | crosswalk | manual only — there is no reliable automatic signal from a deal to a Procore project |

**Win probability lives on the stage definition, not the deal.** Weighted
pipeline forecasting is impossible without pulling the pipelines endpoint, which
is easy to miss because the deal object looks complete on its own.

---

## The three-way join

There is no shared key. The crosswalk resolves it, in strict precedence:

```
manual (Controller CSV)  ->  always wins
exact  (project number)  ->  normalised, minimum 3 characters
fuzzy  (name)            ->  only when unambiguous, above the confidence floor
```

`dim_Project` is the **union of the crosswalk and every project id observed on a
fact**, so a missing mapping produces a visible row on the Unmapped Projects page
rather than a quietly smaller total.

The minimum-3-character rule on exact matching exists because a project numbered
`"1"` would otherwise match every QuickBooks job whose name contains a 1 — which
is most of them.

---

## Manual inputs

Nothing built so far requires manual entry beyond the crosswalk overrides
(`Files/reference/project_crosswalk.csv`).

Two things would need it if asked for later: annual revenue and margin targets
for variance reporting, and crew capacity by month. Both would land as `man_*`
tables through the same reference-file path.
