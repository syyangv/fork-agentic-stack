"""Read-only provider for authoritative agentic-stack governance memory."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from .._core import REDACTED, redact
from ..contracts import ContractError, ProvenanceRef, RetrievalItem
from ..governance_recall import recall_lessons


class GovernanceProvider:
    def __init__(self, agent_root: str | Path, project_id: str, word_set) -> None:
        self.root = Path(agent_root)
        self.project_id = project_id
        self.word_set = word_set

    def retrieve(self, intent: str, top_k: int = 3) -> tuple[list[RetrievalItem], dict]:
        warnings: list[str] = []
        items: list[RetrievalItem] = []
        documents = (
            ("permission", "protocols/permissions.md", self._whole_document),
            ("preference", "memory/personal/PREFERENCES.md", self._preferences),
            ("decision", "memory/semantic/DECISIONS.md", self._active_decisions),
            ("review_queue", "memory/working/REVIEW_QUEUE.md", self._whole_document),
        )
        present = set()
        for kind, relative, parser in documents:
            try:
                records = parser(self.root / relative)
                created, record_warnings = self._record_items(kind, relative, records)
                items.extend(created)
                warnings.extend(record_warnings)
                if created:
                    present.add(kind)
            except (OSError, UnicodeError, ContractError, ValueError) as exc:
                warnings.append(f"governance_document_error:{kind}:{type(exc).__name__}")

        lessons, meta = recall_lessons(
            intent, self.root / "memory/semantic/lessons.jsonl",
            self.root / "memory/semantic/LESSONS.md", self.word_set, top_k=top_k,
        )
        for lesson in lessons:
            try:
                items.append(self._lesson_item(lesson))
            except ContractError as exc:
                warnings.append(f"governance_lesson_error:{lesson.get('id') or 'seed'}:{type(exc).__name__}")
        if "permission" not in present:
            warnings.append("governance_permissions_missing")
        health = {
            "status": "healthy" if not warnings else "degraded",
            "warnings": list(warnings),
            "accepted_lessons_considered": meta["considered"],
        }
        return items, health

    @staticmethod
    def _whole_document(path: Path) -> list[str]:
        if not path.is_file():
            return []
        content = path.read_text(encoding="utf-8")
        return [content] if content.strip() else []

    @staticmethod
    def _preferences(path: Path) -> list[str]:
        if not path.is_file():
            return []
        records = []
        for line in path.read_text(encoding="utf-8").splitlines():
            value = line.strip()
            if not value.startswith("- "):
                continue
            value = value[2:].strip()
            if not value or value.startswith("_(e.g.") or value in {"_", "-"}:
                continue
            records.append(value)
        return records

    @staticmethod
    def _active_decisions(path: Path) -> list[str]:
        if not path.is_file():
            return []
        content = path.read_text(encoding="utf-8")
        records = []
        for match in re.finditer(r"(?ms)^## (?!#)(.+?)(?=^## |\Z)", content):
            record = "## " + match.group(1).strip()
            status = re.search(r"(?im)^\*\*Status:\*\*\s*([^\n]+)", record)
            if status and status.group(1).strip().lower() == "active":
                records.append(record)
        return records

    def _record_items(self, kind: str, relative: str, records: list[str]):
        items, warnings = [], []
        reasons = {
            "permission": "authoritative permissions",
            "preference": "configured user preference",
            "decision": "active architectural decision",
            "review_queue": "pending review queue",
        }
        status = "raw" if kind == "review_queue" else "accepted"
        for record_index, record in enumerate(records):
            cleaned = redact(record)
            if cleaned != record:
                warning = f"governance_redacted:{kind}"
                if warning not in warnings:
                    warnings.append(warning)
            for chunk_index, summary in enumerate(_chunks(cleaned)):
                source_id = f"{kind}:{_digest(relative + ':' + str(record_index) + ':' + str(chunk_index) + ':' + record)[:24]}"
                provenance = self._provenance(kind, source_id, relative, record)
                items.append(RetrievalItem(
                    item_id=source_id, lane="governance", type=kind, summary=summary,
                    scope={"project_id": self.project_id, "harness": None}, status=status,
                    provider_score=1.0, selection_reason=reasons[kind],
                    provenance=(provenance.to_dict(),), token_estimate=_tokens(summary), expires_at=None,
                ))
        return items, warnings

    def _lesson_item(self, lesson: dict) -> RetrievalItem:
        claim = redact(lesson["claim"])
        source = lesson.get("source", "unknown")
        source_id = lesson.get("id") or "seed:" + _digest(claim)[:16]
        relative = "memory/semantic/lessons.jsonl" if source == "lessons.jsonl" else "memory/semantic/LESSONS.md"
        provenance = self._provenance("lesson", str(source_id), relative, lesson["claim"])
        return RetrievalItem(
            item_id=str(source_id), lane="governance", type="lesson", summary=claim,
            scope={"project_id": self.project_id, "harness": None}, status="accepted",
            provider_score=float(lesson["lexical_overlap"]),
            selection_reason="accepted lesson with lexical intent overlap",
            provenance=(provenance.to_dict(),), token_estimate=_tokens(claim), expires_at=None,
        )

    def _provenance(self, kind: str, source_id: str, relative: str, content: str) -> ProvenanceRef:
        path = self.root / relative
        observed = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat().replace("+00:00", "Z")
        return ProvenanceRef(
            kind=kind, provider="agentic-stack", source_id=source_id,
            project_id=self.project_id, repository_revision=None,
            source_hash="sha256:" + _digest(content), observed_at=observed,
            confidence=1.0, freshness="fresh", locator={"path": relative},
        )


def _chunks(value: str, limit: int = 2_000) -> list[str]:
    return [value[index:index + limit] for index in range(0, len(value), limit)] or [REDACTED]


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _tokens(value: str) -> int:
    return min(1_200, (len(value) + 3) // 4)
