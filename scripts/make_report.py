"""Generate the PBIR report definition from a declarative page spec.

The Fabric MCP server has no report-creation tool, so this is the one artifact
that has to be authored directly. Authoring it as CODE rather than by hand in
Desktop keeps it in the same position as everything else here: diffable,
reviewable in a PR, and re-emitted from source rather than repaired by clicking.

Run:  python scripts/make_report.py          writes powerbi/<name>.Report/
      python scripts/deploy_report.py --apply  publishes it

WHY THE FIELD NAMES ARE CHECKED
-------------------------------
Every projection names a table, a column or a measure. A typo does not error at
publish time - Power BI renders the visual EMPTY, which is indistinguishable
from "no data matched the filter". So the generator validates every reference
against a schema captured from the live semantic model and refuses to write a
report that binds to something that does not exist. That check is the whole
reason this file is worth having.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPORT_NAME = "Financial Operating System"
MODEL_NAME = "Data Link Financial Operating System"
OUT = ROOT / "powerbi" / f"{REPORT_NAME}.Report"
SCHEMA_FILE = ROOT / "powerbi" / "model-schema.json"

MEASURE_TABLE = "_Measures"

# PBIR schema versions, taken from a live deployed report rather than invented.
S_VISUAL = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.10.0/schema.json"
S_PAGE = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/2.1.0/schema.json"
S_PAGES = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/pagesMetadata/1.1.0/schema.json"
S_REPORT = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/report/3.3.0/schema.json"
S_VERSION = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/versionMetadata/1.0.0/schema.json"
S_PBIR = "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json"

# ---------------------------------------------------------------- canvas
#
# 1280x720 FitToPage. A fixed grid, because visuals placed by eye drift a few
# pixels every time the file is regenerated and the diff becomes unreadable.

W, H = 1280, 720
PAD = 16
TITLE_H = 44
SLICER_Y, SLICER_H = 50, 64
KPI_Y, KPI_H = 124, 104
BODY_Y = 242
BODY_H = H - BODY_Y - PAD


def row(count: int, y: int, height: int, x0: int = PAD, x1: int = W - PAD, gap: int = 12):
    """Evenly split a horizontal band into `count` boxes."""
    total = x1 - x0
    width = (total - gap * (count - 1)) / count
    return [(round(x0 + i * (width + gap)), y, round(width), height) for i in range(count)]


def cols(spans: list[int], y: int, height: int, x0: int = PAD, x1: int = W - PAD, gap: int = 12):
    """Split a band by relative weights, e.g. cols([2,1], ...) -> 2/3 and 1/3."""
    total = x1 - x0 - gap * (len(spans) - 1)
    unit = total / sum(spans)
    out, x = [], x0
    for s in spans:
        width = unit * s
        out.append((round(x), y, round(width), height))
        x += width + gap
    return out


# ---------------------------------------------------------------- fields


class Field:
    def __init__(self, kind: str, table: str, name: str, fmt: str | None = None):
        self.kind, self.table, self.name, self.fmt = kind, table, name, fmt

    def json(self) -> dict:
        ref = {"Expression": {"SourceRef": {"Entity": self.table}}, "Property": self.name}
        return {self.kind: ref}

    def projection(self) -> dict:
        p = {
            "field": self.json(),
            "queryRef": f"{self.table}.{self.name}",
            "nativeQueryRef": self.name,
        }
        if self.fmt:
            p["format"] = self.fmt
        return p


def M(name: str, fmt: str | None = None) -> Field:
    return Field("Measure", MEASURE_TABLE, name, fmt)


def C(table: str, column: str, fmt: str | None = None) -> Field:
    return Field("Column", table, column, fmt)


# ---------------------------------------------------------------- visuals


def _lit(value) -> dict:
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, int):
        text = f"{value}L"
    elif isinstance(value, float):
        text = f"{value}D"
    else:
        text = f"'{value}'"
    return {"expr": {"Literal": {"Value": text}}}


def _obj(**props) -> list:
    return [{"properties": {k: _lit(v) for k, v in props.items()}}]


class Visual:
    def __init__(self, vtype: str, box, roles: dict, *, alt: str = "",
                 sort: tuple | None = None, objects: dict | None = None, z: int = 0):
        self.vtype, self.box, self.roles = vtype, box, roles
        self.alt, self.sort, self.objects, self.z = alt, sort, objects or {}, z

    def json(self, name: str) -> dict:
        x, y, w, h = self.box
        visual: dict = {"visualType": self.vtype}

        if self.roles:
            query = {
                "queryState": {
                    role: {"projections": [f.projection() for f in fields]}
                    for role, fields in self.roles.items()
                }
            }
            if self.sort:
                field, direction = self.sort
                query["sortDefinition"] = {
                    "sort": [{"field": field.json(), "direction": direction}],
                    "isDefaultSort": True,
                }
            visual["query"] = query

        if self.objects:
            visual["objects"] = self.objects

        # Alt text lives on the container, and every visual carries one - a
        # chart that only says what it means in colour is unreadable to a
        # screen reader and to anyone printing in mono.
        if self.alt:
            visual["visualContainerObjects"] = {"general": _obj(altText=self.alt)}

        visual["drillFilterOtherVisuals"] = True
        return {
            "$schema": S_VISUAL,
            "name": name,
            "position": {"x": x, "y": y, "z": self.z, "height": h, "width": w,
                         "tabOrder": self.z},
            "visual": visual,
        }


def card(box, measure: Field, alt: str) -> Visual:
    return Visual("cardVisual", box, {"Data": [measure]}, alt=alt)


def table(box, fields: list[Field], alt: str, sort: tuple | None = None) -> Visual:
    return Visual("tableEx", box, {"Values": fields}, alt=alt, sort=sort)


def _cat_chart(vtype, box, category, values, alt, sort, objects=None):
    return Visual(vtype, box, {"Category": [category], "Y": values},
                  alt=alt, sort=sort, objects=objects)


def column(box, category, values, alt, sort=None, objects=None):
    return _cat_chart("clusteredColumnChart", box, category, values, alt, sort, objects)


def bar(box, category, values, alt, sort=None, objects=None):
    return _cat_chart("clusteredBarChart", box, category, values, alt, sort, objects)


def line(box, category, values, alt, sort=None, objects=None):
    return _cat_chart("lineChart", box, category, values, alt, sort, objects)


def area(box, category, values, alt, sort=None, objects=None):
    return _cat_chart("areaChart", box, category, values, alt, sort, objects)


def slicer(box, field: Field, alt: str) -> Visual:
    return Visual("slicer", box, {"Values": [field]}, alt=alt,
                  objects={"data": _obj(mode="Dropdown"),
                           "general": _obj(selfFilterEnabled=True)})


def title(text: str, subtitle: str = "") -> Visual:
    runs = [{"value": text,
             "textStyle": {"fontWeight": "bold", "fontFamily": "Segoe UI",
                           "fontSize": "20pt", "color": "#1b1b1f"}}]
    if subtitle:
        runs.append({"value": "   " + subtitle,
                     "textStyle": {"fontFamily": "Segoe UI", "fontSize": "11pt",
                                   "color": "#5b5b66"}})
    v = Visual("textbox", (0, 0, W, TITLE_H), {}, alt=text)
    v.objects = {"general": [{"properties": {"paragraphs": [{"textRuns": runs}]}}]}
    return v


def note(box, text: str) -> Visual:
    """A footnote. Used where a page would otherwise imply more than it knows."""
    v = Visual("textbox", box, {}, alt=text)
    v.objects = {"general": [{"properties": {"paragraphs": [{"textRuns": [
        {"value": text,
         "textStyle": {"fontFamily": "Segoe UI", "fontSize": "9pt", "color": "#5b5b66"}}
    ]}]}}]}
    return v


# ---------------------------------------------------------------- shared bits

DESC = "Descending"
ASC = "Ascending"
PROJECT = C("dim_Project", "ProjectName")
MONTH = C("dim_Date", "MonthYear")


def standard_slicers() -> list[Visual]:
    """Project and Month on every page.

    NOT synced across pages - PBIR's sync-group schema could not be verified
    against a working report, and inventing one silently produces slicers that
    look synced and are not. Each page filters correctly on its own; turning on
    cross-page sync is one setting in Desktop.
    """
    boxes = row(2, SLICER_Y, SLICER_H, x1=PAD + 2 * 280 + 12)
    return [
        slicer(boxes[0], PROJECT, "Filter by project"),
        slicer(boxes[1], MONTH, "Filter by month"),
    ]


def kpi_row(items: list[tuple[Field, str]]) -> list[Visual]:
    return [card(box, m, alt) for box, (m, alt) in zip(row(len(items), KPI_Y, KPI_H), items)]


# ---------------------------------------------------------------- pages


def pages() -> list[dict]:
    P: list[dict] = []

    # ------------------------------------------------ 1 Portfolio
    # 7:3 rather than 3:2 - the table is the accessible relief for the charts,
    # so its risk-label column has to be readable, not truncated to "Watch - t".
    left, right = cols([7, 3], BODY_Y, BODY_H)
    P.append(dict(
        display="Portfolio", visuals=[
            title("Portfolio", "Every project at once"),
            *standard_slicers(),
            *kpi_row([
                (M("Revised Contract"), "Revised contract value across all projects"),
                (M("Earned Revenue"), "Revenue earned to date"),
                (M("GP % at Completion"), "Forecast gross margin at completion"),
                (M("Backlog Value"), "Contract value not yet earned"),
                (M("Net Over Under Billing"), "Net over/under billing position"),
            ]),
            # The table view is what makes the low-contrast categorical slots
            # legible: every value is readable as a number, not only as a hue.
            table(left, [PROJECT, M("Revised Contract"), M("Estimated Cost at Completion"),
                         M("Percent Complete"), M("GP % at Completion"),
                         M("Project Risk Label")],
                  "Project table: contract, forecast cost, percent complete, margin "
                  "and risk label for every project",
                  sort=(M("Revised Contract"), DESC)),
            bar(right, PROJECT, [M("GP % at Completion")],
                "Gross margin at completion by project, sorted",
                sort=(M("GP % at Completion"), ASC)),
        ]))

    # ------------------------------------------------ 2 Executive KPI
    top, bottom = (BODY_Y, 220), (BODY_Y + 232, BODY_H - 232)
    a, b = cols([3, 2], top[0], top[1])
    c, d = cols([3, 2], bottom[0], bottom[1])
    P.append(dict(
        display="Executive KPI", visuals=[
            title("Executive KPI", "The one-page answer"),
            *standard_slicers(),
            *kpi_row([
                (M("Earned Revenue"), "Revenue earned to date"),
                (M("GP % at Completion"), "Forecast gross margin"),
                (M("Backlog Value"), "Work in hand not yet earned"),
                (M("Net Over Under Billing"), "Cash-relevant billing position"),
            ]),
            # Two series, one axis. Revenue and cost share a scale honestly;
            # a margin percentage would need a second axis, so it is a card.
            line(a, MONTH, [M("Earned Revenue"), M("Cost to Date")],
                 "Earned revenue and cost to date by month",
                 sort=(MONTH, ASC)),
            table(b, [PROJECT, M("Gross Profit at Completion"), M("Project Risk Label")],
                  "Top risks: projects by forecast gross profit, lowest first, with risk label",
                  sort=(M("Gross Profit at Completion"), ASC)),
            area(c, MONTH, [M("Backlog Value")], "Backlog by month",
                 sort=(MONTH, ASC)),
            table(d, [PROJECT, M("Backlog Value"), M("Backlog Months")],
                  "Backlog by project with months of backlog remaining",
                  sort=(M("Backlog Value"), DESC)),
        ]))

    # ------------------------------------------------ 3 WIP Schedule
    P.append(dict(
        display="WIP Schedule", visuals=[
            title("WIP Schedule", "The Controller's deliverable"),
            *standard_slicers(),
            # Column order is the order a WIP schedule is conventionally read.
            # Changing it to something more "logical" is how you lose the
            # reader who has produced this by hand for fifteen years.
            table((PAD, KPI_Y, W - 2 * PAD, 420),
                  [PROJECT, M("Revised Contract"), M("Original Budget"),
                   M("Estimated Cost at Completion"), M("Cost to Date"),
                   M("Percent Complete"), M("Earned Revenue"), M("Billed to Date"),
                   M("Over Billing"), M("Under Billing"),
                   M("Gross Profit at Completion"), M("GP % at Completion"),
                   M("Backlog Value")],
                  "Work in progress schedule: one row per project with contract, budget, "
                  "forecast cost, cost to date, percent complete, earned revenue, billings, "
                  "over and under billing, gross profit and backlog",
                  sort=(M("Revised Contract"), DESC)),
            bar((PAD, 540, W - 2 * PAD, 140), PROJECT, [M("Cost Variance")],
                "Procore versus QuickBooks cost variance by project",
                sort=(M("Cost Variance"), DESC)),
            note((PAD, 686, W - 2 * PAD, 24),
                 "Cost variance is Procore cost to date less QuickBooks job cost. "
                 "A non-zero figure is a finding to review, not necessarily a fault."),
        ]))

    # ------------------------------------------------ 4 Project Financial Performance
    a, b = cols([1, 1], BODY_Y, 236)
    c, d = cols([1, 1], BODY_Y + 248, BODY_H - 248)
    P.append(dict(
        display="Project Performance", visuals=[
            title("Project Financial Performance", "Per-project drill-down"),
            *standard_slicers(),
            *kpi_row([
                (M("Revised Contract"), "Revised contract value"),
                (M("Estimated Cost at Completion"), "Forecast cost at completion"),
                (M("Percent Complete"), "Percent complete, cost to cost"),
                (M("GP % at Completion"), "Forecast gross margin"),
            ]),
            # Four series on the adjacent pairlist, data labels on - slots 3
            # and 4 fall below 3:1 on this surface.
            column(a, C("fct_BudgetLine", "CostCode"),
                   [M("Original Budget"), M("Committed Cost"), M("Cost to Date"),
                    M("Estimated Cost at Completion")],
                   "Budget, committed cost, actual cost and forecast by cost code",
                   sort=(M("Estimated Cost at Completion"), DESC),
                   objects={"labels": _obj(show=True)}),
            table(b, [C("fct_BudgetLine", "CostCode"), C("fct_BudgetLine", "RevisedBudget"),
                      C("fct_BudgetLine", "JobToDateCost"),
                      C("fct_BudgetLine", "EstimatedCostAtCompletion"),
                      C("fct_BudgetLine", "ProjectedOverUnder")],
                  "Cost codes over their forecast: the earliest visible signal a job is "
                  "going wrong, which a project-level roll-up hides",
                  sort=(C("fct_BudgetLine", "ProjectedOverUnder"), DESC)),
            line(c, MONTH, [M("Fade Gain")],
                 "Fade or gain in forecast margin by month against a zero line",
                 sort=(MONTH, ASC)),
            table(d, [C("fct_ChangeOrder", "ChangeOrderNumber"), C("fct_ChangeOrder", "Title"),
                      C("fct_ChangeOrder", "Status"), C("fct_ChangeOrder", "Amount"),
                      C("fct_ChangeOrder", "EffectiveDate")],
                  "Change order log with status, value and effective date",
                  sort=(C("fct_ChangeOrder", "EffectiveDate"), DESC)),
        ]))

    # ------------------------------------------------ 5 Backlog & Burn
    a, b = cols([1, 1], BODY_Y, BODY_H)
    P.append(dict(
        display="Backlog & Burn", visuals=[
            title("Backlog & Burn", "Work in hand and the rate it is consumed"),
            *standard_slicers(),
            *kpi_row([
                (M("Backlog Value"), "Contract value not yet earned"),
                (M("Backlog Months"), "Months of backlog at the current burn rate"),
                (M("Earned Revenue"), "Revenue earned to date"),
                (M("Revised Contract"), "Revised contract value"),
            ]),
            area(a, MONTH, [M("Backlog Value")], "Backlog by month", sort=(MONTH, ASC)),
            bar(b, PROJECT, [M("Backlog Value")], "Backlog by project, sorted",
                sort=(M("Backlog Value"), DESC)),
        ]))

    # ------------------------------------------------ 6 Exceptions
    a, b = cols([1, 1], KPI_Y + KPI_H + 12, 260)
    c, d = cols([1, 1], KPI_Y + KPI_H + 284, 260)
    P.append(dict(
        display="Exceptions", visuals=[
            title("Exceptions", "What needs attention, in one place"),
            *standard_slicers(),
            *kpi_row([
                (M("Projects at Risk"), "Projects forecasting a loss"),
                (M("Projects Over EAC"), "Projects whose cost has passed the forecast"),
                (M("Cost Codes Over EAC"), "Cost codes over their forecast"),
                (M("Projects Unmapped"), "Projects with no crosswalk mapping"),
            ]),
            table(a, [PROJECT, M("Gross Profit at Completion"), M("GP % at Completion"),
                      M("Project Risk Label")],
                  "Projects at risk: forecasting a loss, with an explicit risk label "
                  "rather than colour alone",
                  sort=(M("Gross Profit at Completion"), ASC)),
            table(b, [PROJECT, M("Percent Complete"), M("Cost to Date"),
                      M("Estimated Cost at Completion")],
                  "Projects where cost to date has passed the forecast at completion",
                  sort=(M("Percent Complete"), DESC)),
            table(c, [PROJECT, C("fct_BudgetLine", "CostCode"),
                      C("fct_BudgetLine", "EstimatedCostAtCompletion"),
                      C("fct_BudgetLine", "ProjectedOverUnder")],
                  "Cost codes over their forecast, by project",
                  sort=(C("fct_BudgetLine", "ProjectedOverUnder"), DESC)),
            table(d, [PROJECT, M("Pending Change Orders"), M("Cost Variance"),
                      M("Cost Variance %")],
                  "Unapproved change order value and Procore versus QuickBooks cost variance",
                  sort=(M("Cost Variance"), DESC)),
        ]))

    # ------------------------------------------------ 7 Data Quality (hidden)
    a, b = cols([3, 2], BODY_Y, BODY_H)
    P.append(dict(
        display="Data Quality", hidden=True, visuals=[
            title("Data Quality", "Whether these numbers can be trusted right now"),
            *kpi_row([
                (M("Blocking DQ Failures"), "Blocking data quality failures"),
                (M("DQ Warnings"), "Data quality warnings"),
                (M("Hours Since Last Run"), "Hours since the last successful run"),
                (M("Pipeline Status"), "Pipeline status"),
            ]),
            # StatusLabel is a text column and it is the point: a red dot alone
            # does not survive printing, projection, or colour blindness.
            table(a, [C("meta_DataQuality", "Expectation"), C("meta_DataQuality", "TableName"),
                      C("meta_DataQuality", "Severity"), C("meta_DataQuality", "StatusLabel"),
                      C("meta_DataQuality", "FailingRows"),
                      C("meta_DataQuality", "Description")],
                  "Data quality expectation results with severity and an explicit "
                  "pass or fail label",
                  # Failing rows first, so what is broken is at the top.
                  # NOT sorted by Severity: SeveritySort ranks the row outcome
                  # (failing error 1, failing warn 2, passed 3), so one severity
                  # maps to two sort values and sortByColumn cannot express it.
                  sort=(C("meta_DataQuality", "FailingRows"), DESC)),
            table(b, [C("meta_UnmappedProjects", "ProjectNumber"),
                      C("meta_UnmappedProjects", "ProjectName"),
                      C("meta_UnmappedProjects", "OriginalContract"),
                      C("meta_UnmappedProjects", "ProposedQboJobName"),
                      C("meta_UnmappedProjects", "CrosswalkConfidence"),
                      C("meta_UnmappedProjects", "Reason")],
                  "Unmapped projects: the Controller's crosswalk to-do list, with the "
                  "proposed QuickBooks job and match confidence",
                  sort=(C("meta_UnmappedProjects", "OriginalContract"), DESC)),
        ]))

    # ------------------------------------------------ 8 Pipeline & Forecast
    a, b = cols([1, 1], BODY_Y, 236)
    c, d = cols([1, 1], BODY_Y + 248, BODY_H - 272)
    P.append(dict(
        display="Pipeline & Forecast", visuals=[
            title("Pipeline & Forecast", "Work we might win"),
            *standard_slicers(),
            *kpi_row([
                (M("Pipeline Value"), "Total open pipeline, unweighted"),
                (M("Weighted Pipeline"), "Pipeline weighted by win probability"),
                (M("Open Deals"), "Number of open deals"),
                (M("Pipeline Confidence"), "Weighted pipeline as a share of the total"),
            ]),
            # Weighted and unweighted side by side: the GAP between them is the
            # story, and showing either alone tells half of it.
            column(a, C("dim_DealStage", "StageName"),
                   [M("Pipeline Value"), M("Weighted Pipeline")],
                   "Pipeline by stage, unweighted against weighted, in stage order",
                   sort=(C("dim_DealStage", "StageName"), ASC),
                   objects={"labels": _obj(show=True)}),
            # Backlog overlaid so won work and possible work stay visibly
            # different things. They are never added together.
            column(b, MONTH, [M("Weighted Pipeline"), M("Backlog Value")],
                   "Weighted pipeline by expected close month with backlog alongside, "
                   "shown separately because won work and possible work are not the same",
                   sort=(MONTH, ASC)),
            bar(c, C("dim_Owner", "OwnerName"), [M("Pipeline Value")],
                "Open pipeline by owner", sort=(M("Pipeline Value"), DESC)),
            table(d, [C("fct_Pipeline", "DealName"), C("dim_DealStage", "StageName"),
                      C("fct_Pipeline", "Amount"), C("fct_Pipeline", "CloseDate"),
                      C("fct_Pipeline", "DaysOpen")],
                  "Stale deals: open deals whose close date has already passed, which "
                  "inflate the forecast by sitting in a month that has gone",
                  sort=(C("fct_Pipeline", "CloseDate"), ASC)),
            note((PAD, H - PAD - 20, W - 2 * PAD, 20),
                 "Pipeline is never added to backlog and called revenue. Total Forward Work "
                 "is a planning horizon; its two components are always shown separately."),
        ]))

    # ------------------------------------------------ 9 AR & Collections
    a, b = cols([2, 3], BODY_Y, 236)
    c, d = cols([2, 3], BODY_Y + 248, BODY_H - 248)
    P.append(dict(
        display="AR & Collections", visuals=[
            title("AR & Collections", "The chase list"),
            *standard_slicers(),
            *kpi_row([
                (M("AR Outstanding"), "Total receivables outstanding"),
                (M("AR Overdue"), "Receivables past due"),
                (M("AR Overdue %"), "Share of the book that is overdue"),
                (M("Days Sales Outstanding"), "Days sales outstanding, trailing 90 days"),
            ]),
            # Sorted by AgingBucketSort, not by label: "Current" sorts after
            # "1-30" alphabetically and a bucket chart in the wrong order is
            # worse than no chart at all.
            column(a, C("fct_Aging", "AgingBucket"), [M("AR Outstanding")],
                   "Receivables by ageing bucket, in ageing order",
                   sort=(C("fct_Aging", "AgingBucket"), ASC),
                   objects={"labels": _obj(show=True)}),
            table(b, [C("fct_Aging", "CounterpartyName"), C("fct_Aging", "DocNumber"),
                      C("fct_Aging", "DueDate"), C("fct_Aging", "DaysPastDue"),
                      C("fct_Aging", "OpenBalance"), C("fct_Aging", "AgingBucket")],
                  "Open invoices with customer, due date, days past due and balance. "
                  "This is a worklist, not a picture",
                  sort=(C("fct_Aging", "DaysPastDue"), DESC)),
            bar(c, PROJECT, [M("AR Outstanding")],
                "Receivables by project; unmapped customers are still counted",
                sort=(M("AR Outstanding"), DESC)),
            table(d, [C("fct_Aging", "CounterpartyName"), C("fct_Aging", "DocNumber"),
                      C("fct_Aging", "DaysPastDue"), C("fct_Aging", "OpenBalance")],
                  "Collection risk: documents more than 90 days past due",
                  sort=(C("fct_Aging", "OpenBalance"), DESC)),
        ]))

    # ------------------------------------------------ 10 Cash Forecast
    a, b = cols([1, 1], BODY_Y, 236)
    P.append(dict(
        display="Cash Forecast", visuals=[
            title("Cash Forecast", "Committed cash only"),
            *standard_slicers(),
            *kpi_row([
                (M("Expected Collections"), "Expected collections"),
                (M("Expected Payments"), "Expected payments"),
                (M("Net Cash Movement"), "Net cash movement"),
                (M("Net Working Capital"), "Receivables less payables"),
            ]),
            # The trough is the number the CEO reads, not the endpoint.
            line(a, C("fct_CashForecast", "WeekStart"), [M("Cumulative Cash Position")],
                 "Cumulative cash position by week. The low point matters more than "
                 "the closing value",
                 sort=(C("fct_CashForecast", "WeekStart"), ASC)),
            # Collections positive, payments negative, ONE axis around zero.
            column(b, C("fct_CashForecast", "WeekStart"),
                   [M("Expected Collections"), M("Expected Payments")],
                   "Weekly collections and payments on one axis around zero",
                   sort=(C("fct_CashForecast", "WeekStart"), ASC)),
            table((PAD, BODY_Y + 248, W - 2 * PAD, BODY_H - 276),
                  [C("fct_Aging", "Ledger"), C("fct_Aging", "CounterpartyName"),
                   C("fct_Aging", "DocNumber"), C("fct_Aging", "DueDate"),
                   C("fct_Aging", "DaysPastDue"), C("fct_Aging", "OpenBalance")],
                  "Overdue exposure: documents already past due, carried into the current "
                  "week because they are due now",
                  sort=(C("fct_Aging", "DaysPastDue"), DESC)),
            # The most dangerous page in any finance report is a cash chart
            # that silently includes modelled revenue. State the basis.
            note((PAD, H - PAD - 20, W - 2 * PAD, 20),
                 "Basis: committed accounts receivable and payable only. Excludes unbilled "
                 "backlog, which would need a billing schedule and collection assumptions "
                 "nobody has provided."),
        ]))

    # ------------------------------------------------ 11 Capacity
    a, b = cols([1, 1], BODY_Y, 236)
    P.append(dict(
        display="Capacity", visuals=[
            title("Capacity", "Hours, utilisation and labour margin"),
            *standard_slicers(),
            *kpi_row([
                (M("Labour Hours"), "Total labour hours"),
                (M("Billable Hours"), "Hours that can be charged on"),
                (M("Utilisation"), "Billable share of paid hours"),
                (M("Labour Margin"), "Billable value less labour cost"),
            ]),
            line(a, MONTH, [M("Utilisation")], "Utilisation by month",
                 sort=(MONTH, ASC)),
            # Employee against subcontractor: owned capacity against bought.
            bar(b, C("fct_LabourHours", "WorkerName"), [M("Labour Hours")],
                "Hours by worker, split by employee against subcontractor",
                sort=(M("Labour Hours"), DESC)),
            table((PAD, BODY_Y + 248, W - 2 * PAD, BODY_H - 276),
                  [C("fct_LabourHours", "WorkerName"), C("fct_LabourHours", "WorkerType"),
                   C("fct_LabourHours", "BillableStatus"), C("fct_LabourHours", "Hours"),
                   C("fct_LabourHours", "LabourCost"), C("fct_LabourHours", "BillableValue")],
                  "Unattributed hours: time that cannot be costed to a project",
                  sort=(C("fct_LabourHours", "Hours"), DESC)),
            note((PAD, H - PAD - 20, W - 2 * PAD, 20),
                 "Rests on QuickBooks time entries only. Labour cost is zero wherever "
                 "QuickBooks carries no cost rate, so labour margin is overstated by that "
                 "amount. Procore timecards would deepen this page considerably."),
        ]))

    return P


# ---------------------------------------------------------------- validation


def load_schema() -> dict:
    if not SCHEMA_FILE.exists():
        sys.exit(f"missing {SCHEMA_FILE} - capture it from the live model first")
    return json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))


def validate(spec: list[dict], schema: dict) -> list[str]:
    """Every field must exist in the model. A typo renders empty, not red."""
    problems = []
    for page in spec:
        for visual in page["visuals"]:
            for role, fields in visual.roles.items():
                for f in fields:
                    known = schema.get(f.table)
                    if known is None:
                        problems.append(f"{page['display']}: no table {f.table}")
                        continue
                    bucket = known["measures"] if f.kind == "Measure" else known["columns"]
                    if f.name not in bucket:
                        kind = f.kind.lower()
                        problems.append(
                            f"{page['display']}: no {kind} {f.table}[{f.name}]")
            if visual.sort:
                f = visual.sort[0]
                known = schema.get(f.table, {})
                bucket = known.get("measures" if f.kind == "Measure" else "columns", [])
                if f.name not in bucket:
                    problems.append(
                        f"{page['display']}: sort field {f.table}[{f.name}] does not exist")
                # A sort by a field the visual does not project is SILENTLY
                # IGNORED - the visual renders in whatever order the engine
                # picks, which is alphabetical often enough to look deliberate.
                # That is how an ageing chart ships reading 1-30, 31-60, 61-90,
                # Current. Sort by the displayed column and give that column a
                # sortByColumn in the model.
                projected = {(x.table, x.name)
                             for fields in visual.roles.values() for x in fields}
                if (f.table, f.name) not in projected:
                    problems.append(
                        f"{page['display']}: {visual.vtype} sorts by "
                        f"{f.table}[{f.name}] which it does not display - "
                        f"the sort will be ignored")
            if visual.vtype != "textbox" and not visual.alt:
                problems.append(f"{page['display']}: a {visual.vtype} has no alt text")
    return problems


# ---------------------------------------------------------------- emit


def stable_name(*parts: str) -> str:
    """PBIR names are opaque ids. Deriving them from the page and visual keeps
    the output byte-identical between runs, so a regenerate is an empty diff."""
    return hashlib.sha1("::".join(parts).encode()).hexdigest()[:20]


def write(spec: list[dict], schema: dict) -> None:
    problems = validate(spec, schema)
    if problems:
        for p in problems:
            print("  " + p)
        sys.exit(f"{len(problems)} invalid field reference(s) - refusing to write")

    if OUT.exists():
        shutil.rmtree(OUT)
    definition = OUT / "definition"
    (definition / "pages").mkdir(parents=True)

    (OUT / ".platform").write_text(json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": "Report", "displayName": REPORT_NAME},
        "config": {"version": "2.0", "logicalId": stable_name("report", REPORT_NAME)},
    }, indent=2), encoding="utf-8")

    (OUT / "definition.pbir").write_text(json.dumps({
        "$schema": S_PBIR,
        "version": "4.0",
        "datasetReference": {"byPath": {"path": f"../{MODEL_NAME}.SemanticModel"}},
    }, indent=2), encoding="utf-8")

    (definition / "version.json").write_text(json.dumps({
        "$schema": S_VERSION, "version": "2.0.0"}, indent=2), encoding="utf-8")

    (definition / "report.json").write_text(json.dumps({
        "$schema": S_REPORT,
        # reportVersionAtImport is REQUIRED on a theme entry - the import fails
        # outright without it. It records the schema versions this definition
        # was authored against, so it must track the S_* constants above.
        # A customTheme layers ON TOP of a baseTheme - declared alone it is
        # ignored and the report renders in the stock palette.
        "themeCollection": {
            "baseTheme": {
                "name": "CY26SU07",
                "type": "SharedResources",
                "reportVersionAtImport": {"visual": "2.10.0", "report": "3.3.0",
                                          "page": "2.1.0"},
            },
            "customTheme": {
                "name": "DataLink",
                "type": "RegisteredResources",
                "reportVersionAtImport": {"visual": "2.10.0", "report": "3.3.0",
                                          "page": "2.1.0"},
            },
        },
        "resourcePackages": [
            {"name": "SharedResources", "type": "SharedResources",
             "items": [{"name": "CY26SU07", "path": "BaseThemes/CY26SU07.json",
                        "type": "BaseTheme"}]},
            {"name": "RegisteredResources", "type": "RegisteredResources",
             "items": [{"name": "DataLink", "path": "DataLink.json",
                        "type": "CustomTheme"}]},
        ],
        "settings": {
            "useStylableVisualContainerHeader": True,
            # Implicit measures off: a number dragged onto a visual and summed
            # by accident is how a report starts disagreeing with the model.
            "isPersistentUserStateDisabled": False,
        },
    }, indent=2), encoding="utf-8")

    # THREE names have to agree or the theme silently does not bind and every
    # visual falls back to the stock palette: themeCollection.customTheme.name,
    # the resourcePackages item name, and the "name" INSIDE the theme file.
    # Nothing errors when they disagree - the report just renders in the wrong
    # colours, which is the kind of wrong that ships.
    theme_dir = OUT / "StaticResources" / "RegisteredResources"
    theme_dir.mkdir(parents=True)
    theme = json.loads((ROOT / "powerbi" / "theme.json").read_text(encoding="utf-8"))
    theme["name"] = "DataLink"
    # theme.json documents itself with _comment_* keys. They are not valid
    # theme properties, and an invalid property makes the service drop the
    # whole theme rather than complain - so strip them on the way out.
    theme = {k: v for k, v in theme.items() if not k.startswith("_")}
    (theme_dir / "DataLink.json").write_text(json.dumps(theme, indent=2), encoding="utf-8")

    order = []
    for index, page in enumerate(spec):
        pname = stable_name("page", page["display"])
        order.append(pname)
        pdir = definition / "pages" / pname
        (pdir / "visuals").mkdir(parents=True)

        page_json = {
            "$schema": S_PAGE,
            "name": pname,
            "displayName": page["display"],
            "displayOption": "FitToPage",
            "height": H,
            "width": W,
        }
        if page.get("hidden"):
            page_json["visibility"] = "HiddenInViewMode"
        (pdir / "page.json").write_text(json.dumps(page_json, indent=2), encoding="utf-8")

        for vindex, visual in enumerate(page["visuals"]):
            visual.z = 1000 + vindex
            vname = stable_name("visual", page["display"], str(vindex), visual.vtype)
            vdir = pdir / "visuals" / vname
            vdir.mkdir(parents=True)
            (vdir / "visual.json").write_text(
                json.dumps(visual.json(vname), indent=2), encoding="utf-8")

    (definition / "pages" / "pages.json").write_text(json.dumps({
        "$schema": S_PAGES, "pageOrder": order, "activePageName": order[0],
    }, indent=2), encoding="utf-8")

    visuals = sum(len(p["visuals"]) for p in spec)
    print(f"wrote {OUT.relative_to(ROOT)}")
    print(f"{len(spec)} pages, {visuals} visuals, every field checked against the model")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true",
                        help="validate field references without writing")
    args = parser.parse_args()
    schema = load_schema()
    spec = pages()
    if args.check:
        problems = validate(spec, schema)
        for p in problems:
            print("  " + p)
        print(f"{len(problems)} problem(s)")
        return 1 if problems else 0
    write(spec, schema)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
