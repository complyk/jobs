#!/usr/bin/env python3
"""Close the two gaps left by the registry sweep.

Part A - DATE RESOLUTION. Hits whose platform exposed no publish field are
opened directly and dated from their own page (JSON-LD datePosted first).

Part B - CAREERS-PAGE FINGERPRINTING. Enterprise ATS tenants (Oracle HCM,
SuccessFactors, Taleo, iCIMS, Phenom, Avature, Eightfold) use opaque tenant
codes that cannot be guessed from a company name, which is why most UAE banks
came back unmapped. So instead of guessing: fetch each employer's real careers
page, follow redirects, fingerprint which ATS it runs from the final URL and
page source, extract the tenant code, and query that system properly. Any
JobPosting JSON-LD on the page is harvested at the same time.

Writes data/deep_sweep_results.json.
"""

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, urljoin

import requests
import urllib3

urllib3.disable_warnings()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "7"))
NOW = datetime.now(timezone.utc)
CUTOFF = NOW - timedelta(days=WINDOW_DAYS)
TIMEOUT = 20

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

KEYWORDS = re.compile(
    r"(mlro|money laundering|compliance|financial crime|\baml\b|\bcft\b|sanctions|"
    r"regulatory affairs|fincrime)", re.I)
SENIOR = re.compile(
    r"(head\b|chief|director|\bvp\b|vice president|\bsvp\b|lead\b|senior|manager|"
    r"mlro|\bcco\b|principal|officer)", re.I)
JUNIOR = re.compile(r"(analyst|associate\b|intern\b|graduate|trainee|assistant|clerk)", re.I)
KEEP_ANYWAY = re.compile(r"(mlro|head\b|chief|director|\bvp\b|\bsvp\b|lead\b)", re.I)
UAE = re.compile(
    r"(\buae\b|united arab emirates|dubai|abu dhabi|difc|adgm|sharjah|"
    r"ras al khaimah|\bajman\b|fujairah)", re.I)

# Employer -> corporate domain, for careers-page fingerprinting.
DOMAINS = {
    "First Abu Dhabi Bank": "bankfab.com", "Emirates NBD": "emiratesnbd.com",
    "Abu Dhabi Commercial Bank": "adcb.com", "Mashreq": "mashreq.com",
    "Abu Dhabi Islamic Bank": "adib.ae", "Dubai Islamic Bank": "dib.ae",
    "RAKBank": "rakbank.ae", "Commercial Bank of Dubai": "cbd.ae",
    "National Bank of Fujairah": "nbf.ae", "Emirates Islamic": "emiratesislamic.ae",
    "Ajman Bank": "ajmanbank.ae", "Sharjah Islamic Bank": "sib.ae",
    "United Arab Bank": "uab.ae", "Invest Bank": "investbank.ae",
    "Wio Bank": "wio.io", "Zand Bank": "zand.ae",
    "Al Maryah Community Bank": "almaryahbank.ae",
    "HSBC": "hsbc.com", "Standard Chartered": "sc.com", "Citi": "citi.com",
    "Barclays": "barclays.com", "Deutsche Bank": "db.com", "JPMorgan": "jpmorganchase.com",
    "Goldman Sachs": "goldmansachs.com", "Morgan Stanley": "morganstanley.com",
    "BNP Paribas": "bnpparibas.com", "Societe Generale": "societegenerale.com",
    "Credit Agricole": "credit-agricole.com", "Julius Baer": "juliusbaer.com",
    "UBS": "ubs.com", "Lombard Odier": "lombardodier.com", "Pictet": "pictet.com",
    "EFG Hermes": "efghermes.com", "Arqaam Capital": "arqaamcapital.com",
    "SHUAA Capital": "shuaa.com", "Emirates Investment Bank": "eibank.com",
    "Dubai Financial Market": "dfm.ae", "Abu Dhabi Securities Exchange": "adx.ae",
    "Nasdaq Dubai": "nasdaqdubai.com", "DIFC": "difc.ae", "ADGM": "adgm.com",
    "Dubai Multi Commodities Centre": "dmcc.ae", "VARA": "vara.ae",
    "Securities and Commodities Authority": "sca.gov.ae",
    "Central Bank of the UAE": "centralbank.ae", "DFSA": "dfsa.ae",
    "Mubadala": "mubadala.com", "ADQ": "adq.ae", "ADIA": "adia.ae",
    "Lunate": "lunate.com", "Investcorp": "investcorp.com", "Gulf Capital": "gulfcapital.com",
    "Waha Capital": "wahacapital.ae", "Alpha Dhabi": "alphadhabi.ae",
    "International Holding Company": "ihcuae.com", "Dubai Holding": "dubaiholding.com",
    "Amanat Holdings": "amanat.com", "Daman Investments": "daman.ae",
    "Sukoon Insurance": "sukoon.com", "Salama": "salama.ae", "GIG Gulf": "giggulf.ae",
    "Orient Insurance": "insuranceuae.com", "Emirates Insurance": "eminsco.com",
    "Abu Dhabi National Insurance": "adnic.ae", "Dubai Insurance": "dubins.ae",
    "Takaful Emarat": "takafulemarat.com", "Union Insurance": "unioninsurance.ae",
    "Network International": "network.ae", "Magnati": "magnati.com",
    "Emaar": "emaar.com", "DAMAC": "damacproperties.com", "Aldar": "aldar.com",
    "Majid Al Futtaim": "majidalfuttaim.com", "Chalhoub Group": "chalhoubgroup.com",
    "Al Futtaim": "alfuttaim.com", "Emirates Group": "emirates.com",
    "Etihad Airways": "etihad.com", "DP World": "dpworld.com", "AD Ports": "adports.ae",
    "ADNOC": "adnoc.ae", "Masdar": "masdar.ae", "TAQA": "taqa.com",
    "Etisalat": "etisalat.ae", "du": "du.ae", "G42": "g42.ai",
    "Careem": "careem.com", "Noon": "noon.com", "Talabat": "talabat.com",
    "Property Finder": "propertyfinder.ae", "Binance": "binance.com",
    "Crypto.com": "crypto.com", "Kraken": "kraken.com", "Deribit": "deribit.com",
    "M2": "m2.com", "Multibank": "multibankfx.com", "Hex Trust": "hextrust.com",
    "Komainu": "komainu.com", "Laser Digital": "laserdigital.com",
    "Equiti": "equiti.com", "ADSS": "adss.com", "Century Financial": "century.ae",
    "Michael Page": "michaelpage.ae", "Robert Walters": "robertwalters.ae",
    "Hays": "hays.ae", "Charterhouse": "charterhouseme.com",
    "Cooper Fitch": "cooperfitch.ae", "Selby Jennings": "selbyjennings.com",
    "Taylor Root": "taylorroot.com", "Barclay Simpson": "barclaysimpson.com",
    "Halian": "halian.com", "Korn Ferry": "kornferry.com",
}

# ATS fingerprints: (regex over final URL + page source) -> platform name
FINGERPRINTS = [
    (r"myworkdayjobs\.com|workdayjobs", "workday"),
    (r"oraclecloud\.com|/hcmUI/CandidateExperience|hcmRestApi", "oracle-hcm"),
    (r"successfactors\.(com|eu)|career\d*\.successfactors", "successfactors"),
    (r"taleo\.net", "taleo"),
    (r"icims\.com", "icims"),
    (r"phenompeople\.com|phenom\.com", "phenom"),
    (r"avature\.net", "avature"),
    (r"eightfold\.ai", "eightfold"),
    (r"csod\.com|cornerstoneondemand", "cornerstone"),
    (r"greenhouse\.io", "greenhouse"),
    (r"lever\.co", "lever"),
    (r"ashbyhq\.com", "ashby"),
    (r"smartrecruiters\.com", "smartrecruiters"),
    (r"workable\.com", "workable"),
    (r"recruitee\.com", "recruitee"),
    (r"teamtailor\.com", "teamtailor"),
    (r"bamboohr\.com", "bamboohr"),
    (r"personio\.(de|com)", "personio"),
    (r"pinpointhq\.com", "pinpoint"),
    (r"jobvite\.com", "jobvite"),
    (r"dayforcehcm\.com", "dayforce"),
    (r"recruiting\.ultipro\.com", "ultipro"),
    (r"paycomonline\.net", "paycom"),
    (r"darwinbox\.(in|com)", "darwinbox"),
    (r"zohorecruit\.com", "zoho-recruit"),
    (r"peoplehr|breezy\.hr", "breezy"),
]

CAREER_PATHS = ["/careers", "/en/careers", "/careers/", "/about-us/careers",
                "/en/about-us/careers", "/careers/job-search", "/jobs"]

results = {"dated": [], "fingerprints": [], "new_hits": [], "log": []}


def log(m):
    results["log"].append(m)
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
    for f in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d %b %Y", "%B %d, %Y",
              "%a, %d %b %Y %H:%M:%S %z", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            d = datetime.strptime(s, f)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def jsonld_postings(html):
    out = []
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>',
                         html, re.S | re.I):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        stack = [data]
        while stack:
            n = stack.pop()
            if isinstance(n, list):
                stack.extend(n)
            elif isinstance(n, dict):
                if "JobPosting" in str(n.get("@type", "")):
                    loc = n.get("jobLocation")
                    locs = json.dumps(loc) if loc else ""
                    out.append({"title": n.get("title"), "location": locs[:200],
                                "url": n.get("url") or n.get("@id") or "",
                                "posted": n.get("datePosted")})
                stack.extend(v for v in n.values() if isinstance(v, (dict, list)))
    return out


def in_scope(title, blob):
    t = (title or "").strip()
    if not t or not KEYWORDS.search(t) or not SENIOR.search(t):
        return False
    if JUNIOR.search(t) and not KEEP_ANYWAY.search(t):
        return False
    return bool(UAE.search(blob))


# ------------------------------------------------------------- Part A: dates

def resolve_date(hit):
    url = hit.get("url")
    if not url:
        return {**hit, "resolved_date": None, "resolved_evidence": "no url"}
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, verify=False,
                         allow_redirects=True)
        if r.status_code >= 400:
            return {**hit, "resolved_date": None,
                    "resolved_evidence": f"HTTP {r.status_code} (dead or blocked)"}
        html = r.text
        for p in jsonld_postings(html):
            d = to_dt(p.get("posted"))
            if d:
                return {**hit, "resolved_date": d.date().isoformat(),
                        "resolved_evidence": "JSON-LD datePosted",
                        "in_window": d >= CUTOFF}
        for pat, lbl in ((r'"datePosted"\s*:\s*"([^"]+)"', "embedded datePosted"),
                         (r'"publishedAt"\s*:\s*"([^"]+)"', "embedded publishedAt"),
                         (r'"published_on"\s*:\s*"([^"]+)"', "embedded published_on"),
                         (r'"createdAt"\s*:\s*"([^"]+)"', "embedded createdAt"),
                         (r'"postedDate"\s*:\s*"([^"]+)"', "embedded postedDate")):
            m = re.search(pat, html, re.I)
            if m:
                d = to_dt(m.group(1))
                if d:
                    return {**hit, "resolved_date": d.date().isoformat(),
                            "resolved_evidence": lbl, "in_window": d >= CUTOFF}
        return {**hit, "resolved_date": None,
                "resolved_evidence": "page fetched, no date field present"}
    except Exception as e:
        return {**hit, "resolved_date": None,
                "resolved_evidence": f"fetch error: {type(e).__name__}"}


# ----------------------------------------------- Part B: careers fingerprint

def fingerprint(item):
    employer, domain = item
    tried = []
    for scheme_host in (f"https://careers.{domain}", f"https://www.{domain}",
                        f"https://{domain}"):
        paths = [""] if scheme_host.startswith("https://careers.") else CAREER_PATHS
        for path in paths:
            url = scheme_host + path
            tried.append(url)
            try:
                r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, verify=False,
                                 allow_redirects=True)
            except Exception:
                continue
            if r.status_code >= 400:
                continue
            blob = (r.url or "") + " " + r.text[:400000]
            plat = None
            for pat, name in FINGERPRINTS:
                if re.search(pat, blob, re.I):
                    plat = name
                    break
            # harvest any JobPosting JSON-LD on the page itself
            for p in jsonld_postings(r.text):
                if in_scope(p.get("title"), f"{p.get('location','')} {p.get('url','')}"):
                    d = to_dt(p.get("posted"))
                    results["new_hits"].append({
                        "employer": employer, "title": p["title"],
                        "location": p.get("location", "")[:120],
                        "url": p.get("url") or r.url,
                        "posted_date": d.date().isoformat() if d else None,
                        "evidence": "careers page JSON-LD datePosted",
                        "in_window": bool(d and d >= CUTOFF),
                        "platform": plat or "careers-page"})
            if plat:
                tenant = None
                m = re.search(r"https?://([a-z0-9_-]+)\.(?:wd\d+\.myworkdayjobs|"
                              r"taleo|icims|csod|avature|phenompeople|eightfold|"
                              r"successfactors|oraclecloud)\.", blob, re.I)
                if m:
                    tenant = m.group(1)
                m2 = re.search(r"(https?://[a-z0-9.-]*(?:oraclecloud|successfactors|"
                               r"myworkdayjobs|taleo|icims|avature|phenompeople|"
                               r"eightfold|csod)\.[a-z.]+[^\s\"'<>]{0,120})", blob, re.I)
                return {"employer": employer, "domain": domain, "platform": plat,
                        "tenant": tenant, "careers_url": r.url,
                        "ats_url": m2.group(1) if m2 else None, "status": "ok"}
    return {"employer": employer, "domain": domain, "platform": None,
            "status": "no careers page reachable", "tried": len(tried)}


def main():
    mega = json.load(open(os.path.join(ROOT, "data", "mega_sweep_results.json")))
    undated = [h for h in mega["hits"] if not h.get("posted_date")]

    print(f"=== PART A: resolving {len(undated)} undated hits ===", flush=True)
    with ThreadPoolExecutor(max_workers=10) as ex:
        for r in ex.map(resolve_date, undated):
            results["dated"].append(r)
            print(f"  {str(r.get('resolved_date')):12} {r['employer'][:18]:18} "
                  f"{r['title'][:46]:46} {r['resolved_evidence'][:44]}", flush=True)

    mapped = set(json.load(open(os.path.join(ROOT, "data",
                                             "ats_platform_map.json")))["employers"])
    todo = [(e, d) for e, d in DOMAINS.items() if e not in mapped]
    print(f"\n=== PART B: fingerprinting careers pages for {len(todo)} "
          f"unmapped employers ===", flush=True)
    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = [ex.submit(fingerprint, t) for t in todo]
        for fut in as_completed(futs):
            try:
                fp = fut.result()
            except Exception:
                continue
            results["fingerprints"].append(fp)
            if fp.get("platform"):
                print(f"  {fp['employer'][:28]:28} -> {fp['platform']:16} "
                      f"tenant={fp.get('tenant')} {(fp.get('ats_url') or '')[:70]}",
                      flush=True)

    found = [f for f in results["fingerprints"] if f.get("platform")]
    print(f"\nFingerprinted {len(found)}/{len(todo)} employers to a known ATS", flush=True)
    from collections import Counter
    for p, c in Counter(f["platform"] for f in found).most_common():
        print(f"  {p:18} {c}", flush=True)

    nh = results["new_hits"]
    print(f"\nNew UAE compliance roles from careers pages: {len(nh)} "
          f"({sum(1 for h in nh if h['in_window'])} in window)", flush=True)
    for h in nh:
        print(f"  {'IN-WINDOW' if h['in_window'] else 'older    '} "
              f"{str(h['posted_date']):12} {h['employer'][:20]:20} {h['title'][:56]}",
              flush=True)

    newly_dated = [d for d in results["dated"] if d.get("resolved_date")]
    print(f"\nDated {len(newly_dated)}/{len(undated)} previously undated hits; "
          f"{sum(1 for d in newly_dated if d.get('in_window'))} fall in window", flush=True)

    out = {"run_utc": NOW.isoformat(), "window_days": WINDOW_DAYS,
           "cutoff": CUTOFF.date().isoformat(), **results}
    with open(os.path.join(ROOT, "data", "deep_sweep_results.json"), "w") as f:
        json.dump(out, f, indent=2)
    print("\nWrote data/deep_sweep_results.json", flush=True)


if __name__ == "__main__":
    main()
