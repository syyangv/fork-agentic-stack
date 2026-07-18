"""Revalidate accepted code guidance against current CRG/test evidence."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "memory"))
from candidate_lock import atomic_write_json, candidate_lifecycle_lock
from orchestration.identity import derive_project_identity
from orchestration.revalidation import (
    RevalidationIndex, load_evidence_rows, revalidate_lessons,
    validate_live_candidate_evidence,
)
from orchestration.providers.crg_evidence import CrgEvidenceProvider, EvidenceLedger
from render_lessons import load_lessons


def main() -> int:
    parser = argparse.ArgumentParser(description="Revalidate code-linked memory.")
    parser.add_argument("--repo-root", default=os.environ.get("AGENTIC_PROJECT_ROOT", str(BASE.parent)))
    parser.add_argument("--registry", default=os.environ.get("AGENTIC_CRG_REGISTRY"))
    args = parser.parse_args()
    repo = Path(args.repo_root).resolve(strict=False)
    identity = derive_project_identity(repo, os.environ.get("AGENTIC_GIT_REMOTE"))
    ledger_path = BASE / "memory/evidence/ledger.jsonl"
    rows = load_evidence_rows(ledger_path) if ledger_path.is_file() else []
    provider = CrgEvidenceProvider(
        repo_root=repo, project_id=identity.project_id,
        registry_path=args.registry, ledger=EvidenceLedger(ledger_path),
    )
    health = provider.health()
    revision = health.get("repository_revision") or subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True,
    ).strip()
    semantic = BASE / "memory/semantic"
    changed = revalidate_lessons(
        semantic, project_id=identity.project_id, revision=revision,
        evidence_rows=rows, graph_updated_at=health.get("graph_updated_at"),
        live_validator=lambda candidate: validate_live_candidate_evidence(
            candidate, BASE, repo_root=repo, registry_path=args.registry,
            ledger_path=ledger_path,
        ),
    )
    latest = {}
    for lesson in load_lessons(str(semantic)):
        latest[lesson.get("id")] = lesson
    stale_evidence = sorted({
        evidence_id for lesson_id in changed
        for evidence_id in latest.get(lesson_id, {}).get("evidence_ids", [])
        if isinstance(evidence_id, str)
    })
    index = RevalidationIndex(BASE / "memory/evidence/revalidation.sqlite3")
    index.rebuild_from_candidates(BASE / "memory/candidates")
    affected = index.mark_evidence_stale(
        stale_evidence, "accepted guidance requires revalidation",
        f"revision:{revision}",
    )
    candidates = BASE / "memory/candidates"
    with candidate_lifecycle_lock(str(candidates)):
        for candidate_id in affected:
            path = candidates / "graduated" / f"{candidate_id}.json"
            if not path.is_file():
                continue
            candidate = json.loads(path.read_text(encoding="utf-8"))
            candidate["freshness"] = "stale"
            candidate["status"] = "revalidation_needed"
            candidate.setdefault("decisions", []).append({
                "ts": __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ).isoformat(),
                "action": "revalidation_requested", "reviewer": "crg-revalidator",
                "notes": "linked evidence became stale",
            })
            atomic_write_json(str(path), candidate)
    print(json.dumps({
        "status": "revalidated", "lessons_changed": changed,
        "candidates_changed": affected, "health": health,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
