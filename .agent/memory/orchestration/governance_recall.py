"""Pure latest-state governance lesson loading, ranking, and formatting."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable


_STATUS_RE = re.compile(r"status=(\w+)")


def load_structured_state(path: str | Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            if line.strip():
                rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    latest: dict[str, dict] = {}
    order: list[str] = []
    no_id = []
    for row in rows:
        lesson_id = row.get("id")
        if not lesson_id:
            no_id.append(row)
            continue
        if lesson_id not in latest:
            order.append(lesson_id)
        latest[lesson_id] = row
    active = [latest[lesson_id] for lesson_id in order]
    active.extend(no_id)
    return [dict(lesson) for lesson in active]


def load_structured(path: str | Path) -> list[dict]:
    out = []
    for lesson in load_structured_state(path):
        if lesson.get("status") == "accepted":
            lesson.setdefault("_source", "lessons.jsonl")
            out.append(lesson)
    return out


def load_markdown(path: str | Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if not value.startswith("- ") or len(value) <= 2:
            continue
        if "<!--" in value:
            match = _STATUS_RE.search(value.split("<!--", 1)[1])
            if match and match.group(1) != "accepted":
                continue
        claim = value[2:].split("<!--")[0].strip()
        if claim.startswith("[PROVISIONAL]") or (
            claim.startswith("~~") and claim.endswith("~~")
        ):
            continue
        if claim:
            out.append({
                "id": None, "claim": claim, "conditions": [],
                "status": "accepted", "_source": "LESSONS.md",
            })
    return out


def normalize_claim(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", value.lower())).strip()


def merge_sources(structured_path: str | Path, markdown_path: str | Path) -> tuple[list[dict], bool]:
    latest = load_structured_state(structured_path)
    structured = []
    for item in latest:
        if item.get("status") == "accepted":
            item.setdefault("_source", "lessons.jsonl")
            structured.append(item)
    markdown = load_markdown(markdown_path)
    # Latest structured rows include tombstones. They must suppress matching
    # rendered Markdown even though only accepted rows are retrievable.
    seen = {normalize_claim(item.get("claim", "")) for item in latest}
    merged = list(structured)
    merged.extend(item for item in markdown if normalize_claim(item.get("claim", "")) not in seen)
    return merged, not structured


def lexical_score(claim: str, conditions: list[str], query_words: set[str], word_set: Callable[[str], set[str]]) -> float:
    if not query_words:
        return 0.0
    claim_words = word_set(claim)
    condition_words: set[str] = set()
    for condition in conditions or []:
        condition_words |= word_set(condition)
    return (len(query_words & claim_words) + 2 * len(query_words & condition_words)) / (3 * len(query_words))


def recall_lessons(intent: str, structured_path: str | Path, markdown_path: str | Path,
                   word_set: Callable[[str], set[str]], top_k: int = 3,
                   min_score: float = 0.01) -> tuple[list[dict], dict]:
    lessons, only_markdown = merge_sources(structured_path, markdown_path)
    query_words = word_set(intent)
    scored = []
    for lesson in lessons:
        score = lexical_score(lesson.get("claim", ""), lesson.get("conditions", []), query_words, word_set)
        if score >= min_score:
            scored.append((score, lesson))
    scored.sort(key=lambda pair: -pair[0])
    result = [{
        "id": lesson.get("id"), "claim": lesson.get("claim"),
        "conditions": lesson.get("conditions", []),
        "lexical_overlap": round(score, 3),
        "source": lesson.get("_source", "unknown"),
        "accepted_at": lesson.get("accepted_at"),
    } for score, lesson in scored[:top_k]]
    counts: dict[str, int] = {}
    for item in result:
        counts[item["source"]] = counts.get(item["source"], 0) + 1
    return result, {"intent": intent, "considered": len(lessons), "returned": len(result),
                    "source_counts": counts, "only_md_available": only_markdown}


def format_pretty(intent: str, result: list[dict], meta: dict) -> str:
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
        score = item.get("lexical_overlap", item.get("relevance", 0))
        lines.append(f"  [{index}] lexical_overlap={score}  {item['claim']}  [{item.get('source', 'unknown')}]")
        if item["conditions"]:
            lines.append(f"      conditions: {', '.join(item['conditions'])}")
    return "\n".join(lines)
