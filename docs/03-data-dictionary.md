# Data dictionary

**Generated** by `scripts/make_data_dictionary.py` from the deployed
semantic model. Do not edit by hand — re-run the script instead. A data
dictionary maintained separately from the model is wrong within a week and
nothing catches it.

Naming: `*Key` is joined on. `*Id` is the source system's own identifier,
carried as an attribute and **never joined across systems**. Columns are
`PascalCase` in gold and `snake_case` in bronze and silver — the change at
the boundary tells you which shape you are looking at.

## Dimensions

### `dim_Date`

One row per day, 2015-2035. Marked as the date table.

| Column | Type | Sorted by |
|---|---|---|
| `Date` | dateTime |  |
| `Year` | int64 |  |
| `Month` | int64 |  |
| `Quarter` | int64 |  |
| `DayOfMonth` | int64 |  |
| `MonthStart` | dateTime |  |
| `MonthEnd` | dateTime |  |
| `MonthShortName` | string | `Month` |
| `MonthName` | string | `Month` |
| `MonthYear` | string | `MonthYearSort` |
| `MonthYearSort` | int64 |  |
| `QuarterYear` | string | `QuarterYearSort` |
| `QuarterYearSort` | int64 |  |
| `MonthOffset` | int64 |  |
| `IsPast` | boolean |  |
| `IsCurrentMonth` | boolean |  |
| `IsCurrentYear` | boolean |  |

### `dim_DealStage`

One row per HubSpot deal stage. Carries the win probability that weighted forecasting depends on.

| Column | Type | Sorted by |
|---|---|---|
| `DealStageKey` | string |  |
| `StageName` | string | `DisplayOrder` |
| `PipelineName` | string |  |
| `DisplayOrder` | int64 |  |
| `WinProbability` | double |  |
| `IsClosedStage` | boolean |  |
| `StageOutcome` | string |  |

### `dim_Owner`

One row per HubSpot owner.

| Column | Type | Sorted by |
|---|---|---|
| `OwnerKey` | string |  |
| `OwnerName` | string |  |
| `OwnerEmail` | string |  |

### `dim_Project`

One row per project. The union of the crosswalk and every project id observed on a fact.

| Column | Type | Sorted by |
|---|---|---|
| `ProjectKey` | string |  |
| `ProcoreProjectId` | string |  |
| `QboCustomerId` | string |  |
| `HubspotDealId` | string |  |
| `ProjectNumber` | string |  |
| `ProjectName` | string |  |
| `ProjectStatus` | string |  |
| `ProjectStage` | string |  |
| `ProjectType` | string |  |
| `Office` | string |  |
| `Region` | string |  |
| `City` | string |  |
| `StateCode` | string |  |
| `IsActive` | boolean |  |
| `ProjectStart` | dateTime |  |
| `ProjectFinish` | dateTime |  |
| `QboJobName` | string |  |
| `CrosswalkMethod` | string |  |
| `CrosswalkConfidence` | double |  |
| `OriginalContract` | double |  |
| `ContractGrandTotal` | double |  |
| `RetainagePercent` | double |  |
| `ContractStart` | dateTime |  |
| `ContractFinish` | dateTime |  |
| `HasPrimeContract` | boolean |  |
| `HasExecutedContract` | boolean |  |
| `IsInProcore` | boolean |  |
| `IsInCrosswalk` | boolean |  |

## Facts

### `fct_Aging`

One open document, AR and AP in one table discriminated by Ledger. Amounts are positive in both arms.

| Column | Type | Sorted by |
|---|---|---|
| `Ledger` | string |  |
| `AgingKey` | string |  |
| `DocumentId` | string |  |
| `DocNumber` | string |  |
| `CounterpartyId` | string |  |
| `CounterpartyName` | string |  |
| `ProjectKey` | string |  |
| `DocumentDate` | dateTime |  |
| `DueDate` | dateTime |  |
| `TotalAmount` | double |  |
| `OpenBalance` | double |  |
| `DaysPastDue` | int64 |  |
| `AgingBucket` | string | `AgingBucketSort` |
| `AgingBucketSort` | int64 |  |
| `IsOverdue` | boolean |  |
| `MonthStart` | dateTime |  |

### `fct_BudgetLine`

Project x cost code. The detail behind every WIP number.

| Column | Type | Sorted by |
|---|---|---|
| `ProjectKey` | string |  |
| `CostCodeKey` | string |  |
| `BudgetLineKey` | string |  |
| `CostCode` | string |  |
| `Category` | string |  |
| `OriginalBudget` | double |  |
| `ApprovedBudgetChanges` | double |  |
| `RevisedBudget` | double |  |
| `CommittedCost` | double |  |
| `DirectCost` | double |  |
| `JobToDateCost` | double |  |
| `EstimatedCostAtCompletion` | double |  |
| `ForecastToComplete` | double |  |
| `ProjectedOverUnder` | double |  |
| `IsOverEac` | boolean |  |

### `fct_CashForecast`

One week x flow. Committed cash only; excludes unbilled backlog.

| Column | Type | Sorted by |
|---|---|---|
| `WeekStart` | dateTime |  |
| `Flow` | string |  |
| `Amount` | double |  |
| `OverdueAmount` | double |  |
| `DocumentCount` | int64 |  |
| `IsPast` | boolean |  |

### `fct_ChangeOrder`

One change order. Cumulative roll-up happens in DAX, never in the grain.

| Column | Type | Sorted by |
|---|---|---|
| `ProjectKey` | string |  |
| `ChangeOrderKey` | string |  |
| `ContractId` | string |  |
| `ChangeOrderScope` | string |  |
| `ChangeOrderNumber` | string |  |
| `Title` | string |  |
| `Status` | string |  |
| `Amount` | double |  |
| `CreatedDate` | dateTime |  |
| `ApprovedDate` | dateTime |  |
| `EffectiveDate` | dateTime |  |
| `MonthStart` | dateTime |  |
| `HasOutOfRangeDate` | boolean |  |
| `IsApproved` | boolean |  |

### `fct_LabourHours`

One time entry. Cost uses the cost rate, never the billing rate.

| Column | Type | Sorted by |
|---|---|---|
| `LabourKey` | string |  |
| `ProjectKey` | string |  |
| `WorkerKey` | string |  |
| `WorkerName` | string |  |
| `WorkerType` | string |  |
| `BillableStatus` | string |  |
| `ActivityDate` | dateTime |  |
| `MonthStart` | dateTime |  |
| `Hours` | double |  |
| `LabourCost` | double |  |
| `BillableValue` | double |  |
| `IsBillable` | boolean |  |

### `fct_Pipeline`

One open deal. Closed deals are excluded - a pipeline is what might still happen.

| Column | Type | Sorted by |
|---|---|---|
| `DealKey` | string |  |
| `DealStageKey` | string |  |
| `OwnerKey` | string |  |
| `DealName` | string |  |
| `DealType` | string |  |
| `Amount` | double |  |
| `WinProbability` | double |  |
| `WeightedAmount` | double |  |
| `CloseDate` | dateTime |  |
| `CreateDate` | dateTime |  |
| `MonthStart` | dateTime |  |
| `HasOutOfRangeDate` | boolean |  |
| `DaysOpen` | int64 |  |
| `IsPastCloseDate` | boolean |  |

### `fct_WIP`

Project x month. The WIP schedule - the Controller's deliverable.

| Column | Type | Sorted by |
|---|---|---|
| `ProjectKey` | string |  |
| `MonthStart` | dateTime |  |
| `OriginalContract` | double |  |
| `ApprovedChangeOrders` | double |  |
| `PendingChangeOrders` | double |  |
| `RevisedContract` | double |  |
| `OriginalBudget` | double |  |
| `RevisedBudget` | double |  |
| `CommittedCost` | double |  |
| `CostToDate` | double |  |
| `CostToDateQbo` | double |  |
| `EAC` | double |  |
| `BilledToDate` | double |  |
| `OverEacCostCodes` | int64 |  |
| `PercentCompleteRaw` | double |  |
| `PercentComplete` | double |  |
| `EarnedRevenue` | double |  |
| `CostToComplete` | double |  |
| `GrossProfitAtCompletion` | double |  |
| `GrossProfitPctAtCompletion` | double |  |
| `EarnedGrossProfit` | double |  |
| `OverBilling` | double |  |
| `UnderBilling` | double |  |
| `Backlog` | double |  |
| `CostVariance` | double |  |
| `CostVariancePct` | double |  |

## Metadata

### `meta_DataQuality`

The data-quality gate's results, for the report page.

| Column | Type | Sorted by |
|---|---|---|
| `Expectation` | string |  |
| `TableName` | string |  |
| `Severity` | string |  |
| `FailingRows` | int64 |  |
| `Passed` | boolean |  |
| `Description` | string |  |
| `CheckedAt` | dateTime |  |
| `SeveritySort` | int64 |  |
| `StatusLabel` | string |  |

### `meta_PipelineRun`

Drives the liveness measures - when the platform last ran.

| Column | Type | Sorted by |
|---|---|---|
| `BatchId` | string |  |
| `RunAt` | dateTime |  |
| `StepCount` | int64 |  |
| `RowsWritten` | int64 |  |
| `Succeeded` | boolean |  |

### `meta_UnmappedProjects`

The Controller's crosswalk to-do list.

| Column | Type | Sorted by |
|---|---|---|
| `ProjectKey` | string |  |
| `ProjectNumber` | string |  |
| `ProjectName` | string |  |
| `ProjectStatus` | string |  |
| `OriginalContract` | double |  |
| `CrosswalkMethod` | string |  |
| `CrosswalkConfidence` | double |  |
| `ProposedQboCustomerId` | string |  |
| `ProposedQboJobName` | string |  |
| `Reason` | string |  |

## Measure anchor

### `_Measures`

Measure anchor. Holds every measure; carries no data.

| Column | Type | Sorted by |
|---|---|---|
| `Anchor` | int64 |  |

## Measures

68 measures, all on `_Measures`. A measure cannot share a
name with a column on the same table, and the natural names collide
immediately — `EAC` and `Backlog` are both columns on `fct_WIP`. Hanging
every measure off one anchor table avoids that by construction.

### 01 Contract & Change

| Measure | Format | What it is |
|---|---|---|
| `Original Contract` | `$#,0` | Prime contract value before any change orders. |
| `Approved Change Orders` | `$#,0` | Approved prime change orders, CUMULATIVE. Rolling these up per-month rather than cumulatively is the defect that understated portfolio contract value by 16 percent on a comparab... |
| `Pending Change Orders` | `$#,0` | Submitted but not approved. Excluded from Revised Contract - unapproved work is exposure, not revenue. |
| `Revised Contract` | `$#,0` | Original contract plus approved change orders. |
| `Contract Growth %` | `0.0%` |  |

### 02 Budget & Cost

| Measure | Format | What it is |
|---|---|---|
| `Original Budget` | `$#,0` |  |
| `Revised Budget` | `$#,0` |  |
| `Committed Cost` | `$#,0` |  |
| `Cost to Date` | `$#,0` | Job-to-date cost per Procore. The reporting number. |
| `Estimated Cost at Completion` | `$#,0` | Floors at revised budget where the Procore budget view reports zero, so an unforecast job does not appear complete having spent nothing. |
| `Cost to Complete` | `$#,0` |  |
| `Percent Complete` | `0.0%` | Totals divided, NOT an average of per-project percentages - averaging weights a small job the same as a large one. |

### 03 Revenue & Margin

| Measure | Format | What it is |
|---|---|---|
| `Earned Revenue` | `$#,0` | Cost-to-cost percentage of completion. SUMX over projects because each project earns at its OWN percent complete - multiplying portfolio contract by portfolio percent complete i... |
| `Gross Profit at Completion` | `$#,0` |  |
| `GP % at Completion` | `0.0%` |  |
| `Earned Gross Profit` | `$#,0` |  |
| `Earned GP %` | `0.0%` |  |
| `Fade Gain` | `0.00%` | Fade (negative) or gain (positive) in forecast margin against the prior month. A job whose margin erodes quietly month over month is the one that hurts. |

### 04 Billing & Backlog

| Measure | Format | What it is |
|---|---|---|
| `Billed to Date` | `$#,0` |  |
| `Over Billing` | `$#,0` | Billings in excess of costs - a LIABILITY. Kept separate from under-billing rather than netted, because netting loses the balance-sheet distinction. |
| `Under Billing` | `$#,0` | Costs in excess of billings - an ASSET, and usually a cash-flow problem. |
| `Net Over Under Billing` | `$#,0` |  |
| `Backlog Value` | `$#,0` | Revised contract not yet earned. What the company still has to build. |
| `Backlog Months` | `0.0` | Months of work remaining at the trailing three-month burn rate. Guarded against a zero burn, which would otherwise render as a blank tile that reads like missing data. |

### 05 Reconciliation

| Measure | Format | What it is |
|---|---|---|
| `Cost to Date (QuickBooks)` | `$#,0` | Job cost per QuickBooks. Loaded alongside Procore, never blended with it. |
| `Cost Variance` | `$#,0` | Procore minus QuickBooks. A non-zero value is a FINDING, not necessarily a bug: an AP invoice not yet entered, a cost coded to the wrong job, an accrual Procore does not know ab... |
| `Cost Variance %` | `0.0%` |  |
| `Unmapped Contract Value` | `$#,0` | Contract value that cannot be reconciled to QuickBooks yet. The number that motivates finishing the crosswalk. |
| `Projects Unmapped` | `#,0` |  |

### 06 Risk & Exceptions

| Measure | Format | What it is |
|---|---|---|
| `Cost Codes Over EAC` | `#,0` |  |
| `Project Risk Label` | `` | Text label beside every RAG indicator. Red and green are separated by only dE 7.1 under deuteranopia, below the dE 8 legibility floor, so no status in this report is conveyed by... |
| `Projects at Risk` | `#,0` |  |
| `Projects Over EAC` | `#,0` | Cost has passed the forecast. Revenue recognition caps at 100 percent but the over-run stays visible here. |

### 07 Pipeline liveness

| Measure | Format | What it is |
|---|---|---|
| `Last Successful Run` | `yyyy-mm-dd hh:nn` |  |
| `Hours Since Last Run` | `0` |  |
| `Pipeline Status` | `` | The spreadsheet this replaces cannot say when it was last correct. A dashboard silently showing three-week-old numbers is worse than one that is obviously broken. |
| `Blocking DQ Failures` | `#,0` | Expectations that stopped the pipeline. COALESCE to zero on purpose: a blank KPI tile reads as missing data, when the answer is none. |
| `DQ Warnings` | `#,0` | Findings to review with the Controller - true of the real data, not necessarily defects. |

### 08 Pipeline & Forecast

| Measure | Format | What it is |
|---|---|---|
| `Pipeline Value` | `$#,0` | Total open pipeline, unweighted. The headline number, and the one that flatters - it assumes every deal closes. |
| `Weighted Pipeline` | `$#,0` | Pipeline weighted by win probability. The number to plan against. Probability comes from the STAGE, not the deal, except where the portal set a deal-level override. |
| `Open Deals` | `#,0` |  |
| `Average Deal Size` | `$#,0` |  |
| `Pipeline Confidence` | `0.0%` | Weighted pipeline as a share of the unweighted total. A low ratio means the pipeline is full of early-stage work - a very different position from the same headline number sittin... |
| `Stale Deals` | `#,0` | Deals whose close date has passed but are still open. These quietly inflate a forecast: the value sits in a month that has been and gone. |
| `Stale Pipeline Value` | `$#,0` |  |
| `Average Days Open` | `0` |  |
| `Total Forward Work` | `$#,0` | Backlog plus weighted pipeline - work in hand plus work we might win. The two are NOT the same quality of number and are shown separately as well; this is the planning horizon, ... |

### 09 Receivables

| Measure | Format | What it is |
|---|---|---|
| `AR Outstanding` | `$#,0` |  |
| `AR Overdue` | `$#,0` |  |
| `AR Over 90 Days` | `$#,0` |  |
| `AR Overdue %` | `0.0%` |  |
| `AR Weighted Average Days` | `0` | Weighted average age of the receivables book, in days. Weighted by value, because one large old invoice matters more than several small fresh ones - a plain average of ages hide... |
| `Days Sales Outstanding` | `0` | Days sales outstanding, trailing 90 days. Guarded against a zero-revenue window, which would otherwise render as a blank tile that reads like missing data. |
| `Invoices Outstanding` | `#,0` |  |

### 10 Payables & Cash

| Measure | Format | What it is |
|---|---|---|
| `AP Outstanding` | `$#,0` |  |
| `AP Overdue` | `$#,0` |  |
| `Net Working Capital` | `$#,0` | Receivables less payables. Both sides are positive in fct_Aging, so this subtraction is the only place the relationship between them is expressed. |
| `Expected Collections` | `$#,0` |  |
| `Expected Payments` | `$#,0` |  |
| `Net Cash Movement` | `$#,0` | Collections less payments over the selected weeks. Already signed in fct_CashForecast, so this is a plain sum. |
| `Cumulative Cash Position` | `$#,0` | Running cash position across the forecast window. This is the shape the CEO actually reads - the trough matters more than the total. |
| `Cash Forecast Basis` | `` | Committed cash movement only. Does NOT include work in backlog that has not been billed, because turning backlog into cash needs a billing schedule and collection assumptions th... |

### 11 Capacity

| Measure | Format | What it is |
|---|---|---|
| `Labour Hours` | `#,0.0` |  |
| `Labour Cost` | `$#,0` |  |
| `Billable Hours` | `#,0.0` |  |
| `Utilisation` | `0.0%` | The core capacity number: what share of paid hours can be charged on. |
| `Labour Margin` | `$#,0` | Value of billable hours at the client rate, less what those hours cost. Uses BillableValue and LabourCost, computed from the billing rate and the COST rate respectively - confla... |
| `Unattributed Hours` | `#,0.0` |  |

