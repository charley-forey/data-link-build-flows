# What has been built

**For:** Data Link Technology Services
**Date:** 18 August 2026
**Scope:** Procore + QuickBooks Online + HubSpot → one governed reporting layer

---

## The problem this replaces

Three systems hold the numbers that describe the business, and none of them talk
to each other:

- **Procore** knows what the projects are doing — budgets, contracts, change orders, committed cost, cost to date, forecast at completion.
- **QuickBooks** knows what the business is doing — the general ledger, invoices, bills, what is owed and what is owing.
- **HubSpot** knows what might happen next — the deals in the pipeline and how likely each is to close.

Producing a WIP schedule, a project P&L, a backlog report or a cash forecast
today means someone opens all three, exports to Excel, and reconciles by hand.
That takes days a month, it is only as current as the last export, and every
number is one copy-paste away from being wrong with no way to tell.

**The ask was not another application to maintain.** It was to make the three
systems you already own feed one source of truth that produces the reporting
automatically — and that you can later ask questions of.

---

## What now exists

A Microsoft Fabric platform in your own tenant, and an 11-page Power BI report
built on it.

### The reporting

| Page | Answers |
|---|---|
| **Portfolio** | Every project at once — contract, forecast cost, percent complete, margin, risk |
| **Executive KPI** | The one-page position: revenue, margin, backlog, billing |
| **WIP Schedule** | The Controller's deliverable, in the order a WIP schedule is conventionally read. Exports to Excel |
| **Project Performance** | Per-project drill-down to cost-code level, change order log, margin fade |
| **Backlog & Burn** | Work in hand and the rate it is being consumed |
| **Exceptions** | What needs attention: projects at risk, cost over forecast, unapproved change orders, system disagreements |
| **Pipeline & Forecast** | Open pipeline, weighted by the win probability of the stage it sits in |
| **AR & Collections** | The chase list — who owes what, how overdue, worth how much |
| **Cash Forecast** | Committed cash in and out by week |
| **Capacity** | Hours, utilisation, and what labour is actually earning |
| **Data Quality** | Whether today's numbers can be trusted — see below |

### Underneath it

- Every figure is **derived from the source systems**, never typed in.
- The data is **layered**: the raw API response is kept exactly as received, then typed and validated, then shaped into the reporting model. A mistake in the shaping is a re-run, not a re-extract.
- **53 automated quality checks** run before anything publishes. A check that would make a number *wrong* stops the run outright.
- The whole thing is **code in a repository**. Fabric is a deployment target, so a broken item is fixed by re-running a script, not by remembering what someone clicked.

---

## The part worth understanding

### Nothing links your three systems

There is no shared identifier between a Procore project, a QuickBooks job and a
HubSpot deal. Every number that combines them depends on getting that link
right, so the link is **curated, not guessed**:

1. A mapping your Controller enters by hand always wins.
2. Then an exact project-number match.
3. Then a name match — **only when it is unambiguous**.

If a project resembles two QuickBooks jobs equally well, the system does not
pick one. That would be a coin flip dressed up as a decision, and it would
quietly attribute one job's cost to another job's revenue. It goes to a review
page instead.

Projects that are missing a mapping **still flow through, flagged**. A gap makes
a number visibly incomplete rather than quietly smaller — which is the whole
difference between a report you can trust and one you cannot.

### The report says when it was last correct

A spreadsheet cannot tell you it is stale. This can. The Data Quality page
carries the time of the last successful run, whether the quality gate passed,
and every check that failed with the rows that failed it.

Status is **never shown as colour alone** — every indicator carries a word.
Red and green are not distinguishable for a meaningful share of readers, and
this report goes to a CEO and a Controller.

---

## Where it stands today

Everything above is **built and running end to end** against your sandbox
Procore, QuickBooks and HubSpot accounts. The full chain — API call, raw store,
validation, reporting model, quality gate, published report — has been executed
and the output verified against the source figures.

Because these are sandboxes, the amounts are small and mostly demo data. What
has been proven is the *machinery*: every number is derived, every accounting
identity holds, and the checks catch what they are meant to catch.

### One thing is not yet hands-off

**Pulling the data from the three APIs is currently started by hand.** Everything
after that point — validating it, building the reporting model, running the 53
checks, refreshing the report — is automatic.

The reason is narrow and fixable. Credentials for Procore, QuickBooks and
HubSpot have to live somewhere the platform can read them safely, and that
means an Azure Key Vault. One has not been set up yet, so the three extraction
steps run from a workstation where the credentials already are, and hand their
output to the automated half.

We have deliberately **not** worked around this by pasting credentials into
Fabric settings. Those are readable by anyone with access to the workspace,
which is not an acceptable place for the keys to your accounting system.

Setting up the Key Vault is a small piece of work and it is item 6 below. Until
then, treat the platform as "one command to start, then automatic" rather than
fully unattended — and note that a scheduled overnight refresh is not possible
until it is done.

**Live checks currently reporting:** 53 run, 0 blocking failures, 3 warnings.
The three warnings are real conditions worth seeing, not faults — QuickBooks
cost and labour hours that cannot yet be attributed to a project, and payables
past due.

---

## What the numbers do *not* yet include

Stated plainly, because a confident chart with nothing behind it is worse than
no chart:

- **The cash forecast covers committed cash only** — invoices already raised and bills already received. It does *not* project cash from work in backlog. Doing that needs a billing schedule and collection assumptions we have not been given; inventing them would put a confident line on a chart with nothing behind it. Backlog is reported separately, and the gap between the two is the honest answer.
- **Labour cost reads zero** wherever QuickBooks carries no cost rate, which is currently everywhere. Labour margin is overstated by exactly that amount until real cost rates are entered.
- **The sales pipeline reads zero** because the HubSpot portal has no deals in it. The forecast is built and waiting.
- **Capacity rests on QuickBooks time entries only.** Procore timecards would deepen it considerably.

---

## What we need from you

1. **Which Procore budget view is the standard one.** Procore returns a different set of columns per view, so the pipeline pins one by name. This is the single most important thing to confirm before a production run.
2. **Whether QuickBooks job cost is dimensioned by `Customer:Job`, by `Class`, or by native Projects.** Changes which column the crosswalk joins on.
3. **Materiality thresholds** for flagging a Procore↔QuickBooks cost disagreement.
4. **Whether production Procore is a separate company ID** from the sandbox, and what request quota that tenant has.
5. **One historical month's WIP schedule** that your Controller produced by hand. Reproducing it and tying it out line by line is the real acceptance test; everything so far is a precondition for it.
6. **Whether an Azure subscription is available for a Key Vault.** This is what turns the platform fully unattended and makes a scheduled refresh possible. It is the only thing standing between where we are and no-touch operation.

---

## Suggested next steps

1. Confirm the six items above.
2. Set up the Key Vault, which makes everything below unattended rather than semi-manual.
3. Point the platform at production Procore and QuickBooks.
4. Reconcile one historical month against the Controller's own spreadsheet. **This is the real acceptance test** — everything so far is a precondition for it.
5. Load real deals into HubSpot and the pipeline forecast starts reporting.
6. Agree a refresh schedule (America/Phoenix — "yesterday's numbers" has to mean yesterday to whoever is reading).
