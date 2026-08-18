# Runbook

Day-to-day operation, and what to do when something breaks.

---

## Normal operation

Two pipelines, deliberately separate.

| Pipeline | Contents | Why separate |
|---|---|---|
| `DL_Ingest_Pipeline` | `dl_01_extract_procore`, `dl_02_extract_qbo` (parallel) | An API outage or an expired token must not stop the model rebuilding on whatever data did land. |
| `DL_Master_Pipeline` | `dl_10_bronze_to_silver` → `dl_30_build_gold` → `dl_40_dq_checks` | Dependencies are **`Succeeded`**, not `Completed`: a failed stage stops the run rather than letting gold rebuild over stale bronze and publish numbers that look current. |

Recommended schedule, **America/Phoenix** — "yesterday's numbers" has to mean
yesterday to the person reading it, not to UTC:

```
02:10  DL_Ingest_Pipeline
03:10  DL_Master_Pipeline
04:10  semantic model refresh
```

Leave a gap between the gold build and the model refresh. A Direct Lake reframe
that lands mid-write binds to a half-written table.

The off-the-hour minutes are intentional. Everyone schedules on `:00`.

---

## First-run checklist

Run these in order. Each one is a precondition for the next.

1. **`dl_00_bootstrap`** — creates every bronze and control table empty but
   correctly typed. Safe to re-run; it only creates what does not exist.
2. **`python scripts/qbo_authorize.py`** — the one-time QuickBooks consent.
   Nothing QuickBooks-related works until this has been done.
3. **`dl_01_extract_procore`** — confirm the request count in
   `Files/_diag/extract_procore.json` against the 600/hour quota.
4. **`dl_02_extract_qbo`**.
5. **`DL_Master_Pipeline`**.
6. Review `dl_dq_results` **with the Controller**. Warnings are findings to
   discuss, not defects to suppress.

---

## Diagnosing a failed notebook

Fabric's job API reports `Failed` and nothing else — no per-cell detail. So
every notebook writes structured diagnostics to `Files/_diag/<name>.json`, and
the Python traceback appears in **stdout**, not stderr (stderr carries Spark
logs).

```
get_job_status            -> is it actually terminal
get_notebook_driver_logs  -> log_type="stdout" for the Python error
Files/_diag/<name>.json   -> what the run had done before it died
```

---

## Failure modes, most likely first

### QuickBooks: `invalid_grant` on the token exchange

**The single most likely production failure, and the least obvious.**

The refresh token rotates on **every** use and hard-expires at 100 days.
`dl_02_extract_qbo` persists the new one to `dl_meta_token` *before* pulling any
data, precisely so a crash mid-run cannot lose it.

Diagnose in this order:

```sql
SELECT source, obtained_at, batch_id
FROM dl_meta_token WHERE source = 'quickbooks'
ORDER BY obtained_at DESC LIMIT 5;
```

- **Rows present, newest under 100 days old** → the token was used elsewhere and rotated out from under the pipeline. Something else is calling the API with the same credentials. Find it; two consumers cannot share one refresh token.
- **Newest over 100 days old** → it lapsed. Re-run `scripts/qbo_authorize.py`.
- **No rows** → the first run never completed. Check that `.env` or Key Vault still holds the seed token from authorisation.

> **Do not** paste the value from `.env` back in as a fix without checking
> `dl_meta_token` first. The `.env` value is a stale seed after the first
> successful run, and using it invalidates the token that currently works.

Two expectations watch this: warn at 60 days, **block at 85**. The block is
deliberate — 15 days is enough notice to schedule a browser consent, and a
silent expiry is not.

### Procore: `QuotaExhausted`

600 requests/hour, per client. Procore does **not** send `Retry-After` on a 429;
it sends `X-Rate-Limit-Reset`, a Unix epoch.

The run stops cleanly rather than half-loading. Watermarks for endpoints already
completed have advanced, so **re-running resumes rather than restarting** —
that is the whole point of advancing the watermark only on success.

If it recurs every night, in order of preference:

1. Confirm incremental filters are actually applying. A `full` mode in
   `Files/_diag/extract_procore.json` for an endpoint with `incremental:` set
   means its watermark is not being written.
2. Reduce project-scoped endpoints, or move them to a weekly run.
3. Split the registry across two scheduled runs an hour apart.

### Procore: 404 on a project-scoped endpoint

Almost always the missing `Procore-Company-Id` header, **not** a permissions
problem. Procore answers 404 rather than 403, which reads as "this project does
not have that tool enabled" and sends people hunting through permission
settings for hours.

The header is sent on every version by design (`Endpoint.needs_company_header`
returns `True` unconditionally). If you see a 404, check that first.

A genuinely empty endpoint is also possible — some Procore endpoints return
`200` with zero rows unless given a date window, which is what `date_range_days`
in the registry is for.

### Budget detail rows are empty or missing columns

Procore returns a **different column set per budget view**. The registry pins one
view by name:

```yaml
where_field: name
where_value: "Data Link Standard Budget View"
```

If that name does not match a view in the tenant, no parent is selected and the
endpoint is skipped with a message saying so. Check
`dl_bronze_procore_budget_views` for the real names.

If rows arrive but the money columns are zero, the tenant's column keys differ
from the standard ids. `dl_bronze_procore_budget_detail_columns` lists the
actual keys for the pinned view — pin them into the COALESCE chains in
`transformation/sql/silver/10_procore_silver.sql`.

### Blocking data-quality failure

The pipeline stopped on purpose. Nothing downstream ran, and the report still
shows the last good numbers.

```sql
SELECT expectation, table_name, failing_rows, description
FROM dl_dq_results
WHERE severity = 'error' AND NOT passed
ORDER BY checked_at DESC;

SELECT * FROM dl_dq_rejects WHERE _dq_expectation = '<name>' LIMIT 100;
```

Every expectation returns the **failing rows**, not a boolean — so the second
query shows exactly which records are wrong.

Fix the data or the transform. **Do not downgrade the expectation to a warning
to get the run green.** If an expectation genuinely describes a business
condition rather than a defect, that is a design change worth making
deliberately and writing down — not a workaround applied at 2am.

### A table is missing from the report but exists in the lakehouse

Direct Lake drops a table **silently** when a declared type disagrees with the
Delta table. It appears as a missing table, not an error.

`dl_30_build_gold` writes the real schema to `Files/_diag/gold_schema.json` on
every run. Compare it against the semantic model definition; regenerate the
model from that file rather than hand-editing types.

### The report shows stale numbers and nobody noticed

This is what the pipeline-liveness measures exist for. `Pipeline Status` on the
report reads "Current", "Late", or "STALE - these numbers may be weeks old",
derived from `meta_PipelineRun`.

A dashboard silently showing three-week-old numbers is worse than one that is
obviously broken, because nobody stops trusting it.

---

## Changing things

**Adding a Procore endpoint** — one entry in
`ingestion/procore/config/endpoints.yml`, then re-upload it to
`Files/config/procore_endpoints.yml` and re-run `dl_00_bootstrap` to create the
table. No notebook changes.

Set `incremental:` **only** where the endpoint genuinely accepts
`filters[updated_at]`. Several Procore endpoints document `filters[created_at]`
but not `updated_at`; incrementing on `created_at` misses status changes on
existing records — a change order going from pending to approved would never be
picked up, and the contract value would be quietly wrong rather than obviously
missing.

**Changing a transform** — edit the `.sql`, run `python tests/test_gold.py`,
re-upload to `Files/sql/`. The test runs the real files, so a broken join fails
locally in seconds instead of at 3am in Spark.

**Changing a notebook** — edit `scripts/make_notebooks.py`, never the `.ipynb`.
A hand-edited notebook is overwritten by the next deploy and the change is lost
silently. The generator compiles every cell before writing.

---

## Secrets

One function, `get_secret()`: Key Vault when `DATALINK_KEYVAULT_URL` is set,
environment variable otherwise, and a `RuntimeError` naming the fix when
neither. The vault URL is itself an environment variable, so nothing hardcodes a
vault and pointing at a different tenant is a config change.

For production, the QuickBooks refresh token needs somewhere writable, because
it rotates. Either:

- grant the workspace identity **Key Vault Secrets Officer** on that one secret, or
- leave it in `dl_meta_token` (current behaviour), which needs no Azure subscription.

**Never** put a secret in a Spark property or a workspace environment variable.
Both are plaintext-readable by any workspace member.

If Key Vault is unavailable entirely, the fallback is the landing split:
extraction runs locally where the secret already lives and writes JSONL to
OneLake `Files/_landing/`; a credential-free notebook lands it to bronze.
