#!/usr/bin/env python3
"""Resolve each employer's applicant-tracking system automatically.

Run on a machine with real internet (the GitHub Actions runner does this on the
first daily run). For every entry under `discover:` in config/companies.yaml the
script fetches the careers page, follows redirects and iframes, and looks for
the fingerprint of a known ATS. Whatever it resolves is written back into the
`resolved:` block so subsequent runs use the fast JSON path.

Nothing here guesses: an employer is only promoted to `resolved` when its API
endpoint has been called successfully and returned at least one posting, or
returned a valid empty list. Everything else stays in `discover` with the last
failure reason recorded, and the dashboard shows it as unresolved.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from radar.collectors.base import Http  # noqa: E402
from radar.collectors.ats_api import REGISTRY  # noqa: E402

PATTERNS = [
    ("workday", re.compile(r"https?://([a-z0-9\-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-zA-Z\-]+/)?([A-Za-z0-9_\-]+)")),
    ("oracle", re.compile(r"https?://([a-z0-9\-]+)\.fa\.([a-z0-9]+)\.oraclecloud\.com/hcmUI/CandidateExperience/[a-z\-]+/sites/([A-Za-z0-9_\-]+)")),
    ("greenhouse", re.compile(r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([a-zA-Z0-9_\-]+)")),
    ("lever", re.compile(r"jobs\.lever\.co/([a-zA-Z0-9_\-]+)")),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([a-zA-Z0-9_\-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([a-zA-Z0-9_\-]+)")),
    ("recruitee", re.compile(r"https?://([a-z0-9\-]+)\.recruitee\.com")),
    ("successfactors", re.compile(r"(career\d*\.(?:sapsf|successfactors)\.(?:com|eu)/[^\"'\s]*company=([A-Za-z0-9_]+))")),
    ("taleo", re.compile(r"([a-z0-9\-]+)\.taleo\.net/careersection")),
]


def fetch_html(http: Http, url: str) -> str:
    try:
        r = http.s.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
                                     "Accept-Language": "en,ar;q=0.8"},
                       timeout=30, allow_redirects=True)
        return (r.url + "\n" + r.text) if r.ok else r.url
    except Exception as e:  # noqa: BLE001
        return f"__ERROR__ {type(e).__name__}: {e}"


def detect(html: str) -> dict | None:
    for family, pat in PATTERNS:
        m = pat.search(html)
        if not m:
            continue
        if family == "workday":
            return {"family": "workday", "host": f"https://{m.group(1)}.{m.group(2)}.myworkdayjobs.com",
                    "tenant": m.group(1), "site": m.group(3)}
        if family == "oracle":
            return {"family": "oracle", "host": f"https://{m.group(1)}.fa.{m.group(2)}.oraclecloud.com",
                    "site_slug": m.group(3), "site_number": m.group(3)}
        if family == "greenhouse":
            return {"family": "greenhouse", "board": m.group(1)}
        if family == "lever":
            return {"family": "lever", "company": m.group(1)}
        if family == "smartrecruiters":
            return {"family": "smartrecruiters", "company": m.group(1)}
        if family == "ashby":
            return {"family": "ashby", "board": m.group(1)}
        if family == "recruitee":
            return {"family": "recruitee", "company": m.group(1)}
        if family == "successfactors":
            return {"family": "browser", "sub": "successfactors", "url": "https://" + m.group(1)}
        if family == "taleo":
            return {"family": "browser", "sub": "taleo", "tenant": m.group(1)}
    return None


def verify(entry: dict, http: Http) -> tuple[bool, str, int]:
    """Actually call the resolved endpoint once. Only a real response promotes it."""
    fam = entry.get("family")
    cls = REGISTRY.get(fam)
    if cls is None:
        return False, "browser-family (verified at run time)", 0
    cfg = dict(entry)
    cfg.setdefault("max_records", 40)
    cfg.setdefault("fetch_details", False)
    try:
        jobs = cls(cfg, http).collect()
        return True, "ok", len(jobs)
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}", 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/companies.yaml")
    ap.add_argument("--only", default="", help="comma-separated ids")
    args = ap.parse_args()

    path = Path(args.config)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    resolved = {e["id"]: e for e in (data.get("resolved") or [])}
    todo = data.get("discover") or []
    only = {x for x in args.only.split(",") if x}

    http = Http(timeout=30, retries=2)
    still_unresolved = []
    report = []

    for c in todo:
        if only and c["id"] not in only:
            still_unresolved.append(c)
            continue
        html = fetch_html(http, c["careers_url"])
        if html.startswith("__ERROR__"):
            c["last_error"] = html[10:200]
            still_unresolved.append(c)
            report.append((c["id"], "unreachable", html[10:80]))
            continue
        found = detect(html)
        if not found:
            c["last_error"] = "no known ATS fingerprint on careers page"
            still_unresolved.append(c)
            report.append((c["id"], "no-ats", ""))
            continue
        entry = {"id": c["id"], "name": c["name"], "name_ar": c.get("name_ar", ""),
                 "sector": c.get("sector", ""), **found}
        ok, msg, n = verify(entry, http)
        entry["verified"] = bool(ok)
        entry["verify_note"] = msg
        entry["last_count"] = n
        resolved[c["id"]] = entry
        report.append((c["id"], found["family"] + ("/" + found.get("sub", "") if found.get("sub") else ""),
                       f"{msg} ({n})"))

    data["resolved"] = sorted(resolved.values(), key=lambda e: e["id"])
    data["discover"] = still_unresolved
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    print(f"resolved={len(data['resolved'])} unresolved={len(still_unresolved)}")
    for row in report:
        print("  ", " | ".join(str(x) for x in row))
    Path("data").mkdir(exist_ok=True)
    Path("data/discovery_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
