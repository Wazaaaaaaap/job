#!/usr/bin/env python3
"""Load the hand-collected first batch (data/seed_jobs.json) into the store.

The seed exists so the dashboard is useful on day one, before the scheduled
engine has had a chance to run. Seed rows are tagged source_family="manual" and
carry no description, so the matcher renormalises their score and the dashboard
labels them. The first automated run supersedes them.
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from radar.matching.scorer import Matcher  # noqa: E402
from radar.models import Job  # noqa: E402
from radar.store import Store  # noqa: E402


def main():
    data = json.loads((ROOT / "data/seed_jobs.json").read_text(encoding="utf-8"))
    base = date.fromisoformat(data["collected_on"])
    listings = data["listing_sources"]
    store = Store(ROOT / "data/jobs.db")
    matcher = Matcher(ROOT / "config/profile.json")

    n = 0
    for row in data["jobs"]:
        posted = (base - timedelta(days=int(row.get("days_ago", 0)))).isoformat()
        j = Job(
            source="seed:sabbar", source_family="manual",
            external_id=row["url"], title=row["title"], company=row["company"],
            url=row["url"], location_raw=row["location"], posted_at=posted,
            description="",
        )
        matcher.score(j)
        store.upsert(j)
        n += 1
    store.commit()
    print(f"seeded {n} postings")


if __name__ == "__main__":
    main()
