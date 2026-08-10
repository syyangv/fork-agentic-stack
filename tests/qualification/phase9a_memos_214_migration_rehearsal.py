#!/usr/bin/env python3
"""Isolated MemOS 2.0.10 -> 2.0.14 migration, restore, and rollback rehearsal."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agent" / "memory"))

from orchestration.memos_backup import create_project_backup, restore_project_backup
from orchestration.memos_bridge import BridgeConfig, MemOSBridgeClient
from orchestration.memos_runtime import build_memos_config, write_config_atomic


PROJECT_ID = "0123456789abcdef"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bridge_path(plugin: Path) -> Path:
    return plugin / "node_modules/@memtensor/memos-local-plugin/dist/bridge.cjs"


def package_path(plugin: Path) -> Path:
    return plugin / "node_modules/@memtensor/memos-local-plugin/package.json"


def open_client(plugin: Path, project: Path) -> MemOSBridgeClient:
    memos_home = project / "profiles" / PROJECT_ID / "memos-plugin"
    home = project.parent / "homes" / project.parent.name
    home.mkdir(parents=True, exist_ok=True)
    memos_home.mkdir(parents=True, exist_ok=True)
    config = memos_home / "config.yaml"
    if not config.exists():
        write_config_atomic(config, build_memos_config(PROJECT_ID))
    environment = {
        "HOME": str(home), "MEMOS_HOME": str(memos_home),
        "MEMOS_CONFIG_FILE": str(config), "MEMOS_TELEMETRY_ENABLED": "0",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    command = (
        "node", str(bridge_path(plugin)), "--agent=hermes", "--no-viewer",
        f"--runtime-scope={PROJECT_ID}", f"--home={memos_home}",
    )
    return MemOSBridgeClient(BridgeConfig(
        command=command, env=environment, inherit_environment=False,
        call_timeout=20, shutdown_timeout=3, circuit_cooldown=0,
    ))


def add_episode(client: MemOSBridgeClient, index: int) -> str:
    session = f"migration-session-{index}"
    namespace = {
        "agentKind": "hermes", "profileId": PROJECT_ID,
        "workspaceId": PROJECT_ID, "workspacePath": "/synthetic/migration",
        "sessionKey": session,
    }
    common = {"agent": "hermes", "sessionId": session, "namespace": namespace}
    client.call("session.open", {**common, "meta": {"synthetic": True}}, timeout=20)
    started = client.call("turn.start", {
        **common, "userText": f"synthetic migration training turn {index}",
        "contextHints": {"synthetic": True}, "ts": int(time.time() * 1000),
    }, timeout=20)
    episode = started["query"]["episodeId"]
    client.call("turn.end", {
        **common, "episodeId": episode,
        "agentText": f"synthetic migration verified answer {index}",
        "toolCalls": [], "contextHints": {"synthetic": True},
        "ts": int(time.time() * 1000),
    }, timeout=20)
    client.call("episode.close", {"episodeId": episode}, timeout=20)
    client.call("session.close", {"sessionId": session}, timeout=20)
    return episode


def db_evidence(project: Path) -> dict:
    databases = sorted(project.rglob("*.db")) + sorted(project.rglob("*.sqlite3"))
    result = {}
    for database in databases:
        uri = f"file:{database.resolve().as_posix()}?mode=ro&immutable=1"
        with sqlite3.connect(uri, uri=True) as connection:
            quick = connection.execute("PRAGMA quick_check").fetchone()[0]
            schemas = connection.execute(
                "SELECT type,name,coalesce(sql,'') FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
            ).fetchall()
            tables = [row[1] for row in schemas if row[0] == "table"]
            counts = {}
            content = {}
            for table in tables:
                quoted = '"' + table.replace('"', '""') + '"'
                rows = connection.execute(f"SELECT * FROM {quoted}").fetchall()
                columns = [item[0] for item in connection.execute(
                    f"SELECT * FROM {quoted} LIMIT 0"
                ).description]
                counts[table] = len(rows)
                content[table] = sorted(
                    [dict(zip(columns, row)) for row in rows],
                    key=lambda value: json.dumps(value, sort_keys=True, default=str),
                )
        canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), default=str)
        result[database.relative_to(project).as_posix()] = {
            "quick_check": quick, "schema": schemas, "row_counts": counts,
            "canonical_content_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
            "file_sha256": sha256(database),
        }
    return result


def marker_inventory(project: Path) -> list[dict]:
    rows = []
    for path in sorted(project.rglob(".migrations/*")):
        rows.append({
            "path": path.relative_to(project).as_posix(), "sha256": sha256(path),
            "content": json.loads(path.read_text("utf-8")),
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin-2010", required=True, type=Path)
    parser.add_argument("--plugin-2014", required=True, type=Path)
    parser.add_argument("--artifact-2010", required=True, type=Path)
    parser.add_argument("--artifact-2014", required=True, type=Path)
    parser.add_argument("--work-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    system_tmp = Path(tempfile.gettempdir()).resolve()
    try:
        args.work_root.resolve().relative_to(system_tmp)
    except ValueError:
        raise SystemExit(
            f"--work-root must be below the system temp directory: {system_tmp}"
        )
    if args.work_root.exists():
        raise SystemExit("--work-root must not exist")
    args.work_root.mkdir(parents=True)
    args.output.mkdir(parents=True, exist_ok=True)
    versions = {
        "2.0.10": json.loads(package_path(args.plugin_2010).read_text())["version"],
        "2.0.14": json.loads(package_path(args.plugin_2014).read_text())["version"],
    }
    if versions != {"2.0.10": "2.0.10", "2.0.14": "2.0.14"}:
        raise SystemExit(f"unexpected plugin versions: {versions}")

    fresh = args.work_root / "fresh-2014" / PROJECT_ID
    fresh_client = open_client(args.plugin_2014, fresh)
    fresh_health = fresh_client.health()
    fresh_client.close()

    old = args.work_root / "old-source" / PROJECT_ID
    old_client = open_client(args.plugin_2010, old)
    old_health = old_client.call("core.health", timeout=20)
    episodes = [add_episode(old_client, index) for index in range(2)]
    old_client.close()
    pre_upgrade = db_evidence(old)

    backup = create_project_backup(old, args.work_root / "backups", PROJECT_ID)
    upgrade = args.work_root / "upgrade-copy" / PROJECT_ID
    rollback = args.work_root / "pristine-rollback" / PROJECT_ID
    shutil.copytree(old, upgrade)
    shutil.copytree(old, rollback)

    upgraded_client = open_client(args.plugin_2014, upgrade)
    upgraded_health = upgraded_client.health()
    upgraded_client.call("memory.list_episodes", {
        "agent": "hermes", "namespace": {
            "agentKind": "hermes", "profileId": PROJECT_ID,
            "workspaceId": PROJECT_ID,
        }, "limit": 10,
    }, timeout=20)
    upgraded_client.close()

    upgraded_backup = create_project_backup(
        upgrade, args.work_root / "upgraded-backups", PROJECT_ID,
    )
    upgraded_restored = args.work_root / "upgraded-restored-copy" / PROJECT_ID
    upgraded_restore_rollback = restore_project_backup(
        upgraded_backup, upgraded_restored, PROJECT_ID,
    )
    upgraded_restored_client = open_client(args.plugin_2014, upgraded_restored)
    upgraded_restored_health = upgraded_restored_client.health()
    upgraded_restored_client.close()

    restore_parent = args.work_root / "restored-copy"
    restored = restore_parent / PROJECT_ID
    restored_rollback = restore_project_backup(backup, restored, PROJECT_ID)
    restored_client = open_client(args.plugin_2010, restored)
    restored_health = restored_client.call("core.health", timeout=20)
    restored_client.close()

    rollback_client = open_client(args.plugin_2010, rollback)
    rollback_health = rollback_client.call("core.health", timeout=20)
    rollback_client.close()

    paths = {"fresh_2014": fresh, "pre_upgrade_2010": old,
             "upgraded_copy_2014": upgrade, "backup_restored_2010": restored,
             "upgraded_backup_restored_2014": upgraded_restored,
             "pristine_rollback_2010": rollback}
    evidence = {
        "schema": "agentic.memory.phase9a-migration-rehearsal.v1",
        "scope": "isolated synthetic data only; deployed state untouched",
        "commands": [
            "python3 tests/qualification/phase9a_memos_214_migration_rehearsal.py "
            "--plugin-2010 $PLUGIN_2010 --plugin-2014 $PLUGIN_2014 "
            "--artifact-2010 $ARTIFACT_2010 --artifact-2014 $ARTIFACT_2014 "
            "--work-root $SYSTEM_TEMP/phase9a-migration --output $EVIDENCE_DIR"
        ],
        "versions": versions,
        "health_versions": {
            "fresh": fresh_health.get("version"), "old": old_health.get("version"),
            "upgraded": upgraded_health.get("version"),
            "upgraded_backup_restored": upgraded_restored_health.get("version"),
            "restored": restored_health.get("version"),
            "rollback": rollback_health.get("version"),
        },
        "seed_episode_ids": episodes,
        "databases": {name: db_evidence(path) for name, path in paths.items()},
        "config_migration_inventory": {
            name: marker_inventory(path) for name, path in paths.items()
        },
        "backups": {
            "pre_upgrade": {
                "path": backup.relative_to(args.work_root).as_posix(),
                "manifest_sha256": sha256(backup / "manifest.json"),
                "restored_replaced_existing": restored_rollback is not None,
            },
            "upgraded_with_marker": {
                "path": upgraded_backup.relative_to(args.work_root).as_posix(),
                "manifest_sha256": sha256(upgraded_backup / "manifest.json"),
                "restored_replaced_existing": upgraded_restore_rollback is not None,
            },
        },
        "artifacts": {
            version: {"bridge_sha256": sha256(bridge_path(plugin)),
                      "package_sha256": sha256(package_path(plugin)),
                      "artifact_tgz_sha256": sha256(artifact)}
            for version, plugin, artifact in (
                ("2.0.10", args.plugin_2010, args.artifact_2010),
                ("2.0.14", args.plugin_2014, args.artifact_2014),
            )
        },
        "pre_upgrade_snapshot": pre_upgrade,
    }
    evidence_path = args.output / "migration-evidence.json"
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    checksum_inputs = {
        "repo:tests/qualification/phase9a_memos_214_migration_rehearsal.py": Path(__file__),
        "evidence:migration-evidence.json": evidence_path,
        "backup:pre-upgrade/manifest.json": backup / "manifest.json",
        "backup:upgraded/manifest.json": upgraded_backup / "manifest.json",
        "runtime-2.0.10:dist/bridge.cjs": bridge_path(args.plugin_2010),
        "runtime-2.0.10:package.json": package_path(args.plugin_2010),
        "artifact:memos-local-plugin-2.0.10.tgz": args.artifact_2010,
        "runtime-2.0.14:dist/bridge.cjs": bridge_path(args.plugin_2014),
        "runtime-2.0.14:package.json": package_path(args.plugin_2014),
        "runtime-2.0.14:package-lock.json": args.plugin_2014 / "package-lock.json",
        "artifact:memos-local-plugin-2.0.14.tgz": args.artifact_2014,
    }
    checksums = {label: sha256(path) for label, path in checksum_inputs.items()}
    (args.output / "SHA256SUMS.json").write_text(
        json.dumps(checksums, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
