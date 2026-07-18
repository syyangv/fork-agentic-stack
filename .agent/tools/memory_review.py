"""Unified human review surface for candidate provenance and transitions."""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "memory"))

from candidate_lock import atomic_write_json, candidate_lifecycle_lock
from orchestration.revalidation import (
    RevalidationIndex, load_evidence_rows, validate_live_candidate_evidence,
)
from orchestration._core import validate_schema
from render_lessons import append_lesson_updates, load_lessons, render_lessons

CANDIDATES = BASE / "memory/candidates"
SEMANTIC = BASE / "memory/semantic"
LEDGER = BASE / "memory/evidence/ledger.jsonl"


def _candidate_path(candidate_id: str) -> Path:
    for subdir in ("", "deferred", "rejected", "graduated"):
        path = CANDIDATES / subdir / f"{candidate_id}.json"
        if path.is_file():
            return path
    raise FileNotFoundError(f"candidate not found: {candidate_id}")


def inspect_candidate(candidate_id: str) -> dict:
    path = _candidate_path(candidate_id)
    candidate = json.loads(path.read_text(encoding="utf-8"))
    evidence_by_id = {}
    evidence_error = None
    if LEDGER.is_file():
        try:
            evidence_by_id = {row["evidence_id"]: row for row in load_evidence_rows(LEDGER)}
        except Exception as exc:
            evidence_error = type(exc).__name__
    chain = []
    for evidence_id in candidate.get("evidence_refs", candidate.get("evidence_ids", [])):
        chain.append({
            "evidence_id": evidence_id,
            "record": evidence_by_id.get(evidence_id),
            "status": "resolved" if evidence_id in evidence_by_id else "missing",
        })
    lessons = [
        row for row in load_lessons(str(SEMANTIC))
        if row.get("source_candidate") == candidate_id
    ]
    return {
        "candidate": candidate, "source_path": str(path.relative_to(BASE)),
        "provider_ids": candidate.get("provider_ids", {}),
        "evidence_chain": chain, "evidence_error": evidence_error,
        "lesson_transitions": lessons,
    }


def finalize_graduated(candidate_id: str, rationale: str, reviewer: str) -> dict:
    if not rationale.strip():
        raise ValueError("acceptance rationale is required")
    with candidate_lifecycle_lock(str(CANDIDATES)):
        path = CANDIDATES / "graduated" / f"{candidate_id}.json"
        if not path.is_file():
            raise ValueError("candidate is not graduated")
        candidate = json.loads(path.read_text(encoding="utf-8"))
        prior_status = candidate.get("status")
        if prior_status not in {"provisional", "revalidation_needed"}:
            raise ValueError("candidate is not awaiting explicit acceptance")
        evidence_snapshot = None
        if candidate.get("code_specific"):
            evidence_snapshot = validate_live_candidate_evidence(candidate, BASE)
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        def build_update(rows):
            latest = None
            for lesson in rows:
                if lesson.get("source_candidate") == candidate_id:
                    latest = lesson
            if latest is None or latest.get("status") != prior_status:
                return []
            snapshot = evidence_snapshot or latest.get("evidence_snapshot")
            return [{
                **latest, "status": "accepted", "accepted_at": now,
                "reviewer": reviewer, "rationale": rationale,
                "evidence_snapshot": snapshot,
                "evidence_ids": candidate.get("evidence_refs", []),
                "code_refs": candidate.get("code_refs", []),
                "project_scope": candidate.get("project_scope", {}),
                "provider_ids": candidate.get("provider_ids", {}),
                "source_layer": candidate.get("source_layer", "behavioral"),
                "source_kind": candidate.get("source_kind"),
                "target_kind": candidate.get("target_kind"),
                "code_specific": bool(candidate.get("code_specific")),
            }]
        updates = append_lesson_updates(str(SEMANTIC), build_update)
        if not updates:
            raise ValueError("matching latest lesson state is missing")
        accepted = updates[0]
        render_lessons(str(SEMANTIC))
        candidate["status"] = "accepted"
        candidate["accepted_at"] = now
        candidate.setdefault("decisions", []).append({
            "ts": now, "action": "accepted", "reviewer": reviewer,
            "notes": rationale,
        })
        atomic_write_json(str(path), candidate)
        if prior_status == "revalidation_needed":
            provider_ids = candidate.get("provider_ids", {})
            RevalidationIndex(
                BASE / "memory/evidence/revalidation.sqlite3"
            ).clear_provider_stale(
                "memos-local",
                list(provider_ids.values()) if isinstance(provider_ids, dict) else [],
            )
        return accepted


# Backward-compatible import name for callers introduced earlier in Phase 7.
finalize_provisional = finalize_graduated


def classify_candidate(
    candidate_id: str, reviewer: str, code_refs: list[str], non_code: bool,
) -> None:
    """Require an explicit human code/non-code decision before graduation."""
    with candidate_lifecycle_lock(str(CANDIDATES)):
        path = CANDIDATES / f"{candidate_id}.json"
        graduated = False
        if not path.is_file():
            # Graduated candidates were classified during their initial review.
            path = CANDIDATES / "graduated" / f"{candidate_id}.json"
            graduated = path.is_file()
        if not path.is_file():
            raise ValueError("candidate is not available for classification")
        candidate = json.loads(path.read_text(encoding="utf-8"))
        classified = any(
            row.get("action") in {"classified_code", "classified_non_code"}
            for row in candidate.get("decisions", []) if isinstance(row, dict)
        )
        if graduated and (code_refs or non_code):
            raise ValueError(
                "graduated candidate classification cannot be changed during acceptance"
            )
        if not code_refs and not non_code:
            if classified:
                return
            raise ValueError(
                "behavioral candidates require --non-code-confirmed or --code-ref FILE::SYMBOL"
            )
        if code_refs and non_code:
            raise ValueError("code and non-code classification are mutually exclusive")
        parsed = []
        for value in code_refs:
            if "::" not in value:
                raise ValueError("code refs must use FILE::QUALIFIED_SYMBOL")
            file_path, qualified_name = value.split("::", 1)
            if not file_path.strip() or not qualified_name.strip():
                raise ValueError("code refs must use FILE::QUALIFIED_SYMBOL")
            parsed.append({
                "file_path": file_path.strip()[:500],
                "qualified_name": qualified_name.strip()[:500],
            })
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        candidate["code_specific"] = bool(parsed)
        candidate["code_refs"] = parsed
        candidate.setdefault("decisions", []).append({
            "ts": now,
            "action": "classified_code" if parsed else "classified_non_code",
            "reviewer": reviewer,
            "notes": "explicit human code-scope classification",
        })
        atomic_write_json(str(path), candidate)


def refresh_evidence(candidate_id: str, reviewer: str, evidence_refs: list[str]) -> None:
    """Explicitly replace revision-bound ledger evidence before graduation."""
    if not evidence_refs:
        return
    if any(
        len(value) != 68 or not value.startswith("evi_")
        or any(char not in "0123456789abcdef" for char in value[4:])
        for value in evidence_refs
    ):
        raise ValueError("evidence refs must use evi_ followed by 64 lowercase hex characters")
    with candidate_lifecycle_lock(str(CANDIDATES)):
        paths = [
            CANDIDATES / f"{candidate_id}.json",
            CANDIDATES / "graduated" / f"{candidate_id}.json",
        ]
        path = next((value for value in paths if value.is_file()), None)
        if path is None:
            raise ValueError("candidate is not available for evidence refresh")
        candidate = json.loads(path.read_text(encoding="utf-8"))
        if candidate.get("status") not in {"staged", "revalidation_needed"}:
            raise ValueError("evidence can be refreshed only before graduation or reacceptance")
        if candidate.get("code_specific") is not True:
            raise ValueError("revision-bound evidence refresh requires code classification")
        preserved = [
            value for value in candidate.get("evidence_refs", [])
            if isinstance(value, str) and not value.startswith("evi_")
        ]
        candidate["evidence_refs"] = list(dict.fromkeys([
            *preserved, *evidence_refs,
        ]))[:100]
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        candidate.setdefault("decisions", []).append({
            "ts": now, "action": "evidence_refreshed", "reviewer": reviewer,
            "notes": f"attached {len(evidence_refs)} reviewer-selected ledger evidence rows",
        })
        validate_schema(candidate, "candidate-v1.schema.json")
        atomic_write_json(str(path), candidate)


def _delegate(script: str, arguments: list[str]) -> int:
    return subprocess.run(
        [sys.executable, str(BASE / "tools" / script), *arguments], check=False,
    ).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Review memory candidates.")
    sub = parser.add_subparsers(dest="action", required=True)
    inspect = sub.add_parser("inspect"); inspect.add_argument("candidate_id")
    for action in ("provisional", "accept", "reject", "defer"):
        command = sub.add_parser(action); command.add_argument("candidate_id")
        command.add_argument("--rationale" if action in {"provisional", "accept"} else "--reason", required=True)
        command.add_argument("--reviewer", default="host-agent")
        if action in {"provisional", "accept"}:
            classification = command.add_mutually_exclusive_group()
            classification.add_argument("--code-ref", action="append", default=[])
            classification.add_argument("--non-code-confirmed", action="store_true")
            command.add_argument("--evidence-ref", action="append", default=[])
    reopen = sub.add_parser("reopen"); reopen.add_argument("candidate_id"); reopen.add_argument("--reviewer", default="host-agent")
    retract = sub.add_parser("retract"); retract.add_argument("lesson_id"); retract.add_argument("--rationale", required=True); retract.add_argument("--reviewer", default="host-agent")
    args = parser.parse_args()
    if args.action == "inspect":
        print(json.dumps(inspect_candidate(args.candidate_id), indent=2))
        return 0
    if args.action == "reopen":
        return _delegate("reopen.py", [args.candidate_id, "--reviewer", args.reviewer])
    if args.action == "retract":
        return _delegate("retract_lesson.py", [args.lesson_id, "--rationale", args.rationale, "--reviewer", args.reviewer])
    if args.action == "reject":
        return _delegate("reject.py", [args.candidate_id, "--reason", args.reason, "--reviewer", args.reviewer])
    if args.action == "defer":
        return _delegate("defer.py", [args.candidate_id, "--reason", args.reason, "--reviewer", args.reviewer])
    try:
        classify_candidate(
            args.candidate_id, args.reviewer, args.code_ref,
            args.non_code_confirmed,
        )
        refresh_evidence(args.candidate_id, args.reviewer, args.evidence_ref)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if args.action == "provisional":
        return _delegate("graduate.py", [args.candidate_id, "--provisional", "--rationale", args.rationale, "--reviewer", args.reviewer])
    try:
        if (CANDIDATES / "graduated" / f"{args.candidate_id}.json").is_file():
            finalize_graduated(args.candidate_id, args.rationale, args.reviewer)
            return 0
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return _delegate("graduate.py", [args.candidate_id, "--rationale", args.rationale, "--reviewer", args.reviewer])


if __name__ == "__main__":
    raise SystemExit(main())
