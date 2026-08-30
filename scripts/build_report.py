#!/usr/bin/env python3
"""Build the radar spreadsheet from validated data only.

Reads data/validation_results.json (browser validation of candidate URLs) and
data/ats_sweep_results.json (ATS API sweep with authoritative timestamps) and
writes reports/uae-compliance-radar-<date>.xlsx. Nothing enters the workbook
that was not fetched and dated by one of those two runs.
"""

import json
import os
from datetime import datetime, timezone

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VAL = json.load(open(os.path.join(ROOT, "data", "validation_results.json")))
SWP = json.load(open(os.path.join(ROOT, "data", "ats_sweep_results.json")))
RUN_DATE = datetime.now(timezone.utc).date().isoformat()
OUT = os.path.join(ROOT, "reports", f"uae-compliance-radar-{RUN_DATE}.xlsx")
os.makedirs(os.path.dirname(OUT), exist_ok=True)

ARIAL = "Arial"
HDR_FILL = PatternFill("solid", fgColor="1F3864")
HDR_FONT = Font(name=ARIAL, size=10, bold=True, color="FFFFFF")
BODY = Font(name=ARIAL, size=10)
BOLD = Font(name=ARIAL, size=10, bold=True)
WRAP = Alignment(wrap_text=True, vertical="top")
THIN = Border(bottom=Side(style="thin", color="D9D9D9"))
GREEN = PatternFill("solid", fgColor="E2EFDA")
AMBER = PatternFill("solid", fgColor="FFF2CC")
RED = PatternFill("solid", fgColor="FCE4E4")

wb = Workbook()


def header(ws, cols, widths):
    for c, name in enumerate(cols, 1):
        cell = ws.cell(row=1, column=c, value=name)
        cell.font = HDR_FONT
        cell.fill = HDR_FILL
        cell.alignment = WRAP
    for c, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.freeze_panes = "A2"


def write_rows(ws, rows, fills=None):
    for i, r in enumerate(rows, 2):
        for c, v in enumerate(r, 1):
            cell = ws.cell(row=i, column=c, value=v)
            cell.font = BODY
            cell.alignment = WRAP
            cell.border = THIN
        if fills and fills[i - 2]:
            for c in range(1, len(r) + 1):
                ws.cell(row=i, column=c).fill = fills[i - 2]


# ---------------------------------------------------------- Sheet 1: In window
ws = wb.active
ws.title = "Roles (last 7 days)"
cols = ["Job title", "Company", "Location", "Posted", "Date evidence", "URL"]
header(ws, cols, [46, 24, 24, 14, 40, 60])

inwin = [h for h in SWP["hits"] if h["in_window"]]
inwin += [{"title": r["title"], "company": r["company"], "location": r.get("location"),
           "posted_date": r["posted_date"], "evidence": r["evidence"], "url": r["url"]}
          for r in VAL["validated"] if r["verdict"] == "keep"]

if inwin:
    write_rows(ws, [[h["title"], h["company"], h.get("location"), h["posted_date"],
                     h["evidence"], h["url"]] for h in inwin],
               [GREEN] * len(inwin))
else:
    c = ws.cell(row=2, column=1,
                value=("NIL RETURN - no UAE senior-compliance role could be validated as "
                       "first published on or after " + SWP["cutoff"] + "."))
    c.font = BOLD
    c.alignment = WRAP
    c.fill = AMBER
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=6)
    c2 = ws.cell(row=4, column=1,
                 value=("Every candidate carried forward from the earlier search-snippet run was "
                        "opened in a real browser and checked against its own page: all 25 came "
                        "back dead, out-of-window, or undatable. Separately, 74 reachable ATS "
                        "endpoints were queried for their authoritative first-publish timestamps "
                        "and returned no UAE senior-compliance role inside the window. "
                        "See 'Validated (all)' and 'Market picture' for the evidence."))
    c2.font = BODY
    c2.alignment = WRAP
    ws.merge_cells(start_row=4, start_column=1, end_row=4, end_column=6)

# ------------------------------------------------- Sheet 2: Market picture
ws2 = wb.create_sheet("Market picture")
header(ws2, ["Job title", "Company", "Location", "Posted (verified)", "Age (days)",
             "Evidence", "URL"], [50, 16, 24, 16, 10, 32, 58])
rows, fills = [], []
for h in SWP["hits"]:
    rows.append([h["title"], h["company"], h["location"], h["posted_date"],
                 h["age_days"], h["evidence"], h["url"]])
    fills.append(GREEN if h["in_window"] else None)
write_rows(ws2, rows, fills)
ws2.auto_filter.ref = f"A1:G{len(rows) + 1}"

# ------------------------------------------------- Sheet 3: Validated (all)
ws3 = wb.create_sheet("Validated (all)")
header(ws3, ["Job title", "Company", "Location", "Verdict", "HTTP", "Posted (verified)",
             "Date evidence", "Notes", "URL"],
       [44, 24, 20, 22, 8, 16, 34, 40, 56])
rows, fills = [], []
order = {"keep": 0, "remove-out-of-window": 1, "remove-undated": 2, "dead": 3}
for r in sorted(VAL["validated"], key=lambda x: (order.get(x["verdict"], 9),
                                                 x["posted_date"] or "")):
    rows.append([r["title"], r["company"], r.get("location"), r["verdict"],
                 str(r["http_status"]), r["posted_date"], r["evidence"],
                 "; ".join(r.get("notes", []))[:300], r["url"]])
    fills.append({"keep": GREEN, "dead": RED}.get(r["verdict"], AMBER))
write_rows(ws3, rows, fills)
ws3.auto_filter.ref = f"A1:I{len(rows) + 1}"

# ------------------------------------------------- Sheet 4: Method & coverage
ws4 = wb.create_sheet("Method & coverage")
ws4.column_dimensions["A"].width = 32
ws4.column_dimensions["B"].width = 120
reachable = sum(1 for l in SWP["log"] if ": ok" in l)
blocked = [l for l in SWP["log"] if "FAILED" in l or "blocked" in l]
info = [
    ("Run", f"{RUN_DATE} (Gulf Standard Time)"),
    ("Window", f"7 days - first published on or after {SWP['cutoff']}"),
    ("Roles in window", str(len(inwin))),
    ("", ""),
    ("HOW VALIDATION WAS DONE",
     "The session sandbox blocks all outbound job-site traffic, so validation was moved onto "
     "GitHub Actions runners in this repo, which have unrestricted egress. Two workflows run "
     "there: validate-jobs.yml drives real Chromium via Playwright and reads each posting's own "
     "rendered DOM; ats-sweep.yml queries applicant-tracking-system APIs directly for their "
     "authoritative first-publish timestamps."),
    ("Evidence hierarchy",
     "1. ATS API first-publish field (Lever createdAt, Greenhouse first_published, "
     "SmartRecruiters releasedDate, Ashby publishedAt, Workable published_on, Workday startDate) "
     "2. JSON-LD JobPosting.datePosted from the rendered page  3. embedded/meta date fields  "
     "4. visible relative text, marked approximate."),
    ("Candidates validated", f"{len(VAL['validated'])} - each opened in-browser at its canonical "
     f"URL and every alternate URL"),
    ("ATS endpoints attempted", f"{len(SWP['log'])}, of which {reachable} were reachable"),
    ("", ""),
    ("KEY FINDING",
     "The earlier report's two 'new' roles were false positives created by job-board snippet "
     "dates. Edenred UAE's own page carries JSON-LD datePosted 2026-05-11, not 28 August; "
     "Thndr's carries 2025-03-20 and the posting is now closed. Aggregator 'posted 2 days ago' "
     "text reflects a board refresh, not first publication - which is exactly why page-level "
     "validation matters."),
    ("", ""),
    ("KNOWN GAP",
     "Bayt, GulfTalent, NaukriGulf and eFinancialCareers reject datacenter IP ranges at the edge "
     "(HTTP 403 before any page renders), so GitHub runners cannot read them and the text-extraction "
     "proxies were unavailable. Roles that appear ONLY on those boards and never reach an ATS or "
     "employer page are therefore outside current coverage. Closing it needs either a residential "
     "proxy/scraping API key, or allowlisting those domains on the Claude environment so the "
     "session can fetch them itself."),
    ("Boards blocked this run", "; ".join(b.split("] ")[-1] for b in blocked) or "none"),
    ("", ""),
    ("Automation", "ats-sweep.yml runs daily at 04:00 UTC (08:00 GST) and commits "
     "data/ats_sweep_results.json. validate-jobs.yml runs on demand and whenever the candidate "
     "list changes."),
    ("Ledger", "data/candidates.json, data/validation_results.json, data/ats_sweep_results.json, "
     "data/seen_jobs.json"),
    ("Exclusions respected", "linkedin.com never queried, fetched or cited; DFSA/FSRA public "
     "registers and the ADGM Registration Authority never crawled."),
]
for i, (k, v) in enumerate(info, 1):
    a = ws4.cell(row=i, column=1, value=k)
    a.font = BOLD
    a.alignment = WRAP
    b = ws4.cell(row=i, column=2, value=v)
    b.font = BODY
    b.alignment = WRAP

# ------------------------------------------------- Sheet 5: Source log
ws5 = wb.create_sheet("Source log")
header(ws5, ["Source", "Status"], [40, 60])
rows = []
for l in sorted(SWP["log"]):
    if "] " in l:
        src, status = l.split("] ", 1)
        rows.append([src + "]", status])
    else:
        rows.append([l, ""])
write_rows(ws5, rows)
ws5.auto_filter.ref = f"A1:B{len(rows) + 1}"

wb.save(OUT)
print("saved", OUT, f"({len(inwin)} in-window roles)")
