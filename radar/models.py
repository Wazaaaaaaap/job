"""Unified job schema + normalisation helpers."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------- text utils

_AR_DIACRITICS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")
_NON_WORD = re.compile(r"[^\w؀-ۿ]+", re.UNICODE)


def normalize_text(s: str | None) -> str:
    """Lowercase, strip Arabic diacritics, unify alef/ya/ta-marbuta, squash space."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    s = _AR_DIACRITICS.sub("", s)
    s = (s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
           .replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي"))
    s = s.lower()
    s = _NON_WORD.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def strip_html(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</p>|</li>|</div>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
          .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'"))
    return re.sub(r"[ \t]+\n", "\n", re.sub(r"[ \t]{2,}", " ", s)).strip()


# ---------------------------------------------------------------- city canon

CITY_CANON: dict[str, tuple[str, str]] = {}
_CITY_TABLE = [
    ("riyadh", "Riyadh", "الرياض", ["riyad", "الرياض", "ar riyadh", "ar-riyadh"]),
    ("jazan", "Jazan", "جازان", ["jizan", "gizan", "جيزان", "جازان", "jazan city"]),
    ("jeddah", "Jeddah", "جدة", ["jiddah", "jedda", "جده", "جدة"]),
    ("dammam", "Dammam", "الدمام", ["الدمام", "ad dammam"]),
    ("khobar", "Khobar", "الخبر", ["al khobar", "alkhobar", "الخبر"]),
    ("dhahran", "Dhahran", "الظهران", ["الظهران"]),
    ("jubail", "Jubail", "الجبيل", ["al jubail", "الجبيل"]),
    ("yanbu", "Yanbu", "ينبع", ["ينبع"]),
    ("makkah", "Makkah", "مكة", ["mecca", "مكه", "مكة المكرمة"]),
    ("madinah", "Madinah", "المدينة المنورة", ["medina", "المدينه", "al madinah"]),
    ("abha", "Abha", "أبها", ["ابها"]),
    ("khamis", "Khamis Mushait", "خميس مشيط", ["khamis mushayt", "خميس مشيط"]),
    ("tabuk", "Tabuk", "تبوك", ["تبوك"]),
    ("hail", "Hail", "حائل", ["ha'il", "حايل", "حائل"]),
    ("qassim", "Qassim", "القصيم", ["buraidah", "بريدة", "القصيم", "unaizah"]),
    ("najran", "Najran", "نجران", ["نجران"]),
    ("neom", "NEOM", "نيوم", ["نيوم", "neom"]),
    ("kaec", "KAEC", "مدينة الملك عبدالله الاقتصادية", ["king abdullah economic city"]),
    ("ahsa", "Al Ahsa", "الأحساء", ["hofuf", "الاحساء", "الهفوف"]),
    ("eastern", "Eastern Province", "المنطقة الشرقية", ["eastern region", "المنطقه الشرقيه", "الشرقية"]),
    ("baha", "Al Baha", "الباحة", ["al baha", "albaha", "الباحه"]),
    ("bisha", "Bisha", "بيشة", ["بيشه"]),
    ("sabya", "Sabya", "صبيا", ["صبيا"]),
    ("sakaka", "Sakaka", "سكاكا", ["jouf", "الجوف", "سكاكا"]),
    ("hafar", "Hafar Al Batin", "حفر الباطن", ["hafar al batin", "حفر الباطن"]),
    ("arar", "Arar", "عرعر", ["عرعر", "northern borders"]),
    ("rabigh", "Rabigh", "رابغ", ["رابغ"]),
    ("remote", "Remote", "عن بعد", ["عن بعد", "remote", "work from home", "hybrid remote"]),
    # Country-level fallback, matched only after every city above fails.
    ("saudi", "Saudi Arabia", "السعودية — مدينة غير محددة",
     ["saudi arabia", "ksa", "kingdom of saudi arabia", "السعوديه", "المملكه العربيه السعوديه"]),
]
for _k, _en, _ar, _al in _CITY_TABLE:
    for _a in [_en, _ar, _k, *_al]:
        CITY_CANON[normalize_text(_a)] = (_en, _ar)


def canon_city(raw: str | None) -> tuple[str, str]:
    """Return (english, arabic) canonical city. Falls back to the raw string."""
    n = normalize_text(raw)
    if not n:
        return ("Unspecified", "غير محدد")
    for key, val in CITY_CANON.items():
        if key and key in n:
            return val
    return (str(raw).strip()[:60], str(raw).strip()[:60])


def is_saudi(raw_location: str | None, extra: str = "") -> bool:
    n = normalize_text(f"{raw_location} {extra}")
    if not n:
        return False
    if any(t in n for t in ["saudi", "ksa", "السعوديه", "السعودية", "المملكه العربيه"]):
        return True
    return any(k in n for k, _ in [(k, v) for k, v in CITY_CANON.items()] if k and k in n)


# ---------------------------------------------------------------- job record

@dataclass
class Job:
    source: str                      # e.g. "workday:almarai"
    source_family: str               # workday | oracle | greenhouse | jadarat | ...
    external_id: str
    title: str
    company: str
    url: str
    location_raw: str = ""
    city_en: str = "Unspecified"
    city_ar: str = "غير محدد"
    description: str = ""
    employment_type: str = ""
    posted_at: str = ""              # ISO date if known
    first_seen: str = ""
    last_seen: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    # filled by the matcher
    score: float = 0.0
    score_breakdown: dict[str, Any] = field(default_factory=dict)
    matched_skills: list[str] = field(default_factory=list)
    missing_signals: list[str] = field(default_factory=list)
    verdict: str = ""

    def __post_init__(self):
        self.title = (self.title or "").strip()
        self.company = (self.company or "").strip()
        self.description = strip_html(self.description)[:12000]
        if self.location_raw and self.city_en == "Unspecified":
            self.city_en, self.city_ar = canon_city(self.location_raw)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.first_seen = self.first_seen or now
        self.last_seen = now

    @property
    def fingerprint(self) -> str:
        """Stable id used for dedup across sources (same role posted twice)."""
        basis = "|".join([
            normalize_text(self.company)[:40],
            normalize_text(self.title)[:60],
            normalize_text(self.city_en)[:25],
        ])
        return hashlib.sha1(basis.encode()).hexdigest()[:16]

    @property
    def uid(self) -> str:
        return hashlib.sha1(f"{self.source}|{self.external_id}".encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["uid"] = self.uid
        d["fingerprint"] = self.fingerprint
        d.pop("raw", None)
        return d

    @property
    def haystack(self) -> str:
        return normalize_text(f"{self.title} {self.company} {self.description}")
