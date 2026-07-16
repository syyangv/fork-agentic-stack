"""Independent frozen recall baseline used only for rollout comparison.

Do not refactor this module to call governance_recall: independence is what
allows --legacy to detect eligibility, ranking, or formatting regressions.
"""
from __future__ import annotations

import json
import re
from pathlib import Path


def recall(intent, lessons_jsonl, lessons_md, word_set, top_k=3, min_score=0.01):
    latest, order = {}, []
    path = Path(lessons_jsonl)
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = row.get("id") or f"__row_{len(order)}"
            if key not in latest:
                order.append(key)
            latest[key] = row
    structured = []
    blocked_claims = set()
    for key in order:
        row = latest[key]
        normalized = _normalize(row.get("claim", ""))
        if normalized:
            blocked_claims.add(normalized)
        if row.get("status") == "accepted":
            item = dict(row)
            item["_source"] = "lessons.jsonl"
            structured.append(item)
    markdown = []
    md_path = Path(lessons_md)
    if md_path.is_file():
        for line in md_path.read_text(encoding="utf-8").splitlines():
            value = line.strip()
            if not value.startswith("- "):
                continue
            annotation = re.search(r"status=(\w+)", value)
            claim = value[2:].split("<!--", 1)[0].strip()
            if ((annotation and annotation.group(1) != "accepted") or
                    claim.startswith("[PROVISIONAL]") or
                    (claim.startswith("~~") and claim.endswith("~~")) or
                    _normalize(claim) in blocked_claims):
                continue
            markdown.append({"id": None, "claim": claim, "conditions": [],
                             "status": "accepted", "_source": "LESSONS.md"})
    lessons = structured + markdown
    query = word_set(intent)
    scored = []
    for lesson in lessons:
        claim_words = word_set(lesson.get("claim", ""))
        condition_words = set()
        for condition in lesson.get("conditions", []):
            condition_words |= word_set(condition)
        score = 0.0 if not query else (
            len(query & claim_words) + 2 * len(query & condition_words)
        ) / (3 * len(query))
        if score >= min_score:
            scored.append((score, lesson))
    scored.sort(key=lambda pair: -pair[0])
    result = [{"id": item.get("id"), "claim": item.get("claim"),
               "conditions": item.get("conditions", []),
               "lexical_overlap": round(score, 3),
               "source": item.get("_source", "unknown"),
               "accepted_at": item.get("accepted_at")}
              for score, item in scored[:top_k]]
    counts = {}
    for item in result:
        counts[item["source"]] = counts.get(item["source"], 0) + 1
    meta = {"intent": intent, "considered": len(lessons), "returned": len(result),
            "source_counts": counts, "only_md_available": not structured}
    return result, meta


def format_pretty(intent, result, meta):
    lines = [f"Consulted lessons for intent: {intent!r}",
             f"  ({meta['considered']} accepted lessons available in corpus)"]
    if meta.get("source_counts"):
        summary = ", ".join(f"{source}:{count}" for source, count in sorted(meta["source_counts"].items()))
        lines.append(f"  → returned {meta['returned']}: {summary}")
    if not result:
        lines.append("  → no relevant lessons. Proceeding without prior guidance.")
        return "\n".join(lines)
    lines.append("")
    for index, item in enumerate(result, 1):
        lines.append(f"  [{index}] lexical_overlap={item['lexical_overlap']}  {item['claim']}  [{item['source']}]")
        if item["conditions"]:
            lines.append(f"      conditions: {', '.join(item['conditions'])}")
    return "\n".join(lines)


def _normalize(value):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", value.lower())).strip()
