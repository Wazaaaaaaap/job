"""Render the dashboard.

Two outputs from one template:
  docs/index.html    full document for GitHub Pages
  docs/artifact.html fragment (no doctype/html/head/body) for publishing as an
                     Artifact, which supplies its own page skeleton
"""
from __future__ import annotations

import json
from pathlib import Path

TEMPLATE = Path(__file__).with_name("template.html")

DOC_OPEN = """<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
"""
DOC_MID = "</head>\n<body>\n"
DOC_CLOSE = "</body>\n</html>\n"

_HEAD_TAGS = ("<title", "<link", "<style")


def _split_head_body(fragment: str) -> tuple[str, str]:
    """The template starts with head-ish tags then page content."""
    idx = fragment.find("<header")
    if idx == -1:
        return "", fragment
    return fragment[:idx], fragment[idx:]


def _slim(payload: dict) -> dict:
    """Trim the payload embedded in the page: descriptions are capped and raw
    config is reduced to what the UI actually reads."""
    jobs = []
    for j in payload.get("jobs", []):
        d = dict(j)
        d["description"] = (d.get("description") or "")[:1800]
        d.pop("raw", None)
        jobs.append(d)
    return {
        "generated_at": payload.get("generated_at", ""),
        "notice": payload.get("notice"),
        "jobs": jobs,
        "per_source": payload.get("per_source", {}),
        "errors": payload.get("errors", {}),
        "sources": {"platforms": [
            {k: v for k, v in p.items() if k in
             ("id", "name_ar", "name_en", "tier", "enabled", "saved_searches", "reason_ar")}
            for p in (payload.get("sources", {}).get("platforms") or [])
        ]},
    }


def render_dashboard(payload: dict, out_path: str | Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    tpl = TEMPLATE.read_text(encoding="utf-8")
    data = json.dumps(_slim(payload), ensure_ascii=False).replace("</script>", "<\\/script>")
    fragment = tpl.replace("__PAYLOAD__", data)

    head, body = _split_head_body(fragment)
    out_path.write_text(DOC_OPEN + head + DOC_MID + body + DOC_CLOSE, encoding="utf-8")
    (out_path.parent / "artifact.html").write_text(fragment, encoding="utf-8")
    return out_path
