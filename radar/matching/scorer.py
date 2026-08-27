"""CV <-> vacancy matching.

Deterministic, explainable, and free to run. Five weighted components produce a
0-100 fit score plus a breakdown so every number on the dashboard can be traced
back to the text that produced it.

    title      30   how close the job title is to the candidate's target roles
    skills     35   weighted coverage of the CV's skill clusters in the posting
    seniority  15   does the required experience band fit a 2026 graduate
    location   12   city preference weighting
    freshness   8   recency of the posting

Hard filters (score forced to 0) run before scoring: excluded occupations,
non-Saudi locations, and postings demanding a licence or degree the CV lacks.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone, date
from pathlib import Path

from ..models import normalize_text

WEIGHTS = {"title": 30.0, "skills": 35.0, "seniority": 15.0, "location": 12.0, "freshness": 8.0}

# years-of-experience patterns, English + Arabic
_YEARS_PATTERNS = [
    re.compile(r"(\d{1,2})\s*[-–to]{1,3}\s*(\d{1,2})\s*\+?\s*(?:years?|yrs?)", re.I),
    re.compile(r"(?:minimum|min\.?|at least|over)\s*(?:of\s*)?(\d{1,2})\s*\+?\s*(?:years?|yrs?)", re.I),
    re.compile(r"(\d{1,2})\s*\+\s*(?:years?|yrs?)", re.I),
    re.compile(r"(\d{1,2})\s*(?:years?|yrs?)\s*(?:of\s*)?(?:relevant\s*|related\s*|proven\s*)?experience", re.I),
    re.compile(r"(?:خبره|خبرة)\s*(?:لا\s*تقل\s*عن\s*)?(\d{1,2})\s*(?:سنه|سنة|سنوات)"),
    re.compile(r"(\d{1,2})\s*(?:سنه|سنة|سنوات)\s*(?:خبره|خبرة)"),
]

_GENERIC_TOKENS = {
    "engineer", "engineering", "specialist", "officer", "analyst", "coordinator",
    "assistant", "executive", "consultant", "agent", "representative", "technician",
    "manager", "supervisor", "lead", "senior", "junior", "staff", "expert",
    "مهندس", "اخصايي", "مسيول", "منسق", "محلل", "فني", "مساعد", "مشرف",
}

_SENIOR_WORDS = ["senior", "sr.", " sr ", "lead", "principal", "manager", "head of", "director",
                 "vp ", "vice president", "chief", "supervisor iii", "expert", "specialist iv",
                 "planner iv", "planner iii", "engineer iii", "engineer iv", "level iii",
                 "مدير", "رئيس", "قائد", "كبير", "استشاري"]
_ENTRY_WORDS = ["graduate", "fresh graduate", "entry level", "entry-level", "junior", "trainee",
                "intern", "internship", "co-op", "coop", "apprentice", "associate", "cooperative training",
                "خريج", "خريجين", "متدرب", "تدريب", "مبتدئ", "حديث التخرج"]

_LICENCE_BLOCKERS = ["scfhs", "saudi commission for health", "prometric", "dataflow",
                     "pmp certified required", "cpa license", "bar admission",
                     "رخصة هيئة التخصصات", "رخصة سياقة ثقيلة"]

_DEGREE_BLOCKERS = [
    ("phd", ["phd required", "doctorate required", "ph.d. required", "دكتوراه"]),
    ("medical", ["mbbs", "md degree", "doctor of medicine", "بكالوريوس طب"]),
    ("law", ["llb", "law degree", "بكالوريوس حقوق"]),
]


class Matcher:
    def __init__(self, profile_path: str | Path = "config/profile.json"):
        self.p = json.loads(Path(profile_path).read_text(encoding="utf-8"))
        self._skills = self.p["skills"]
        self._targets = [normalize_text(t) for t in
                         self.p["target_titles"]["en"] + self.p["target_titles"]["ar"]]
        self._city_w = {}
        for c in self.p["preferences"]["cities_priority"]:
            self._city_w[normalize_text(c["city_en"])] = c["weight"]
            self._city_w[normalize_text(c["city_ar"])] = c["weight"]
        self._default_city_w = self.p["preferences"]["default_city_weight"]
        self._reject = [normalize_text(t) for t in
                        self.p["exclusions"]["hard_reject_titles"] +
                        self.p["exclusions"]["hard_reject_titles_ar"]]

    # ------------------------------------------------------------- helpers
    @staticmethod
    def required_years(text: str) -> int | None:
        """Smallest credible minimum-years figure stated in the posting."""
        vals = []
        for pat in _YEARS_PATTERNS:
            for m in pat.finditer(text):
                try:
                    vals.append(int(m.group(1)))
                except Exception:
                    continue
        vals = [v for v in vals if 0 <= v <= 25]
        return min(vals) if vals else None

    def _title_score(self, title: str) -> tuple[float, str]:
        n = normalize_text(title)
        if not n:
            return 0.0, "no title"
        best, hit = 0.0, ""
        for t in self._targets:
            if not t:
                continue
            if t == n:
                return 1.0, t
            if t in n or n in t:
                cand = min(len(t), len(n)) / max(len(t), len(n))
                cand = max(cand, 0.72)
                if cand > best:
                    best, hit = cand, t
        if best:
            return best, hit
        # Token-overlap fallback. Generic job-title nouns are stripped first:
        # without that, "Technical Engineer" scores against every target that
        # ends in "engineer" and every posting looks like a partial match.
        tn = set(n.split()) - _GENERIC_TOKENS
        if not tn:
            return 0.0, ""
        for t in self._targets:
            tt = set(t.split()) - _GENERIC_TOKENS
            if not tt:
                continue
            ov = len(tn & tt) / len(tt)
            if ov > best:
                best, hit = ov, t
        return min(best, 0.5), hit

    def _skill_score(self, hay: str) -> tuple[float, list[str], list[str]]:
        total_w, got_w = 0.0, 0.0
        matched, missing = [], []
        for cat, spec in self._skills.items():
            w = spec["weight"]
            total_w += w
            terms = spec.get("terms_en", []) + spec.get("terms_ar", [])
            hits = [t for t in terms if normalize_text(t) and normalize_text(t) in hay]
            if hits:
                # diminishing returns: 1 hit = 0.55 of the cluster, 4+ hits = full
                cov = min(1.0, 0.55 + 0.15 * (len(hits) - 1))
                got_w += w * cov
                matched.extend(sorted(set(hits))[:6])
            else:
                missing.append(cat)
        return (got_w / total_w if total_w else 0.0), sorted(set(matched))[:18], missing

    def _seniority_score(self, title: str, hay: str) -> tuple[float, str]:
        yrs = self.required_years(hay)
        n_title = normalize_text(title)
        note = ""
        s = 0.6  # unstated requirement -> neutral-positive
        if yrs is not None:
            note = f"يطلب {yrs} سنة خبرة"
            if yrs <= 1:
                s = 1.0
            elif yrs == 2:
                s = 0.85
            elif yrs == 3:
                s = 0.6
            elif yrs == 4:
                s = 0.3
            elif yrs <= 6:
                s = 0.12
            else:
                s = 0.0
        if any(w in n_title or w in hay[:600] for w in [normalize_text(x) for x in _ENTRY_WORDS]):
            s = max(s, 0.95)
            note = (note + " | " if note else "") + "موجّه للخريجين/المبتدئين"
        if any(w in n_title for w in [normalize_text(x) for x in _SENIOR_WORDS]):
            s = min(s, 0.15)
            note = (note + " | " if note else "") + "مسمّى قيادي/أقدم"
        return s, note

    def _location_score(self, city_en: str, city_ar: str, hay: str) -> tuple[float, str]:
        for key in (normalize_text(city_en), normalize_text(city_ar)):
            if key in self._city_w:
                return self._city_w[key], city_ar or city_en
        if "عن بعد" in hay or "remote" in hay:
            return 0.85, "عن بعد"
        return self._default_city_w, city_ar or city_en or "غير محدد"

    @staticmethod
    def _freshness_score(posted_at: str, first_seen: str) -> tuple[float, int | None]:
        ref = posted_at or (first_seen or "")[:10]
        try:
            d = date.fromisoformat(ref[:10])
        except Exception:
            return 0.5, None
        age = (datetime.now(timezone.utc).date() - d).days
        if age < 0:
            age = 0
        if age <= 3:
            return 1.0, age
        if age <= 7:
            return 0.85, age
        if age <= 14:
            return 0.65, age
        if age <= 30:
            return 0.4, age
        if age <= 60:
            return 0.15, age
        return 0.0, age

    # ---------------------------------------------------------------- main
    def score(self, job) -> None:
        """Mutates the Job in place with score, breakdown, verdict."""
        hay = job.haystack
        n_title = normalize_text(job.title)

        # -------- hard filters
        blockers = []
        if any(r and r in n_title for r in self._reject):
            blockers.append("مهنة خارج نطاق السيرة")
        if any(b in hay for b in _LICENCE_BLOCKERS):
            blockers.append("يتطلب ترخيصًا مهنيًا غير متوفر")
        for label, needles in _DEGREE_BLOCKERS:
            if any(x in hay for x in needles):
                blockers.append(f"يتطلب مؤهلًا مختلفًا ({label})")
                break
        if blockers:
            job.score = 0.0
            job.verdict = "مستبعد"
            job.score_breakdown = {"blocked": blockers}
            job.missing_signals = blockers
            return

        t, t_hit = self._title_score(job.title)
        sk, matched, missing = self._skill_score(hay)
        sen, sen_note = self._seniority_score(job.title, hay)
        loc, loc_label = self._location_score(job.city_en, job.city_ar, hay)
        fr, age = self._freshness_score(job.posted_at, job.first_seen)

        # A posting whose description was not retrievable carries no skill
        # evidence either way. Scoring it as zero-coverage would understate the
        # fit, so the skills component is dropped and the remaining weights are
        # renormalised to 100. The dashboard labels these explicitly.
        thin = len(job.description.strip()) < 120
        if thin:
            avail = WEIGHTS["title"] + WEIGHTS["seniority"] + WEIGHTS["location"] + WEIGHTS["freshness"]
            k = 100.0 / avail
            raw = k * (t * WEIGHTS["title"] + sen * WEIGHTS["seniority"]
                       + loc * WEIGHTS["location"] + fr * WEIGHTS["freshness"])
        else:
            k = 1.0
            raw = (t * WEIGHTS["title"] + sk * WEIGHTS["skills"] + sen * WEIGHTS["seniority"]
                   + loc * WEIGHTS["location"] + fr * WEIGHTS["freshness"])
        if thin:
            # Bounded confidence: a posting judged without its own description
            # cannot enter the top band on title and location alone.
            raw = min(raw * 0.85, 69.0)
        job.score = round(raw, 1)
        job.matched_skills = matched
        job.missing_signals = [self._ar_cat(c) for c in missing]
        job.score_breakdown = {
            "thin": thin,
            "title": {"score": round(t * WEIGHTS["title"] * k, 1), "max": round(WEIGHTS["title"] * k, 1),
                      "matched": t_hit},
            "skills": None if thin else {"score": round(sk * WEIGHTS["skills"], 1), "max": WEIGHTS["skills"],
                       "coverage": round(sk * 100)},
            "seniority": {"score": round(sen * WEIGHTS["seniority"] * k, 1),
                          "max": round(WEIGHTS["seniority"] * k, 1),
                          "note": sen_note, "required_years": self.required_years(hay)},
            "location": {"score": round(loc * WEIGHTS["location"] * k, 1),
                         "max": round(WEIGHTS["location"] * k, 1), "city": loc_label},
            "freshness": {"score": round(fr * WEIGHTS["freshness"] * k, 1),
                          "max": round(WEIGHTS["freshness"] * k, 1), "age_days": age},
        }
        if thin:
            # The renormalisation note in the UI already says this; a list of
            # "missing" skill clusters would imply evidence we never had.
            job.missing_signals = []
        job.verdict = self.verdict(job.score)

    @staticmethod
    def verdict(score: float) -> str:
        if score >= 72:
            return "قدّم اليوم"
        if score >= 58:
            return "مرشّح قوي"
        if score >= 44:
            return "يستحق النظر"
        if score >= 30:
            return "احتمال ضعيف"
        return "غير مناسب"

    @staticmethod
    def _ar_cat(cat: str) -> str:
        return {
            "supply_chain": "سلاسل الإمداد",
            "process_improvement": "تحسين العمليات / لين",
            "quality": "الجودة",
            "data_analytics": "تحليل البيانات",
            "simulation_or": "المحاكاة وبحوث العمليات",
            "project_ops": "إدارة المشاريع",
            "domain_exposure": "خبرة القطاع",
        }.get(cat, cat)
