"""Cluster + extract + stage candidates. No graduation here — CLI tools do that.

Pipeline:
  1. cluster_and_extract(entries) — content clusters → structured patterns
  2. write_candidates(patterns, dir) — patterns → candidate JSON files

Every staged candidate carries lifecycle metadata (status, decisions,
rejection_count) from birth so repeated churn is visible rather than looking
fresh each time the pattern recurs.
"""
import os, json, datetime, hashlib
from cluster import content_cluster, extract_pattern
from review_state import _lessons_sha
from validate import extract_lesson_lines, check_exact_duplicate
from candidate_lock import atomic_write_json, candidate_lifecycle_lock


def cluster_and_extract(entries, threshold=0.3):
    """Cluster entries by content similarity, extract a pattern per cluster."""
    clusters = content_cluster(entries, threshold=threshold)
    return {p["name"]: p for p in (extract_pattern(c) for c in clusters)}


def _slug(pattern_or_key):
    """Slug for a pattern. Prefer pattern['id'] (claim-derived, stable across
    cluster membership changes); fall back to md5(key) for legacy callers."""
    if isinstance(pattern_or_key, dict) and pattern_or_key.get("id"):
        return pattern_or_key["id"]
    return hashlib.md5(str(pattern_or_key).encode()).hexdigest()[:12]


def _find_prior(slug, candidates_dir):
    """Look up any prior record for this slug across lifecycle subdirs.

    Returns (prev_dict, location) where location is one of
    'staged' | 'rejected' | 'graduated' | None. A slug can only live in
    one place at a time; the caller is responsible for cleaning up the
    old location when moving the candidate back to staged.
    """
    staged_path = os.path.join(candidates_dir, f"{slug}.json")
    if os.path.isfile(staged_path):
        try:
            with open(staged_path) as f:
                return json.load(f), "staged"
        except (OSError, json.JSONDecodeError):
            pass
    for sub in ("rejected", "deferred", "graduated"):
        path = os.path.join(candidates_dir, sub, f"{slug}.json")
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    return json.load(f), sub
            except (OSError, json.JSONDecodeError):
                pass
    return {}, None


def _human_rejection_is_terminal(candidate):
    """Only deterministic machine rejections may be reconsidered automatically."""
    automated_reviewers = {"heuristic_prefilter", "scheduled-deterministic-triage"}
    rejected = [
        decision for decision in candidate.get("decisions", [])
        if decision.get("action") == "rejected"
    ]
    if not rejected:
        return False
    return rejected[-1].get("reviewer") not in automated_reviewers


def _move_candidate(src, dst):
    os.replace(src, dst)


def write_candidates(patterns, candidates_dir):
    """Stage each pattern as a candidate JSON with lifecycle metadata.

    Checks all three lifecycle subdirs (staged / rejected / graduated) for an
    existing record with the same slug, and preserves its history.
      - staged already: append a new 'staged' decision, keep original staged_at.
      - rejected previously: move back to staged with rejection_count and
        decision log intact. The reviewer sees this as a recurring pattern,
        not a fresh one.
      - graduated previously: skip entirely. The lesson already lives in
        lessons.jsonl; re-staging would only create work the heuristic
        prefilter would then reject on exact-duplicate grounds.
    """
    if not patterns:
        return 0
    with candidate_lifecycle_lock(candidates_dir):
        return _write_candidates_locked(patterns, candidates_dir)


def _write_candidates_locked(patterns, candidates_dir):
    os.makedirs(candidates_dir, exist_ok=True)
    written = 0
    # Read LESSONS.md once — used to check whether specific duplicates that
    # blocked a prior heuristic rejection are still present.
    lessons_path = os.path.join(
        os.path.dirname(candidates_dir), "semantic", "LESSONS.md")
    lessons_text = ""
    if os.path.exists(lessons_path):
        try:
            with open(lessons_path, encoding="utf-8") as stream:
                lessons_text = stream.read()
        except OSError:
            pass
    current_terminal_lessons = set(extract_lesson_lines(lessons_text))

    for key, p in patterns.items():
        claim = (p.get("claim") or "").strip()
        if not claim:
            continue

        # Claim-level terminal check: if this exact claim is already an
        # accepted lesson, skip regardless of slug. Cluster membership
        # changes can shift the id (conditions = intersection, shrinks
        # when outlier members join), so the slug-based graduated check
        # below would miss an accepted pattern under a new id. This
        # catches it by claim text, which IS stable. Provisional and
        # legacy lessons don't appear in extract_lesson_lines, so they
        # correctly do NOT block re-review.
        if lessons_text and check_exact_duplicate(claim, lessons_text):
            continue

        # Prefer the claim+conditions id from extract_pattern — stable slug
        # means lifecycle state carries across cluster membership changes.
        slug = _slug(p)
        prev, prev_loc = _find_prior(slug, candidates_dir)

        # Recover a prior interrupted restage/reopen. The source record was
        # already atomically updated before its same-filesystem move failed.
        if prev_loc in ("rejected", "graduated") and prev.get("status") == "staged":
            src = os.path.join(candidates_dir, prev_loc, f"{slug}.json")
            dst = os.path.join(candidates_dir, f"{slug}.json")
            _move_candidate(src, dst)
            written += 1
            continue

        # Fully-accepted lesson — terminal, never resurrect.
        if prev_loc == "graduated" and prev.get("status") != "provisional":
            continue

        # A human rejection is terminal. New evidence may be inspected only
        # after an explicit reopen transition moves the record back to staged.
        if prev_loc == "rejected" and _human_rejection_is_terminal(prev):
            continue
        if prev_loc == "deferred":
            continue

        # For rejected + provisional-graduated, re-stage ONLY when something
        # material has changed since the last decision. Comparing reviewer
        # identity ("heuristic" vs "human") was a blunt proxy; what actually
        # matters is whether evidence or the specific blocker shifted.
        if prev_loc in ("rejected", "graduated"):
            last = (prev.get("decisions") or [])[-1] if prev.get("decisions") else {}
            prev_evidence = set(last.get("evidence_snapshot", []))
            new_evidence = set(p.get("evidence_ids", []))
            # Only NEW supporting episodes count as a change worth re-review.
            # Equality comparison would trigger on routine decay (old evidence
            # archived out of the cluster), even though nothing new arrived
            # and the original blocker is unchanged.
            evidence_changed = bool(new_evidence - prev_evidence)

            # Did the specific lesson(s) that triggered this rejection go
            # away? Uses stamped duplicate_claims rather than a whole-file
            # LESSONS.md hash — unrelated graduations no longer cause
            # heuristic-rejected candidates to churn.
            stamped_dups = last.get("duplicate_claims") or []
            if stamped_dups:
                blocker_still_present = any(
                    d in current_terminal_lessons for d in stamped_dups)
            else:
                # No specific stamp (older rejection or human reject).
                # Provisional and human-rejected cases gate on evidence alone.
                blocker_still_present = True

            if not evidence_changed and blocker_still_present:
                continue

        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        decisions = prev.get("decisions", [])
        decisions.append({"ts": now, "action": "staged", "reviewer": "auto_dream"})

        # Preserve original staged_at so priority + backlog age signals stay
        # meaningful across re-detections.
        staged_at = prev.get("staged_at") or now

        candidate = {
            "id": slug,
            "key": key,
            "name": p.get("name", key),
            "claim": claim,
            "conditions": p.get("conditions", []),
            "evidence_ids": p.get("evidence_ids", []),
            "cluster_size": p.get("cluster_size", 1),
            "canonical_salience": p.get("canonical_salience", 0.0),
            "staged_at": staged_at,
            "status": "staged",
            "decisions": decisions,
            "rejection_count": prev.get("rejection_count", 0),
        }

        staged_path = os.path.join(candidates_dir, f"{slug}.json")
        if prev_loc in ("rejected", "graduated"):
            prior_path = os.path.join(candidates_dir, prev_loc, f"{slug}.json")
            atomic_write_json(prior_path, candidate)
            _move_candidate(prior_path, staged_path)
        else:
            atomic_write_json(staged_path, candidate)
        written += 1
    return written
