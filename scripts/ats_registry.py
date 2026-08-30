#!/usr/bin/env python3
"""Registry of applicant-tracking systems and their public job endpoints.

Each entry describes one ATS/recruiting platform: how to build a candidate URL
for a given tenant slug, and how to turn the response into job records.

Two kinds of entry:
  * tenant platforms - need a company slug; probed across the employer list.
  * global platforms - expose a cross-company search; queried directly for UAE.

A parser returns a list of dicts: {title, location, url, posted, evidence}.
`posted` may be None; the sweep records that honestly rather than guessing.
"""

import json
import re
from datetime import datetime, timezone

JSON_HDRS = {"Accept": "application/json, text/plain, */*"}


def _g(d, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


def _loc_from(obj):
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        for k in ("name", "text", "city", "location", "locationName"):
            if obj.get(k):
                v = obj[k]
                return v if isinstance(v, str) else str(v)
        parts = [str(obj.get(k, "")) for k in ("city", "region", "state", "country")]
        return " ".join(p for p in parts if p).strip()
    if isinstance(obj, list):
        return ", ".join(_loc_from(o) for o in obj if o)[:200]
    return ""


# ----------------------------------------------------------------- parsers

def p_greenhouse(j, slug):
    return [{"title": x.get("title"), "location": _loc_from(x.get("location")),
             "url": x.get("absolute_url"),
             "posted": x.get("first_published") or x.get("updated_at"),
             "evidence": "Greenhouse first_published" if x.get("first_published")
                         else "Greenhouse updated_at"}
            for x in j.get("jobs", [])]


def p_lever(j, slug):
    return [{"title": x.get("text"), "location": _loc_from(x.get("categories", {}).get("location")),
             "url": x.get("hostedUrl"), "posted": x.get("createdAt"),
             "evidence": "Lever createdAt"} for x in j]


def p_ashby(j, slug):
    return [{"title": x.get("title"), "location": _loc_from(x.get("location")),
             "url": x.get("jobUrl"), "posted": x.get("publishedAt") or x.get("updatedAt"),
             "evidence": "Ashby publishedAt"} for x in j.get("jobs", [])]


def p_smartrecruiters(j, slug):
    out = []
    for x in j.get("content", []):
        out.append({"title": x.get("name"), "location": _loc_from(x.get("location")),
                    "url": f"https://jobs.smartrecruiters.com/{slug}/{x.get('id')}",
                    "posted": x.get("releasedDate") or x.get("createdOn"),
                    "evidence": "SmartRecruiters releasedDate"})
    return out


def p_workable_widget(j, slug):
    return [{"title": x.get("title"),
             "location": f"{x.get('city','')} {x.get('country','')}".strip(),
             "url": x.get("url"), "posted": x.get("published_on"),
             "evidence": "Workable published_on"} for x in j.get("jobs", [])]


def p_workable_v3(j, slug):
    return [{"title": x.get("title"),
             "location": _loc_from(x.get("locations") or x.get("location")),
             "url": f"https://apply.workable.com/{slug}/j/{x.get('shortcode')}/",
             "posted": x.get("published"), "evidence": "Workable published"}
            for x in j.get("results", [])]


def p_recruitee(j, slug):
    return [{"title": x.get("title"),
             "location": _loc_from({"city": x.get("city"), "country": x.get("country")}),
             "url": x.get("careers_url") or x.get("careers_apply_url"),
             "posted": x.get("published_at") or x.get("created_at"),
             "evidence": "Recruitee published_at"} for x in j.get("offers", [])]


def p_teamtailor(j, slug):
    jobs = j.get("jobs", j if isinstance(j, list) else [])
    return [{"title": _g(x, "title", "name"), "location": _loc_from(x.get("location")),
             "url": _g(x, "careersite-job-url", "url", "link"),
             "posted": _g(x, "created-at", "created_at", "published_at"),
             "evidence": "Teamtailor created-at"} for x in jobs]


def p_personio_xml(text, slug):
    out = []
    for m in re.finditer(r"<position>(.*?)</position>", text, re.S | re.I):
        blk = m.group(1)

        def tag(t):
            mm = re.search(rf"<{t}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{t}>", blk, re.S | re.I)
            return mm.group(1).strip() if mm else None
        out.append({"title": tag("name"), "location": tag("office") or tag("city"),
                    "url": tag("url") or f"https://{slug}.jobs.personio.de/",
                    "posted": tag("createdAt") or tag("created_at"),
                    "evidence": "Personio createdAt"})
    return out


def p_bamboo(j, slug):
    res = j.get("result", j.get("jobs", []))
    return [{"title": _g(x, "jobOpeningName", "title"),
             "location": _loc_from(x.get("location")),
             "url": f"https://{slug}.bamboohr.com/careers/{x.get('id')}",
             "posted": _g(x, "datePosted", "postedDate"),
             "evidence": "BambooHR datePosted"} for x in res]


def p_breezy(j, slug):
    return [{"title": x.get("name"), "location": _loc_from(x.get("location")),
             "url": x.get("url") or f"https://{slug}.breezy.hr/p/{x.get('id')}",
             "posted": _g(x, "published_date", "creation_date"),
             "evidence": "Breezy published_date"} for x in (j if isinstance(j, list) else [])]


def p_pinpoint(j, slug):
    data = j.get("data", j if isinstance(j, list) else [])
    return [{"title": _g(x, "title"), "location": _loc_from(x.get("location")),
             "url": _g(x, "url", "apply_url"), "posted": _g(x, "published_at", "created_at"),
             "evidence": "Pinpoint published_at"} for x in data]


def p_comeet(j, slug):
    return [{"title": x.get("name"), "location": _loc_from(x.get("location")),
             "url": x.get("url_comeet_hosted_page") or x.get("url_active_page"),
             "posted": _g(x, "time_updated", "date_created"),
             "evidence": "Comeet date_created"} for x in (j if isinstance(j, list) else [])]


def p_recruiterbox(j, slug):
    return [{"title": x.get("title"), "location": _loc_from(x.get("location")),
             "url": x.get("hosted_url"), "posted": x.get("created_on"),
             "evidence": "Recruiterbox created_on"} for x in j.get("objects", [])]


def p_jobvite(j, slug):
    reqs = j.get("requisitions", j.get("jobs", []))
    return [{"title": _g(x, "title", "name"), "location": _loc_from(x.get("location")),
             "url": _g(x, "applyUrl", "url"), "posted": _g(x, "date", "postedDate"),
             "evidence": "Jobvite date"} for x in reqs]


def p_eightfold(j, slug):
    positions = j.get("positions", j.get("data", []))
    return [{"title": _g(x, "name", "title"), "location": _loc_from(x.get("location")),
             "url": _g(x, "canonicalPositionUrl", "positionUrl", "url"),
             "posted": _g(x, "t_create", "created_at", "postedDate"),
             "evidence": "Eightfold t_create"} for x in positions]


def p_freshteam(j, slug):
    return [{"title": x.get("title"), "location": _loc_from(x.get("branch")),
             "url": x.get("applicant_url"), "posted": _g(x, "published_date", "created_at"),
             "evidence": "Freshteam published_date"} for x in (j if isinstance(j, list) else [])]


def p_zohorecruit(j, slug):
    data = j.get("data", [])
    return [{"title": _g(x, "Posting_Title", "title"),
             "location": _loc_from({"city": x.get("City"), "country": x.get("Country")}),
             "url": x.get("url") or f"https://{slug}.zohorecruit.com/jobs/Careers",
             "posted": _g(x, "Date_Opened", "Created_Time"),
             "evidence": "Zoho Recruit Date_Opened"} for x in data]


def p_workday(j, slug):
    host = getattr(p_workday, "_host", "")
    out = []
    for x in j.get("jobPostings", []):
        out.append({"title": x.get("title"), "location": x.get("locationsText", ""),
                    "url": host + (x.get("externalPath") or ""),
                    "posted": x.get("startDate") or x.get("postedOn"),
                    "evidence": "Workday startDate"})
    return out


def p_oracle_hcm(j, slug):
    items = []
    for it in j.get("items", []):
        for req in it.get("requisitionList", []):
            items.append({
                "title": req.get("Title"),
                "location": req.get("PrimaryLocation") or req.get("Location") or "",
                "url": req.get("Link") or "",
                "posted": req.get("PostedDate"),
                "evidence": "Oracle HCM PostedDate"})
    return items


def p_icims_rss(text, slug):
    out = []
    for m in re.finditer(r"<item>(.*?)</item>", text, re.S | re.I):
        blk = m.group(1)

        def tag(t):
            mm = re.search(rf"<{t}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{t}>", blk, re.S | re.I)
            return mm.group(1).strip() if mm else None
        out.append({"title": tag("title"), "location": tag("location") or "",
                    "url": tag("link"), "posted": tag("pubDate"),
                    "evidence": "iCIMS RSS pubDate"})
    return out


def p_rss_generic(text, slug):
    return p_icims_rss(text, slug)


def p_jsonld_page(text, slug):
    """Fallback: scrape JSON-LD JobPosting blocks out of a careers page."""
    out = []
    for m in re.finditer(
            r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', text, re.S | re.I):
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
                    out.append({
                        "title": n.get("title"),
                        "location": _loc_from(n.get("jobLocation")),
                        "url": n.get("url") or n.get("@id") or "",
                        "posted": n.get("datePosted"),
                        "evidence": "JSON-LD datePosted"})
                stack.extend(v for v in n.values() if isinstance(v, (dict, list)))
    return out


# ------------------------------------------------------- tenant platforms
# fmt: off
TENANT_PLATFORMS = [
 ("greenhouse",       ["https://boards-api.greenhouse.io/v1/boards/{s}/jobs"], "json", p_greenhouse),
 ("greenhouse-embed", ["https://api.greenhouse.io/v1/boards/{s}/embed/jobs"], "json", p_greenhouse),
 ("lever",            ["https://api.lever.co/v0/postings/{s}?mode=json"], "json", p_lever),
 ("ashby",            ["https://api.ashbyhq.com/posting-api/job-board/{s}"], "json", p_ashby),
 ("smartrecruiters",  ["https://api.smartrecruiters.com/v1/companies/{s}/postings?limit=100"], "json", p_smartrecruiters),
 ("workable-widget",  ["https://apply.workable.com/api/v1/widget/accounts/{s}?details=true"], "json", p_workable_widget),
 ("workable-v3",      ["https://apply.workable.com/api/v3/accounts/{s}/jobs"], "json", p_workable_v3),
 ("recruitee",        ["https://{s}.recruitee.com/api/offers/"], "json", p_recruitee),
 ("teamtailor",       ["https://{s}.teamtailor.com/jobs.json",
                       "https://careers.{s}.com/jobs.json"], "json", p_teamtailor),
 ("personio",         ["https://{s}.jobs.personio.de/xml",
                       "https://{s}.jobs.personio.com/xml"], "text", p_personio_xml),
 ("bamboohr",         ["https://{s}.bamboohr.com/careers/list"], "json", p_bamboo),
 ("breezy",           ["https://{s}.breezy.hr/json"], "json", p_breezy),
 ("pinpoint",         ["https://{s}.pinpointhq.com/postings.json"], "json", p_pinpoint),
 ("comeet",           ["https://www.comeet.co/careers-api/2.0/company/{s}/positions?token=&details=true"], "json", p_comeet),
 ("recruiterbox",     ["https://{s}.recruiterbox.com/api/v1/openings/"], "json", p_recruiterbox),
 ("jazzhr",           ["https://{s}.applytojob.com/apply/jobs/rss"], "text", p_rss_generic),
 ("jobvite",          ["https://jobs.jobvite.com/api/jobs?companyId={s}&careerSiteType=external"], "json", p_jobvite),
 ("eightfold",        ["https://{s}.eightfold.ai/api/apply/v2/jobs?domain={s}.com&start=0&num=100",
                       "https://careers.{s}.com/api/apply/v2/jobs?domain={s}.com&start=0&num=100"], "json", p_eightfold),
 ("freshteam",        ["https://{s}.freshteam.com/api/job_postings"], "json", p_freshteam),
 ("zohorecruit",      ["https://{s}.zohorecruit.com/recruit/v2/Job_Openings"], "json", p_zohorecruit),
 ("icims",            ["https://careers-{s}.icims.com/jobs/search?ss=1&format=rss",
                       "https://{s}.icims.com/jobs/search?ss=1&format=rss"], "text", p_icims_rss),
 ("taleo",            ["https://{s}.taleo.net/careersection/rss/jobfeed.rss"], "text", p_rss_generic),
 ("jobylon",          ["https://{s}.jobylon.com/api/jobs/"], "json", p_pinpoint),
 ("factorial",        ["https://{s}.factorialhr.com/api/v1/ats/job_postings"], "json", p_pinpoint),
 ("softgarden",       ["https://{s}.softgarden.io/api/rest/v3/frontend/jobs"], "json", p_pinpoint),
 ("join",             ["https://join.com/api/public/companies/{s}/jobs"], "json", p_pinpoint),
 ("catsone",          ["https://{s}.catsone.com/careers/rss"], "text", p_rss_generic),
 ("careers-page",     ["https://www.careers-page.com/{s}"], "text", p_jsonld_page),
 ("getro",            ["https://api.getro.com/api/v2/collections/{s}/search/jobs"], "json", p_pinpoint),
 ("rippling",         ["https://ats.rippling.com/api/v1/board/{s}/jobs"], "json", p_pinpoint),
 ("gusto",            ["https://jobs.gusto.com/api/boards/{s}/jobs"], "json", p_pinpoint),
 ("homerun",          ["https://{s}.homerun.co/api/jobs"], "json", p_pinpoint),
 ("manatal",          ["https://api.manatal.com/open/v3/career-page/{s}/jobs/"], "json", p_pinpoint),
 ("talentlyft",       ["https://{s}.talentlyft.com/api/public/jobs"], "json", p_pinpoint),
 ("occupop",          ["https://{s}.occupop.com/api/jobs"], "json", p_pinpoint),
 ("workstream",       ["https://www.workstream.us/api/v1/companies/{s}/jobs"], "json", p_pinpoint),
 ("polymer",          ["https://www.polymer.co/api/v1/boards/{s}/jobs"], "json", p_pinpoint),
 ("applied",          ["https://app.beapplied.com/api/v1/organisations/{s}/jobs"], "json", p_pinpoint),
 ("hirehive",         ["https://{s}.hirehive.com/api/v1/jobs"], "json", p_pinpoint),
 ("careerplug",       ["https://{s}.careerplug.com/jobs.rss"], "text", p_rss_generic),
 ("applicantpro",     ["https://{s}.applicantpro.com/jobs/feed.rss"], "text", p_rss_generic),
 ("clearcompany",     ["https://{s}.clearcompany.com/careers/jobs/feed"], "text", p_rss_generic),
 ("smartjob-crelate", ["https://{s}.crelate.com/portal/api/jobs"], "json", p_pinpoint),
 ("vincere",          ["https://{s}.vincere.io/api/v2/public/jobs"], "json", p_pinpoint),
 ("loxo",             ["https://app.loxo.co/api/public/{s}/jobs"], "json", p_pinpoint),
 ("bullhorn",         ["https://public-rest{s}.bullhornstaffing.com/rest-services/search/JobOrder"], "json", p_pinpoint),
 ("dayforce",         ["https://{s}.dayforcehcm.com/CandidatePortal/api/v1/{s}/Jobs"], "json", p_pinpoint),
 ("ultipro",          ["https://recruiting.ultipro.com/{s}/JobBoard/api/JobBoardView/LoadSearchResults"], "json", p_pinpoint),
 ("paylocity",        ["https://recruiting.paylocity.com/recruiting/v2/api/jobs/{s}"], "json", p_pinpoint),
 ("paycom",           ["https://{s}.paycomonline.net/v4/ats/web.php/jobs/ViewAll?clientkey={s}"], "text", p_jsonld_page),
 ("cornerstone",      ["https://{s}.csod.com/services/x/career-site/v1/search?siteId=1"], "json", p_pinpoint),
 ("avature",          ["https://{s}.avature.net/careers/SearchJobs/?jobRecordsPerPage=100&format=json"], "json", p_pinpoint),
 ("phenom",           ["https://{s}.phenompeople.com/api/jobs?limit=100"], "json", p_pinpoint),
 ("darwinbox",        ["https://{s}.darwinbox.in/ms/candidate/careers/api/jobs",
                       "https://{s}.darwinbox.com/ms/candidate/careers/api/jobs"], "json", p_pinpoint),
 ("keka",             ["https://{s}.keka.com/careers/api/jobs"], "json", p_pinpoint),
 ("successfactors",   ["https://career{s}.successfactors.eu/careers?company={s}"], "text", p_jsonld_page),
 ("oraclecloud",      ["https://{s}.fa.em2.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList&finder=findReqs;siteNumber=CX_1,limit=100"], "json", p_oracle_hcm),
 ("fountain",         ["https://{s}.fountain.com/api/v1/positions"], "json", p_pinpoint),
 ("harri",            ["https://api.harri.com/v1/{s}/jobs"], "json", p_pinpoint),
 ("hireology",        ["https://{s}.hireology.com/api/jobs"], "json", p_pinpoint),
 ("trakstar",         ["https://{s}.hire.trakstar.com/api/v1/jobs"], "json", p_pinpoint),
 ("jobscore",         ["https://careers.jobscore.com/api/v1/{s}/jobs"], "json", p_pinpoint),
 ("careersite-generic", ["https://careers.{s}.com/", "https://{s}.com/careers",
                         "https://jobs.{s}.com/"], "text", p_jsonld_page),
]
# fmt: on

# Workday needs (tenant, site) pairs and a POST; handled separately by the sweeper.
WORKDAY_SITES = [
    "External", "Careers", "careers", "External_Career_Site", "en-US", "Global",
    "ExternalCareerSite", "PROFESSIONAL", "Professional_Careers", "jobs",
]


# ------------------------------------------------------- global platforms
GLOBAL_PLATFORMS = [
    # (name, url, kind, parser) - cross-company searches filtered to the UAE
    ("workable-global",
     "https://jobs.workable.com/api/v1/jobs?query=compliance&location=United%20Arab%20Emirates",
     "json", lambda j, s: [
         {"title": x.get("title"), "location": _loc_from(x.get("location")),
          "url": x.get("url") or x.get("shortlink"),
          "posted": _g(x, "published_on", "created_at"),
          "evidence": "Workable global published_on"}
         for x in (j.get("results") or j.get("jobs") or [])]),
    ("themuse",
     "https://www.themuse.com/api/public/jobs?location=Dubai%2C%20United%20Arab%20Emirates&page=0",
     "json", lambda j, s: [
         {"title": x.get("name"),
          "location": _loc_from(x.get("locations")),
          "url": (x.get("refs") or {}).get("landing_page"),
          "posted": x.get("publication_date"),
          "evidence": "The Muse publication_date"} for x in j.get("results", [])]),
    ("arbeitnow",
     "https://www.arbeitnow.com/api/job-board-api",
     "json", lambda j, s: [
         {"title": x.get("title"), "location": x.get("location"),
          "url": x.get("url"), "posted": x.get("created_at"),
          "evidence": "Arbeitnow created_at"} for x in j.get("data", [])]),
    ("remotive",
     "https://remotive.com/api/remote-jobs?search=compliance",
     "json", lambda j, s: [
         {"title": x.get("title"), "location": x.get("candidate_required_location"),
          "url": x.get("url"), "posted": x.get("publication_date"),
          "evidence": "Remotive publication_date"} for x in j.get("jobs", [])]),
    ("jobicy",
     "https://jobicy.com/api/v2/remote-jobs?count=100&tag=compliance",
     "json", lambda j, s: [
         {"title": x.get("jobTitle"), "location": x.get("jobGeo"),
          "url": x.get("url"), "posted": x.get("pubDate"),
          "evidence": "Jobicy pubDate"} for x in j.get("jobs", [])]),
    ("himalayas",
     "https://himalayas.app/jobs/api?limit=100&search=compliance",
     "json", lambda j, s: [
         {"title": x.get("title"), "location": _loc_from(x.get("locationRestrictions")),
          "url": x.get("applicationLink") or x.get("url"),
          "posted": x.get("pubDate"), "evidence": "Himalayas pubDate"}
         for x in j.get("jobs", [])]),
    ("hub71-getro",
     "https://api.getro.com/api/v2/collections/hub71/search/jobs?hitsPerPage=100&query=compliance",
     "json", lambda j, s: [
         {"title": (x.get("job") or x).get("title"),
          "location": _loc_from((x.get("job") or x).get("locations")),
          "url": (x.get("job") or x).get("url"),
          "posted": (x.get("job") or x).get("created_at"),
          "evidence": "Getro created_at"} for x in (j.get("results") or j.get("hits") or [])]),
]
