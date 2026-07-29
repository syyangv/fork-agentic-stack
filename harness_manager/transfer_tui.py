"""Onboarding-style transfer wizard and non-interactive transfer CLI."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import uuid
from pathlib import Path
from dataclasses import dataclass
from typing import Callable, Iterable

try:  # POSIX transfer roots use an advisory inode-bound transaction lock.
    import fcntl
except ImportError:  # pragma: no cover - Windows does not support this stack.
    fcntl = None  # type: ignore[assignment]

from . import install as install_mod
from . import profiles as profiles_mod
from . import schema as schema_mod
from . import state as state_mod
from .transfer_bundle import (
    apply_import_plan,
    BundleSecurityError,
    copy_agent_template,
    decode_payload,
    encode_bundle,
    export_bundle,
    preflight_import,
    validate_target_root,
)
from .transfer_plan import DEFAULT_SCOPES, VALID_TARGETS, build_plan


MAX_TRANSACTION_SNAPSHOT_BYTES = 32 * 1024 * 1024
MAX_TRANSACTION_SNAPSHOT_ENTRIES = 1_000


@dataclass(frozen=True)
class _SnapshotEntry:
    relative: Path
    kind: str
    mode: int | None
    mtime_ns: int | None
    content: bytes | str | None
    dev: int | None = None
    ino: int | None = None


def run(argv: list[str], target_root: Path, stack_root: Path) -> int:
    if not argv:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            print(
                "error: agentic-stack transfer is interactive in a TTY.\n"
                "use `agentic-stack transfer --help` for non-interactive export/import flags.",
                file=sys.stderr,
            )
            return 2
        return run_wizard(target_root=target_root, stack_root=stack_root)

    if argv[0] in ("--help", "-h"):
        print_help()
        return 0

    cmd = argv[0]
    if cmd == "export":
        return cmd_export(argv[1:], target_root=target_root, stack_root=stack_root)
    if cmd == "import":
        return cmd_import(argv[1:], target_root=target_root, stack_root=stack_root)

    print(f"error: unknown transfer command '{cmd}'", file=sys.stderr)
    print("run `agentic-stack transfer --help` for usage.", file=sys.stderr)
    return 2


def print_help() -> None:
    print(
        """agentic-stack transfer

Open an onboarding-style TUI wizard for moving portable .agent memory into
Codex, Cursor, Windsurf, or a terminal-only AGENTS.md setup.
The default bundle contains only preferences, decisions, accepted lessons,
and the validated bounded evidence ledger. Runtime files, databases, caches,
permissions, skills, and all CRG graph databases/registries/snapshots are
excluded from the default data transfer; rebuild CRG locally after import.
Behavioral MemOS data is never included by a scope: it requires the explicit
`--behavioral-export` flag plus a project ID, provenance, and output path.

Usage:
  agentic-stack transfer
  agentic-stack transfer export --target codex --print-curl
  agentic-stack transfer export --behavioral-export --project-id <16-hex> \\
      --project-provenance <repo> --behavioral-output <directory>
  agentic-stack transfer import --payload <payload> --sha256 <digest> --target codex

Commands:
  export    Build a portable transfer bundle from this repo's .agent memory
  import    Import a transfer bundle into this repo and install target adapters
"""
    )


def cmd_export(argv: list[str], target_root: Path, stack_root: Path) -> int:
    parser = argparse.ArgumentParser(prog="agentic-stack transfer export")
    parser.add_argument("--intent", default="transfer memory")
    parser.add_argument("--target", action="append", choices=VALID_TARGETS)
    parser.add_argument("--scope", action="append")
    parser.add_argument("--print-curl", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--payload-file")
    parser.add_argument("--behavioral-export", action="store_true")
    parser.add_argument("--project-id")
    parser.add_argument("--project-provenance")
    parser.add_argument("--behavioral-output")
    parser.add_argument("--behavioral-data-root")
    parser.add_argument("--repository-root")
    args = parser.parse_args(argv)

    behavioral_scope_tokens = {"behavioral", "behavioral_db", "behavioral_skills", "memos"}
    if any(
        isinstance(scope, str) and scope.casefold().strip().replace("-", "_") in behavioral_scope_tokens
        for scope in (args.scope or [])
    ) and not args.behavioral_export:
        print(
            "error: behavioral data requires --behavioral-export; --scope cannot select it",
            file=sys.stderr,
        )
        return 2

    if args.behavioral_export:
        if args.scope or args.target or args.print_curl or args.payload_file:
            print(
                "error: --behavioral-export is a separate non-authoritative export and cannot use transfer scopes/targets",
                file=sys.stderr,
            )
            return 2
        missing = [
            name for name, value in (
                ("--project-id", args.project_id),
                ("--project-provenance", args.project_provenance),
                ("--behavioral-output", args.behavioral_output),
            ) if not value
        ]
        if missing:
            print(
                "error: --behavioral-export requires " + ", ".join(missing),
                file=sys.stderr,
            )
            return 2
        from .behavioral_export import BehavioralExportError, export_behavioral_artifact

        data_root = Path(args.behavioral_data_root or (target_root / ".agent" / "runtime" / "memos"))
        try:
            artifact = export_behavioral_artifact(
                data_root / str(args.project_id),
                Path(str(args.behavioral_output)),
                str(args.project_id),
                provenance=str(args.project_provenance),
                repo_root=Path(args.repository_root) if args.repository_root else target_root,
                code_root=target_root / ".agent" / "runtime" / "providers",
            )
        except (BehavioralExportError, OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"exported non-authoritative behavioral artifact: {artifact}")
        return 0

    agent_root = target_root / ".agent"
    if not agent_root.is_dir():
        print("error: .agent/ not found in current project", file=sys.stderr)
        return 2

    plan = build_plan(
        args.intent,
        stack_root,
        targets=args.target,
        scopes=args.scope or DEFAULT_SCOPES,
        operation="generate-curl",
    )
    try:
        bundle = export_bundle(agent_root, targets=plan.targets, scopes=plan.scopes)
    except (BundleSecurityError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    payload, digest = encode_bundle(bundle)
    command = build_curl_command(payload, digest, plan.targets[0])

    if args.payload_file:
        Path(args.payload_file).write_text(payload, encoding="utf-8")

    if args.print_curl:
        print(command)

    summary = {
        "targets": list(plan.targets),
        "scopes": list(plan.scopes),
        "payload": payload,
        "sha256": digest,
        "curl": command,
    }
    if args.json:
        print(json.dumps(summary, separators=(",", ":")))
    elif not args.print_curl:
        print(f"payload={payload}")
        print(f"sha256={digest}")
        print(f"curl={command}")
    return 0


def execute_import_transaction(
    bundle: dict[str, object],
    target_root: Path,
    stack_root: Path,
    *,
    fault: Callable[[str], None] | None = None,
) -> tuple[dict[str, object], list[str]]:
    """Apply bootstrap, adapter wiring, and bundle data as one rollback unit.

    The initial preflight validates untrusted bundle data before any template
    copy. The transaction snapshots only stack-managed mutation paths; it does
    not walk or execute arbitrary project files.
    """
    # This boundary precedes *all* preflight reads, snapshots, and template
    # writes.  A root symlink would otherwise turn trusted bootstrap output
    # into an outside write before descriptor-relative final commit begins.
    target_root = validate_target_root(target_root)
    with _TargetTransferLock(target_root):
        initial_plan = preflight_import(bundle, target_root)
        targets = bundle.get("targets", [])
        if not isinstance(targets, list) or any(not isinstance(item, str) for item in targets):
            raise ValueError("transfer targets must be a list of strings")
        _preflight_adapters(targets, target_root, stack_root)
        transaction = _TargetTransaction.capture(
            target_root,
            _transaction_paths(targets, target_root, stack_root, initial_plan),
        )
        try:
            transaction.assert_unchanged()
            if not _agent_root_exists(transaction.root_fd):
                copy_agent_template(
                    stack_root, target_root, target_root_fd=transaction.root_fd
                )
            transaction.refresh_baseline()
            _run_fault(fault, "after_bootstrap")
            transaction.assert_unchanged()
            adapter_results = apply_adapters(targets, target_root, stack_root)
            transaction.refresh_baseline()
            _run_fault(fault, "after_adapters")
            transaction.assert_unchanged()
            # Re-plan against trusted scaffolding and adapter state. This also
            # captures the optimistic baseline used by the final data commit.
            final_plan = preflight_import(bundle, target_root)
            transaction.assert_unchanged()
            result = apply_import_plan(final_plan, fault=fault)
            return result, adapter_results
        except Exception:
            transaction.restore()
            raise
        finally:
            transaction.close()


def _run_fault(fault: Callable[[str], None] | None, phase: str) -> None:
    if fault is not None:
        fault(phase)


def _agent_root_exists(root_fd: int) -> bool:
    try:
        info = os.stat(".agent", dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("transfer .agent root is not a safe directory")
    return True


class _TargetTransferLock:
    """Serialize cooperating transfers on the validated target directory."""

    def __init__(self, target_root: Path) -> None:
        self.target_root = target_root
        self.descriptor = -1

    def __enter__(self) -> "_TargetTransferLock":
        self.descriptor = _open_target_root_fd(self.target_root)
        if fcntl is not None:
            fcntl.flock(self.descriptor, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_args: object) -> None:
        if self.descriptor >= 0:
            if fcntl is not None:
                fcntl.flock(self.descriptor, fcntl.LOCK_UN)
            os.close(self.descriptor)
            self.descriptor = -1


class _TargetTransaction:
    """Bounded, descriptor-rooted rollback snapshot for transfer mutations.

    ``root_fd`` pins the validated target directory.  Neither capture nor
    rollback resolves a path through the target name after that point, so a
    later symlink swap cannot redirect an outside read, write, or removal.
    """

    def __init__(self, target_root: Path, root_fd: int, entries: list[_SnapshotEntry]) -> None:
        self.target_root = target_root
        self.root_fd = root_fd
        self.entries = entries
        self._expected = {entry.relative: _signature_at(root_fd, entry.relative) for entry in entries}

    @classmethod
    def capture(cls, target_root: Path, paths: set[Path]) -> "_TargetTransaction":
        root_fd = _open_target_root_fd(target_root)
        entries: dict[Path, _SnapshotEntry] = {}
        total_bytes = 0
        try:
            for relative in _with_parent_directories(paths):
                # Missing entries count too: otherwise a hostile plan could
                # bypass the advertised snapshot-entry limit without files.
                if len(entries) >= MAX_TRANSACTION_SNAPSHOT_ENTRIES:
                    raise ValueError("transfer rollback snapshot exceeds entry bound")
                entry = _snapshot_at(root_fd, relative)
                if entry.kind == "file":
                    size = len(entry.content) if isinstance(entry.content, bytes) else 0
                    if size > MAX_TRANSACTION_SNAPSHOT_BYTES:
                        raise ValueError(
                            f"transfer rollback snapshot exceeds size bound: {relative}"
                        )
                    total_bytes += size
                    if total_bytes > MAX_TRANSACTION_SNAPSHOT_BYTES:
                        raise ValueError("transfer rollback snapshot exceeds total size bound")
                entries[relative] = entry
            return cls(target_root, root_fd, list(entries.values()))
        except Exception:
            os.close(root_fd)
            raise

    def refresh_baseline(self) -> None:
        """Acknowledge the transaction's own completed mutation phase."""
        self._expected = {
            entry.relative: _signature_at(self.root_fd, entry.relative)
            for entry in self.entries
        }

    def assert_unchanged(self) -> None:
        for relative, expected in self._expected.items():
            if _signature_at(self.root_fd, relative) != expected:
                raise ValueError(
                    f"transfer target changed concurrently: {relative.as_posix()}"
                )

    def restore(self) -> None:
        """Restore only paths still owned by this transaction.

        If a concurrent writer changed an affected path, preserving that edit
        is safer than overwriting it with an old snapshot.  Other unchanged
        paths are restored through the pinned root descriptor.
        """
        unchanged = {
            entry.relative
            for entry in self.entries
            if _signature_at(self.root_fd, entry.relative) == self._expected[entry.relative]
        }
        # Remove transaction-created topology, deepest first.
        for entry in sorted(self.entries, key=lambda item: len(item.relative.parts), reverse=True):
            if entry.kind == "missing" and entry.relative in unchanged:
                _remove_at(self.root_fd, entry.relative)

        # Restore original regular files and symlinks before directory metadata.
        for entry in self.entries:
            if entry.relative not in unchanged:
                continue
            if entry.kind == "file":
                _restore_file_at(self.root_fd, entry)
            elif entry.kind == "symlink":
                _restore_symlink_at(self.root_fd, entry)

        for entry in sorted(self.entries, key=lambda item: len(item.relative.parts), reverse=True):
            if entry.kind == "directory" and entry.relative in unchanged:
                _restore_directory_metadata_at(self.root_fd, entry)

    def close(self) -> None:
        if self.root_fd >= 0:
            os.close(self.root_fd)
            self.root_fd = -1


def _open_target_root_fd(target_root: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(target_root, flags)
    except OSError as exc:
        raise ValueError("transfer target root is not a safe real directory") from exc


def _open_parent_at(root_fd: int, relative_parent: Path, *, create: bool = False) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.dup(root_fd)
    try:
        for part in relative_parent.parts:
            if part in ("", "."):
                continue
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o700, dir_fd=descriptor)
                child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _snapshot_at(root_fd: int, relative: Path) -> _SnapshotEntry:
    if relative == Path("."):
        info = os.fstat(root_fd)
        return _SnapshotEntry(relative, "directory", stat.S_IMODE(info.st_mode), info.st_mtime_ns, None, info.st_dev, info.st_ino)
    try:
        parent_fd = _open_parent_at(root_fd, relative.parent)
    except FileNotFoundError:
        # A missing ancestor makes this known mutation path absent as well.
        # Do not turn a fresh bootstrap snapshot into a path-based probe.
        return _SnapshotEntry(relative, "missing", None, None, None)
    try:
        try:
            info = os.stat(relative.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return _SnapshotEntry(relative, "missing", None, None, None)
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            return _SnapshotEntry(relative, "symlink", mode, info.st_mtime_ns, os.readlink(relative.name, dir_fd=parent_fd), info.st_dev, info.st_ino)
        if stat.S_ISDIR(info.st_mode):
            return _SnapshotEntry(relative, "directory", mode, info.st_mtime_ns, None, info.st_dev, info.st_ino)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"transfer cannot snapshot unsafe path: {relative}")
        if info.st_size > MAX_TRANSACTION_SNAPSHOT_BYTES:
            raise ValueError(f"transfer rollback snapshot exceeds size bound: {relative}")
        descriptor = os.open(relative.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError(f"transfer snapshot raced: {relative}")
            content = _read_fd_bytes(descriptor, info.st_size)
        finally:
            os.close(descriptor)
        return _SnapshotEntry(relative, "file", mode, info.st_mtime_ns, content, info.st_dev, info.st_ino)
    finally:
        os.close(parent_fd)


def _signature_at(root_fd: int, relative: Path) -> tuple[object, ...]:
    try:
        entry = _snapshot_at(root_fd, relative)
    except OSError as exc:
        # An ancestor swapped to a symlink/non-directory is an unsafe current
        # state, never an invitation to fall back to a pathname traversal.
        return ("unsafe", exc.errno)
    if entry.kind == "file":
        raw = entry.content if isinstance(entry.content, bytes) else b""
        return ("file", entry.dev, entry.ino, entry.mode, entry.mtime_ns, len(raw), hashlib.sha256(raw).digest())
    if entry.kind == "directory":
        # Directory mtimes are changed by our own safe staging/rmdir work;
        # inode/type/mode still detect a parent replacement or symlink swap.
        return ("directory", entry.dev, entry.ino, entry.mode)
    return (entry.kind, entry.dev, entry.ino, entry.mode, entry.mtime_ns, entry.content)


def _read_fd_bytes(descriptor: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, min(64 * 1024, remaining))
        if not chunk:
            raise ValueError("transfer snapshot changed while being read")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _remove_at(root_fd: int, relative: Path) -> None:
    if relative == Path("."):
        return
    try:
        parent_fd = _open_parent_at(root_fd, relative.parent)
    except FileNotFoundError:
        return
    try:
        _remove_name_at(parent_fd, relative.name)
    finally:
        os.close(parent_fd)


def _remove_name_at(parent_fd: int, name: str) -> None:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    try:
        for child in os.listdir(descriptor):
            _remove_name_at(descriptor, child)
    finally:
        os.close(descriptor)
    os.rmdir(name, dir_fd=parent_fd)


def _restore_file_at(root_fd: int, entry: _SnapshotEntry) -> None:
    parent_fd = _open_parent_at(root_fd, entry.relative.parent, create=True)
    try:
        _remove_name_at(parent_fd, entry.relative.name)
        temporary = f".{entry.relative.name}.transfer-rollback-{uuid.uuid4().hex}"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), entry.mode or 0o600, dir_fd=parent_fd)
        try:
            os.fchmod(descriptor, entry.mode or 0o600)
            raw = entry.content if isinstance(entry.content, bytes) else b""
            _write_fd_all(descriptor, raw)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if entry.mtime_ns is not None:
            os.utime(temporary, ns=(entry.mtime_ns, entry.mtime_ns), dir_fd=parent_fd, follow_symlinks=False)
        os.replace(temporary, entry.relative.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def _restore_symlink_at(root_fd: int, entry: _SnapshotEntry) -> None:
    parent_fd = _open_parent_at(root_fd, entry.relative.parent, create=True)
    try:
        _remove_name_at(parent_fd, entry.relative.name)
        os.symlink(str(entry.content), entry.relative.name, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def _restore_directory_metadata_at(root_fd: int, entry: _SnapshotEntry) -> None:
    descriptor = _open_parent_at(root_fd, entry.relative)
    try:
        os.fchmod(descriptor, entry.mode or 0o700)
        if entry.mtime_ns is not None:
            os.utime(descriptor, ns=(entry.mtime_ns, entry.mtime_ns))
    finally:
        os.close(descriptor)


def _write_fd_all(descriptor: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        written = os.write(descriptor, view)
        view = view[written:]


def _with_parent_directories(paths: set[Path]) -> list[Path]:
    collected: set[Path] = set()
    for path in paths:
        current = path
        while True:
            collected.add(current)
            if current == Path("."):
                break
            current = current.parent
    return sorted(collected, key=lambda item: (len(item.parts), item.as_posix()))


def _transaction_paths(
    targets: list[str], target_root: Path, stack_root: Path, plan: object
) -> set[Path]:
    paths: set[Path] = {Path(relative) for relative, _content in plan.writes}
    if not (target_root / ".agent").exists():
        paths.add(Path(".agent"))
    paths.update({Path(".agent/install.json"), Path(".agent/skills/_manifest.jsonl")})
    for target in targets:
        if target == "terminal":
            paths.add(Path("AGENTS.md"))
            continue
        manifest = schema_mod.validate(stack_root / "adapters" / target / "adapter.json")
        paths.update(Path(entry["dst"]) for entry in manifest["files"])
        link = manifest.get("skills_link")
        if isinstance(link, dict):
            destination = target_root / link["dst"]
            if destination.exists() and destination.is_dir() and not destination.is_symlink():
                raise ValueError(
                    "transfer cannot transactionally replace an existing skills directory; "
                    "reconcile it before importing"
                )
            paths.add(Path(link["dst"]))
    return paths


def cmd_import(argv: list[str], target_root: Path, stack_root: Path) -> int:
    parser = argparse.ArgumentParser(prog="agentic-stack transfer import")
    parser.add_argument("--payload")
    parser.add_argument("--payload-file")
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--target", action="append", choices=VALID_TARGETS)
    args = parser.parse_args(argv)

    if not args.payload and not args.payload_file:
        print("error: provide --payload or --payload-file", file=sys.stderr)
        return 2

    payload = args.payload
    if args.payload_file:
        payload = Path(args.payload_file).read_text(encoding="utf-8").strip()
    try:
        bundle = decode_payload(payload or "", args.sha256)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.target:
        bundle["targets"] = args.target

    try:
        result, adapter_results = execute_import_transaction(
            bundle, target_root, stack_root
        )
    except (BundleSecurityError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        "imported transfer bundle: "
        f"files={result['files_imported']} "
        f"lessons={result['lessons_imported']} "
        f"skills={result['skills_imported']}"
    )
    print("CRG next action: " + str(result["crg_next_action"]))
    if adapter_results:
        print("installed adapters: " + ", ".join(adapter_results))
    return 0


def run_wizard(target_root: Path, stack_root: Path) -> int:
    sys.path.insert(0, str(stack_root))
    from onboard_ui import intro, note, outro, print_banner  # noqa: E402
    import onboard_widgets as widgets  # noqa: E402

    print_banner()
    intro("agentic-stack transfer")
    note(
        "What this does",
        [
            "Builds a portable .agent memory bundle.",
            "Lets you preview target adapter files before writing.",
            "Generates a curl or PowerShell command for another project.",
        ],
    )

    # Behavioral state is intentionally outside all ordinary scope choices.
    # A TTY operator must make two explicit choices and supply a concrete
    # project identity; no natural-language planning route can reach it.
    if widgets.ask_confirm(
        "Export one non-authoritative MemOS behavioral DB/skills artifact? "
        "It may contain derived behavior and is never imported or activated automatically.",
        default=False,
    ):
        project_id = widgets.ask_text("Exact 16-character MemOS project ID", default="")
        provenance = widgets.ask_text("Human-readable project/repository provenance", default="")
        default_output = str(target_root / f"behavioral-export-{project_id or 'project'}")
        output = widgets.ask_text("New artifact directory", default=default_output)
        if not widgets.ask_confirm(
            "Final confirmation: export only data/memos.db and behavioral skills; "
            "the result remains non-authoritative and does not enable MemOS.",
            default=False,
        ):
            outro(["Behavioral export cancelled before inspection."])
            return 1
        from .behavioral_export import BehavioralExportError, export_behavioral_artifact

        try:
            artifact = export_behavioral_artifact(
                target_root / ".agent" / "runtime" / "memos" / project_id,
                Path(output), project_id, provenance=provenance,
                repo_root=target_root,
                code_root=target_root / ".agent" / "runtime" / "providers",
            )
        except (BehavioralExportError, OSError, ValueError) as exc:
            outro([f"Behavioral export failed safely: {exc}"])
            return 1
        outro([f"Exported non-authoritative behavioral artifact: {artifact}"])
        return 0

    intent = widgets.ask_text(
        "What do you want to transfer?",
        default="move my memory into Codex",
        hint="natural language is fine",
    )
    detected = build_plan(intent, stack_root)
    target_choices = list(VALID_TARGETS)
    target_defaults = [target_choices.index(t) for t in detected.targets if t in target_choices]
    chosen_targets = widgets.ask_multiselect(
        "Which targets should receive it?",
        target_choices,
        defaults=target_defaults,
    )
    scope_choices = [
        "preferences",
        "decisions",
        "accepted_lessons",
        "evidence_ledger",
        "skills",
        "working",
        "episodic",
        "candidates",
    ]
    scope_defaults = [scope_choices.index(s) for s in DEFAULT_SCOPES]
    chosen_scopes = widgets.ask_multiselect(
        "What should move?",
        scope_choices,
        defaults=scope_defaults,
    )
    plan = build_plan(intent, stack_root, targets=chosen_targets, scopes=chosen_scopes)
    if plan.sensitive_scopes:
        ok = widgets.ask_confirm(
            "Sensitive memory selected. Continue after reviewing the preview?",
            default=False,
        )
        if not ok:
            outro(["Transfer cancelled before writing."])
            return 1

    note("Preview", preview_lines(plan))
    if not widgets.ask_confirm("Proceed with this transfer plan?", default=True):
        outro(["Transfer cancelled before writing."])
        return 1

    action = widgets.ask_select(
        "What should happen now?",
        ["Generate curl command", "Apply here now", "Both"],
        default=0,
    )
    operation = {
        "Generate curl command": "generate-curl",
        "Apply here now": "apply-here",
        "Both": "both",
    }[action]
    final_plan = build_plan(intent, stack_root, targets=plan.targets, scopes=plan.scopes, operation=operation)

    agent_root = target_root / ".agent"
    bundle = export_bundle(agent_root, targets=final_plan.targets, scopes=final_plan.scopes)
    payload, digest = encode_bundle(bundle)
    command = build_curl_command(payload, digest, final_plan.targets[0])
    lines = []
    if operation in ("generate-curl", "both"):
        lines.append(command)
    if operation in ("apply-here", "both"):
        import_result, adapters = execute_import_transaction(
            bundle, target_root, stack_root
        )
        lines.append(
            f"Applied locally: files={import_result['files_imported']} "
            f"lessons={import_result['lessons_imported']} adapters={', '.join(adapters) or 'none'}"
        )
        lines.append("CRG next action: " + str(import_result["crg_next_action"]))
    lines.append("Verify: python3 .agent/tools/show.py")
    outro(lines)
    return 0


def preview_lines(plan) -> list[str]:
    lines = [
        f"Targets: {', '.join(plan.targets)}",
        f"Scopes: {', '.join(plan.scopes)}",
        f"Operation: {plan.operation}",
    ]
    for warning in plan.warnings:
        lines.append(f"Warning: {warning}")
    lines.append("Adapter files:")
    for action in plan.adapter_actions:
        lines.append(f"- {action.target}: {action.dst} ({action.merge_policy})")
    return lines


def build_curl_command(payload: str, digest: str, target: str) -> str:
    url = "https://raw.githubusercontent.com/codejunkie99/agentic-stack/master/scripts/import-transfer.sh"
    return (
        f"curl -fsSL {url} | sh -s -- "
        f"--target {target} --payload '{payload}' --sha256 {digest}"
    )


def apply_adapters(targets: Iterable[str], target_root: Path, stack_root: Path) -> list[str]:
    applied: list[str] = []
    install_state = state_mod.load(target_root)
    profile = _recorded_profile(install_state)
    for target in targets:
        if target == "terminal":
            _ensure_terminal_agents(target_root)
            applied.append("terminal")
            continue
        manifest_path = stack_root / "adapters" / target / "adapter.json"
        if not manifest_path.is_file():
            continue
        manifest = schema_mod.validate(manifest_path)
        install_mod.install(
            manifest=manifest,
            target_root=target_root,
            adapter_dir=stack_root / "adapters" / target,
            stack_root=stack_root,
            profile=profile,
        )
        applied.append(target)
    return applied


def _preflight_adapters(
    targets: Iterable[str], target_root: Path, stack_root: Path
) -> None:
    target_list = list(targets)
    unknown = sorted(set(target_list) - set(VALID_TARGETS))
    if unknown:
        raise ValueError(f"unsupported transfer target(s): {', '.join(unknown)}")
    install_state = state_mod.load(target_root)
    profile = _recorded_profile(install_state)
    target_agent = target_root / ".agent"
    if target_agent.exists():
        profiles_mod.validate_existing_install(
            profile, install_state, target_agent
        )
    else:
        profiles_mod.validate_blocked_configuration(stack_root / ".agent")
    for target in target_list:
        if target == "terminal":
            continue
        manifest_path = stack_root / "adapters" / target / "adapter.json"
        manifest = schema_mod.validate(manifest_path)
        adapter_dir = stack_root / "adapters" / target
        for entry in manifest["files"]:
            source_root = stack_root if entry.get("from_stack", False) else adapter_dir
            source = source_root / entry["src"]
            if not source.is_file():
                raise ValueError(
                    f"adapter {target!r} source is missing: {entry['src']}"
                )


def _recorded_profile(install_state: object) -> str:
    if isinstance(install_state, dict):
        orchestration = install_state.get("orchestration")
        if isinstance(orchestration, dict):
            profile = orchestration.get("profile")
            if isinstance(profile, str):
                return profiles_mod.validate_profile(profile)
    return profiles_mod.STANDARD


def _ensure_terminal_agents(target_root: Path) -> None:
    path = target_root / "AGENTS.md"
    snippet = (
        "# Agentic-stack brain\n\n"
        "This project uses a portable brain in `.agent/`. Read `.agent/AGENTS.md`, "
        "`.agent/memory/personal/PREFERENCES.md`, and "
        "`.agent/memory/semantic/LESSONS.md` before acting.\n"
    )
    if path.exists():
        existing = path.read_text(encoding="utf-8", errors="replace")
        if ".agent/" in existing:
            return
        path.write_text(existing.rstrip() + "\n\n" + snippet, encoding="utf-8")
    else:
        path.write_text(snippet, encoding="utf-8")
