# Procore — endpoint cheatsheet

Every entry in `ingestion/procore/config/endpoints.yml` cites this file. It
records what was **verified against the tenant**, not what the public docs
claim — the two disagree in several places that cost real time.

## The five facts that matter

**1. `Procore-Company-Id` must be sent on every request, on every API version.**
Without it a v1.0 project-scoped endpoint returns **404**, not 403. A 404 reads
as "this project has no such tool" and looks for hours like a permissions
problem. `build_headers()` always sends it.

**2. The real rate limit on this tenant is ~25 requests per 10 seconds**, not
the 600/hour that is widely quoted. Code tuned for the documented figure behaves
badly against the real one, so the session reads the limit from each response
rather than trusting a constant.

**3. Procore does not send `Retry-After` on a 429.** It sends
`X-Rate-Limit-Limit`, `-Remaining` and `-Reset`, where `-Reset` is a **Unix
epoch**. Blind exponential backoff is wrong here; the session gates on the
remaining-quota header before spending a request it does not have.

**4. A project without a tool enabled answers 403 or 404, and that is normal.**
Not every job uses Financials. Treated as fatal it aborts the whole endpoint and
loses every project that *did* have data — so `ToolUnavailable` counts and skips
instead.

**5. `filters[updated_at]` is not universal.** Several endpoints document
`filters[created_at]` and not `updated_at`. Incrementing on `created_at` misses
status changes to existing records: a change order going pending → approved
would never be picked up, and the contract value would be quietly wrong rather
than obviously missing. Only the endpoints marked below accept `updated_at`.

## Budget views — the EAC spine

`/rest/v1.0/budget_views` → `/budget_views/{id}/detail_rows`.

**Procore returns a different column set per view**, so the registry pins one
view by name. Confirm the standard view before a production run.

The money columns come back with **spaces in their names** —
`Job to Date Costs`, `Revised Budget`, `Estimated Cost at Completion`,
`Projected over Under` (lowercase "over"). `get_json_object($.Job to Date Costs)`
does not parse and returns NULL, which COALESCEs to 0 and produces a WIP
schedule that satisfies every accounting identity and is entirely wrong.
Bracket notation is mandatory; `tests/test_silver_keys.py` pins the exact keys
against a captured payload.

## Paging

`per_page` maxes at 1000. Terminate on the response — a short page or the
`Total` header — never on an assumed page count. Some endpoints return 200 with
zero rows unless given a date window.

## Endpoints in use

| Name | Path | Version | Scope | Incremental | Parent |
|---|---|---|---|---|---|
| `projects` | `/rest/v1.0/companies/{company_id}/projects` | 1.0 | company | `filters[updated_at]` |  |
| `vendors` | `/rest/v1.0/vendors` | 1.0 | company | `filters[updated_at]` |  |
| `offices` | `/rest/v1.0/offices` | 1.0 | company | `—` |  |
| `departments` | `/rest/v1.0/departments` | 1.0 | company | `—` |  |
| `project_regions` | `/rest/v1.0/companies/{company_id}/project_regions` | 1.0 | company | `—` |  |
| `project_stages` | `/rest/v1.0/companies/{company_id}/project_stages` | 1.0 | company | `—` |  |
| `cost_codes` | `/rest/v1.0/cost_codes` | 1.0 | project | `—` |  |
| `budget_views` | `/rest/v1.0/budget_views` | 1.0 | project | `—` |  |
| `budget_detail_rows` | `/rest/v1.0/budget_views/{parent_id}/detail_rows` | 1.0 | parent | `—` | {'endpoint': 'budget_views', 'field': 'id', 'where_field': 'name', 'where_value': 'Procore Standard Budget'} |
| `budget_detail_columns` | `/rest/v1.0/budget_views/{parent_id}/budget_detail_columns` | 1.0 | parent | `—` | {'endpoint': 'budget_views', 'field': 'id', 'where_field': 'name', 'where_value': 'Procore Standard Budget'} |
| `manual_forecast_line_items` | `/rest/v1.0/projects/{project_id}/manual_forecast_line_items` | 1.0 | project | `—` |  |
| `prime_contracts` | `/rest/v1.0/prime_contracts` | 1.0 | project | `filters[updated_at]` |  |
| `prime_contract_line_items` | `/rest/v1.0/prime_contracts/{parent_id}/line_items` | 1.0 | parent | `—` | {'endpoint': 'prime_contracts', 'field': 'id'} |
| `prime_change_orders` | `/rest/v1.0/projects/{project_id}/prime_change_orders` | 1.0 | project | `filters[updated_at]` |  |
| `payment_applications` | `/rest/v1.0/prime_contracts/{parent_id}/payment_applications` | 1.0 | parent | `—` | {'endpoint': 'prime_contracts', 'field': 'id'} |
| `commitments` | `/rest/v1.0/commitments` | 1.0 | project | `—` |  |
| `purchase_order_contracts` | `/rest/v1.0/purchase_order_contracts` | 1.0 | project | `filters[updated_at]` |  |
| `purchase_order_line_items` | `/rest/v1.0/purchase_order_contracts/{parent_id}/line_items` | 1.0 | parent | `—` | {'endpoint': 'purchase_order_contracts', 'field': 'id'} |
| `work_order_contracts` | `/rest/v1.0/work_order_contracts` | 1.0 | project | `filters[updated_at]` |  |
| `work_order_line_items` | `/rest/v1.0/work_order_contracts/{parent_id}/line_items` | 1.0 | parent | `—` | {'endpoint': 'work_order_contracts', 'field': 'id'} |
| `commitment_change_orders` | `/rest/v1.0/projects/{project_id}/commitment_change_orders` | 1.0 | project | `filters[updated_at]` |  |
| `direct_costs` | `/rest/v1.1/projects/{project_id}/direct_costs` | 1.1 | project | `filters[updated_at]` |  |
| `direct_cost_line_items` | `/rest/v1.0/projects/{project_id}/direct_costs/line_items` | 1.0 | project | `—` |  |
| `requisitions` | `/rest/v1.1/requisitions` | 1.1 | project | `filters[updated_at]` |  |

Generated from `ingestion/procore/config/endpoints.yml`; edit the registry, not
this table.
