"""Playwright-backed collection for JavaScript career sites.

Design note
-----------
CSS selectors on third-party career portals break constantly. Two sturdier
signals are used instead, in this order:

1. **XHR interception** - almost every SPA job board fetches its listings as
   JSON. We record every JSON response the page makes and then look for arrays
   of objects that carry job-shaped keys (title/jobTitle/positionName + a link
   or id). This survives redesigns as long as the underlying API is unchanged.
2. **schema.org JobPosting** - many portals emit JSON-LD for SEO. Parsed from
   <script type="application/ld+json">.

CSS selectors are only a third fallback and are declared per-site in
config/sources.yaml so they can be fixed without touching code.
"""
from __future__ import annotations

import json
import logging
import re
from contextlib import contextmanager
from typing import Any, Iterable

from ..models import Job, is_saudi, strip_html

log = logging.getLogger("radar.browser")

TITLE_KEYS = ["title", "jobtitle", "job_title", "positionname", "position_title",
              "name", "vacancytitle", "jobname", "postingtitle"]
LINK_KEYS = ["url", "joburl", "applyurl", "link", "hostedurl", "absolute_url", "permalink"]
ID_KEYS = ["id", "jobid", "job_id", "requisitionid", "reqid", "vacancyid", "postingid", "code"]
LOC_KEYS = ["location", "city", "joblocation", "locationname", "region", "workplace", "cityname"]
DESC_KEYS = ["description", "jobdescription", "content", "details", "summary", "requirements"]
COMPANY_KEYS = ["company", "companyname", "employer", "employername", "organization", "entityname"]


def _lk(d: dict) -> dict:
    return {str(k).lower().replace(" ", ""): v for k, v in d.items()}


def _pick(d: dict, keys: Iterable[str]) -> Any:
    low = _lk(d)
    for k in keys:
        v = low.get(k)
        if isinstance(v, dict):
            v = v.get("name") or v.get("label") or v.get("value") or v.get("en") or v.get("ar")
        if isinstance(v, list) and v and isinstance(v[0], (str, int)):
            v = ", ".join(str(x) for x in v)
        if isinstance(v, (str, int, float)) and str(v).strip():
            return str(v).strip()
    return ""


def looks_like_job(d: Any) -> bool:
    return isinstance(d, dict) and bool(_pick(d, TITLE_KEYS)) and (
        bool(_pick(d, ID_KEYS)) or bool(_pick(d, LINK_KEYS)))


def find_job_arrays(payload: Any, depth: int = 0) -> list[list[dict]]:
    """Walk arbitrary JSON and return every array that looks like a job list."""
    found: list[list[dict]] = []
    if depth > 8:
        return found
    if isinstance(payload, list):
        hits = [x for x in payload if looks_like_job(x)]
        if len(hits) >= 2 or (len(hits) == 1 and len(payload) == 1):
            found.append(hits)
        for x in payload[:50]:
            found += find_job_arrays(x, depth + 1)
    elif isinstance(payload, dict):
        for v in payload.values():
            found += find_job_arrays(v, depth + 1)
    return found


@contextmanager
def browser_page(headless: bool = True, locale: str = "en-US", storage_state: str | None = None):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx = browser.new_context(
            locale=locale,
            timezone_id="Asia/Riyadh",
            viewport={"width": 1440, "height": 1000},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"),
            storage_state=storage_state or None,
        )
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = ctx.new_page()
        try:
            yield page
        finally:
            ctx.close()
            browser.close()


class BrowserHarvest:
    """Navigate a listing URL, capture JSON traffic and JSON-LD, return raw dicts."""

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def run(self) -> list[dict]:
        captured: list[dict] = []
        jsonld: list[dict] = []
        cfg = self.cfg
        pages = cfg.get("pages", 1)

        with browser_page(headless=cfg.get("headless", True),
                          locale=cfg.get("locale", "en-US"),
                          storage_state=cfg.get("storage_state")) as page:

            def on_response(resp):
                try:
                    ct = (resp.headers or {}).get("content-type", "")
                    if "json" not in ct or resp.status >= 400:
                        return
                    if len(captured) > 4000:
                        return
                    body = resp.json()
                    for arr in find_job_arrays(body):
                        captured.extend(arr)
                except Exception:
                    return

            page.on("response", on_response)

            for i in range(pages):
                url = cfg["url"].format(page=i, page1=i + 1, offset=i * cfg.get("page_size", 10))
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=cfg.get("timeout", 45000))
                except Exception as e:  # noqa: BLE001
                    log.warning("goto failed %s: %s", url, e)
                    continue
                page.wait_for_timeout(cfg.get("settle_ms", 3500))
                for _ in range(cfg.get("scrolls", 3)):
                    page.mouse.wheel(0, 2400)
                    page.wait_for_timeout(900)
                for sel in cfg.get("click_more", []):
                    for _ in range(cfg.get("click_more_times", 5)):
                        try:
                            btn = page.locator(sel).first
                            if btn.is_visible():
                                btn.click(timeout=4000)
                                page.wait_for_timeout(2000)
                            else:
                                break
                        except Exception:
                            break
                # JSON-LD
                try:
                    for raw in page.locator('script[type="application/ld+json"]').all_text_contents():
                        try:
                            data = json.loads(raw)
                        except Exception:
                            continue
                        for node in (data if isinstance(data, list) else [data]):
                            graph = node.get("@graph", [node]) if isinstance(node, dict) else []
                            for g in graph:
                                if isinstance(g, dict) and "jobposting" in str(g.get("@type", "")).lower():
                                    jsonld.append(g)
                except Exception:
                    pass
                # CSS fallback
                if not captured and not jsonld and cfg.get("selectors"):
                    captured.extend(self._css_scrape(page, cfg["selectors"]))

        return self._merge(captured, jsonld)

    @staticmethod
    def _css_scrape(page, sel: dict) -> list[dict]:
        out = []
        try:
            cards = page.locator(sel["card"])
            for i in range(min(cards.count(), 100)):
                c = cards.nth(i)
                def txt(key):
                    if not sel.get(key):
                        return ""
                    try:
                        return c.locator(sel[key]).first.inner_text(timeout=2500).strip()
                    except Exception:
                        return ""
                href = ""
                try:
                    href = c.locator(sel.get("link", "a")).first.get_attribute("href") or ""
                except Exception:
                    pass
                if txt("title"):
                    out.append({"title": txt("title"), "company": txt("company"),
                                "location": txt("location"), "url": href,
                                "id": href or txt("title")})
        except Exception:
            pass
        return out

    @staticmethod
    def _merge(captured: list[dict], jsonld: list[dict]) -> list[dict]:
        norm = []
        for g in jsonld:
            loc = g.get("jobLocation") or {}
            if isinstance(loc, list):
                loc = loc[0] if loc else {}
            addr = (loc or {}).get("address", {}) if isinstance(loc, dict) else {}
            city = addr.get("addressLocality") or addr.get("addressRegion") or ""
            norm.append({
                "title": g.get("title", ""),
                "company": ((g.get("hiringOrganization") or {}) or {}).get("name", ""),
                "location": f"{city} {addr.get('addressCountry','') if isinstance(addr,dict) else ''}".strip(),
                "description": strip_html(g.get("description", "")),
                "url": g.get("url") or g.get("@id") or "",
                "id": g.get("identifier", {}).get("value") if isinstance(g.get("identifier"), dict) else g.get("url", ""),
                "datePosted": g.get("datePosted", ""),
            })
        return captured + norm


class BrowserCollector:
    """Turns harvested dicts into Job objects using a per-source config."""

    family = "browser"

    def __init__(self, cfg: dict, http=None):
        self.cfg = cfg
        self.name = cfg["id"]

    def collect(self) -> list[Job]:
        raw = BrowserHarvest(self.cfg).run()
        base = self.cfg.get("url_base", "")
        out, seen = [], set()
        for d in raw:
            title = _pick(d, TITLE_KEYS)
            if not title:
                continue
            jid = _pick(d, ID_KEYS) or _pick(d, LINK_KEYS) or title
            if jid in seen:
                continue
            seen.add(jid)
            link = _pick(d, LINK_KEYS)
            if link and not link.startswith("http"):
                link = base.rstrip("/") + "/" + link.lstrip("/")
            if not link and self.cfg.get("url_template"):
                link = self.cfg["url_template"].format(id=jid)
            loc = _pick(d, LOC_KEYS)
            desc = _pick(d, DESC_KEYS)
            company = _pick(d, COMPANY_KEYS) or self.cfg.get("default_company", self.cfg["id"])
            if self.cfg.get("saudi_only", True) and loc and not is_saudi(loc, self.cfg.get("country_hint", "Saudi Arabia")):
                continue
            out.append(Job(
                source=self.cfg["id"], source_family=self.cfg.get("family", "browser"),
                external_id=str(jid), title=title, company=company,
                url=link or self.cfg.get("url", ""), location_raw=loc,
                description=desc, posted_at=str(d.get("datePosted", ""))[:10], raw=d,
            ))
        return out

    def safe_collect(self):
        try:
            return self.collect(), None
        except Exception as e:  # noqa: BLE001
            log.warning("browser collector %s failed: %s", self.name, e)
            return [], f"{type(e).__name__}: {e}"
