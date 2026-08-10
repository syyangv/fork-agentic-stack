import json
import os
import stat
from pathlib import Path

import pytest

from harness_manager.memos_config_migration import approved_config, migrate_owned_legacy_configs


PROJECT = "0123456789abcdef"


def legacy_config() -> dict:
    value = approved_config(PROJECT)
    value["embedding"] = {"provider": "local", "model": "Xenova/all-MiniLM-L6-v2",
                          "cache": {"enabled": True, "maxItems": 20000}}
    value["llm"] = {"provider": "host", "model": "opus",
                    "fallbackToHost": False, "maxRetries": 0}
    return value


def owned_tree(root: Path) -> list[Path]:
    pilots = root / "pilot-configs"
    pilots.mkdir(parents=True)
    manifest = pilots / f"{PROJECT}.json"
    manifest.write_text(json.dumps({
        "schema": "agentic.memory.evolution-pilot.v2", "enabled": False,
        "project_id": PROJECT, "repo_root": str(root), "provider": "claude_opus",
        "model": "opus", "daily_caps": {"policy": 5, "world_model": 2, "skill": 2, "other": 50},
        "min_distinct_episodes": 3, "timeout_seconds": 60,
    }))
    manifest.chmod(0o600)
    active = root / PROJECT / "profiles" / PROJECT / "memos-plugin" / "config.yaml"
    rollback = root / f".{PROJECT}.rollback-{'a' * 32}" / "profiles" / PROJECT / "memos-plugin" / "config.yaml"
    for path in (active, rollback):
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(legacy_config()))
        path.chmod(0o600)
    return [active, rollback]


def test_migrates_only_manifest_owned_active_and_rollback_configs(tmp_path):
    paths = owned_tree(tmp_path)
    unrelated = tmp_path / "foreign" / "profiles" / PROJECT / "memos-plugin" / "config.yaml"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text(json.dumps(legacy_config()))
    result = migrate_owned_legacy_configs(tmp_path, tmp_path / "provenance")
    assert result.migrated == tuple(paths)
    for path in paths:
        value = json.loads(path.read_text())
        assert value["embedding"] == {"enabled": False, "provider": "lexical", "engine": "sqlite_fts5"}
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(unrelated.read_text())["embedding"]["provider"] == "local"
    record = json.loads(result.provenance.read_text())
    assert record["committed"] is True
    assert len(record["configs"]) == 2


def test_failure_compensates_every_published_config_exactly(tmp_path):
    paths = owned_tree(tmp_path)
    before = [(p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) for p in paths]
    with pytest.raises(RuntimeError, match="injected migration failure"):
        migrate_owned_legacy_configs(tmp_path, tmp_path / "provenance", fail_after=1)
    for path, snapshot in zip(paths, before):
        assert path.read_bytes() == snapshot[0]
        assert path.stat().st_mode == snapshot[1]
        assert path.stat().st_mtime_ns == snapshot[2]


def test_symlinked_owned_config_fails_before_mutation(tmp_path):
    paths = owned_tree(tmp_path)
    target = tmp_path / "target.json"
    target.write_text(json.dumps(legacy_config()))
    paths[1].unlink()
    paths[1].symlink_to(target)
    before = paths[0].read_bytes()
    with pytest.raises(ValueError, match="symlink"):
        migrate_owned_legacy_configs(tmp_path, tmp_path / "provenance")
    assert paths[0].read_bytes() == before


def test_non_owner_or_group_writable_config_is_rejected(tmp_path, monkeypatch):
    paths = owned_tree(tmp_path)
    paths[0].chmod(0o620)
    with pytest.raises(ValueError, match="mode"):
        migrate_owned_legacy_configs(tmp_path, tmp_path / "provenance")


def test_concurrent_replacement_compensates_prior_publication(tmp_path, monkeypatch):
    import harness_manager.memos_config_migration as migration
    paths = owned_tree(tmp_path)
    before = [(p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) for p in paths]
    real = migration._same_identity
    calls = 0
    def changed_on_second(snapshot):
        nonlocal calls
        calls += 1
        return real(snapshot) if calls == 1 else False
    monkeypatch.setattr(migration, "_same_identity", changed_on_second)
    with pytest.raises(RuntimeError, match="changed concurrently"):
        migrate_owned_legacy_configs(tmp_path, tmp_path / "provenance")
    for path, snapshot in zip(paths, before):
        assert path.read_bytes() == snapshot[0]
        assert path.stat().st_mode == snapshot[1]
        assert path.stat().st_mtime_ns == snapshot[2]


def test_owned_set_drift_and_symlinked_provenance_fail_closed(tmp_path):
    paths = owned_tree(tmp_path)
    with pytest.raises(RuntimeError, match="ownership set changed"):
        migrate_owned_legacy_configs(
            tmp_path, tmp_path / "provenance", configs=(paths[0],),
        )
    outside = tmp_path / "outside"
    outside.mkdir()
    provenance = tmp_path / "provenance-link"
    provenance.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinked"):
        migrate_owned_legacy_configs(tmp_path, provenance)


def test_provenance_contains_hashes_not_original_config_bytes(tmp_path):
    owned_tree(tmp_path)
    result = migrate_owned_legacy_configs(tmp_path, tmp_path / "provenance")
    record = json.loads(result.provenance.read_text())
    assert all("original_base64" not in row for row in record["configs"])


def test_partial_lexical_config_with_unsafe_policy_fails_closed(tmp_path):
    paths = owned_tree(tmp_path)
    value = approved_config(PROJECT)
    value["telemetry"]["enabled"] = True
    paths[0].write_text(json.dumps(value)); paths[0].chmod(0o600)
    with pytest.raises(ValueError, match="lexical config is not exact"):
        migrate_owned_legacy_configs(tmp_path, tmp_path / "provenance")
