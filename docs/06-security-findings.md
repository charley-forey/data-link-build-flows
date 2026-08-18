# Security findings

What was found, what was done, and what is still open. Written plainly because
a security note nobody reads is not a control.

---

## This repository is PUBLIC

`github.com/charley-forey/data-link-build-flows` is a public repository, by the
client's explicit choice after being asked. Everything below follows from that.

**Nothing in this repository is a credential.** What it contains is library
code, SQL, endpoint registries, DAX, a report definition and documentation.
Identifiers that appear in the deploy scripts — workspace id, lakehouse id,
semantic model id, report id — are **object identifiers, not secrets**: they
locate an item inside a tenant that still requires authentication to reach.

---

## Findings and what was done

### 1. Client PII was committed, then removed from history — RESOLVED

`Request.txt` contained the client contact's direct phone number, email address
and a revenue figure. It was removed from the working tree **and rewritten out
of the entire git history**, then added to `.gitignore`.

It must never be reintroduced. If a similar briefing document is needed, keep it
outside the repository.

### 2. Plaintext secrets in `.env` — CONTAINED, NOT SOLVED

`.env` holds live credentials for all three sources in plaintext, in a directory
that is not encrypted at rest. It is gitignored and has never been committed
(verified: `git ls-files` matches nothing).

This is acceptable for sandbox credentials during a build. **It is not
acceptable for production.** See "Open" below.

### 3. Secrets are never printed — BY CONSTRUCTION

No script prints a token, and the QuickBooks flow is the one that would tempt
you to. `qbo_authorize.py` persists the refresh token to `.env` via
`update_dotenv()` and prints only whether it succeeded.

`check_token_shape()` reports what *kind* of token was supplied without echoing
the value. The deploy scripts take auth from `az account get-access-token` and
never write it anywhere.

### 4. One function produces every credential — BY CONSTRUCTION

`get_secret(name)`: Key Vault when `DATALINK_KEYVAULT_URL` is set, environment
variable otherwise, and a `RuntimeError` **naming the fix** when neither. There
is no second place for a credential to hide, and no silent `None` that fails
later somewhere less obvious.

The vault URL is itself an environment variable, so nothing hardcodes a vault
and pointing at another tenant is a config change.

### 5. Never put a secret in a Spark property or workspace variable — POLICY

Both are plaintext-readable by **any** workspace member. This is written into
the runbook because it is the obvious-looking shortcut when a notebook cannot
see a credential — which is exactly the situation described next.

---

## Open

### A. No Key Vault, so the in-Fabric extractors cannot run

`DATALINK_KEYVAULT_URL` is not set in the Fabric workspace and no Azure Key
Vault is wired up. `get_secret()` therefore raises inside a notebook, and the
three extractor notebooks — `dl_01_extract_procore`, `dl_02_extract_qbo`,
`dl_03_extract_hubspot` — **fail at their first credential call**. None of them
has ever completed a run in Fabric.

This is a known and deliberate state, not a defect. All source data reaches
bronze through the **landing split**: extraction runs locally where the secret
already lives (`scripts/extract_local.py`) and writes JSONL to
`Files/_landing/`; the credential-free `dl_05_land_to_bronze` loads it. The
medallion, the gate, the model and the report are all fully automated — only the
API-facing half is manual.

**To close it:** provision a Key Vault, set `DATALINK_KEYVAULT_URL` on the
workspace, and grant the workspace identity read on the secrets. The QuickBooks
refresh token additionally needs **write**, because it rotates — either
Key Vault Secrets Officer on that one secret, or leave it in `dl_meta_token`
(current behaviour, needs no Azure subscription).

### B. Sandbox credentials are still in use

Every credential in `.env` is a sandbox credential. Moving to production means
new credentials, and they should go straight into Key Vault rather than through
`.env` at all.

### C. The QuickBooks refresh token is a single point of failure

It rotates on every use and hard-expires at 100 days. The persistence path is
correct and two expectations watch its age (warn 60 days, block 85), but a
failure here is silent for up to an hour and then total. Worth a monitoring
alert in production, not just a report page nobody has open at 2am.

### D. Report and model are readable by any workspace member

No row-level security is configured. Every viewer sees every project's
financials. If project-level or role-level restriction is wanted, it has to be
designed — RLS on `dim_Project` is the natural place, and it should be decided
before the report is shared beyond the current group.

---

## Verification

```bash
git ls-files | grep -iE '\.env$|Request.txt'   # must return nothing
```

Run before every push. It is currently clean.
