"""Read-only provider for authoritative agentic-stack governance memory."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from ..contracts import ProvenanceRef, RetrievalItem
from ..governance_recall import recall_lessons


class GovernanceProvider:
    def __init__(self, agent_root: str | Path, project_id: str, word_set) -> None:
        self.root = Path(agent_root)
        self.project_id = project_id
        self.word_set = word_set

    def retrieve(self, intent: str, top_k: int = 3) -> tuple[list[RetrievalItem], dict]:
        lessons, meta = recall_lessons(
            intent,
            self.root / "memory/semantic/lessons.jsonl",
            self.root / "memory/semantic/LESSONS.md",
            self.word_set,
            top_k=top_k,
        )
        items = [self._lesson_item(lesson) for lesson in lessons]
        documents = (
            ("permission", "protocols/permissions.md", "authoritative permissions", "accepted"),
            ("preference", "memory/personal/PREFERENCES.md", "user preferences", "accepted"),
            ("decision", "memory/semantic/DECISIONS.md", "architectural decisions", "accepted"),
            ("review_queue", "memory/working/REVIEW_QUEUE.md", "pending review queue", "raw"),
        )
        present = set()
        for kind, relative, reason, status in documents:
            item = self._document_item(kind, relative, reason, status)
            if item:
                items.append(item)
                present.add(kind)
        warnings = []
        if "permission" not in present:
            warnings.append("governance_permissions_missing")
        health = {
            "status": "healthy" if not warnings else "degraded",
            "warnings": warnings,
            "accepted_lessons_considered": meta["considered"],
        }
        return items, health

    def _lesson_item(self, lesson: dict) -> RetrievalItem:
        claim = lesson["claim"]
        source = lesson.get("source", "unknown")
        source_id = lesson.get("id") or "seed:" + _digest(claim)[:16]
        path = "memory/semantic/lessons.jsonl" if source == "lessons.jsonl" else "memory/semantic/LESSONS.md"
        provenance = self._provenance("lesson", source_id, path, claim)
        return RetrievalItem(
            item_id=str(source_id), lane="governance", type="lesson", summary=claim,
            scope={"project_id": self.project_id, "harness": None}, status="accepted",
            provider_score=float(lesson["lexical_overlap"]),
            selection_reason="accepted lesson with lexical intent overlap",
            provenance=(provenance.to_dict(),), token_estimate=_tokens(claim), expires_at=None,
        )

    def _document_item(self, kind: str, relative: str, reason: str, status: str):
        path = self.root / relative
        if not path.is_file():
            return None
        content = path.read_text(encoding="utf-8")
        if not content.strip():
            return None
        summary = content[:2_000]
        source_id = f"{kind}:{_digest(relative)}"
        provenance = self._provenance(kind, source_id, relative, content)
        return RetrievalItem(
            item_id=source_id, lane="governance", type=kind, summary=summary,
            scope={"project_id": self.project_id, "harness": None}, status=status,
            provider_score=1.0, selection_reason=reason,
            provenance=(provenance.to_dict(),), token_estimate=_tokens(summary), expires_at=None,
        )

    def _provenance(self, kind: str, source_id: str, relative: str, content: str) -> ProvenanceRef:
        path = self.root / relative
        observed = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat().replace("+00:00", "Z")
        return ProvenanceRef(
            kind=kind, provider="agentic-stack", source_id=str(source_id),
            project_id=self.project_id, repository_revision=None,
            source_hash="sha256:" + _digest(content), observed_at=observed,
            confidence=1.0, freshness="fresh", locator={"path": relative},
        )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _tokens(value: str) -> int:
    return min(1_200, (len(value) + 3) // 4)
