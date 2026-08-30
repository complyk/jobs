#!/usr/bin/env python3
"""Query the enterprise ATS tenants discovered by careers-page fingerprinting.

Oracle HCM, SuccessFactors, Taleo, Avature, Phenom and Eightfold use opaque
tenant codes, so they can only be queried once fingerprinting has revealed the
real host. This reads data/deep_sweep_results.json, reconstructs each tenant's
job API, and pulls UAE compliance roles with their posted dates.

Oracle HCM is the important one here: Emirates NBD, RAKBank, ADGM, DMCC, Emaar
and DP World all run it, and its REST API returns PostedDate directly.

Writes data/enterprise_sweep_results.json.
"""

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

import requests
import urllib3

urllib3.disable_warnings()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "7"))
NOW = datetime.now(timezone.utc)
CUTOFF = NOW - timedelta(days=WINDOW_DAYS)

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-GB,en;q=0.9",
}

KEYWORDS = re.compile(
    r"(mlro|money laundering|compliance|financial crime|\baml\b|\bcft\b|sanctions|"
    r"regulatory affairs|fincrime|regulatory)", re.I)
SENIOR = re.compile(
    r"(head\b|chief|director|\bvp\b|vice president|\bsvp\b|lead\b|senior|manager|"
    r"mlro|\bcco\b|principal|officer)", re.I)
JUNIOR = re.compile(r"(analyst|associate\b|intern\b|graduate|trainee|assistant|clerk)", re.I)
KEEP_ANYWAY = re.compile(r"(mlro|head\b|chief|director|\bvp\b|\bsvp\b|lead\b)", re.I)
UAE = re.compile(
    r"(\buae\b|united arab emirates|dubai|abu dhabi|difc|adgm|sharjah|"
    r"ras al khaimah|\bajman\b|fujairah|emirates)", re.I)

# Oracle HCM tenants confirmed by fingerprinting, plus known UAE-bank hosts.
ORACLE = [
    ("Emirates NBD", "fa-evlo-saasfaprod1.fa.ocs.oraclecloud.com", ["CX_1", "CX_2"]),
    ("RAKBank", "iacqey.fa.ocs.oraclecloud.com", ["CX_1"]),
    ("ADGM", "fa-eukk-saasfaprod1.fa.ocs.oraclecloud.com", ["CX_1"]),
    ("Dubai Multi Commodities Centre", "emag.fa.em8.oraclecloud.com", ["CX_1001", "CX_1"]),
    ("Emaar", "emhm.fa.em2.oraclecloud.com", ["CX_1001", "CX_1"]),
    ("DP World", "ehpv.fa.em2.oraclecloud.com", ["CX_1", "CX_1001"]),
    ("JPMorgan", "jpmc.fa.oraclecloud.com", ["CX_1001", "CX_1"]),
    ("First Abu Dhabi Bank", "ehjd.fa.em2.oraclecloud.com", ["CX_1", "CX_1001"]),
    ("Abu Dhabi Islamic Bank", "eijr.fa.em2.oraclecloud.com", ["CX_1"]),
    ("Mashreq", "emhx.fa.em2.oraclecloud.com", ["CX_1", "CX_1001"]),
]

SUCCESSFACTORS = [
    ("Pictet", "career012.successfactors.eu", "banquepict"),
    ("Equiti", "career2.successfactors.eu", "egmmarketiP2"),
    ("Abu Dhabi Commercial Bank", "career5.successfactors.eu", "adcb"),
]

TALEO = [("Societe Generale", "socgen", "sgcareers")]
AVATURE = [("Emirates Group", "emiratesjobs")]
EIGHTFOLD = [("Citi", "citi")]
PHENOM = [("DAMAC", "DPSDPNGLOBAL"), ("Majid Al Futtaim", "MAFMAFGLOBAL"),
          ("G42", "OGWOGJGLOBAL")]

hits, log = [], []


def note(m):
    log.append(m)
    print("  " + m, flush=True)


def to_dt(v):
    if not v:
        return None
    if isinstance(v, (int, float)):
        if v > 1e11:
            v /= 1000.0
        try:
            return datetime.fromtimestamp(v, tz=timezone.utc)
        except Exception:
            return None
    s = str(v).strip().replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for f in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d %b %Y", "%B %d, %Y", "%Y/%m/%d"):
        try:
            d = datetime.strptime(s, f)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def in_scope(title, blob):
    t = (title or "").strip()
    if not t or not KEYWORDS.search(t) or not SENIOR.search(t):
        return False
    if JUNIOR.search(t) and not KEEP_ANYWAY.search(t):
        return False
    return bool(UAE.search(blob))


def add(platform, employer, title, location, url, posted, evidence):
    d = to_dt(posted)
    hits.append({
        "platform": platform, "employer": employer, "title": str(title).strip(),
        "location": str(location or "").strip()[:120], "url": url or "",
        "posted_date": d.date().isoformat() if d else None, "evidence": evidence,
        "in_window": bool(d and d >= CUTOFF),
        "age_days": (NOW - d).days if d else None})


def sweep_oracle(item):
    employer, host, sites = item
    for site in sites:
        url = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
               f"?onlyData=true&expand=requisitionList.secondaryLocations"
               f"&finder=findReqs;siteNumber={site},limit=200,"
               f"sortBy=POSTING_DATES_DESC")
        try:
            r = requests.get(url, headers=HEADERS, timeout=30, verify=False)
            if r.status_code != 200:
                continue
            data = r.json()
        except Exception as e:
            continue
        reqs, total = [], 0
        for it in data.get("items", []):
            total = it.get("TotalJobsCount", total)
            reqs.extend(it.get("requisitionList", []))
        if not reqs:
            continue
        n = 0
        for q in reqs:
            title = q.get("Title")
            loc = (q.get("PrimaryLocation") or q.get("Location") or "")
            secondary = " ".join(
                str(s.get("Name", "")) for s in (q.get("secondaryLocations") or []))
            if in_scope(title, f"{loc} {secondary}"):
                jid = q.get("Id") or q.get("RequisitionId")
                add("oracle-hcm", employer, title, loc,
                    f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{jid}",
                    q.get("PostedDate"), "Oracle HCM PostedDate")
                n += 1
        return f"[oracle-hcm] {employer} ({site}): {len(reqs)} reqs listed, {n} UAE compliance"
    return f"[oracle-hcm] {employer}: no reachable site endpoint"


def sweep_successfactors(item):
    employer, host, company = item
    for path in (f"https://{host}/search?company={company}",
                 f"https://{host}/career?company={company}",
                 f"https://{host}/careers?company={company}"):
        try:
            r = requests.get(path, headers=HEADERS, timeout=30, verify=False)
            if r.status_code != 200 or len(r.text) < 500:
                continue
        except Exception:
            continue
        html = r.text
        n = 0
        for m in re.finditer(
                r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
            try:
                data = json.loads(m.group(1))
            except Exception:
                continue
            stack = [data]
            while stack:
                nd = stack.pop()
                if isinstance(nd, list):
                    stack.extend(nd)
                elif isinstance(nd, dict):
                    if "JobPosting" in str(nd.get("@type", "")):
                        loc = json.dumps(nd.get("jobLocation") or "")
                        if in_scope(nd.get("title"), loc):
                            add("successfactors", employer, nd.get("title"), loc[:100],
                                nd.get("url") or path, nd.get("datePosted"),
                                "SuccessFactors JSON-LD datePosted")
                            n += 1
                    stack.extend(v for v in nd.values() if isinstance(v, (dict, list)))
        # fall back to embedded job rows
        for m in re.finditer(r'"jobTitle"\s*:\s*"([^"]+)"[^}]*?"location"\s*:\s*"([^"]*)"'
                             r'[^}]*?"postedDate"\s*:\s*"([^"]*)"', html):
            if in_scope(m.group(1), m.group(2)):
                add("successfactors", employer, m.group(1), m.group(2), path,
                    m.group(3), "SuccessFactors postedDate")
                n += 1
        return f"[successfactors] {employer}: page ok, {n} UAE compliance"
    return f"[successfactors] {employer}: no reachable career page"


def sweep_taleo(item):
    employer, tenant, section = item
    for url in (f"https://{tenant}.taleo.net/careersection/rss/jobfeed.rss",
                f"https://{tenant}.taleo.net/careersection/{section}/jobsearch.ftl"):
        try:
            r = requests.get(url, headers=HEADERS, timeout=25, verify=False)
            if r.status_code != 200:
                continue
        except Exception:
            continue
        n = 0
        for m in re.finditer(r"<item>(.*?)</item>", r.text, re.S | re.I):
            blk = m.group(1)

            def tag(t):
                mm = re.search(rf"<{t}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{t}>", blk, re.S | re.I)
                return mm.group(1).strip() if mm else ""
            if in_scope(tag("title"), tag("title") + tag("description")):
                add("taleo", employer, tag("title"), "", tag("link"),
                    tag("pubDate"), "Taleo RSS pubDate")
                n += 1
        return f"[taleo] {employer}: ok, {n} UAE compliance"
    return f"[taleo] {employer}: no reachable feed"


def sweep_avature(item):
    employer, tenant = item
    for url in (f"https://{tenant}.avature.net/careers/SearchJobs/?listFilterMode=1&jobRecordsPerPage=100",
                f"https://{tenant}.avature.net/careers/SearchJobs"):
        try:
            r = requests.get(url, headers=HEADERS, timeout=25, verify=False)
            if r.status_code != 200:
                continue
        except Exception:
            continue
        n = 0
        for m in re.finditer(r'<a[^>]+href="([^"]*JobDetail[^"]*)"[^>]*>(.*?)</a>',
                             r.text, re.S | re.I):
            title = re.sub(r"<[^>]+>", " ", m.group(2)).strip()
            if in_scope(title, r.text[max(0, m.start() - 400):m.start() + 400]):
                add("avature", employer, title, "UAE",
                    f"https://{tenant}.avature.net{m.group(1)}", None,
                    "Avature listing (no date field)")
                n += 1
        return f"[avature] {employer}: ok, {n} UAE compliance"
    return f"[avature] {employer}: no reachable search page"


def sweep_eightfold(item):
    employer, tenant = item
    url = (f"https://{tenant}.eightfold.ai/api/apply/v2/jobs?domain={tenant}.com"
           f"&start=0&num=100&query=compliance&location=United%20Arab%20Emirates")
    try:
        r = requests.get(url, headers=HEADERS, timeout=25, verify=False)
        if r.status_code != 200:
            return f"[eightfold] {employer}: HTTP {r.status_code}"
        data = r.json()
    except Exception as e:
        return f"[eightfold] {employer}: {type(e).__name__}"
    positions = data.get("positions", [])
    n = 0
    for p in positions:
        loc = p.get("location") or " ".join(p.get("locations") or [])
        if in_scope(p.get("name"), str(loc)):
            add("eightfold", employer, p.get("name"), str(loc),
                p.get("canonicalPositionUrl") or p.get("positionUrl"),
                p.get("t_create"), "Eightfold t_create")
            n += 1
    return f"[eightfold] {employer}: ok ({len(positions)} positions, {n} UAE compliance)"


def sweep_phenom(item):
    employer, code = item
    for url in (f"https://{employer.lower().replace(' ', '')}.phenompeople.com/api/jobs"
                f"?keyword=compliance&location=United+Arab+Emirates&limit=100",):
        try:
            r = requests.get(url, headers=HEADERS, timeout=25, verify=False)
            if r.status_code != 200:
                continue
            data = r.json()
        except Exception:
            continue
        jobs = data.get("jobs", data.get("refineSearch", {}).get("data", {}).get("jobs", []))
        n = 0
        for j in jobs:
            jd = j.get("jobId") and j or j.get("job", j)
            if in_scope(jd.get("title"), str(jd.get("cityState") or jd.get("location") or "")):
                add("phenom", employer, jd.get("title"),
                    str(jd.get("cityState") or ""), jd.get("applyUrl") or jd.get("jobUrl"),
                    jd.get("postedDate"), "Phenom postedDate")
                n += 1
        return f"[phenom] {employer}: ok ({len(jobs)} jobs, {n} UAE compliance)"
    return f"[phenom] {employer}: no reachable API"


def main():
    print(f"Enterprise ATS sweep - cutoff {CUTOFF.date().isoformat()}\n", flush=True)
    print("=== Oracle HCM (UAE banks and authorities) ===", flush=True)
    with ThreadPoolExecutor(max_workers=10) as ex:
        for res in ex.map(sweep_oracle, ORACLE):
            note(res)
    print("\n=== SuccessFactors / Taleo / Avature / Eightfold / Phenom ===", flush=True)
    with ThreadPoolExecutor(max_workers=10) as ex:
        for fn, items in ((sweep_successfactors, SUCCESSFACTORS), (sweep_taleo, TALEO),
                          (sweep_avature, AVATURE), (sweep_eightfold, EIGHTFOLD),
                          (sweep_phenom, PHENOM)):
            for res in ex.map(fn, items):
                note(res)

    seen, uniq = set(), []
    for h in hits:
        k = (h["url"], h["title"], h["employer"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(h)
    uniq.sort(key=lambda h: (h["posted_date"] or ""), reverse=True)
    inwin = [h for h in uniq if h["in_window"]]

    print(f"\n=== {len(uniq)} UAE compliance roles on enterprise systems "
          f"({len(inwin)} in window) ===", flush=True)
    for h in uniq:
        print(f"  {'IN-WINDOW' if h['in_window'] else 'older    '} "
              f"{str(h['posted_date']):12} {h['employer'][:22]:22} "
              f"{h['title'][:56]:56} {h['platform']}", flush=True)

    with open(os.path.join(ROOT, "data", "enterprise_sweep_results.json"), "w") as f:
        json.dump({"run_utc": NOW.isoformat(), "cutoff": CUTOFF.date().isoformat(),
                   "window_days": WINDOW_DAYS, "hits": uniq, "log": log}, f, indent=2)
    print("\nWrote data/enterprise_sweep_results.json", flush=True)


if __name__ == "__main__":
    main()
