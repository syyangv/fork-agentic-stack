from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness_manager.memos_retirement import retire_memos
from harness_manager import memos_retirement


def _seed(root: Path) -> None:
    agent = root / ".agent"
    (agent / "memory/semantic").mkdir(parents=True)
    (agent / "memory/episodic").mkdir(parents=True)
    (agent / "memory/orchestration").mkdir(parents=True)
    (agent / "runtime/memos").mkdir(parents=True)
    (agent / "runtime/providers/memos-local-plugin/2.0.14").mkdir(parents=True)
    (agent / "runtime/crg/project").mkdir(parents=True)
    (agent / "memory/semantic/lessons.jsonl").write_text("keep\n")
    (agent / "memory/episodic/events.jsonl").write_text("keep\n")
    (agent / "runtime/crg/project/graph.db").write_text("keep")
    (agent / "memory/orchestration/memos_bridge.py").write_text("retire")
    (agent / "runtime/memos/state.db").write_text("retire")
    (agent / "runtime/providers/memos-local-plugin/2.0.14/package.json").write_text("retire")
    (agent / "install.json").write_text(json.dumps({
        "schema_version": 1,
        "orchestration": {
            "profile": "standard",
            "memos_capability": "available",
            "memos_mode": "off",
            "evolution_enabled": False,
        },
        "adapters": {},
    }))


def test_retirement_preserves_governance_and_crg(tmp_path: Path) -> None:
    _seed(tmp_path)
    backup = tmp_path / "backup"

    result = retire_memos(tmp_path, backup_root=backup)

    assert result["status"] == "retired"
    assert (tmp_path / ".agent/memory/semantic/lessons.jsonl").read_text() == "keep\n"
    assert (tmp_path / ".agent/memory/episodic/events.jsonl").read_text() == "keep\n"
    assert (tmp_path / ".agent/runtime/crg/project/graph.db").read_text() == "keep"
    assert not (tmp_path / ".agent/runtime/memos").exists()
    assert not (tmp_path / ".agent/runtime/providers/memos-local-plugin").exists()
    state = json.loads((tmp_path / ".agent/install.json").read_text())
    assert state["orchestration"]["architecture"] == "governed-memory-code-evidence"
    assert not any("memos" in key.lower() for key in state["orchestration"])
    assert (backup / "MANIFEST.json").is_file()


def test_retirement_refuses_symlinked_owned_path(tmp_path: Path) -> None:
    _seed(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    bridge = tmp_path / ".agent/memory/orchestration/memos_bridge.py"
    bridge.unlink()
    bridge.symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        retire_memos(tmp_path, backup_root=tmp_path / "backup")

    assert outside.is_dir()
    assert (tmp_path / ".agent/runtime/memos/state.db").is_file()


def test_retirement_refuses_existing_backup(tmp_path: Path) -> None:
    _seed(tmp_path)
    backup = tmp_path / "backup"
    backup.mkdir()

    with pytest.raises(FileExistsError):
        retire_memos(tmp_path, backup_root=backup)


def test_retirement_refuses_nested_runtime_symlink(tmp_path: Path) -> None:
    _seed(tmp_path)
    outside = tmp_path / "outside.db"
    outside.write_text("user-owned")
    nested = tmp_path / ".agent/runtime/memos/linked.db"
    nested.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink inside"):
        retire_memos(tmp_path, backup_root=tmp_path / "backup")
    assert outside.read_text() == "user-owned"


def test_retirement_allows_contained_npm_bin_links(tmp_path: Path) -> None:
    _seed(tmp_path)
    plugin = tmp_path / ".agent/runtime/providers/memos-local-plugin/2.0.14"
    executable = plugin / "node_modules/tool/bin/tool.js"
    executable.parent.mkdir(parents=True)
    executable.write_text("safe")
    bin_dir = plugin / "node_modules/.bin"
    bin_dir.mkdir()
    (bin_dir / "tool").symlink_to("../tool/bin/tool.js")
    result = retire_memos(tmp_path, backup_root=tmp_path / "backup")
    assert result["status"] == "retired"


def test_retirement_rejects_escaping_npm_bin_link(tmp_path: Path) -> None:
    _seed(tmp_path)
    plugin = tmp_path / ".agent/runtime/providers/memos-local-plugin/2.0.14"
    outside = tmp_path / "outside.js"
    outside.write_text("user-owned")
    bin_dir = plugin / "node_modules/.bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "tool").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink inside"):
        retire_memos(tmp_path, backup_root=tmp_path / "backup")
    assert outside.read_text() == "user-owned"


def test_retirement_is_noop_after_success(tmp_path: Path) -> None:
    _seed(tmp_path)
    retire_memos(tmp_path, backup_root=tmp_path / "first")
    result = retire_memos(tmp_path, backup_root=tmp_path / "second")
    assert result["status"] == "already-retired"


def test_failure_restores_only_captured_paths_and_preserves_memory(tmp_path: Path, monkeypatch) -> None:
    _seed(tmp_path)
    unrelated = tmp_path / ".agent/memory/personal/PREFERENCES.md"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("user-owned\n")
    original_state = (tmp_path / ".agent/install.json").read_bytes()

    def fail(_agent: Path) -> None:
        raise OSError("injected publish failure")

    monkeypatch.setattr(memos_retirement, "_write_governance_config", fail)
    with pytest.raises(OSError, match="injected"):
        retire_memos(tmp_path, backup_root=tmp_path / "backup")

    assert unrelated.read_text() == "user-owned\n"
    assert (tmp_path / ".agent/runtime/memos/state.db").read_text() == "retire"
    assert (tmp_path / ".agent/install.json").read_bytes() == original_state


def test_interrupt_rolls_back_destructive_work(tmp_path: Path, monkeypatch) -> None:
    _seed(tmp_path)
    monkeypatch.setattr(
        memos_retirement, "_write_governance_config",
        lambda _agent: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    with pytest.raises(KeyboardInterrupt):
        retire_memos(tmp_path, backup_root=tmp_path / "backup")
    assert (tmp_path / ".agent/runtime/memos/state.db").read_text() == "retire"
    assert (tmp_path / ".agent/memory/semantic/lessons.jsonl").read_text() == "keep\n"


def test_retirement_rejects_symlinked_agent_root(tmp_path: Path) -> None:
    real = tmp_path / "real-agent"
    real.mkdir()
    (tmp_path / ".agent").symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="real directory"):
        retire_memos(tmp_path, backup_root=tmp_path / "backup")


def test_retirement_rejects_symlinked_backup_ancestor(tmp_path: Path) -> None:
    _seed(tmp_path)
    real = tmp_path / "real-backups"
    real.mkdir()
    alias = tmp_path / "backup-alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="ancestor"):
        retire_memos(tmp_path, backup_root=alias / "retirement")


def test_backup_verification_failure_precedes_deletion(tmp_path: Path, monkeypatch) -> None:
    _seed(tmp_path)
    monkeypatch.setattr(
        memos_retirement, "_verify_copy",
        lambda _source, _destination: (_ for _ in ()).throw(OSError("corrupt copy")),
    )
    with pytest.raises(OSError, match="corrupt copy"):
        retire_memos(tmp_path, backup_root=tmp_path / "backup")
    assert (tmp_path / ".agent/runtime/memos/state.db").read_text() == "retire"
