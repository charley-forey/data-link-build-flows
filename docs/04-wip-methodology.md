# WIP / EAC methodology

This is the authoritative definition of every financial number the platform
produces. It is implemented once, in
`transformation/sql/gold/30_fct_wip.sql`, surfaced through
`powerbi/measures.dax`, and asserted on fixtures by `tests/test_gold.py`.

If this document and the SQL ever disagree, the SQL is wrong — the test suite
encodes what is written here.

---

## Method: cost-to-cost percentage of completion

The standard for a commercial contractor, and the method the Controller already
uses in the spreadsheet this replaces. Revenue is recognised in proportion to
cost incurred against total forecast cost.

```
Revised Contract    = Original Prime Contract + Approved Prime Change Orders
EAC                 = Procore Budget View "Estimated Cost at Completion"
Cost to Date (CTD)  = Procore Job-to-Date Cost
Percent Complete    = CTD / EAC
Earned Revenue      = Revised Contract x Percent Complete
Billed to Date      = Prime contract payment applications
Over-billing        = MAX(Billed - Earned, 0)
Under-billing       = MAX(Earned - Billed, 0)
Gross Profit @ Comp = Revised Contract - EAC
GP% @ Completion    = Gross Profit @ Completion / Revised Contract
Earned GP to Date   = Earned Revenue - CTD
Cost to Complete    = EAC - CTD
Backlog             = Revised Contract - Earned Revenue
Fade / Gain         = GP% @ Completion (this period) - (prior period)
```

### Worked example

A $100,000 contract with a $20,000 approved change order, a $90,000 EAC,
$45,000 of cost to date and $70,000 billed:

| Quantity | Value | Derivation |
|---|---:|---|
| Revised Contract | $120,000 | 100,000 + 20,000 |
| EAC | $90,000 | from the budget view |
| Cost to Date | $45,000 | job-to-date cost |
| Percent Complete | 50.0% | 45,000 / 90,000 |
| Earned Revenue | $60,000 | 120,000 x 0.50 |
| Cost to Complete | $45,000 | 90,000 − 45,000 |
| Gross Profit @ Completion | $30,000 | 120,000 − 90,000 |
| GP% @ Completion | 25.0% | 30,000 / 120,000 |
| Earned Gross Profit | $15,000 | 60,000 − 45,000 |
| Billed to Date | $70,000 | payment applications |
| **Over-billing** | **$10,000** | 70,000 − 60,000 |
| Under-billing | $0 | earned does not exceed billed |
| Backlog | $60,000 | 120,000 − 60,000 |

This exact case is asserted in `tests/test_gold.py` as project `P1`.

---

## The decisions that are easy to get wrong

### 1. Change orders roll up cumulatively, never per month

Every approved change order up to and including the reporting period counts
toward Revised Contract — not just the ones approved *in* that period.

A per-month roll-up is the defect that **understated portfolio contract value by
16% ($4.85M)** on a comparable engagement. It is dangerous precisely because it
looks right: in a month with new change orders the number is plausible, and in a
quiet month the contract value silently reverts toward its original value.

The fact grain is one row per change order; the cumulative sum happens in
`fct_WIP` and in DAX, and both are covered by tests.

### 2. Pending change orders are excluded

Only approved change orders reach Revised Contract. Unapproved work is exposure,
not revenue. Pending value is carried as its own column (`PendingChangeOrders`)
so it is visible without contaminating the schedule.

Approval is derived from the data — `is_executed`, or the presence of an
approval date — rather than from status *text*, because status vocabularies vary
by Procore configuration. One tenant's "Approved" is another's "Executed".

### 3. Percent complete is capped at 100% for revenue, uncapped for reporting

Cost can and does exceed EAC. Recognising more than 100% of the contract cannot
happen, so `PercentComplete` caps at 1.0.

But clamping silently would hide the over-run, so `PercentCompleteRaw` keeps the
true value and drives the "Projects Over EAC" exception. A job at 107% cost
recognises 100% of revenue *and* appears on the exceptions page.

### 4. EAC falls back to revised budget when the forecast is zero

A budget view that has never been forecast returns EAC = 0. Taken literally that
makes percent complete infinite and the job appears complete having spent
nothing.

The floor is `GREATEST(EAC, RevisedBudget)` — the conservative reading, "we
expect to spend what we budgeted". A project manager who has not forecast yet
gets a defensible number rather than a nonsensical one.

### 5. Over- and under-billing are two columns, not one signed number

Billings in excess of costs is a **liability**; costs in excess of billings is
an **asset**. Netting them to a single signed value destroys the distinction the
Controller needs to produce a balance sheet, so both are non-negative columns
and a project can never be in both (asserted as a blocking expectation).

### 6. Procore is the reporting EAC; QuickBooks is the check

Procore's budget view is what the project managers maintain and what they will
defend in a meeting. QuickBooks is what the financial statements say.

**They will not agree, and that is information** — an AP invoice not yet
entered, a cost coded to the wrong job, an accrual Procore does not know about.
So both are carried (`CostToDate`, `CostToDateQbo`) and the difference is a
first-class column (`CostVariance`) rather than something reconciled away.

The variance check is a **warning**, never blocking. Stopping the pipeline over
it would withhold the very reporting needed to investigate it.

### 7. Portfolio percent complete divides totals; it does not average percentages

`DIVIDE([Cost to Date], [EAC])`, not `AVERAGE` of the per-project values.
Averaging weights a $40k job the same as a $4M one and produces a portfolio
number that is wrong in a way that looks entirely plausible.

Earned Revenue is a `SUMX` over projects for the same reason: each project earns
at its own percent complete, and multiplying portfolio contract by portfolio
percent complete is a different number whenever projects differ.

---

## Projects included on the schedule

A project appears in `fct_WIP` when it has a non-zero revised contract **or** a
non-zero revised budget. Prospective jobs carrying neither would otherwise put a
row of zeros on the schedule for every opportunity in Procore.

Projects observed only on a fact — a budget line for a job not yet in the
project list — still reach `dim_Project`, flagged `IsInProcore = FALSE`. Their
cost is real and dropping it would understate the portfolio.

---

## Open items requiring the Controller

These are the assumptions that need confirming before the first production close.
They change parameters, not design.

1. **Which Procore budget view is the standard one.** It must be pinned by name
   in `ingestion/procore/config/endpoints.yml`, because Procore returns a
   different column set per view. Currently set to
   `"Data Link Standard Budget View"` — a placeholder.
2. **The budget-view column keys.** `dl_silver_budget_lines` matches Procore's
   standard column ids through a COALESCE chain. The tenant's actual keys are
   listed by `dl_bronze_procore_budget_detail_columns` after the first run and
   should be pinned exactly.
3. **Materiality thresholds** for the Procore↔QuickBooks variance check,
   currently $5,000 and 10% in `transformation/dq/expectations.py`.
4. **Retainage treatment.** Retainage percent is carried on `dim_Project` but is
   not currently deducted from Billed to Date. Confirm whether the WIP schedule
   should show gross or net of retention.
5. **Whether Procore financial periods are used for month-end lock.** If so,
   `fct_WIP` should key on `financial_period_id` rather than calendar month, and
   the source becomes `/project_status_snapshots`.
