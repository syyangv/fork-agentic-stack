import importlib.util
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest


BRAIN = Path(__file__).resolve().parents[1] / ".agent"


@pytest.fixture
def dashboard(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("dashboard", BRAIN / "tools/show.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in (
        "EPISODIC", "CANDIDATES", "LESSONS_JSONL", "LESSONS_MD",
        "DREAM_LOG", "DREAM_STATE", "MANIFEST", "INFRASTRUCTURE_MANIFEST",
    ):
        monkeypatch.setattr(module, name, str(tmp_path / Path(getattr(module, name)).name))
    return module


def test_lesson_stats_matches_current_lesson_views(dashboard, monkeypatch):
    monkeypatch.syspath_prepend(str(BRAIN / "memory"))
    from orchestration.governance_recall import load_structured_state
    from render_lessons import _dedupe_by_id, load_lessons

    rows = [
        {"id": "retired", "status": "accepted", "claim": "Old guidance"},
        {"id": "current", "status": "provisional", "claim": "Draft guidance"},
        {"id": "draft", "status": "provisional", "claim": "Pending guidance"},
        {"status": "legacy", "claim": "First legacy entry"},
        {"id": "retired", "status": "retracted", "claim": "Old guidance"},
        {"id": "current", "status": "accepted", "claim": "Current guidance"},
        {"status": "legacy", "claim": "Second legacy entry"},
    ]
    path = Path(dashboard.LESSONS_JSONL)
    path.write_text("\n".join(map(json.dumps, rows)) + "\ninvalid json\n", encoding="utf-8")

    stats = json.loads(dashboard.render(json_out=True))["lessons"]
    rendered = _dedupe_by_id(load_lessons(str(path.parent)))
    recalled = load_structured_state(path)
    assert stats["count"] == len(rendered) == len(recalled) == 5
    assert stats["accepted"] == [row for row in recalled if row["status"] == "accepted"]
    assert stats["accepted"] == [rows[5]]
    assert stats["provisional"] == 1
    assert stats["retracted"] == 1
    assert stats["legacy"] == 2
    assert stats["from_md_fallback"] is False


def test_retracted_only_dashboard_reports_zero_accepted(dashboard):
    Path(dashboard.LESSONS_JSONL).write_text(
        '{"id":"one","status":"accepted"}\n{"id":"one","status":"retracted"}\n',
        encoding="utf-8",
    )

    rendered = dashboard.render(plain=True, width=80)
    assert re.search(r"\baccepted\s+0\s+\(source: lessons\.jsonl\)", rendered)
    assert re.search(r"\bretracted\s+1\b", rendered)


def test_markdown_fallback_counts_seed_lessons(dashboard):
    Path(dashboard.LESSONS_MD).write_text("# Lessons\n- Seed lesson\n", encoding="utf-8")

    stats = json.loads(dashboard.render(json_out=True))["lessons"]
    assert stats["count"] == 1
    assert stats["accepted"] == []
    assert stats["from_md_fallback"] is True
    assert "1 seed lessons in LESSONS.md" in dashboard.render(plain=True, width=80)


def test_failing_skills_excludes_harness_captures(dashboard):
    failure = {"result": "failure", "timestamp": datetime.now(timezone.utc).isoformat()}
    harness_names = ("claude-code", "codex", "gemini", "copilot-cli", "pi")
    skill_failure = {**failure, "skill": "real-skill"}
    rows = [
        *({**failure, "skill": name} for name in harness_names),
        {**skill_failure, "harness": "codex"},
        skill_failure,
        skill_failure,
    ]
    Path(dashboard.EPISODIC).write_text("\n".join(map(json.dumps, rows)) + "\n", encoding="utf-8")

    assert dashboard.failing_skills(threshold=1) == [("real-skill", 2)]
