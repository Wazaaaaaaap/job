"""SQLite persistence: dedup, first-seen tracking, run history."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .models import Job

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    uid TEXT PRIMARY KEY,
    fingerprint TEXT,
    source TEXT, source_family TEXT, external_id TEXT,
    title TEXT, company TEXT, url TEXT,
    location_raw TEXT, city_en TEXT, city_ar TEXT,
    description TEXT, employment_type TEXT,
    posted_at TEXT, first_seen TEXT, last_seen TEXT,
    score REAL, score_breakdown TEXT, matched_skills TEXT,
    missing_signals TEXT, verdict TEXT,
    is_open INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_fp ON jobs(fingerprint);
CREATE INDEX IF NOT EXISTS ix_seen ON jobs(last_seen);
CREATE INDEX IF NOT EXISTS ix_score ON jobs(score);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT, finished_at TEXT,
    collected INTEGER, new INTEGER, errors TEXT, per_source TEXT
);
"""


class Store:
    def __init__(self, path: str | Path = "data/jobs.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------- upserts
    def upsert(self, job: Job) -> bool:
        """Insert or refresh. Returns True when the job is new to the store."""
        cur = self.conn.execute("SELECT first_seen FROM jobs WHERE uid=?", (job.uid,))
        row = cur.fetchone()
        is_new = row is None
        if not is_new:
            job.first_seen = row["first_seen"]
        self.conn.execute(
            """INSERT INTO jobs (uid,fingerprint,source,source_family,external_id,title,company,url,
                location_raw,city_en,city_ar,description,employment_type,posted_at,first_seen,last_seen,
                score,score_breakdown,matched_skills,missing_signals,verdict,is_open)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
               ON CONFLICT(uid) DO UPDATE SET
                 last_seen=excluded.last_seen, title=excluded.title, url=excluded.url,
                 description=excluded.description, score=excluded.score,
                 score_breakdown=excluded.score_breakdown, matched_skills=excluded.matched_skills,
                 missing_signals=excluded.missing_signals, verdict=excluded.verdict,
                 city_en=excluded.city_en, city_ar=excluded.city_ar, is_open=1""",
            (job.uid, job.fingerprint, job.source, job.source_family, job.external_id,
             job.title, job.company, job.url, job.location_raw, job.city_en, job.city_ar,
             job.description, job.employment_type, job.posted_at, job.first_seen, job.last_seen,
             job.score, json.dumps(job.score_breakdown, ensure_ascii=False),
             json.dumps(job.matched_skills, ensure_ascii=False),
             json.dumps(job.missing_signals, ensure_ascii=False), job.verdict),
        )
        return is_new

    def close_stale(self, days: int = 10) -> int:
        """Mark postings not seen for N days as closed."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur = self.conn.execute("UPDATE jobs SET is_open=0 WHERE last_seen < ? AND is_open=1", (cutoff,))
        return cur.rowcount

    def commit(self):
        self.conn.commit()

    # ------------------------------------------------------------- queries
    def open_jobs(self, min_score: float = 0.0) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE is_open=1 AND score>=? ORDER BY score DESC, first_seen DESC",
            (min_score,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("score_breakdown", "matched_skills", "missing_signals"):
                try:
                    d[k] = json.loads(d[k] or "null")
                except Exception:
                    d[k] = None
            out.append(d)
        return out

    def dedup(self, jobs: list[dict]) -> list[dict]:
        """Collapse the same role appearing on several sources; keep the richest."""
        best: dict[str, dict] = {}
        priority = {"workday": 5, "oracle": 5, "successfactors": 5, "greenhouse": 5,
                    "lever": 5, "smartrecruiters": 5, "taleo": 4, "jadarat": 4,
                    "sabbar": 3, "aggregator": 2, "manual": 1}
        for j in jobs:
            fp = j["fingerprint"]
            cur = best.get(fp)
            if cur is None:
                j["also_on"] = []
                best[fp] = j
                continue
            cur.setdefault("also_on", [])
            keep_new = (priority.get(j["source_family"], 0), len(j.get("description") or "")) > \
                       (priority.get(cur["source_family"], 0), len(cur.get("description") or ""))
            if keep_new:
                j["also_on"] = cur["also_on"] + [{"source": cur["source"], "url": cur["url"]}]
                best[fp] = j
            else:
                cur["also_on"].append({"source": j["source"], "url": j["url"]})
        return list(best.values())

    def record_run(self, started, finished, collected, new, errors, per_source):
        self.conn.execute(
            "INSERT INTO runs (started_at,finished_at,collected,new,errors,per_source) VALUES (?,?,?,?,?,?)",
            (started, finished, collected, new,
             json.dumps(errors, ensure_ascii=False), json.dumps(per_source, ensure_ascii=False)),
        )
        self.conn.commit()

    def recent_runs(self, n: int = 14) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (n,)).fetchall()]
