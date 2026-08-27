"""Collectors for ATS platforms that expose an unauthenticated JSON API.

Covered families
----------------
workday          POST {host}/wday/cxs/{tenant}/{site}/jobs
oracle           GET  {host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions
greenhouse       GET  https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true
lever            GET  https://api.lever.co/v0/postings/{company}?mode=json
smartrecruiters  GET  https://api.smartrecruiters.com/v1/companies/{company}/postings
ashby            POST https://api.ashbyhq.com/posting-api/job-board/{board}
recruitee        GET  https://{company}.recruitee.com/api/offers/
teamtailor       GET  {feed_url}  (public JSON feed)

Every entry in config/companies.yaml declares: family, plus family-specific keys.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from ..models import Job, is_saudi
from .base import Collector


def _iso(ms_or_str) -> str:
    if not ms_or_str:
        return ""
    try:
        if isinstance(ms_or_str, (int, float)):
            return datetime.fromtimestamp(ms_or_str / 1000, timezone.utc).date().isoformat()
        s = str(ms_or_str)
        return s[:10]
    except Exception:
        return ""


# --------------------------------------------------------------------- Workday
class WorkdayCollector(Collector):
    """Workday's CXS endpoint is public JSON. Paginated 20 at a time."""

    family = "workday"

    def collect(self):
        host = self.cfg["host"].rstrip("/")
        tenant, site = self.cfg["tenant"], self.cfg["site"]
        url = f"{host}/wday/cxs/{tenant}/{site}/jobs"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        offset, limit, out = 0, 20, []
        search_text = self.cfg.get("search_text", "")
        while offset < self.cfg.get("max_records", 400):
            body = {"appliedFacets": self.cfg.get("facets", {}),
                    "limit": limit, "offset": offset, "searchText": search_text}
            data = self.http.post(url, headers=headers, json_body=body)
            posts = (data or {}).get("jobPostings") or []
            if not posts:
                break
            for p in posts:
                loc = p.get("locationsText") or p.get("bulletFields", [""])[0] or ""
                if self.cfg.get("saudi_only", True) and not is_saudi(loc, self.cfg.get("name", "")):
                    continue
                path = p.get("externalPath", "")
                out.append(Job(
                    source=f"workday:{self.cfg['id']}", source_family="workday",
                    external_id=path or p.get("bulletFields", [""])[0],
                    title=p.get("title", ""), company=self.cfg["name"],
                    url=f"{host}/{self.cfg.get('locale','en-US')}/{site}{path}",
                    location_raw=loc, posted_at=_iso(p.get("postedOn")),
                    description=p.get("jobDescription", "") or "",
                    raw=p,
                ))
            offset += limit
            if len(posts) < limit:
                break
        if self.cfg.get("fetch_details", True):
            self._enrich(out, host, tenant, site)
        return out

    def _enrich(self, jobs, host, tenant, site):
        for j in jobs[: self.cfg.get("max_details", 60)]:
            if j.description:
                continue
            try:
                d = self.http.get(f"{host}/wday/cxs/{tenant}/{site}{j.external_id}")
                info = (d or {}).get("jobPostingInfo", {})
                j.description = info.get("jobDescription", "")
                j.posted_at = j.posted_at or _iso(info.get("startDate"))
                j.employment_type = info.get("timeType", "") or info.get("jobRequisitionLocation", {}).get("descriptor", "")
            except Exception:
                continue


# ---------------------------------------------------------------------- Oracle
class OracleORCCollector(Collector):
    """Oracle Recruiting Cloud public REST feed."""

    family = "oracle"

    def collect(self):
        host = self.cfg["host"].rstrip("/")
        site = self.cfg["site_number"]
        base = f"{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
        out, offset, limit = [], 0, 25
        while offset < self.cfg.get("max_records", 300):
            finder = (f"findReqs;siteNumber={site},limit={limit},offset={offset},"
                      f"sortBy=POSTING_DATES_DESC")
            data = self.http.get(base, params={"onlyData": "true", "expand": "requisitionList.secondaryLocations",
                                               "finder": finder})
            items = ((data or {}).get("items") or [{}])[0].get("requisitionList") or []
            if not items:
                break
            for it in items:
                loc = it.get("PrimaryLocation") or ""
                if self.cfg.get("saudi_only", True) and not is_saudi(loc, self.cfg.get("name", "")):
                    continue
                rid = it.get("Id") or it.get("RequisitionId")
                out.append(Job(
                    source=f"oracle:{self.cfg['id']}", source_family="oracle",
                    external_id=str(rid), title=it.get("Title", ""),
                    company=self.cfg["name"],
                    url=f"{host}/hcmUI/CandidateExperience/en/sites/{self.cfg.get('site_slug', site)}/job/{rid}",
                    location_raw=loc, posted_at=_iso(it.get("PostedDate")),
                    description=it.get("ShortDescriptionStr", "") or "",
                    raw=it,
                ))
            offset += limit
            if len(items) < limit:
                break
        return out


# ------------------------------------------------------------------ Greenhouse
class GreenhouseCollector(Collector):
    family = "greenhouse"

    def collect(self):
        board = self.cfg["board"]
        data = self.http.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs",
                             params={"content": "true"})
        out = []
        for j in (data or {}).get("jobs", []):
            loc = (j.get("location") or {}).get("name", "")
            if self.cfg.get("saudi_only", True) and not is_saudi(loc):
                continue
            out.append(Job(
                source=f"greenhouse:{board}", source_family="greenhouse",
                external_id=str(j.get("id")), title=j.get("title", ""),
                company=self.cfg.get("name") or j.get("company_name", board),
                url=j.get("absolute_url", ""), location_raw=loc,
                posted_at=_iso(j.get("updated_at") or j.get("first_published")),
                description=j.get("content", ""), raw=j,
            ))
        return out


# ----------------------------------------------------------------------- Lever
class LeverCollector(Collector):
    family = "lever"

    def collect(self):
        company = self.cfg["company"]
        data = self.http.get(f"https://api.lever.co/v0/postings/{company}", params={"mode": "json"})
        out = []
        for j in data or []:
            loc = (j.get("categories") or {}).get("location", "")
            if self.cfg.get("saudi_only", True) and not is_saudi(loc):
                continue
            out.append(Job(
                source=f"lever:{company}", source_family="lever",
                external_id=j.get("id", ""), title=j.get("text", ""),
                company=self.cfg.get("name", company), url=j.get("hostedUrl", ""),
                location_raw=loc, posted_at=_iso(j.get("createdAt")),
                description=j.get("descriptionPlain") or j.get("description", ""),
                employment_type=(j.get("categories") or {}).get("commitment", ""), raw=j,
            ))
        return out


# -------------------------------------------------------------- SmartRecruiters
class SmartRecruitersCollector(Collector):
    family = "smartrecruiters"

    def collect(self):
        company = self.cfg["company"]
        out, offset = [], 0
        while offset < self.cfg.get("max_records", 300):
            data = self.http.get(f"https://api.smartrecruiters.com/v1/companies/{company}/postings",
                                 params={"limit": 100, "offset": offset,
                                         "country": self.cfg.get("country", "sa")})
            items = (data or {}).get("content", [])
            if not items:
                break
            for j in items:
                loc = j.get("location", {})
                loc_s = ", ".join(x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x)
                out.append(Job(
                    source=f"smartrecruiters:{company}", source_family="smartrecruiters",
                    external_id=j.get("id", ""), title=j.get("name", ""),
                    company=self.cfg.get("name", company),
                    url=f"https://jobs.smartrecruiters.com/{company}/{j.get('id')}",
                    location_raw=loc_s, posted_at=_iso(j.get("releasedDate")),
                    description=json.dumps(j.get("jobAd", {}), ensure_ascii=False)[:6000], raw=j,
                ))
            offset += 100
            if len(items) < 100:
                break
        return out


# ----------------------------------------------------------------------- Ashby
class AshbyCollector(Collector):
    family = "ashby"

    def collect(self):
        board = self.cfg["board"]
        data = self.http.post("https://api.ashbyhq.com/posting-api/job-board/" + board,
                              params={"includeCompensation": "false"})
        out = []
        for j in (data or {}).get("jobs", []):
            loc = j.get("location", "")
            if self.cfg.get("saudi_only", True) and not is_saudi(loc):
                continue
            out.append(Job(
                source=f"ashby:{board}", source_family="ashby",
                external_id=j.get("id", ""), title=j.get("title", ""),
                company=self.cfg.get("name", board), url=j.get("jobUrl", ""),
                location_raw=loc, posted_at=_iso(j.get("publishedAt")),
                description=j.get("descriptionPlain", ""), raw=j,
            ))
        return out


# -------------------------------------------------------------------- Recruitee
class RecruiteeCollector(Collector):
    family = "recruitee"

    def collect(self):
        company = self.cfg["company"]
        data = self.http.get(f"https://{company}.recruitee.com/api/offers/")
        out = []
        for j in (data or {}).get("offers", []):
            loc = ", ".join(x for x in [j.get("city"), j.get("country")] if x)
            if self.cfg.get("saudi_only", True) and not is_saudi(loc):
                continue
            out.append(Job(
                source=f"recruitee:{company}", source_family="recruitee",
                external_id=str(j.get("id")), title=j.get("title", ""),
                company=self.cfg.get("name", company), url=j.get("careers_url", ""),
                location_raw=loc, posted_at=_iso(j.get("published_at")),
                description=j.get("description", ""), raw=j,
            ))
        return out


REGISTRY = {
    "workday": WorkdayCollector,
    "oracle": OracleORCCollector,
    "greenhouse": GreenhouseCollector,
    "lever": LeverCollector,
    "smartrecruiters": SmartRecruitersCollector,
    "ashby": AshbyCollector,
    "recruitee": RecruiteeCollector,
}
