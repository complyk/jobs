#!/usr/bin/env python3
"""Sweep every ATS platform in the registry across every UAE employer.

Runs on a GitHub Actions runner (unrestricted egress).

Phase 1 - DISCOVERY: for each (employer slug, platform URL pattern) pair, work out
whether that tenant exists. Hostnames that vary per tenant are DNS-resolved first,
which is milliseconds and discards the overwhelming majority of non-existent
combinations before any HTTP request is made. Surviving candidates are fetched and
parsed with the platform's own parser.

Phase 2 - GLOBAL: query the cross-company search endpoints (they need no slug).

Phase 3 - FILTER: keep UAE-located senior compliance / financial-crime roles and
date each from its platform's authoritative publish field.

Outputs:
  data/ats_platform_map.json  - employer -> ATS platform discovered (a durable asset)
  data/mega_sweep_results.json - roles found, plus the full per-platform log
"""

import json
import os
import re
import socket
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import requests
import urllib3

urllib3.disable_warnings()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ats_registry import TENANT_PLATFORMS, GLOBAL_PLATFORMS, WORKDAY_SITES, p_workday  # noqa: E402
from employers_uae import all_employers  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "7"))
MAX_SLUGS = int(os.environ.get("MAX_SLUGS_PER_EMPLOYER", "3"))
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "8"))
WORKERS = int(os.environ.get("WORKERS", "96"))
NOW = datetime.now(timezone.utc)
CUTOFF = NOW - timedelta(days=WINDOW_DAYS)

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/xml, text/html, */*",
    "Accept-Language": "en-GB,en;q=0.9",
}

KEYWORDS = re.compile(
    r"(mlro|money laundering|compliance|financial crime|\baml\b|\bcft\b|sanctions|"
    r"regulatory affairs|fincrime)", re.I)
SENIOR = re.compile(
    r"(head\b|chief|director|\bvp\b|vice president|\bsvp\b|lead\b|senior|manager|"
    r"mlro|\bcco\b|principal|officer)", re.I)
JUNIOR = re.compile(
    r"(analyst|associate\b|intern\b|graduate|trainee|assistant|coordinator|"
    r"executive assistant|administrator|clerk)", re.I)
KEEP_ANYWAY = re.compile(r"(mlro|head\b|chief|director|\bvp\b|\bsvp\b|lead\b)", re.I)
UAE = re.compile(
    r"(\buae\b|united arab emirates|dubai|abu dhabi|difc|adgm|sharjah|"
    r"ras al khaimah|\bajman\b|fujairah|emirates)", re.I)

_dns_cache, _dns_lock = {}, threading.Lock()
_log_lock = threading.Lock()
log_lines, hits, platform_map = [], [], defaultdict(list)


def host_resolves(host):
    with _dns_lock:
        if host in _dns_cache:
            return _dns_cache[host]
    try:
        socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        ok = True
    except Exception:
        ok = False
    with _dns_lock:
        _dns_cache[host] = ok
    return ok


def to_dt(v):
    if v in (None, "", 0):
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
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d %b %Y", "%B %d, %Y",
                "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z", "%Y/%m/%d"):
        try:
            d = datetime.strptime(s, fmt)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def in_scope(title, location, blob=""):
    t = (title or "").strip()
    if not t or not KEYWORDS.search(t) or not SENIOR.search(t):
        return False
    if JUNIOR.search(t) and not KEEP_ANYWAY.search(t):
        return False
    return bool(UAE.search(f"{location or ''} {blob}"))


def record(platform, employer, sector, jobs):
    """Filter parsed jobs down to UAE senior compliance roles."""
    kept = 0
    for j in jobs or []:
        try:
            title, loc = j.get("title"), j.get("location") or ""
            if not in_scope(title, loc):
                continue
            d = to_dt(j.get("posted"))
            hits.append({
                "platform": platform, "employer": employer, "sector": sector,
                "title": str(title).strip(), "location": str(loc).strip()[:120],
                "url": j.get("url") or "", "posted_date": d.date().isoformat() if d else None,
                "evidence": j.get("evidence") or "platform field",
                "in_window": bool(d and d >= CUTOFF),
                "age_days": (NOW - d).days if d else None,
            })
            kept += 1
        except Exception:
            continue
    return kept


def fetch(url, kind):
    r = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT, verify=False)
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    if kind == "json":
        ctype = r.headers.get("content-type", "")
        if "json" not in ctype and not r.text.lstrip()[:1] in "[{":
            return None, "not json"
        try:
            return r.json(), None
        except Exception:
            return None, "json parse error"
    if len(r.text) < 200:
        return None, "empty body"
    return r.text, None


def probe_tenant(task):
    platform, patterns, kind, parser, slug, employer, sector = task
    for pat in patterns:
        url = pat.replace("{s}", slug)
        host = urlparse(url).netloc
        if not host:
            continue
        # Tenant-specific hostnames: DNS-filter before spending an HTTP request.
        if "{s}" in urlparse(pat).netloc and not host_resolves(host):
            continue
        try:
            data, err = fetch(url, kind)
        except Exception:
            continue
        if data is None:
            continue
        try:
            jobs = parser(data, slug)
        except Exception:
            continue
        if not jobs:
            continue
        n = record(platform, employer, sector, jobs)
        with _log_lock:
            platform_map[employer].append(
                {"platform": platform, "slug": slug, "url": url,
                 "total_jobs": len(jobs), "uae_compliance_hits": n})
        return (f"[{platform}] {employer} ({slug}): FOUND {len(jobs)} jobs, "
                f"{n} UAE compliance")
    return None


def probe_workday(task):
    slug, employer, sector = task
    for wd in ("wd1", "wd3", "wd5", "wd2", "wd103"):
        host = f"{slug}.{wd}.myworkdayjobs.com"
        if not host_resolves(host):
            continue
        for site in WORKDAY_SITES:
            url = f"https://{host}/wday/cxs/{slug}/{site}/jobs"
            try:
                r = requests.post(
                    url, headers={**HEADERS, "Content-Type": "application/json"},
                    json={"appliedFacets": {}, "limit": 20, "offset": 0,
                          "searchText": "compliance"},
                    timeout=HTTP_TIMEOUT, verify=False)
                if r.status_code != 200:
                    continue
                data = r.json()
            except Exception:
                continue
            p_workday._host = f"https://{host}"
            try:
                jobs = p_workday(data, slug)
            except Exception:
                continue
            if not jobs:
                continue
            n = record("workday", employer, sector, jobs)
            with _log_lock:
                platform_map[employer].append(
                    {"platform": "workday", "slug": f"{slug}/{site}", "url": url,
                     "total_jobs": len(jobs), "uae_compliance_hits": n})
            return f"[workday] {employer} ({slug}/{site}): FOUND {len(jobs)} jobs, {n} UAE compliance"
    return None


def probe_global(item):
    name, url, kind, parser = item
    try:
        data, err = fetch(url, kind)
        if data is None:
            return f"[global:{name}] unavailable ({err})"
        jobs = parser(data, name)
        n = record(f"global:{name}", name, "global", jobs)
        return f"[global:{name}] ok ({len(jobs)} jobs, {n} UAE compliance)"
    except Exception as e:
        return f"[global:{name}] error {type(e).__name__}"


def main():
    employers = all_employers()
    tasks, wd_tasks = [], []
    for sector, name, slugs in employers:
        for slug in slugs[:MAX_SLUGS]:
            wd_tasks.append((slug, name, sector))
            for platform, patterns, kind, parser in TENANT_PLATFORMS:
                tasks.append((platform, patterns, kind, parser, slug, name, sector))

    print(f"Registry: {len(TENANT_PLATFORMS)} tenant platforms + "
          f"{len(GLOBAL_PLATFORMS)} global + Workday", flush=True)
    print(f"Employers: {len(employers)}, slug variants probed: "
          f"{sum(min(len(s), MAX_SLUGS) for _, _, s in employers)}", flush=True)
    print(f"Tenant probes queued: {len(tasks):,}  (+{len(wd_tasks):,} Workday)\n", flush=True)

    print("=== PHASE 1: global cross-company endpoints ===", flush=True)
    with ThreadPoolExecutor(max_workers=8) as ex:
        for res in ex.map(probe_global, GLOBAL_PLATFORMS):
            print("  " + res, flush=True)
            log_lines.append(res)

    print("\n=== PHASE 2: tenant discovery across all platforms ===", flush=True)
    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(probe_tenant, t) for t in tasks]
        for fut in as_completed(futs):
            done += 1
            if done % 5000 == 0:
                print(f"  ... {done:,}/{len(tasks):,} probes, "
                      f"{len(platform_map)} employers matched", flush=True)
            try:
                res = fut.result()
            except Exception:
                res = None
            if res:
                print("  " + res, flush=True)
                log_lines.append(res)

    print("\n=== PHASE 3: Workday tenants ===", flush=True)
    with ThreadPoolExecutor(max_workers=32) as ex:
        for res in ex.map(probe_workday, wd_tasks):
            if res:
                print("  " + res, flush=True)
                log_lines.append(res)

    # de-duplicate hits
    seen, uniq = set(), []
    for h in hits:
        k = (h["url"] or "", h["title"], h["employer"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(h)
    uniq.sort(key=lambda h: (h["posted_date"] or ""), reverse=True)
    inwin = [h for h in uniq if h["in_window"]]

    print(f"\n=== RESULTS ===", flush=True)
    print(f"Employers with a discovered ATS: {len(platform_map)}", flush=True)
    print(f"UAE senior-compliance roles found: {len(uniq)}  "
          f"({len(inwin)} first published within {WINDOW_DAYS} days)\n", flush=True)
    for h in uniq:
        print(f"  {'IN-WINDOW' if h['in_window'] else 'older    '} "
              f"{str(h['posted_date']):12} {h['employer'][:22]:22} "
              f"{h['title'][:56]:56} {h['location'][:22]:22} {h['platform']}", flush=True)

    plat_counts = defaultdict(int)
    for entries in platform_map.values():
        for e in entries:
            plat_counts[e["platform"]] += 1
    print("\nPlatforms that yielded live boards:", flush=True)
    for p, c in sorted(plat_counts.items(), key=lambda x: -x[1]):
        print(f"  {p:22} {c} employer board(s)", flush=True)

    with open(os.path.join(ROOT, "data", "ats_platform_map.json"), "w") as f:
        json.dump({"run_utc": NOW.isoformat(),
                   "employers": {k: v for k, v in sorted(platform_map.items())}},
                  f, indent=2)
    with open(os.path.join(ROOT, "data", "mega_sweep_results.json"), "w") as f:
        json.dump({
            "run_utc": NOW.isoformat(), "window_days": WINDOW_DAYS,
            "cutoff": CUTOFF.date().isoformat(),
            "platforms_in_registry": len(TENANT_PLATFORMS) + len(GLOBAL_PLATFORMS) + 1,
            "employers_probed": len(employers),
            "tenant_probes": len(tasks),
            "employers_with_ats": len(platform_map),
            "hits": uniq, "log": log_lines,
            "platform_yield": dict(plat_counts),
        }, f, indent=2)
    print("\nWrote data/ats_platform_map.json and data/mega_sweep_results.json", flush=True)


if __name__ == "__main__":
    main()
