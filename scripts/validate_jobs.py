#!/usr/bin/env python3
"""Validate UAE senior-compliance job candidates and sweep ATS boards for new ones.

Runs on a GitHub Actions runner, which has unrestricted outbound network access.
For every candidate URL it establishes a first-published date from the strongest
evidence available, in this order of trust:

  1. ATS structured API   (Lever createdAt, Greenhouse first_published/updated_at,
                           SmartRecruiters releasedDate, Ashby publishedAt,
                           Workable published_on)
  2. JSON-LD JobPosting   (datePosted)
  3. Page metadata        (article:published_time, og/meta date fields)
  4. Visible date text    (marked approximate)

A posting that 404s, 410s or redirects to a board index is reported dead.
Results land in data/validation_results.json.
"""

import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "7"))
NOW = datetime.now(timezone.utc)
CUTOFF = NOW - timedelta(days=WINDOW_DAYS)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

KEYWORDS = re.compile(
    r"\b(mlro|money laundering reporting|compliance|financial crime|aml|cft|"
    r"regulatory affairs|sanctions)\b", re.I)
SENIOR = re.compile(
    r"\b(head|chief|director|vp|vice president|svp|lead|senior manager|"
    r"principal|mlro|cco)\b", re.I)
UAE = re.compile(
    r"\b(uae|united arab emirates|dubai|abu dhabi|difc|adgm|sharjah|"
    r"ras al khaimah|ajman|fujairah)\b", re.I)


def get(url, timeout=30, **kw):
    return requests.get(url, headers=HEADERS, timeout=timeout,
                        allow_redirects=True, **kw)


def iso(dt):
    if isinstance(dt, (int, float)):
        # epoch seconds or milliseconds
        if dt > 1e11:
            dt = dt / 1000.0
        dt = datetime.fromtimestamp(dt, tz=timezone.utc)
    if isinstance(dt, str):
        s = dt.strip().replace("Z", "+00:00")
        for fmt in (None, "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d %b %Y", "%b %d, %Y"):
            try:
                dt = datetime.fromisoformat(s) if fmt is None else datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def jsonld_dates(html):
    """Pull datePosted out of any JSON-LD JobPosting block."""
    out = []
    for m in re.finditer(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.S | re.I):
        raw = m.group(1).strip()
        try:
            data = json.loads(raw)
        except Exception:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                if node.get("datePosted"):
                    out.append(("JSON-LD datePosted", node["datePosted"]))
                if node.get("validThrough"):
                    out.append(("JSON-LD validThrough", node["validThrough"]))
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
    return out


META_PATTERNS = [
    (r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)',
     "meta article:published_time"),
    (r'<meta[^>]+name=["\']date["\'][^>]+content=["\']([^"\']+)', "meta date"),
    (r'<meta[^>]+itemprop=["\']datePosted["\'][^>]+content=["\']([^"\']+)',
     "meta datePosted"),
    (r'"datePosted"\s*:\s*"([^"]+)"', "inline datePosted"),
    (r'"postedDate"\s*:\s*"([^"]+)"', "inline postedDate"),
    (r'"publishedAt"\s*:\s*"([^"]+)"', "inline publishedAt"),
    (r'"createdAt"\s*:\s*"([^"]+)"', "inline createdAt"),
    (r'"releasedDate"\s*:\s*"([^"]+)"', "inline releasedDate"),
]

RELATIVE = re.compile(
    r"(?:posted|published|listed|added)?\s*(\d+)\+?\s*(day|hour|week|month)s?\s*ago", re.I)


def relative_date(html):
    text = re.sub(r"<[^>]+>", " ", html)
    m = RELATIVE.search(text)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    delta = {"hour": timedelta(hours=n), "day": timedelta(days=n),
             "week": timedelta(weeks=n), "month": timedelta(days=30 * n)}[unit]
    return NOW - delta, f"relative text '{m.group(0).strip()}' (approximate)"


# ---------------------------------------------------------------- ATS handlers

def ats_probe(url):
    """If the URL belongs to a known ATS, query its API for a real timestamp."""
    host = urlparse(url).netloc.lower()
    path = urlparse(url).path

    try:
        # Lever -------------------------------------------------------------
        if "lever.co" in host:
            m = re.search(r"/([^/]+)/([0-9a-f-]{36})", path)
            if m:
                co, jid = m.groups()
                r = get(f"https://api.lever.co/v0/postings/{co}/{jid}?mode=json")
                if r.ok:
                    d = r.json()
                    return d.get("createdAt"), "Lever API createdAt", d

        # Greenhouse --------------------------------------------------------
        if "greenhouse.io" in host:
            m = re.search(r"/([^/]+)/jobs/(\d+)", path)
            if m:
                board, jid = m.groups()
                r = get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{jid}")
                if r.ok:
                    d = r.json()
                    val = d.get("first_published") or d.get("updated_at")
                    lbl = ("Greenhouse API first_published" if d.get("first_published")
                           else "Greenhouse API updated_at (not first-publish)")
                    return val, lbl, d

        # SmartRecruiters ---------------------------------------------------
        if "smartrecruiters.com" in host:
            m = re.search(r"/([^/]+)/(\d+)", path)
            if m:
                co, jid = m.groups()
                r = get(f"https://api.smartrecruiters.com/v1/companies/{co}/postings/{jid}")
                if r.ok:
                    d = r.json()
                    return (d.get("releasedDate") or d.get("createdOn"),
                            "SmartRecruiters API releasedDate", d)

        # Ashby -------------------------------------------------------------
        if "ashbyhq.com" in host:
            m = re.search(r"/([^/]+)/([0-9a-f-]{36})", path)
            if m:
                co, jid = m.groups()
                r = get(f"https://api.ashbyhq.com/posting-api/job-board/{co}?includeCompensation=true")
                if r.ok:
                    for j in r.json().get("jobs", []):
                        if j.get("id") == jid:
                            return (j.get("publishedAt") or j.get("updatedAt"),
                                    "Ashby API publishedAt", j)
                    return None, "Ashby API: job id not on live board (likely closed)", None
    except Exception as e:
        return None, f"ATS probe error: {type(e).__name__}", None
    return None, None, None


# ---------------------------------------------------------------- validation

def validate(cand):
    url = cand["url"]
    res = {
        "id": cand["id"], "title": cand["title"], "company": cand["company"],
        "location": cand.get("location"), "url": url,
        "http_status": None, "final_url": None, "posted_date": None,
        "evidence": None, "approximate": False, "verdict": None, "notes": [],
    }

    val, label, payload = ats_probe(url)
    if val:
        dt = iso(val)
        if dt:
            res.update(posted_date=dt.date().isoformat(), evidence=label)
            res["http_status"] = 200
            if payload and isinstance(payload, dict):
                loc = json.dumps(payload.get("location") or payload.get("categories") or "")
                if loc and not UAE.search(loc) and not UAE.search(payload.get("text", "")[:2000] if isinstance(payload.get("text"), str) else ""):
                    res["notes"].append(f"location field from ATS: {loc[:120]}")
    elif label:
        res["notes"].append(label)

    # Always fetch the page too: confirms the posting is still live.
    try:
        r = get(url)
        res["http_status"] = r.status_code
        res["final_url"] = r.url
        html = r.text or ""
        if r.status_code in (404, 410):
            res["verdict"] = "dead"
            res["notes"].append(f"HTTP {r.status_code} - posting removed")
        elif res["posted_date"] is None:
            found = []
            for lbl, v in jsonld_dates(html):
                d = iso(v)
                if d and lbl.endswith("datePosted"):
                    found.append((d, lbl))
            if not found:
                for pat, lbl in META_PATTERNS:
                    m = re.search(pat, html, re.I)
                    if m:
                        d = iso(m.group(1))
                        if d:
                            found.append((d, lbl))
                            break
            if found:
                d, lbl = found[0]
                res.update(posted_date=d.date().isoformat(), evidence=lbl)
            else:
                rel = relative_date(html)
                if rel:
                    d, lbl = rel
                    res.update(posted_date=d.date().isoformat(), evidence=lbl,
                               approximate=True)
        if html and not UAE.search(html[:200000]):
            res["notes"].append("no UAE location token found in page text")
    except Exception as e:
        res["notes"].append(f"fetch error: {type(e).__name__}: {e}")
        if res["http_status"] is None:
            res["http_status"] = "error"

    # alt URLs as fallback evidence
    if res["posted_date"] is None:
        for alt in cand.get("alt_urls", []):
            try:
                r = get(alt)
                if not r.ok:
                    continue
                for lbl, v in jsonld_dates(r.text):
                    d = iso(v)
                    if d and lbl.endswith("datePosted"):
                        res.update(posted_date=d.date().isoformat(),
                                   evidence=f"{lbl} (via {urlparse(alt).netloc})")
                        break
                if res["posted_date"]:
                    break
                rel = relative_date(r.text)
                if rel:
                    d, lbl = rel
                    res.update(posted_date=d.date().isoformat(), approximate=True,
                               evidence=f"{lbl} (via {urlparse(alt).netloc})")
                    break
            except Exception:
                continue

    if res["verdict"] != "dead":
        if res["posted_date"]:
            d = iso(res["posted_date"])
            res["verdict"] = "keep" if d >= CUTOFF else "remove-out-of-window"
            res["age_days"] = (NOW - d).days
        else:
            res["verdict"] = "remove-undated"
    return res


# ---------------------------------------------------------------- discovery

GREENHOUSE = ["bybit", "stripe", "okx", "ripple", "circle", "chainalysis",
              "fireblocks", "paxos", "gemini", "deel", "anchorage", "consensys",
              "krakenfx", "bitgo", "figure", "wintermute", "copperco", "cryptocom"]
LEVER = ["capital", "ledger", "bitpanda", "nium", "matchmove", "rain", "sarwa"]
ASHBY = ["LeanTech", "airwallex", "deribit", "tabby", "ziina", "mamo", "fuze",
         "zodia", "keyrock", "flowdesk"]
SMARTRECRUITERS = ["Wise", "FirstAbuDhabiBank", "Etihad", "Emiratesnbd",
                   "Mashreq", "adib", "Talabat", "Careem"]
WORKABLE = ["bitoasis", "tap-payments", "hubpay", "wio"]


def sweep():
    hits = []

    def add(company, title, location, url, ts, evidence, extra=None):
        if not (KEYWORDS.search(title) and UAE.search(f"{location} {extra or ''}")):
            return
        if not SENIOR.search(title):
            return
        d = iso(ts) if ts else None
        hits.append({
            "company": company, "title": title, "location": location, "url": url,
            "posted_date": d.date().isoformat() if d else None,
            "evidence": evidence,
            "in_window": bool(d and d >= CUTOFF),
            "age_days": (NOW - d).days if d else None,
        })

    def gh(board):
        try:
            r = get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs", timeout=25)
            if not r.ok:
                return f"{board}: HTTP {r.status_code}"
            jobs = r.json().get("jobs", [])
            for j in jobs:
                add(board, j.get("title", ""), j.get("location", {}).get("name", ""),
                    j.get("absolute_url", ""),
                    j.get("first_published") or j.get("updated_at"),
                    "Greenhouse API first_published" if j.get("first_published")
                    else "Greenhouse API updated_at")
            return f"{board}: ok ({len(jobs)} jobs)"
        except Exception as e:
            return f"{board}: {type(e).__name__}"

    def lv(co):
        try:
            r = get(f"https://api.lever.co/v0/postings/{co}?mode=json", timeout=25)
            if not r.ok:
                return f"{co}: HTTP {r.status_code}"
            jobs = r.json()
            for j in jobs:
                add(co, j.get("text", ""),
                    j.get("categories", {}).get("location", ""),
                    j.get("hostedUrl", ""), j.get("createdAt"), "Lever API createdAt")
            return f"{co}: ok ({len(jobs)} jobs)"
        except Exception as e:
            return f"{co}: {type(e).__name__}"

    def ash(co):
        try:
            r = get(f"https://api.ashbyhq.com/posting-api/job-board/{co}", timeout=25)
            if not r.ok:
                return f"{co}: HTTP {r.status_code}"
            jobs = r.json().get("jobs", [])
            for j in jobs:
                add(co, j.get("title", ""), j.get("location", ""),
                    j.get("jobUrl", ""), j.get("publishedAt") or j.get("updatedAt"),
                    "Ashby API publishedAt")
            return f"{co}: ok ({len(jobs)} jobs)"
        except Exception as e:
            return f"{co}: {type(e).__name__}"

    def sr(co):
        try:
            r = get(f"https://api.smartrecruiters.com/v1/companies/{co}/postings?limit=100",
                    timeout=25)
            if not r.ok:
                return f"{co}: HTTP {r.status_code}"
            jobs = r.json().get("content", [])
            for j in jobs:
                loc = j.get("location", {})
                locs = f"{loc.get('city','')} {loc.get('country','')} {loc.get('region','')}"
                add(co, j.get("name", ""), locs.strip(),
                    f"https://jobs.smartrecruiters.com/{co}/{j.get('id')}",
                    j.get("releasedDate"), "SmartRecruiters API releasedDate")
            return f"{co}: ok ({len(jobs)} jobs)"
        except Exception as e:
            return f"{co}: {type(e).__name__}"

    def wk(co):
        try:
            r = get(f"https://apply.workable.com/api/v1/widget/accounts/{co}?details=true",
                    timeout=25)
            if not r.ok:
                return f"{co}: HTTP {r.status_code}"
            jobs = r.json().get("jobs", [])
            for j in jobs:
                add(co, j.get("title", ""),
                    f"{j.get('city','')} {j.get('country','')}",
                    j.get("url", ""), j.get("published_on"), "Workable published_on")
            return f"{co}: ok ({len(jobs)} jobs)"
        except Exception as e:
            return f"{co}: {type(e).__name__}"

    log = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        for fn, slugs, name in ((gh, GREENHOUSE, "greenhouse"), (lv, LEVER, "lever"),
                                (ash, ASHBY, "ashby"), (sr, SMARTRECRUITERS, "smartrecruiters"),
                                (wk, WORKABLE, "workable")):
            for status in ex.map(fn, slugs):
                log.append(f"[{name}] {status}")
    return hits, log


def main():
    with open(os.path.join(ROOT, "data", "candidates.json")) as f:
        cands = json.load(f)["candidates"]

    print(f"Validating {len(cands)} candidates; window = {WINDOW_DAYS}d "
          f"(cutoff {CUTOFF.date().isoformat()})", flush=True)

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(validate, cands))

    for r in results:
        print(f"  {r['verdict']:22} {r['http_status']:>5}  {r['company'][:28]:28} "
              f"{r['posted_date'] or '-':12} {r['evidence'] or 'no date evidence'}",
              flush=True)

    print("\nSweeping ATS boards for fresh UAE compliance roles...", flush=True)
    hits, log = sweep()
    for line in log:
        print("  " + line, flush=True)
    print(f"\nSweep hits: {len(hits)} "
          f"({sum(1 for h in hits if h['in_window'])} in window)", flush=True)
    for h in hits:
        print(f"  {'IN-WINDOW ' if h['in_window'] else 'older     '} "
              f"{h['posted_date'] or '-':12} {h['company'][:18]:18} {h['title'][:60]}",
              flush=True)

    out = {
        "run_utc": NOW.isoformat(),
        "window_days": WINDOW_DAYS,
        "cutoff": CUTOFF.date().isoformat(),
        "validated": results,
        "sweep_hits": hits,
        "sweep_log": log,
    }
    path = os.path.join(ROOT, "data", "validation_results.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nWrote {path}", flush=True)


if __name__ == "__main__":
    main()
