# HubSpot — endpoint cheatsheet

Records what was verified against the portal.

## The token

It must be a **private-app token** and starts `pat-`. Two other credentials look
plausible and both 401 here:

- a **developer API key** (`na2-…`) — authenticates against the developer surface, not the portal
- a **personal access key** (`CiRu-…`) — the CLI credential

`check_token_shape()` warns up front rather than letting the first call fail,
and it never prints the token itself.

## Win probability lives on the stage, not the deal

This is why `/crm/pipelines/{version}/deals` is pulled at all. A deal knows
which stage it is in; only the **stage definition** knows what that stage is
worth. Weighted pipeline forecasting is therefore a join, and a deal-level
`hs_deal_stage_probability` override wins only where the portal has set one.

Stages that deals reference but the pipeline definition no longer contains —
renamed or deleted after deals moved through them — are unioned into
`dim_DealStage` at probability 0, so referential integrity holds by
construction rather than by hope.

## Quirks

- **`Retry-After` is in MILLISECONDS**, unlike Procore and QuickBooks. Treating it as seconds turns a 200ms backoff into 200 seconds and the run appears to hang.
- Booleans arrive as the **strings** `"true"` / `"false"`, so silver compares `LOWER(TRIM(...)) = 'true'`.
- Paging is by `after` cursor, not offset.
- The search endpoint caps at **10,000 results** per window. `search_since()` raises rather than truncating — a quiet truncation is missing pipeline data that nothing downstream can detect.
- Properties are requested **explicitly, never wildcard**. Portals accumulate hundreds of custom properties; a wildcard pull is slow and produces a bronze schema that changes whenever a sales admin adds a field.

API version is pinned by `HUBSPOT_API_VERSION` (currently `2026-03`).

## Objects in use

| Name | Object type | Bronze table | Properties | Associations |
|---|---|---|---|---|
| `deals` | `deals` | `dl_bronze_hubspot_deals` | 16 | companies, contacts |
| `companies` | `companies` | `dl_bronze_hubspot_companies` | 9 | — |
| `contacts` | `contacts` | `dl_bronze_hubspot_contacts` | 9 | — |
| `line_items` | `line_items` | `dl_bronze_hubspot_line_items` | 7 | — |

## Reference sets

Not CRM objects, and they do not page the same way, so they use dedicated
helpers.

| Name | Bronze table |
|---|---|
| `deal_pipelines` | `dl_bronze_hubspot_pipelines` |
| `owners` | `dl_bronze_hubspot_owners` |

Generated from `ingestion/hubspot/config/objects.yml`.
