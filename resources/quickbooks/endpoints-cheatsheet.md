# QuickBooks Online — endpoint cheatsheet

Records what was verified against the sandbox realm.

## The token is the whole risk

**The refresh token rotates on every use** and hard-expires at 8,640,000
seconds (100 days). The rotated value must be persisted *before* any data is
pulled — `dl_02_extract_qbo` writes it to `dl_meta_token` immediately after the
exchange.

If that write is ever skipped, the integration keeps working until the access
token expires and then fails permanently, roughly an hour after whoever changed
it stopped watching. Two expectations watch the token's age: warn at 60 days,
block at 85.

The stored token always beats the one in `.env` or Key Vault — QuickBooks
invalidates the previous refresh token the moment a new one is issued, so the
configured value is stale after the first successful run.

## Names that are not what you expect

- The aged receivables report is **`AgedReceivableDetail`**, not `ARAgingDetail`. The latter returns 400.
- `TimeActivity` splits duration across **`Hours` and `Minutes` as separate fields**. A 90-minute entry is `Hours=1, Minutes=30`; reading `Hours` alone silently loses a third of the time.
- `BillableStatus` is a three-value enum: `Billable`, `NotBillable`, **`HasBeenBilled`**. The third means already invoiced — the *most* billable state. A `LIKE 'BILLABLE%'` test catches the first and silently misses the third, understating utilisation by every hour that has actually been billed.
- `CostRate` is frequently `0` in sandbox data. That is faithful, not a mapping bug — but labour margin is overstated by exactly that amount until real rates exist.

## Paging and incremental

`STARTPOSITION` is **1-based**, not 0-based. Page with
`STARTPOSITION`/`MAXRESULTS`; incremental via
`/cdc?entities=…&changedSince=…` with a 30-day look-back and a periodic full
reconcile.

Job cost linkage is the `Customer:Job` hierarchy (`CustomerRef`), plus
`ClassRef` and line-level `ProjectRef`. Which one this client actually uses
changes the crosswalk join column and is an open question.

## Entities in use

| Entity | Bronze table | Mode | CDC |
|---|---|---|---|
| `Account` | `dl_bronze_qbo_accounts` | full reload | — |
| `Customer` | `dl_bronze_qbo_customers` | full reload | — |
| `Vendor` | `dl_bronze_qbo_vendors` | full reload | — |
| `Item` | `dl_bronze_qbo_items` | full reload | — |
| `Class` | `dl_bronze_qbo_classes` | full reload | — |
| `Department` | `dl_bronze_qbo_departments` | full reload | — |
| `Term` | `dl_bronze_qbo_terms` | full reload | — |
| `Invoice` | `dl_bronze_qbo_invoices` | incremental | yes |
| `Bill` | `dl_bronze_qbo_bills` | incremental | yes |
| `Purchase` | `dl_bronze_qbo_purchases` | incremental | yes |
| `JournalEntry` | `dl_bronze_qbo_journal_entries` | incremental | yes |
| `Payment` | `dl_bronze_qbo_payments` | incremental | yes |
| `BillPayment` | `dl_bronze_qbo_bill_payments` | incremental | yes |
| `VendorCredit` | `dl_bronze_qbo_vendor_credits` | incremental | yes |
| `CreditMemo` | `dl_bronze_qbo_credit_memos` | incremental | yes |
| `Deposit` | `dl_bronze_qbo_deposits` | incremental | yes |
| `TimeActivity` | `dl_bronze_qbo_time_activities` | incremental | yes |

## Reports

Reports return a nested `Rows`/`ColData` tree rather than a list, so each is
stored **whole** and flattened in silver where the shape is visible in SQL.

| Report | Bronze table |
|---|---|
| `GeneralLedger` | `dl_bronze_qbo_general_ledger` |
| `ProfitAndLossDetail` | `dl_bronze_qbo_profit_and_loss_detail` |
| `AgedReceivableDetail` | `dl_bronze_qbo_ar_aging_detail` |
| `TrialBalance` | `dl_bronze_qbo_trial_balance` |

Generated from `ingestion/qbo/config/entities.yml`.
