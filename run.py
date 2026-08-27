#!/usr/bin/env python3
"""Daily entry point: collect -> score -> persist -> render.

    python run.py --all              full run (APIs + browser sources)
    python run.py --apis-only        skip Playwright (fast, CI-safe smoke test)
    python run.py --render-only      rebuild the dashboard from the existing DB
    python run.py --local            also run tier-3 sources with a local browser
                                     profile (LinkedIn/Indeed) - personal machine
                                     only, never in CI
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from radar.collectors.ats_api import REGISTRY
from radar.collectors.base import Http
from radar.collectors.browser import BrowserCollector
from radar.matching.scorer import Matcher
from radar.render.dashboard import render_dashboard
from radar.store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("radar")

ROOT = Path(__file__).resolve().parent


def load_yaml(p: str) -> dict:
    return yaml.safe_load((ROOT / p).read_text(encoding="utf-8")) or {}


def build_collectors(args) -> list:
    http = Http()
    out = []
    companies = load_yaml("config/companies.yaml")
    for e in companies.get("resolved") or []:
        cls = REGISTRY.get(e.get("family"))
        if cls:
            out.append(cls(e, http))
        elif not args.apis_only:
            out.append(BrowserCollector({**e, "id": e["id"], "family": "browser",
                                         "default_company": e.get("name", e["id"])}))
    if args.apis_only:
        return out
    for s in (load_yaml("config/sources.yaml").get("platforms") or []):
        if not s.get("enabled"):
            continue
        if s.get("tier") == 3 and not args.local:
            continue
        cfg = dict(s)
        if s.get("tier") == 3:
            cfg["storage_state"] = os.environ.get("RADAR_BROWSER_STATE", "")
            cfg["headless"] = False
        out.append(BrowserCollector(cfg))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--apis-only", action="store_true")
    ap.add_argument("--render-only", action="store_true")
    ap.add_argument("--local", action="store_true")
    ap.add_argument("--db", default="data/jobs.db")
    ap.add_argument("--out", default="docs/index.html")
    ap.add_argument("--min-score", type=float, default=25.0)
    args = ap.parse_args()

    store = Store(ROOT / args.db)
    matcher = Matcher(ROOT / "config/profile.json")

    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    per_source: dict[str, int] = {}
    errors: dict[str, str] = {}
    collected = new = 0

    if not args.render_only:
        for col in build_collectors(args):
            jobs, err = col.safe_collect()
            if err:
                errors[col.name] = err
            per_source[col.name] = len(jobs)
            for j in jobs:
                matcher.score(j)
                if store.upsert(j):
                    new += 1
                collected += 1
            store.commit()
            log.info("%-28s %4d jobs %s", col.name, len(jobs), f"ERR {err}" if err else "")
        closed = store.close_stale(days=int(os.environ.get("RADAR_STALE_DAYS", 10)))
        log.info("closed %d stale postings", closed)

    finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
    store.record_run(started, finished, collected, new, errors, per_source)

    rows = store.dedup(store.open_jobs(min_score=args.min_score))

    # Seed rows keep a link back to the public listing they were read from, so a
    # posting that has since expired is still traceable to a working page.
    seed_path = ROOT / "data/seed_jobs.json"
    if seed_path.exists():
        seed = json.loads(seed_path.read_text(encoding="utf-8"))
        by_url = {j["url"]: seed["listing_sources"].get(j.get("listing", ""), "")
                  for j in seed.get("jobs", [])}
        for r in rows:
            link = by_url.get(r.get("url"))
            if link:
                r.setdefault("also_on", []).append({"source": "قائمة صبار المصدر", "url": link})

    notice = None
    if any(r["source_family"] == "manual" for r in rows) and not per_source:
        notice = ("هذه دفعة أولية جُمعت يدويًا من قوائم صبار العامة قبل أول تشغيل مجدول. "
                  "أوصاف الإعلانات غير مجلوبة فيها، فالدرجات محسوبة على المسمى والموقع "
                  "ومستوى الخبرة والحداثة فقط ومحدودة بسقف ٦٩. أول تشغيل آلي يستبدلها "
                  "ببيانات كاملة من مصادر واجهات التوظيف.")

    payload = {
        "notice": notice,
        "generated_at": finished,
        "jobs": rows,
        "runs": store.recent_runs(),
        "errors": errors,
        "per_source": per_source,
        "profile": json.loads((ROOT / "config/profile.json").read_text(encoding="utf-8")),
        "sources": load_yaml("config/sources.yaml"),
        "companies": load_yaml("config/companies.yaml"),
    }
    Path(ROOT / "data/latest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    render_dashboard(payload, ROOT / args.out)
    log.info("wrote %s with %d jobs (%d new this run)", args.out, len(rows), new)
    return 0


if __name__ == "__main__":
    sys.exit(main())
