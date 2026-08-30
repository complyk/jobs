#!/usr/bin/env python3
"""Browser-based validation + discovery for the UAE senior-compliance radar.

Runs on a GitHub Actions runner (unrestricted egress) and drives a real
Chromium via Playwright, so bot-protected boards that refuse plain HTTP
(Bayt, GulfTalent, eFinancialCareers, jobaaj) render and can be read.

Three phases:
  1. VALIDATE  - open every candidate URL in the browser, read a first-published
                 date from the rendered DOM (JSON-LD datePosted first, then meta,
                 then visible relative text), and detect dead/expired postings.
  2. DISCOVER  - search UAE job boards for senior compliance / MLRO / financial
                 crime roles, collect job URLs, then open each job page and take
                 its date from the page itself, not the listing snippet.
  3. REPORT    - write data/validation_results.json.

Everything fails soft: a site that breaks is logged, never fatal.
"""

import json
import os
import re
import sys
import traceback
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "7"))
NOW = datetime.now(timezone.utc)
CUTOFF = NOW - timedelta(days=WINDOW_DAYS)
MAX_JOBS_PER_BOARD = int(os.environ.get("MAX_JOBS_PER_BOARD", "18"))

KEYWORDS = re.compile(
    r"(mlro|money laundering|compliance|financial crime|\baml\b|\bcft\b|sanctions)", re.I)
SENIOR = re.compile(
    r"(head\b|chief|director|\bvp\b|vice president|\bsvp\b|lead\b|senior manager|"
    r"senior compliance|mlro|\bcco\b|manager)", re.I)
STRICT_SENIOR = re.compile(
    r"(head\b|chief|director|\bvp\b|vice president|\bsvp\b|lead\b|senior|mlro|\bcco\b)", re.I)
UAE = re.compile(
    r"(uae|united arab emirates|dubai|abu dhabi|difc|adgm|sharjah|ras al khaimah|ajman)", re.I)
OUT_OF_SCOPE = re.compile(
    r"(analyst|associate\b|intern\b|graduate|assistant|coordinator|executive assistant|"
    r"internal audit|auditor|legal counsel|kyc officer|onboarding)", re.I)

DEAD_TEXT = re.compile(
    r"(no longer accepting|no longer available|job (?:has )?expired|position (?:has been )?filled|"
    r"this job is closed|posting (?:is )?closed|not found|page you.{0,20}looking for)", re.I)

RELATIVE = re.compile(r"(\d+)\+?\s*(minute|hour|day|week|month)s?\s+ago", re.I)
ABS_DATE = re.compile(
    r"(?:posted|published|date posted)[:\s]*"
    r"(\d{1,2}\s+\w{3,9}\s+\d{4}|\w{3,9}\s+\d{1,2},\s*\d{4}|\d{4}-\d{2}-\d{2})", re.I)


def parse_iso(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        if v > 1e11:
            v = v / 1000.0
        return datetime.fromtimestamp(v, tz=timezone.utc)
    s = str(v).strip().replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y",
                "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def extract_date(page):
    """Return (datetime, evidence_label, approximate) from the rendered page."""
    # 1. JSON-LD JobPosting.datePosted
    try:
        blocks = page.eval_on_selector_all(
            'script[type="application/ld+json"]', "els => els.map(e => e.textContent)")
    except Exception:
        blocks = []
    for raw in blocks:
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
                t = str(node.get("@type", ""))
                if "JobPosting" in t and node.get("datePosted"):
                    d = parse_iso(node["datePosted"])
                    if d:
                        return d, "JSON-LD datePosted", False
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
    # 2. embedded state / meta
    try:
        html = page.content()
    except Exception:
        html = ""
    for pat, lbl in (
        (r'"datePosted"\s*:\s*"([^"]+)"', "embedded datePosted"),
        (r'"postedDate"\s*:\s*"([^"]+)"', "embedded postedDate"),
        (r'"publishedAt"\s*:\s*"([^"]+)"', "embedded publishedAt"),
        (r'"published_on"\s*:\s*"([^"]+)"', "embedded published_on"),
        (r'"firstPublished"\s*:\s*"([^"]+)"', "embedded firstPublished"),
        (r'<meta[^>]+(?:property|name)=["\']article:published_time["\'][^>]+content=["\']([^"\']+)',
         "meta article:published_time"),
    ):
        m = re.search(pat, html, re.I)
        if m:
            d = parse_iso(m.group(1))
            if d:
                return d, lbl, False
    # 3. visible text
    try:
        text = page.inner_text("body")[:20000]
    except Exception:
        text = ""
    m = ABS_DATE.search(text)
    if m:
        d = parse_iso(m.group(1))
        if d:
            return d, f"page text '{m.group(0)[:40]}'", False
    m = RELATIVE.search(text)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = {"minute": timedelta(minutes=n), "hour": timedelta(hours=n),
                 "day": timedelta(days=n), "week": timedelta(weeks=n),
                 "month": timedelta(days=30 * n)}[unit]
        return NOW - delta, f"relative text '{m.group(0)}'", True
    return None, None, False


def open_page(ctx, url, wait_ms=2500):
    page = ctx.new_page()
    page.set_default_timeout(35000)
    status = None
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=35000)
        status = resp.status if resp else None
        try:
            page.wait_for_load_state("networkidle", timeout=6000)
        except Exception:
            pass
        page.wait_for_timeout(wait_ms)
    except Exception as e:
        return page, status, f"{type(e).__name__}: {str(e)[:120]}"
    return page, status, None


def validate_candidates(ctx, candidates):
    results = []
    for c in candidates:
        r = {"id": c["id"], "title": c["title"], "company": c["company"],
             "location": c.get("location"), "url": c["url"],
             "http_status": None, "posted_date": None, "evidence": None,
             "approximate": False, "verdict": None, "notes": []}
        urls = [c["url"]] + list(c.get("alt_urls", []))
        for i, u in enumerate(urls):
            page, status, err = open_page(ctx, u)
            try:
                if i == 0:
                    r["http_status"] = status
                if err:
                    r["notes"].append(f"{urlparse(u).netloc}: {err}")
                    continue
                try:
                    body = page.inner_text("body")[:6000]
                except Exception:
                    body = ""
                if status in (404, 410) or DEAD_TEXT.search(body[:3000]):
                    r["notes"].append(
                        f"{urlparse(u).netloc}: dead/expired (HTTP {status})")
                    if i == 0:
                        r["verdict"] = "dead"
                    continue
                d, lbl, approx = extract_date(page)
                if d:
                    r["posted_date"] = d.date().isoformat()
                    r["evidence"] = lbl + ("" if i == 0 else f" (via {urlparse(u).netloc})")
                    r["approximate"] = approx
                    r["verdict"] = None
                    break
            finally:
                try:
                    page.close()
                except Exception:
                    pass
        if r["verdict"] != "dead":
            if r["posted_date"]:
                d = parse_iso(r["posted_date"])
                r["verdict"] = "keep" if d >= CUTOFF else "remove-out-of-window"
                r["age_days"] = (NOW - d).days
            else:
                r["verdict"] = "remove-undated"
        print(f"  {r['verdict']:22} {str(r['http_status']):>5} "
              f"{r['company'][:26]:26} {str(r['posted_date']):12} "
              f"{str(r['evidence'])[:52]}", flush=True)
        results.append(r)
    return results


BOARDS = [
    ("bayt-mlro", "https://www.bayt.com/en/uae/jobs/mlro-jobs/", r"/en/uae/jobs/[^/]+-\d+/$"),
    ("bayt-head-compliance", "https://www.bayt.com/en/uae/jobs/head-of-compliance-jobs/",
     r"/en/uae/jobs/[^/]+-\d+/$"),
    ("bayt-financial-crime", "https://www.bayt.com/en/uae/jobs/financial-crime-jobs/",
     r"/en/uae/jobs/[^/]+-\d+/$"),
    ("bayt-head-aml", "https://www.bayt.com/en/uae/jobs/head-of-aml-jobs/",
     r"/en/uae/jobs/[^/]+-\d+/$"),
    ("gulftalent-compliance", "https://www.gulftalent.com/uae/jobs/title/compliance-officer",
     r"/uae/jobs/[a-z0-9-]+-\d+$"),
    ("gulftalent-search", "https://www.gulftalent.com/uae/jobs/q/mlro",
     r"/uae/jobs/[a-z0-9-]+-\d+$"),
    ("naukrigulf-compliance", "https://www.naukrigulf.com/compliance-jobs-in-uae",
     r"/[a-z0-9-]+-jobs?-in-[a-z-]+-[a-z0-9-]*\d+$"),
    ("efc-uae", "https://www.efinancialcareers.com/jobs/in-united-arab-emirates-gulf?q=compliance",
     r"/jobs-[A-Za-z_%\-]+\.id\d+"),
]


def discover(ctx):
    found, log = [], []
    seen = set()
    for name, url, href_pat in BOARDS:
        page, status, err = open_page(ctx, url, wait_ms=3500)
        try:
            if err or (status and status >= 400):
                log.append(f"{name}: FAILED (status={status}, {err or 'http error'})")
                continue
            try:
                anchors = page.eval_on_selector_all(
                    "a", "els => els.map(e => [e.href, (e.innerText||'').trim()])")
            except Exception as e:
                log.append(f"{name}: anchor scrape failed ({type(e).__name__})")
                continue
            cands = []
            pat = re.compile(href_pat)
            for href, text in anchors:
                if not href or not text or len(text) < 6:
                    continue
                if not pat.search(urlparse(href).path + ("?" if urlparse(href).query else "")) \
                        and not pat.search(href):
                    continue
                if not KEYWORDS.search(text) or not STRICT_SENIOR.search(text):
                    continue
                if OUT_OF_SCOPE.search(text):
                    continue
                if href in seen:
                    continue
                seen.add(href)
                cands.append((href, text))
            log.append(f"{name}: ok, {len(anchors)} links, {len(cands)} in-scope candidates")
            for href, text in cands[:MAX_JOBS_PER_BOARD]:
                jp, jstatus, jerr = open_page(ctx, href, wait_ms=1800)
                try:
                    if jerr or (jstatus and jstatus >= 400):
                        found.append({"title": text, "url": href, "board": name,
                                      "posted_date": None,
                                      "evidence": f"job page unreachable ({jstatus or jerr})",
                                      "location": None, "in_window": False})
                        continue
                    d, lbl, approx = extract_date(jp)
                    try:
                        btext = jp.inner_text("body")[:8000]
                    except Exception:
                        btext = ""
                    loc = ""
                    lm = re.search(
                        r"(Dubai|Abu Dhabi|Sharjah|DIFC|ADGM|Ras Al Khaimah|Ajman|"
                        r"United Arab Emirates|UAE)", btext, re.I)
                    if lm:
                        loc = lm.group(1)
                    rec = {
                        "title": text, "url": href, "board": name, "location": loc,
                        "posted_date": d.date().isoformat() if d else None,
                        "evidence": lbl, "approximate": approx,
                        "uae_confirmed": bool(UAE.search(btext)),
                        "in_window": bool(d and d >= CUTOFF),
                        "age_days": (NOW - d).days if d else None,
                    }
                    found.append(rec)
                    flag = "IN-WINDOW" if rec["in_window"] else "older    "
                    print(f"    {flag} {str(rec['posted_date']):12} {text[:64]}", flush=True)
                finally:
                    try:
                        jp.close()
                    except Exception:
                        pass
        finally:
            try:
                page.close()
            except Exception:
                pass
        print(f"  [{name}] {log[-1]}", flush=True)
    return found, log


def main():
    with open(os.path.join(ROOT, "data", "candidates.json")) as f:
        candidates = json.load(f)["candidates"]

    print(f"Window {WINDOW_DAYS}d, cutoff {CUTOFF.date().isoformat()}", flush=True)
    validated, discovered, dlog = [], [], []

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            viewport={"width": 1440, "height": 900},
            locale="en-GB",
            timezone_id="Asia/Dubai",
            extra_http_headers={"Accept-Language": "en-GB,en;q=0.9"},
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")

        print("\n=== PHASE 1: validating candidates in browser ===", flush=True)
        try:
            validated = validate_candidates(ctx, candidates)
        except Exception:
            traceback.print_exc()

        print("\n=== PHASE 2: board discovery ===", flush=True)
        try:
            discovered, dlog = discover(ctx)
        except Exception:
            traceback.print_exc()

        browser.close()

    keep = [r for r in validated if r["verdict"] == "keep"]
    fresh = [d for d in discovered if d.get("in_window")]
    print(f"\nSUMMARY: {len(keep)} candidates kept, {len(fresh)} fresh in-window "
          f"board roles, {len(discovered)} board roles inspected", flush=True)

    out = {
        "run_utc": NOW.isoformat(), "window_days": WINDOW_DAYS,
        "cutoff": CUTOFF.date().isoformat(), "method": "playwright-chromium on GH runner",
        "validated": validated, "discovered": discovered, "discovery_log": dlog,
    }
    with open(os.path.join(ROOT, "data", "validation_results.json"), "w") as f:
        json.dump(out, f, indent=2)
    print("Wrote data/validation_results.json", flush=True)


if __name__ == "__main__":
    main()
