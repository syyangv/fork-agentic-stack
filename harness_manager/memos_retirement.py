"""One-time, preservation-first retirement of the rejected MemOS provider."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from pathlib import Path

from . import state


_OWNED_PATHS = (
    "memory/orchestration/assist_gate.py",
    "memory/orchestration/evolution_eval.py",
    "memory/orchestration/host_evolution.py",
    "memory/orchestration/memos_backup.py",
    "memory/orchestration/memos_bridge.py",
    "memory/orchestration/memos_factory.py",
    "memory/orchestration/memos_journal.py",
    "memory/orchestration/memos_runtime.py",
    "memory/orchestration/promotion.py",
    "memory/orchestration/providers/memos_local.py",
    "runtime/memos",
    "runtime/providers/memos-local-plugin",
)
_BACKUP_ONLY = ("memory/orchestration/config.json", "install.json")


def retire_memos(target_root: str | Path, *, backup_root: str | Path) -> dict[str, object]:
    target = Path(target_root).resolve()
    agent = target / ".agent"
    backup = Path(backup_root)
    _validate_transaction_roots(target, agent, backup)
    with state.install_state_lock(target):
        return _retire_memos_locked(target, agent, backup)


def _retire_memos_locked(target: Path, agent: Path, backup: Path) -> dict[str, object]:
    if backup.exists() or backup.is_symlink():
        raise FileExistsError(f"retirement backup already exists: {backup}")
    document = state._load_no_lock(state.install_state_path(target))
    if _already_retired(document, agent):
        return {"status": "already-retired", "removed": []}

    present = tuple((relative, agent / relative) for relative in _OWNED_PATHS if (agent / relative).exists() or (agent / relative).is_symlink())
    for relative, path in present:
        _validate_owned_tree(path, relative)
    for relative in _BACKUP_ONLY:
        path = agent / relative
        if path.is_symlink():
            raise ValueError(f"refusing symlinked retirement state path: {relative}")

    backup.mkdir(parents=True, mode=0o700)
    copied: list[str] = []
    try:
        for relative, source in present:
            destination = backup / "payload" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, destination, symlinks=False)
            else:
                shutil.copy2(source, destination, follow_symlinks=False)
            _verify_copy(source, destination)
            copied.append(relative)
        for relative in _BACKUP_ONLY:
            source = agent / relative
            if source.is_file() and not source.is_symlink():
                destination = backup / "payload" / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                _verify_copy(source, destination)
                copied.append(relative)
        manifest = _write_manifest(backup, copied)

        for _, path in sorted(present, key=lambda item: len(item[1].parts), reverse=True):
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        _write_retired_state(target, document)
        _write_governance_config(agent)
        result = {"status": "retired", "removed": [r for r, _ in present], "manifest": manifest}
    except BaseException:
        _restore_payload(agent, backup / "payload", copied)
        raise
    try:
        _make_read_only(backup)
    except OSError as exc:
        result["backup_hardening_warning"] = type(exc).__name__
    return result


def _already_retired(document: object, agent: Path) -> bool:
    orchestration = document.get("orchestration") if isinstance(document, dict) else None
    return (
        isinstance(orchestration, dict)
        and orchestration.get("architecture") == "governed-memory-code-evidence"
        and not any((agent / relative).exists() or (agent / relative).is_symlink() for relative in _OWNED_PATHS)
    )


def _validate_owned_tree(path: Path, relative: str) -> None:
    if path.is_symlink():
        raise ValueError(f"refusing symlinked MemOS-owned path: {relative}")
    if path.is_dir():
        for root, dirs, files in os.walk(path, followlinks=False):
            for name in (*dirs, *files):
                child = Path(root) / name
                if child.is_symlink():
                    if _approved_npm_bin_link(path, child, relative):
                        continue
                    raise ValueError(f"refusing symlink inside MemOS-owned path: {relative}")
                mode = child.lstat().st_mode
                if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                    raise ValueError(f"refusing special file inside MemOS-owned path: {relative}")


def _approved_npm_bin_link(root: Path, child: Path, relative: str) -> bool:
    if relative != "runtime/providers/memos-local-plugin":
        return False
    parts = child.relative_to(root).parts
    if len(parts) != 4 or parts[1:3] != ("node_modules", ".bin"):
        return False
    try:
        raw_target = os.readlink(child)
        if os.path.isabs(raw_target):
            return False
        resolved_root = root.resolve(strict=True)
        resolved_target = child.resolve(strict=True)
        resolved_target.relative_to(resolved_root)
        return resolved_target.is_file() and not resolved_target.is_symlink()
    except (OSError, ValueError):
        return False


def _write_retired_state(target: Path, document: object) -> None:
    value = dict(document) if isinstance(document, dict) else state.empty(target, "unknown")
    prior = value.get("orchestration") if isinstance(value.get("orchestration"), dict) else {}
    next_record = {
        "profile": "standard",
        "architecture": "governed-memory-code-evidence",
        "providers": ["governance", "crg-evidence"],
    }
    if "scheduled_runtime" in prior:
        next_record["scheduled_runtime"] = prior["scheduled_runtime"]
    value["orchestration"] = next_record
    state._save_locked(state.install_state_path(target), value)


def _write_governance_config(agent: Path) -> None:
    config = agent / "memory/orchestration/config.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "agentic.memory.config.v2",
        "architecture": "governed-memory-code-evidence",
        "total_token_budget": 7800,
        "lane_reserves": {"governance": 4800, "evidence": 3000},
        "project_aliases": {},
    }
    temporary = config.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, config)


def _write_manifest(backup: Path, copied: list[str]) -> str:
    files = {}
    payload = backup / "payload"
    for path in sorted(p for p in payload.rglob("*") if p.is_file()):
        files[path.relative_to(payload).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {"schema": "agentic.memos-retirement-backup.v1", "copied_roots": copied, "files": files}
    encoded = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    (backup / "MANIFEST.json").write_bytes(encoded)
    return hashlib.sha256(encoded).hexdigest()


def _validate_transaction_roots(target: Path, agent: Path, backup: Path) -> None:
    if agent.is_symlink() or not agent.is_dir():
        raise ValueError("target .agent must be a real directory")
    absolute_backup = backup if backup.is_absolute() else (Path.cwd() / backup)
    current = absolute_backup.parent
    while True:
        if current.exists() and current.is_symlink():
            raise ValueError("retirement backup ancestor must not be a symlink")
        if current == current.parent:
            break
        current = current.parent


def _verify_copy(source: Path, destination: Path) -> None:
    if source.is_file():
        if hashlib.sha256(source.read_bytes()).digest() != hashlib.sha256(destination.read_bytes()).digest():
            raise OSError(f"retirement backup verification failed: {source}")
        return
    source_files = {path.relative_to(source).as_posix(): hashlib.sha256(path.read_bytes()).digest() for path in source.rglob("*") if path.is_file()}
    destination_files = {path.relative_to(destination).as_posix(): hashlib.sha256(path.read_bytes()).digest() for path in destination.rglob("*") if path.is_file()}
    if source_files != destination_files:
        raise OSError(f"retirement backup verification failed: {source}")


def _restore_payload(agent: Path, payload: Path, copied: list[str]) -> None:
    if not payload.is_dir():
        return
    for relative in sorted(copied, key=lambda value: len(Path(value).parts)):
        source = payload / relative
        destination = agent / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.is_dir():
                shutil.rmtree(destination)
            else:
                destination.unlink()
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)


def _make_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        path.chmod(0o500 if path.is_dir() else 0o400)
    root.chmod(0o500)
