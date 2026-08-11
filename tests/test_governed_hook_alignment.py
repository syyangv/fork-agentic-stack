import importlib.util
import json
import subprocess
import sys
from pathlib import Path

from harness_manager import doctor


ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / ".agent" / "harness" / "hooks"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, HOOKS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_codex_normalizes_evidenced_shell_shape():
    module = load("codex_post_tool")
    name, tool_input, response = module.normalize({
        "cwd": "/repo",
        "tool_input": {"command": "pytest -q"},
        "output": "1 passed",
        "exit_code": 0,
    })
    assert name == "Bash"
    assert tool_input == {"command": "pytest -q"}
    assert response == {"output": "1 passed", "exit_code": 0}


def test_codex_normalizes_command_alias_and_failure_fields():
    module = load("codex_post_tool")
    name, tool_input, response = module.normalize({
        "command": "false", "stderr": "failed", "exit_code": 1,
    })
    assert name == "Bash"
    assert tool_input == {"command": "false"}
    assert response["stderr"] == "failed"
    assert response["exit_code"] == 1


def test_session_complete_is_silent_and_appends_schema_valid_record(tmp_path):
    deployed = tmp_path / ".agent"
    deployed.mkdir()
    subprocess.run(["cp", "-R", str(ROOT / ".agent" / "harness"), str(deployed)], check=True)
    episodic = deployed / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl"
    episodic.parent.mkdir(parents=True)
    hook = deployed / "harness" / "hooks" / "session_complete.py"
    result = subprocess.run(
        [sys.executable, str(hook), "codex"],
        input=json.dumps({"session_id": "session-1", "transcript": "must not persist"}),
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0
    assert result.stdout == ""
    entry = json.loads(episodic.read_text().strip())
    assert entry["skill"] == "codex"
    assert entry["action"] == "session-complete"
    assert "transcript" not in json.dumps(entry)


def test_doctor_inventory_is_cross_harness_and_flags_only_real_orphans(tmp_path):
    hooks = tmp_path / ".agent" / "harness" / "hooks"
    hooks.mkdir(parents=True)
    for name in ("claude_code_post_tool.py", "codex_post_tool.py", "gemini_post_tool.py", "orphan.py"):
        (hooks / name).write_text("# fixture\n")
    configs = {
        ".claude/settings.json": "claude_code_post_tool.py",
        ".codex/hooks.json": "codex_post_tool.py",
        ".gemini/settings.json": "gemini_post_tool.py",
    }
    for rel, script in configs.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"hooks": {"PostToolUse": [{"hooks": [{
            "command": f'python3 "$ROOT/.agent/harness/hooks/{script}"'
        }]}]}}))
    status, lines = doctor._audit_harness_hook_wiring(tmp_path)
    assert status == doctor.YELLOW
    detail = "\n".join(lines)
    assert "orphan.py" in detail
    assert "gemini_post_tool.py" not in detail


def test_source_hooks_have_no_retired_provider_terms():
    active = ["claude_code_post_tool.py", "codex_post_tool.py", "gemini_post_tool.py",
              "session_complete.py", "_governed_capture.py"]
    text = "\n".join((HOOKS / name).read_text().casefold() for name in active)
    assert "memos" not in text
    assert "memtensor" not in text
    assert "behavioral provider" not in text


def test_durable_capture_redacts_common_credentials(monkeypatch):
    sys.path.insert(0, str(ROOT / ".agent" / "harness"))
    from hooks import post_execution as module
    written = []
    monkeypatch.setattr(module, "append_jsonl", lambda _path, entry: written.append(entry) or entry)
    module.log_execution(
        "codex", "bash: TOKEN=abc123secret pytest", "Authorization: BearerValue",
        True, reflection="used sk-proj_abcdefghijklmnop", importance=3,
    )
    serialized = json.dumps(written[0])
    assert "abc123secret" not in serialized
    assert "BearerValue" not in serialized
    assert "abcdefghijklmnop" not in serialized
    assert "[REDACTED]" in serialized
