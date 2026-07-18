"""Dependency-free local FTS ranking for authoritative governance records."""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence


def rank_governance_records(
    intent: str, records: Sequence[tuple[str, str]],
) -> dict[str, float]:
    """Return bounded local relevance without sending governance text off-host."""
    if not records:
        return {}
    terms = _terms(intent)
    if not terms:
        return {record_id: 0.0 for record_id, _text in records}
    try:
        with sqlite3.connect(":memory:") as connection:
            connection.execute(
                "create virtual table governance using fts5(record_id unindexed, content, "
                "tokenize='porter unicode61')"
            )
            connection.executemany(
                "insert into governance values (?,?)", records,
            )
            query = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)
            rows = connection.execute(
                "select record_id from governance where governance match ? order by rank",
                (query,),
            ).fetchall()
        ranked = {
            str(row[0]): 1.0 / (index + 1) for index, row in enumerate(rows)
        }
    except sqlite3.Error:
        ranked = {}
    lowered = set(terms)
    for record_id, text in records:
        if record_id not in ranked:
            words = set(_terms(text))
            ranked[record_id] = len(lowered & words) / max(1, len(lowered))
    return ranked


def _terms(value: str) -> list[str]:
    return [
        term.casefold() for term in re.findall(r"[\w\-]{2,}", value, re.UNICODE)
    ][:50]
