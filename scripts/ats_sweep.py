#!/usr/bin/env python3
"""Wide ATS sweep for UAE senior-compliance roles, with real publish timestamps.

Runs on a GitHub Actions runner. Queries applicant-tracking-system APIs directly
(the earliest point a role becomes public, before it propagates to job boards)
and records each hit's authoritative timestamp:

    Lever            createdAt        - true first-publish
    Greenhouse       first_published  - true first-publish (falls back to updated_at)
    SmartRecruiters  releasedDate     - true first-publish
    Ashby            publishedAt      - true first-publish
    Workable         published_on     - true first-publish
    Workday          postedOn / startDate
    Oracle HCM       PostedDate

Bot-protected job boards (Bayt, GulfTalent, eFinancialCareers) refuse datacenter
IPs outright, so those are attempted through a text-extraction proxy that fetches
from its own address; failures are logged, never silently skipped.

Writes data/ats_sweep_results.json.
"""

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "7"))
NOW = datetime.now(timezone.utc)
CUTOFF = NOW - timedelta(days=WINDOW_DAYS)

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
}

KEYWORDS = re.compile(
    r"(mlro|money laundering|compliance|financial crime|\baml\b|\bcft\b|sanctions|"
    r"regulatory affairs)", re.I)
SENIOR = re.compile(
    r"(head\b|chief|director|\bvp\b|vice president|\bsvp\b|lead\b|senior|manager|"
    r"mlro|\bcco\b|principal)", re.I)
OUT_OF_SCOPE = re.compile(
    r"(analyst|associate\b|intern\b|graduate|assistant|coordinator|specialist|"
    r"internal audit|auditor|legal counsel|engineer|developer|data scien)", re.I)
UAE = re.compile(
    r"(\buae\b|united arab emirates|dubai|abu dhabi|difc|adgm|sharjah|"
    r"ras al khaimah|ajman|fujairah)", re.I)

hits, log = [], []


def note(msg):
    log.append(msg)
    print("  " + msg, flush=True)


def to_dt(v):
    if v in (None, "", 0):
        return None
    if isinstance(v, (int, float)):
        if v > 1e11:
            v = v / 1000.0
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
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d %b %Y", "%B %d, %Y",
                "%Y-%m-%dT%H:%M:%S%z", "%Y/%m/%d"):
        try:
            d = datetime.strptime(s, fmt)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def add(source, company, title, location, url, ts, evidence, extra_text=""):
    title = (title or "").strip()
    location = (location or "").strip()
    if not title or not KEYWORDS.search(title) or not SENIOR.search(title):
        return
    if OUT_OF_SCOPE.search(title) and not re.search(r"(mlro|head|chief|director)", title, re.I):
        return
    if not UAE.search(f"{location} {extra_text}"):
        return
    d = to_dt(ts)
    hits.append({
        "source": source, "company": company, "title": title, "location": location,
        "url": url, "posted_date": d.date().isoformat() if d else None,
        "evidence": evidence, "in_window": bool(d and d >= CUTOFF),
        "age_days": (NOW - d).days if d else None,
    })


def gj(url, **kw):
    return requests.get(url, headers=HEADERS, timeout=25, **kw)


# --------------------------------------------------------------- ATS handlers

GREENHOUSE = """bybit okx binance bitget kucoin gateio mexcglobal htx bingx
circleinternetfinancial ripplelabs chainalysis fireblocks paxosglobal geminitrust
krakendigitalassetexchange coinbase consensys anchoragedigital bitgo galaxydigital
copperco zodiamarkets wintermutetrading amberblockchain falconxbravo keyrock
flowtraders imc optiver dagangnexchange stripe checkoutcom rapyd nium thunes
airwallexglobal paysafe payoneer remitly wisecareers tabby tamara telr
network-international magnati pinelabs deel remotecom papayaglobal ebury ifxpayments
capitalcom xtb plus500 exness swissquote saxobank interactivebrokers ig-group
lmax cmcmarkets tickmill pepperstone axitrader fxpro thinkmarkets
mubadalacapital investcorp lunateagility adia gulfcapital shuaacapital
emiratesnbdgroup mashreqbank fabbank adcbbank rakbankuae wioBank zandbank
liv-bank yap alethenafinance sarwa baraka stashaway hubpay careemtech
propertyfinder bayut dubizzle noon talabat kitopi swvl fetchr huda
g42ai presight core42 space42 e-and etisalat du-telecom""".split()

LEVER = """capital ledger bitpanda matchmove rain-financial sarwaco stashaway
bitoasis fuze hubpay ziina mamopay telrpayments postpay spotii cashew
huspy sarwa lean-technologies tarabut fintech-galaxy nymcard
bitfury elliptic complyadvantage featurespace quantexa napier
tradeling floward instashop washmen careem""".split()

ASHBY = """LeanTech airwallex deribit tabby ziina mamo fuze zodia keyrock flowdesk
tarabut nymcard hubpay ramp mercury brex modernTreasury unit alloy persona
sardine sumsub veriff onfido trulioo socure chainalysis trmlabs elliptic
bitso lemon buenbit belo ripio""".split()

SMARTRECRUITERS = """Wise FirstAbuDhabiBank Etihad EmiratesNBD Mashreq adib
Talabat Careem Majid-Al-Futtaim Chalhoub AlFuttaim Emaar DAMAC Aldar
Publicis Accenture Visa Mastercard WesternUnion Adyen Klarna N26 Revolut
Bolt Wolt Delivery-Hero HelloFresh Zalando""".split()

WORKABLE = """bitoasis tap-payments hubpay wio yallacompare sarwa baraka
lean-technologies nymcard telr postpay spotii cashew huspy tarabut""".split()

# (tenant, site) pairs for Workday-hosted careers portals
WORKDAY = [("db", "DBWebsite"), ("hsbc", "External"), ("citi", "2"),
           ("standardchartered", "Careers"), ("ubs", "UBSCareers"),
           ("jpmc", "jpmc"), ("gs", "Goldman"), ("baml", "BankofAmerica"),
           ("mufg", "MUFG"), ("natwest", "NatWest"), ("barclays", "External"),
           ("visa", "Visa_External"), ("mastercard", "CorporateCareers"),
           ("pwc", "Global_Experienced_Careers"), ("kpmg", "KPMG"),
           ("aig", "AIG"), ("marsh", "MMC"), ("aon", "Aon")]


def sweep_greenhouse(board):
    try:
        r = gj(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs")
        if r.status_code != 200:
            return f"[greenhouse] {board}: HTTP {r.status_code}"
        jobs = r.json().get("jobs", [])
        n = 0
        for j in jobs:
            loc = (j.get("location") or {}).get("name", "")
            if UAE.search(loc):
                n += 1
            add("greenhouse", board, j.get("title", ""), loc,
                j.get("absolute_url", ""),
                j.get("first_published") or j.get("updated_at"),
                "Greenhouse first_published" if j.get("first_published")
                else "Greenhouse updated_at")
        return f"[greenhouse] {board}: ok ({len(jobs)} jobs, {n} UAE)"
    except Exception as e:
        return f"[greenhouse] {board}: {type(e).__name__}"


def sweep_lever(co):
    try:
        r = gj(f"https://api.lever.co/v0/postings/{co}?mode=json")
        if r.status_code != 200:
            return f"[lever] {co}: HTTP {r.status_code}"
        jobs = r.json()
        n = 0
        for j in jobs:
            loc = (j.get("categories") or {}).get("location", "") or ""
            if UAE.search(loc):
                n += 1
            add("lever", co, j.get("text", ""), loc, j.get("hostedUrl", ""),
                j.get("createdAt"), "Lever createdAt")
        return f"[lever] {co}: ok ({len(jobs)} jobs, {n} UAE)"
    except Exception as e:
        return f"[lever] {co}: {type(e).__name__}"


def sweep_ashby(co):
    try:
        r = gj(f"https://api.ashbyhq.com/posting-api/job-board/{co}")
        if r.status_code != 200:
            return f"[ashby] {co}: HTTP {r.status_code}"
        jobs = r.json().get("jobs", [])
        n = 0
        for j in jobs:
            loc = j.get("location", "") or ""
            if UAE.search(loc):
                n += 1
            add("ashby", co, j.get("title", ""), loc, j.get("jobUrl", ""),
                j.get("publishedAt") or j.get("updatedAt"), "Ashby publishedAt")
        return f"[ashby] {co}: ok ({len(jobs)} jobs, {n} UAE)"
    except Exception as e:
        return f"[ashby] {co}: {type(e).__name__}"


def sweep_smartrecruiters(co):
    try:
        r = gj(f"https://api.smartrecruiters.com/v1/companies/{co}/postings?limit=100")
        if r.status_code != 200:
            return f"[smartrecruiters] {co}: HTTP {r.status_code}"
        jobs = r.json().get("content", [])
        n = 0
        for j in jobs:
            loc = j.get("location", {}) or {}
            locs = " ".join(str(loc.get(k, "")) for k in ("city", "region", "country"))
            if UAE.search(locs):
                n += 1
            add("smartrecruiters", co, j.get("name", ""), locs.strip(),
                f"https://jobs.smartrecruiters.com/{co}/{j.get('id')}",
                j.get("releasedDate") or j.get("createdOn"),
                "SmartRecruiters releasedDate")
        return f"[smartrecruiters] {co}: ok ({len(jobs)} jobs, {n} UAE)"
    except Exception as e:
        return f"[smartrecruiters] {co}: {type(e).__name__}"


def sweep_workable(co):
    try:
        r = gj(f"https://apply.workable.com/api/v1/widget/accounts/{co}?details=true")
        if r.status_code != 200:
            return f"[workable] {co}: HTTP {r.status_code}"
        jobs = r.json().get("jobs", [])
        n = 0
        for j in jobs:
            loc = f"{j.get('city','')} {j.get('country','')}"
            if UAE.search(loc):
                n += 1
            add("workable", co, j.get("title", ""), loc, j.get("url", ""),
                j.get("published_on"), "Workable published_on")
        return f"[workable] {co}: ok ({len(jobs)} jobs, {n} UAE)"
    except Exception as e:
        return f"[workable] {co}: {type(e).__name__}"


def sweep_workday(pair):
    tenant, site = pair
    for host in (f"https://{tenant}.wd3.myworkdayjobs.com",
                 f"https://{tenant}.wd1.myworkdayjobs.com",
                 f"https://{tenant}.wd5.myworkdayjobs.com"):
        url = f"{host}/wday/cxs/{tenant}/{site}/jobs"
        try:
            r = requests.post(url, headers={**HEADERS, "Content-Type": "application/json"},
                              json={"appliedFacets": {}, "limit": 20, "offset": 0,
                                    "searchText": "compliance"}, timeout=25)
            if r.status_code != 200:
                continue
            data = r.json()
            posts = data.get("jobPostings", [])
            n = 0
            for j in posts:
                loc = j.get("locationsText", "") or ""
                if UAE.search(loc):
                    n += 1
                add("workday", tenant, j.get("title", ""), loc,
                    host + (j.get("externalPath") or ""),
                    j.get("startDate") or j.get("postedOn"),
                    f"Workday {'startDate' if j.get('startDate') else 'postedOn'}")
            return f"[workday] {tenant}/{site}: ok ({len(posts)} jobs, {n} UAE)"
        except Exception:
            continue
    return f"[workday] {tenant}/{site}: no reachable endpoint"


# --------------------------------------------- bot-blocked boards via proxy

BLOCKED_BOARDS = [
    ("bayt-mlro", "https://www.bayt.com/en/uae/jobs/mlro-jobs/"),
    ("bayt-head-compliance", "https://www.bayt.com/en/uae/jobs/head-of-compliance-jobs/"),
    ("bayt-head-aml", "https://www.bayt.com/en/uae/jobs/head-of-aml-jobs/"),
    ("gulftalent-compliance", "https://www.gulftalent.com/uae/jobs/title/compliance-officer"),
    ("naukrigulf-compliance", "https://www.naukrigulf.com/compliance-jobs-in-uae"),
    ("efc-uae", "https://www.efinancialcareers.com/jobs/in-united-arab-emirates-gulf?q=compliance"),
]

REL = re.compile(r"(\d+)\+?\s*(hour|day|week|month)s?\s+ago", re.I)


def sweep_blocked_board(item):
    name, url = item
    for proxy_name, proxy_url in (("r.jina.ai", f"https://r.jina.ai/{url}"),
                                  ("textance", f"https://urltomarkdown.herokuapp.com/?url={url}")):
        try:
            r = requests.get(proxy_url, headers={"User-Agent": HEADERS["User-Agent"],
                                                 "Accept": "text/plain,*/*"}, timeout=45)
            if r.status_code != 200 or len(r.text) < 500:
                continue
            text = r.text
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            found = 0
            for i, line in enumerate(lines):
                if not (KEYWORDS.search(line) and SENIOR.search(line)):
                    continue
                if OUT_OF_SCOPE.search(line) and not re.search(r"(mlro|head|chief|director)", line, re.I):
                    continue
                window = " ".join(lines[i:i + 6])
                m = REL.search(window)
                ts, ev = None, "no date in extracted listing"
                if m:
                    n, unit = int(m.group(1)), m.group(2).lower()
                    delta = {"hour": timedelta(hours=n), "day": timedelta(days=n),
                             "week": timedelta(weeks=n), "month": timedelta(days=30 * n)}[unit]
                    ts = (NOW - delta).isoformat()
                    ev = f"listing text '{m.group(0)}' via {proxy_name} (approximate)"
                urlm = re.search(r"\((https?://[^)]+)\)", window)
                link = urlm.group(1) if urlm else url
                title = re.sub(r"[\[\]#*]", "", line)[:120]
                add(f"board:{name}", name.split("-")[0], title, "UAE", link, ts, ev,
                    extra_text="UAE")
                found += 1
            return f"[board] {name}: ok via {proxy_name} ({found} in-scope lines)"
        except Exception as e:
            continue
    return f"[board] {name}: FAILED (datacenter IP blocked; proxies unavailable)"


def main():
    print(f"ATS sweep - window {WINDOW_DAYS}d, cutoff {CUTOFF.date().isoformat()}", flush=True)
    tasks = []
    with ThreadPoolExecutor(max_workers=16) as ex:
        for fn, items in ((sweep_greenhouse, GREENHOUSE), (sweep_lever, LEVER),
                          (sweep_ashby, ASHBY), (sweep_smartrecruiters, SMARTRECRUITERS),
                          (sweep_workable, WORKABLE), (sweep_workday, WORKDAY),
                          (sweep_blocked_board, BLOCKED_BOARDS)):
            for it in items:
                tasks.append(ex.submit(fn, it))
        for fut in as_completed(tasks):
            try:
                res = fut.result()
            except Exception as e:
                res = f"task error: {type(e).__name__}"
            if "ok (" in res and " 0 UAE)" in res:
                log.append(res)          # keep in log, don't spam stdout
            else:
                note(res)

    # de-duplicate on url
    seen, uniq = set(), []
    for h in hits:
        k = h["url"] or (h["company"], h["title"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(h)
    uniq.sort(key=lambda h: (h["posted_date"] or ""), reverse=True)

    inwin = [h for h in uniq if h["in_window"]]
    print(f"\n=== UAE senior-compliance hits: {len(uniq)} "
          f"({len(inwin)} within {WINDOW_DAYS} days) ===", flush=True)
    for h in uniq:
        print(f"  {'IN-WINDOW' if h['in_window'] else 'older    '} "
              f"{str(h['posted_date']):12} {h['company'][:16]:16} "
              f"{h['title'][:58]:58} {h['location'][:24]:24} {h['evidence'][:34]}",
              flush=True)

    ok = sum(1 for l in log if ": ok" in l)
    print(f"\nSources: {len(log)} attempted, {ok} reachable", flush=True)

    out = {"run_utc": NOW.isoformat(), "window_days": WINDOW_DAYS,
           "cutoff": CUTOFF.date().isoformat(), "hits": uniq, "log": sorted(log)}
    with open(os.path.join(ROOT, "data", "ats_sweep_results.json"), "w") as f:
        json.dump(out, f, indent=2)
    print("Wrote data/ats_sweep_results.json", flush=True)


if __name__ == "__main__":
    main()
