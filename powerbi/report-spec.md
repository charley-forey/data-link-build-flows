# Report spec — Financial Operating System

Seven pages for phase 1. Every page carries the same two synced slicers
(**Project**, **Month**) and a footer naming the reporting period and the
`Pipeline Status` measure.

Canvas 1280×720, `FitToPage`. Every visual carries `altText`.

---

## Non-negotiables

**Red/amber/green cannot be made colorblind-safe as colour alone.** Measured
red↔green separation is ΔE 7.1 under deuteranopia — below the ΔE 8 floor.
Therefore **every status indicator ships with an icon or a text label beside the
colour**. The `Project Risk Label` measure exists for this; use it, not
conditional formatting alone.

**Never a dual-axis chart.** Two measures on two y-scales is the single most
common chart mistake — it lets the author imply any relationship they like by
choosing the scales. Two measures of different magnitude become two charts,
small multiples, or a common index.

**Three categorical slots fall below 3:1 on this surface** (aqua, yellow,
magenta). Any visual using them ships **visible data labels or a table view**.
That is a requirement, not a preference.

**Scatter and small-multiple visuals cap at three series.** Those forms compare
all pairs rather than adjacent ones, and the fourth slot puts yellow beside
orange — a pair that fails the separation floors.

**Colour follows the entity, never its rank.** A slicer that changes the series
count must not repaint the survivors.

---

## 1 · Portfolio

Every project at once — the view leadership has never had.

| Visual | Form | Notes |
|---|---|---|
| KPI row | 5 cards | Revised Contract · Earned Revenue · GP% @ Completion · Backlog · Net Over/Under Billing |
| Project table | table | One row per project: contract, EAC, % complete, GP%, backlog, `Project Risk Label`. Sortable, exportable. **This is the table view that provides relief for the low-contrast slots.** |
| Margin by project | bar, sorted | `GP % at Completion`. Direct-labelled; status colour + label, never colour alone. |
| Contract vs earned | bar | Two series, adjacent pairlist, slots 1–2. |

---

## 2 · Executive KPI

The one-page answer for the CEO.

| Visual | Form | Notes |
|---|---|---|
| Hero row | 4 cards | Revenue YTD · GP% · Backlog · Cash-relevant Over/Under Billing |
| Revenue & GP trend | line, 2 series | Monthly. 2px lines, markers ≥8px, direct-label the last point only — never a number on every point. |
| Backlog burn | area | Backlog by month with `Backlog Months` as a card beside it |
| Top 5 risks | table | Projects sorted by `Gross Profit at Completion` ascending, with label |

---

## 3 · WIP Schedule

**The Controller's deliverable.** The table they produce by hand today.

One matrix, one row per project, columns in the order a WIP schedule is
conventionally read:

```
Project · Revised Contract · Original Budget · EAC · Cost to Date ·
% Complete · Earned Revenue · Billed to Date · Over-billing ·
Under-billing · GP @ Completion · GP% @ Completion · Backlog
```

Totals row on. Export to Excel enabled — the Controller will want to tie it out
by hand for the first few closes, and refusing that would guarantee the platform
is not trusted.

Below it: `Cost Variance` per project, so the Procore↔QuickBooks disagreement is
visible on the same page as the numbers it affects.

---

## 4 · Project Financial Performance

Per-project drill-down. Project slicer drives everything.

| Visual | Form | Notes |
|---|---|---|
| Header cards | 4 | Revised Contract · EAC · % Complete · GP% @ Completion |
| Budget vs committed vs actual vs EAC by cost code | grouped bar | Four series — adjacent pairlist, slots 1–4. Data labels on. |
| Cost codes over EAC | table | From `fct_BudgetLine[IsOverEac]`. The earliest visible signal a job is going wrong, and exactly what a project-level roll-up hides. |
| Fade / gain trend | line | `Fade Gain` by month. Zero line emphasised; diverging colour with a **neutral gray midpoint**. |
| Change order log | table | Approved and pending, with `EffectiveDate` |

---

## 5 · Backlog & Burn

| Visual | Form | Notes |
|---|---|---|
| Backlog by month | area | |
| Backlog by project | bar, sorted | |
| Burn curve | line | Earned revenue per month, trailing 3-month average as a second series |
| Months of backlog | card | `Backlog Months` |

---

## 6 · Exceptions

What needs attention, in one place. This is the page that answers the CEO's ask: "identify
exceptions or risks without someone manually digging".

| Visual | Notes |
|---|---|
| Projects at risk | `Gross Profit at Completion` < 0, with `Project Risk Label` |
| Projects over EAC | `PercentCompleteRaw` > 1 — cost has passed the forecast |
| EAC below cost to date | the forecast needs updating by the PM |
| Cost codes over EAC | project × cost code detail |
| Unapproved change orders | pending value by project, with age |
| Large Procore↔QuickBooks variances | over the materiality thresholds |

Every row links through to page 4 filtered to that project.

---

## 7 · Data Quality

Hidden from the page navigator, reachable from the footer.

Surfaces bad data instead of letting it flow silently into a roll-up.

| Visual | Source |
|---|---|
| Gate status | `Blocking DQ Failures` and `DQ Warnings` cards |
| Expectation results | `meta_DataQuality`, sorted by `SeveritySort`, with the `StatusLabel` text column — **not colour alone** |
| Unmapped projects | `meta_UnmappedProjects` — the Controller's crosswalk to-do list, with the proposed QuickBooks job and confidence |
| Freshness | `Last Successful Run`, `Hours Since Last Run`, `Pipeline Status` |

---

## Phase 2 pages

Pipeline & Forecast (HubSpot), Cash Forecast, AR/Collections, Capacity Planning.
All four sit on the same foundation; `sv_deals` is already declared as an empty
typed view so the model compiles today and goes live by changing that one view.

---

## Build note

The Fabric MCP server has no report-creation tool. The semantic model is built
programmatically; this report is authored as a PBIP project in `powerbi/` and
published from Power BI Desktop or the Power BI REST API. Everything it binds to
is created from code, so the report is the only hand-placed artifact — and
`theme.json` carries the palette so the hand-placement cannot drift off-system.
