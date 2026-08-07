"""Read-only audit of installed adapters.

Reads .agent/install.json, verifies each adapter's tracked files still
exist and post-install state is still valid. Reports green/yellow/red
per adapter. Exits 0 on all-green-or-yellow, 1 on any red.

First run on a pre-v0.9.0 project (no install.json) detects adapters
from filesystem signals and ASKS before synthesizing — never silently
writes. Codex's UX framing: doctor must not mutate without consent.
"""
from __future__ import annotations

import os
import json
import shlex
import shutil
import sys
import hashlib
import importlib.util
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Mapping

from . import schema as schema_mod
from . import state as state_mod
from . import scheduled_runtime
from . import scheduler_doctor
from . import profiles as profiles_mod
from . import upgrade as upgrade_mod
from . import __version__
from .scheduled_review_health import default_scheduler_path, inspect_scheduler


# Detection signals: (filename, signal_strength) tuples per adapter.
#
# STRONG means: this path existing proves *we* installed this adapter here.
# Both consumers rely on that and only that — state.legacy_unregistered_adapters()
# gates pre-v0.9 migration on it, and cli.py pre-checks onboarding boxes with it,
# where a false positive can install over a user's file.
#
# So a path may only be strong if nothing but our installer creates it. That
# rules out three categories, all of which used to be marked strong here:
#   - files the user or the harness vendor authors (.windsurfrules,
#     opencode.json, .claude/settings.json, .gemini/settings.json)
#   - anything under .agent/, which is the shared brain rather than any one
#     adapter's footprint (codex's .agent/skills matched every brain-present
#     project, so codex was reported as legacy-unregistered everywhere)
#   - generic root filenames (CLAUDE.md, AGENTS.md, run.py) — already weak
#
# assert_signals_consistent() below enforces this; a test calls it.
DETECT_SIGNALS = {
    "claude-code": [
        ("CLAUDE.md", "weak"),
        (".claude/settings.json", "weak"),
    ],
    "cursor": [(".cursor/rules/agentic-stack.mdc", "strong")],
    "zed": [(".rules", "weak")],  # generic root filename; ambiguous alone
    "windsurf": [
        (".windsurf/rules/agentic-stack.md", "strong"),
        (".windsurfrules", "weak"),
    ],
    "openclaw": [(".openclaw-system.md", "strong")],
    "pi": [(".pi/extensions/memory-hook.ts", "strong")],
    "codex": [(".agent/skills", "weak")],
    # Single entry: this key was previously declared twice, and the second
    # literal silently discarded the first, dropping .gemini/settings.json
    # from detection entirely.
    "gemini": [
        ("GEMINI.md", "weak"),
        ("gemini.md", "weak"),
        (".gemini/settings.json", "weak"),
        (".gemini/skills", "strong"),
    ],
    "antigravity": [("ANTIGRAVITY.md", "strong")],
    "opencode": [("opencode.json", "weak")],
    "hermes": [("AGENTS.md", "weak")],  # AGENTS.md alone is ambiguous
    "standalone-python": [("run.py", "weak")],
    "copilot-cli": [(".github/instructions/agentic-stack.instructions.md", "strong")],
}

VALID_SIGNAL_STRENGTHS = {"strong", "weak"}


def assert_signals_consistent() -> None:
    """Raise if DETECT_SIGNALS violates the strong-signal contract.

    Cheap to call, and the only thing standing between a copy-pasted
    manifest and a false-positive install over someone's file.
    """
    from . import schema as schema_mod

    for adapter, signals in DETECT_SIGNALS.items():
        seen: set[str] = set()
        for path, strength in signals:
            if strength not in VALID_SIGNAL_STRENGTHS:
                raise ValueError(
                    f"{adapter}: signal '{path}' has strength '{strength}'; "
                    f"expected one of {sorted(VALID_SIGNAL_STRENGTHS)}"
                )
            if path in seen:
                raise ValueError(f"{adapter}: signal '{path}' listed twice")
            seen.add(path)
            if strength != "strong":
                continue
            if schema_mod.is_shared_filename(path):
                raise ValueError(
                    f"{adapter}: '{path}' is authored by users or other tools, "
                    f"so its presence does not prove we installed anything; "
                    f"mark it weak"
                )
            if path == ".agent" or path.startswith(".agent/"):
                raise ValueError(
                    f"{adapter}: '{path}' is part of the shared brain, not this "
                    f"adapter's footprint, so it matches every brain-present "
                    f"project; mark it weak"
                )


# ---- statuses ---------------------------------------------------------

GREEN = "green"
YELLOW = "yellow"
RED = "red"


def audit(target_root: Path | str, log: Callable[[str], None] | None = None) -> int:
    """Run read-only audit. Returns exit code (0 if no red, 1 otherwise)."""
    if log is None:
        log = print

    # os.path.abspath (not Path.resolve) is deliberate: it normalizes
    # `.`/`..` and prepends cwd for relative paths but does NOT canon-
    # icalize symlinks. The legacy bash installer used the logical
    # path (`cd "$TARGET" && pwd`) to derive the openclaw agent name
    # via cksum, and post_install.py does the same. If doctor resolves
    # symlinks here, a pre-v0.9 openclaw install under e.g. a symlinked
    # `~/src/app` workspace gets a DIFFERENT hashed agent name during
    # synthesis than the bash installer registered — doctor then can't
    # recover the agent from ~/.openclaw/openclaw.json, and a later
    # remove has no agent_name to unregister, orphaning the entry.
    target_root = Path(os.path.abspath(str(target_root)))
    try:
        doc = state_mod.load(target_root)
    except (OSError, json.JSONDecodeError) as exc:
        log("✗ install-state red")
        log(f"    install.json is unreadable: {type(exc).__name__}")
        return 1

    if doc is None:
        base_result = _audit_pre_v090(target_root, log)
        return max(base_result, _audit_scheduled_reviewer(log=log))

    # install.json present → strict read-only audit
    log(f"auditing {len(doc.get('adapters', {}))} installed adapter(s) in {target_root}")
    log("")
    any_red = False
    for adapter_name in sorted(doc.get("adapters", {}).keys()):
        entry = doc["adapters"][adapter_name]
        status, lines = _audit_adapter(target_root, adapter_name, entry)
        glyph = {GREEN: "✓", YELLOW: "⚠", RED: "✗"}[status]
        log(f"{glyph} {adapter_name:18s} {status}")
        for line in lines:
            log(f"    {line}")
        if status == RED:
            any_red = True

    orchestration_status, orchestration_lines = _audit_orchestration(target_root)
    glyph = {GREEN: "✓", YELLOW: "⚠", RED: "✗"}[orchestration_status]
    log(f"{glyph} orchestration       {orchestration_status}")
    for line in orchestration_lines:
        log(f"    {line}")
    if orchestration_status == RED:
        any_red = True

    if _audit_scheduled_reviewer(log=log):
        any_red = True

    log("")
    log(f"summary: {_summary(doc, any_red)}")
    return 1 if any_red else 0


def _audit_scheduled_reviewer(
    *, log: Callable[[str], None] = print, home: Path | None = None
) -> int:
    # Host homes are never inspected by doctor.  Tests/approved callers may
    # inject a temporary home to audit a legacy shim fixture.
    if home is None:
        log("⚠ scheduled-reviewer observations require an injected fixture")
        return 0
    health = inspect_scheduler(default_scheduler_path(home))
    if health.status != RED:
        return 0
    log(f"✗ scheduled-reviewer red")
    log(f"    {health.path}")
    for reason in health.reasons:
        log(f"    {reason}")
    log("    automatic acceptance is forbidden; update or disable this scheduler")
    return 1


def _audit_adapter(
    target_root: Path, adapter_name: str, entry: dict
) -> tuple[str, list[str]]:
    """Returns (status, list_of_detail_lines)."""
    lines: list[str] = []

    # Check all tracked files (both freshly-written and overwritten) still exist.
    # Both categories matter for "is the adapter still wired" — only the
    # remove-time semantics differ (overwritten files are user-owned and
    # NOT deleted on remove).
    missing = []
    for f in entry.get("files_written", []) + entry.get("files_overwritten", []):
        if not (target_root / f).exists():
            missing.append(f)
    # Also check file_results for paths that install recorded as
    # skipped_existing (merge_policy: skip_if_exists, file pre-existed)
    # or left_alone (merge_or_alert, file already referenced .agent/).
    # These aren't in files_written/files_overwritten but are still part
    # of the adapter's wiring — without this check, deleting e.g. run.py
    # after installing standalone-python leaves the adapter visibly
    # green in doctor when it's actually broken.
    for r in entry.get("file_results", []):
        if r.get("result") in ("skipped_existing", "left_alone"):
            dst = r.get("dst")
            if dst and not (target_root / dst).exists() and dst not in missing:
                missing.append(dst)
    if missing:
        lines.append(f"missing files: {', '.join(missing)}")
        return RED, lines

    status_overall = GREEN

    # Files where install hit `merge_or_alert` and the existing file did
    # NOT reference .agent/. The adapter is "installed" in the sense that
    # we recorded the entry, but the brain is not actually wired until
    # the user merges the snippet. Re-check current file content — they
    # may have merged it since install. Yellow if still un-merged; green
    # if they merged.
    still_alerted = []
    for f in entry.get("files_alerted", []):
        p = target_root / f
        if not p.is_file():
            still_alerted.append(f"{f} (file missing entirely)")
            continue
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            still_alerted.append(f"{f} (unreadable)")
            continue
        if ".agent/" not in content:
            still_alerted.append(f)
    if still_alerted:
        lines.append(
            f"merge required: {', '.join(still_alerted)} — install printed a snippet to paste in"
        )
        status_overall = YELLOW

    # Check skills_link target exists
    sl = entry.get("skills_link")
    if sl:
        dst = target_root / sl["dst"]
        if not dst.exists():
            lines.append(f"skills_link {sl['dst']} missing")
            return RED, lines
        # If it's a symlink, check it doesn't dangle
        if dst.is_symlink() and not dst.exists():
            lines.append(f"skills_link {sl['dst']} dangles")
            return RED, lines
        # Verify the link (or rsynced dir) still resolves to the manifest
        # target. A user who repoints `.agent/skills` / `.pi/skills` to
        # a different directory would otherwise get a green doctor even
        # though the adapter is no longer reading the project's
        # .agent/skills tree.
        expected_target = sl.get("target")
        if expected_target and dst.is_symlink():
            try:
                resolved = dst.resolve()
                expected_abs = (target_root / expected_target).resolve()
                if resolved != expected_abs:
                    lines.append(
                        f"skills_link {sl['dst']} points to {resolved} "
                        f"(expected {expected_abs})"
                    )
                    status_overall = RED
            except OSError as e:
                lines.append(f"skills_link {sl['dst']} unreadable: {e}")
                status_overall = RED

    # Check post_install state. Only verify external state for actions that
    # actually succeeded — if registration was skipped at install time
    # (binary_missing, failed, etc.), the recorded result IS the source of
    # truth and there's nothing on-disk to verify against.
    for r in entry.get("post_install_results", []):
        action = r.get("action", "?")
        st = r.get("status", "?")
        if action == "openclaw_register_workspace":
            agent = r.get("agent_name", "?")
            if st in ("ok", "already_exists"):
                # Registration claimed success at install time; verify it's
                # still true. RED if the agent is now gone from openclaw config.
                check_status = _check_openclaw_agent(agent)
                if check_status == "ok":
                    lines.append(f"openclaw agent '{agent}' registered")
                elif check_status == "binary_missing":
                    # "binary_missing" is a historical misnomer here —
                    # _check_openclaw_agent reads ~/.openclaw/openclaw.json
                    # directly rather than calling the binary, so this
                    # status means the CONFIG FILE is absent. For a
                    # registration that was previously ok, an absent
                    # config file means every registered agent is gone
                    # — the adapter is objectively broken, not merely
                    # unverifiable. RED, not YELLOW.
                    lines.append(
                        f"openclaw agent '{agent}' was registered, but "
                        f"~/.openclaw/openclaw.json no longer exists — "
                        f"registration lost"
                    )
                    status_overall = RED
                elif check_status == "missing":
                    lines.append(
                        f"openclaw agent '{agent}' was registered, but no longer "
                        f"present in ~/.openclaw/openclaw.json"
                    )
                    status_overall = RED
            elif st == "binary_missing":
                lines.append(
                    f"openclaw registration skipped at install time (binary not "
                    f"on PATH); fallback hint was printed. install with `openclaw` "
                    f"present, or use the `--system-prompt-file` fallback."
                )
                status_overall = max(status_overall, YELLOW, key=_status_rank)
            else:
                # Failed at install time and we recorded that. Don't escalate
                # to red on every audit — the failure is already known and the
                # user has a fallback hint.
                lines.append(
                    f"openclaw registration {st} at install time "
                    f"(see install.json for details / fallback hint)"
                )
                status_overall = max(status_overall, YELLOW, key=_status_rank)
        else:
            # Unknown post_install action — just record
            lines.append(f"post_install {action}: {st}")

    if adapter_name == "claude-code":
        hook_status, hook_lines = _audit_claude_hook_wiring(target_root)
        if hook_lines:
            lines.extend(hook_lines)
            status_overall = max(status_overall, hook_status, key=_status_rank)

    # .agent/ brain still intact?
    if not (target_root / ".agent" / "AGENTS.md").is_file():
        lines.append(".agent/AGENTS.md missing — brain not present")
        return RED, lines

    return status_overall, lines


def _check_openclaw_agent(agent_name: str) -> str:
    """Check if openclaw agent is still registered. ok | missing | binary_missing

    Reads ~/.openclaw/openclaw.json directly — does NOT require the
    openclaw binary to be on PATH at audit time. The user may have
    registered the agent on a machine where openclaw was installed,
    then audited from a different shell where it's not. Reading the
    config file is the source of truth either way.

    Returns:
      ok             — agent is in openclaw.json
      missing        — openclaw.json exists but agent not in it
      binary_missing — openclaw config file itself is absent (no install)
    """
    try:
        import json
        cfg = Path.home() / ".openclaw" / "openclaw.json"
        if not cfg.is_file():
            return "binary_missing"
        data = json.loads(cfg.read_text(encoding="utf-8"))
        agents = (data.get("agents") or {}).get("list") or []
        for a in agents:
            if a.get("id") == agent_name:
                return "ok"
        return "missing"
    except (OSError, json.JSONDecodeError):
        return "binary_missing"


def _status_rank(s: str) -> int:
    return {GREEN: 0, YELLOW: 1, RED: 2}[s]


def _audit_claude_hook_wiring(target_root: Path) -> tuple[str, list[str]]:
    settings = target_root / ".claude" / "settings.json"
    if not settings.is_file():
        return GREEN, []
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return YELLOW, [f".claude/settings.json unreadable JSON: {e}"]

    referenced = _claude_hook_references(data)
    lines: list[str] = []
    missing = [
        rel
        for rel in sorted(referenced)
        if rel.startswith(".agent/") and not (target_root / rel).is_file()
    ]
    if missing:
        lines.append(f"missing hook command file(s): {', '.join(missing)}")

    hooks_dir = target_root / ".agent" / "harness" / "hooks"
    if hooks_dir.is_dir():
        wired = {rel for rel in referenced if rel.startswith(".agent/harness/hooks/")}
        orphaned = []
        for path in sorted(hooks_dir.glob("*.py")):
            if _ignore_claude_orphan_candidate(path.name):
                continue
            rel = path.relative_to(target_root).as_posix()
            if rel not in wired:
                orphaned.append(rel)
        if orphaned:
            lines.append(
                "orphaned hook files not referenced by .claude/settings.json: "
                + ", ".join(orphaned)
            )
    return (YELLOW if lines else GREEN), lines


def _claude_hook_references(settings: dict) -> set[str]:
    refs: set[str] = set()
    hooks = settings.get("hooks") or {}
    if not isinstance(hooks, dict):
        return refs
    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for hook in entry.get("hooks") or []:
                if not isinstance(hook, dict):
                    continue
                command = hook.get("command")
                if isinstance(command, str):
                    refs.update(_agent_paths_from_command(command))
    return refs


def _agent_paths_from_command(command: str) -> set[str]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    refs: set[str] = set()
    for token in tokens:
        if ".agent/" not in token:
            continue
        rel = token[token.index(".agent/"):].strip(";,")
        if rel.endswith(".py"):
            refs.add(rel)
    return refs


def _ignore_claude_orphan_candidate(filename: str) -> bool:
    if filename == "__init__.py" or filename.startswith("_"):
        return True
    if filename in {
        "on_failure.py",
        "post_execution.py",
        "pre_tool_call.py",
        "pi_post_tool.py",
    }:
        return True
    return False


def _summary(doc: dict, any_red: bool) -> str:
    n = len(doc.get("adapters", {}))
    if any_red:
        return f"{n} adapter(s), at least 1 red — see above"
    return f"{n} adapter(s), all green or yellow"


# ---- orchestration supportability audit -----------------------------

def _audit_orchestration(
    target_root: Path, *, stack_root: Path | None = None,
    environ: dict[str, str] | None = None,
    scheduler_fixture: Mapping[str, object] | None = None,
) -> tuple[str, list[str]]:
    """Observe Gate 2 provider/configuration state without changing it.

    This intentionally has no repair/start/rebuild operation.  It only opens
    the CRG database through the provider's read-only health path and validates
    the pinned MemOS tree without constructing a bridge client.
    """
    target_root = Path(target_root)
    agent = target_root / ".agent"
    stack_root = Path(stack_root or os.environ.get("AGENTIC_STACK_ROOT", Path(__file__).parent.parent))
    env = dict(os.environ if environ is None else environ)
    lines: list[str] = []
    status = GREEN

    def add(level: str, message: str) -> None:
        nonlocal status
        lines.append(message)
        status = max(status, level, key=_status_rank)

    try:
        doc = state_mod.load(target_root)
    except (OSError, json.JSONDecodeError) as exc:
        return RED, [f"install-state unreadable: {type(exc).__name__}"]
    if not isinstance(doc, dict):
        return RED, ["install-state missing; install with an explicit profile"]
    if doc.get("schema_version") != state_mod.SCHEMA_VERSION:
        add(RED, f"unsupported install-state schema {doc.get('schema_version')!r}; supported {state_mod.SCHEMA_VERSION}")
    if not isinstance(doc.get("adapters"), dict):
        add(RED, "install-state adapters must be an object")
    orchestration = doc.get("orchestration")
    profile: str | None = None
    if not isinstance(orchestration, dict):
        add(RED, "installation profile is missing or malformed")
    else:
        profile = orchestration.get("profile") if isinstance(orchestration.get("profile"), str) else None
        try:
            if profile is None:
                raise ValueError("installation profile is missing")
            profiles_mod.validate_profile(profile)
            profiles_mod.validate_blocked_profile_state(orchestration)
            add(GREEN, f"profile {profile}; Phase 8 quality gate blocked")
        except ValueError as exc:
            add(RED, f"Phase 8/profile invariant failed: {exc}")
        try:
            runtime = scheduled_runtime.runtime_from_record(
                orchestration.get("scheduled_runtime"),
                forbidden_roots=(target_root, stack_root),
            )
            add(GREEN, f"scheduled Python runtime {runtime.path} ({runtime.version})")
        except ValueError as exc:
            add(RED, str(exc))

    config_path = agent / "memory" / "orchestration" / "config.json"
    config = None
    if not config_path.is_file():
        add(RED, "orchestration config missing")
    else:
        try:
            config = validate_orchestration_config_data(config_path)
            if config["mode"] != "off":
                add(RED, f"Phase 8 quality gate blocked: orchestration mode must remain off (found {config['mode']!r})")
            else:
                add(GREEN, "orchestration config schema and lane budgets valid; effective mode off")
        except Exception as exc:
            add(RED, f"orchestration config invalid: {type(exc).__name__}: {exc}")

    infra = agent / "infrastructure.json"
    try:
        infra_doc = json.loads(infra.read_text(encoding="utf-8"))
        if not isinstance(infra_doc, dict) or infra_doc.get("schema_version") != 1:
            add(RED, f"unsupported infrastructure schema {infra_doc.get('schema_version') if isinstance(infra_doc, dict) else None!r}; supported 1")
        else:
            add(GREEN, "infrastructure schema 1 supported")
    except (OSError, json.JSONDecodeError) as exc:
        add(RED, f"infrastructure inventory invalid: {type(exc).__name__}")

    if profile == profiles_mod.MINIMAL:
        incompatible = [rel for rel in sorted(profiles_mod.minimal_omitted_paths()) if (agent / rel).exists()]
        if incompatible:
            add(RED, "profile-incompatible MemOS capability files present: " + ", ".join(incompatible))
        else:
            add(GREEN, "MemOS capability absent as expected for minimal profile")
    elif profile == profiles_mod.STANDARD:
        factory = agent / "memory" / "orchestration" / "memos_factory.py"
        if not factory.is_file():
            add(RED, "standard profile MemOS capability module missing")
        else:
            _audit_memos_artifact(agent, stack_root, env, add)

    _audit_crg(target_root, agent, stack_root, env, add)
    _audit_drift(agent, stack_root / ".agent", profile, add)
    if scheduler_fixture is not None:
        fixture_now = scheduler_fixture.get("now") if isinstance(scheduler_fixture, Mapping) else None
        if not isinstance(fixture_now, str):
            add(RED, "scheduler fixture missing bounded observation timestamp")
        else:
            scheduler_status, scheduler_lines = scheduler_doctor.audit_scheduler_fixture(
                scheduler_fixture, now=fixture_now,
            )
            for line in scheduler_lines:
                add({scheduler_doctor.GREEN: GREEN, scheduler_doctor.YELLOW: YELLOW,
                     scheduler_doctor.RED: RED}[scheduler_status], "scheduler " + line)
    return status, lines


@contextmanager
def _trusted_orchestration_module(source_agent: Path, dotted: str):
    """Load one deployed orchestration module under a throw-away package name.

    A doctor run can inspect two different brains in one interpreter.  Loading
    under a unique package and removing every temporary module afterwards
    avoids a global ``sys.path`` insertion or a stale ``orchestration.*`` cache.
    """
    package_root = source_agent / "memory" / "orchestration"
    init = package_root / "__init__.py"
    module_path = package_root.joinpath(*dotted.split(".")).with_suffix(".py")
    if dotted.startswith("providers."):
        module_path = package_root / "providers" / (dotted.rsplit(".", 1)[1] + ".py")
    if not init.is_file() or not module_path.is_file():
        raise FileNotFoundError(f"deployed orchestration module missing: {dotted}")
    token = hashlib.sha256(str(source_agent.resolve(strict=False)).encode()).hexdigest()[:16]
    package = f"_doctor_orchestration_{token}"
    created: set[str] = set()
    prior_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location(
            package, init, submodule_search_locations=[str(package_root)],
        )
        if spec is None or spec.loader is None:
            raise ImportError("cannot create deployed orchestration package spec")
        root = importlib.util.module_from_spec(spec)
        sys.modules[package] = root
        created.add(package)
        spec.loader.exec_module(root)
        if "." in dotted:
            parent_name, leaf = dotted.rsplit(".", 1)
            parent_path = package_root.joinpath(*parent_name.split("."))
            parent_spec = importlib.util.spec_from_file_location(
                f"{package}.{parent_name}", parent_path / "__init__.py",
                submodule_search_locations=[str(parent_path)],
            )
            if parent_spec is None or parent_spec.loader is None:
                raise ImportError(f"cannot create deployed package spec: {parent_name}")
            parent = importlib.util.module_from_spec(parent_spec)
            sys.modules[parent_spec.name] = parent
            created.add(parent_spec.name)
            parent_spec.loader.exec_module(parent)
            name = f"{parent_spec.name}.{leaf}"
        else:
            name = f"{package}.{dotted}"
        module_spec = importlib.util.spec_from_file_location(name, module_path)
        if module_spec is None or module_spec.loader is None:
            raise ImportError(f"cannot create deployed module spec: {dotted}")
        module = importlib.util.module_from_spec(module_spec)
        sys.modules[name] = module
        created.add(name)
        module_spec.loader.exec_module(module)
        yield module
    finally:
        sys.dont_write_bytecode = prior_dont_write_bytecode
        for name in sorted((key for key in sys.modules if key == package or key.startswith(package + ".")), reverse=True):
            sys.modules.pop(name, None)


def _read_process_command(pid: int) -> str | None:
    """Read a process command only; never signal, start, or stop it."""
    try:
        result = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            check=False, capture_output=True, text=True, timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    command = result.stdout.strip()
    return command or None


def _audit_owned_memos_processes(data_root: str | Path, bridge: Path, add: Callable[[str, str], None]) -> None:
    """Recognize only attestations bound to this exact bridge and runtime root."""
    root = Path(data_root).expanduser().resolve(strict=False)
    if not root.is_dir():
        return
    observed = False
    for attestation in sorted(root.glob("*/bridge-process.json")):
        try:
            record = json.loads(attestation.read_text(encoding="utf-8"))
            pid = record["pid"]
            recorded_bridge = Path(record["bridge"]).resolve(strict=False)
            project_root = Path(record["project_root"]).resolve(strict=False)
            if not isinstance(pid, int) or recorded_bridge != bridge.resolve(strict=False):
                continue
            if project_root != attestation.parent.resolve(strict=False):
                continue
            command = _read_process_command(pid)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
        observed = True
        if command is not None and str(recorded_bridge) in command:
            add(RED, f"MemOS bridge unexpectedly running for {project_root} (pid {pid}); doctor did not stop it")
        else:
            add(YELLOW, f"MemOS bridge attestation present but process observation unavailable for {project_root}")
    if not observed:
        # No process state is the normal disabled condition; do not invent one.
        return


def _trusted_source_agent(stack_root: Path) -> Path | None:
    """Accept executable provider code only from this harness-manager's source tree."""
    own_root = Path(__file__).resolve().parent.parent
    try:
        if stack_root.resolve(strict=False) != own_root:
            return None
    except OSError:
        return None
    candidate = own_root / ".agent"
    return candidate if candidate.is_dir() else None


def validate_orchestration_config_data(path: Path) -> dict[str, object]:
    """Strictly validate deployed config as JSON data, never by importing it."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read orchestration config: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("orchestration config must be an object")
    required = {"schema", "mode", "total_token_budget", "lane_reserves", "project_aliases"}
    if set(value) != required or value.get("schema") != "agentic.memory.config.v1":
        raise ValueError("unsupported orchestration config schema or keys")
    if value.get("mode") not in {"off", "shadow", "assist"}:
        raise ValueError("orchestration mode is invalid")
    total = value.get("total_token_budget")
    lanes = value.get("lane_reserves")
    aliases = value.get("project_aliases")
    if (not isinstance(total, int) or isinstance(total, bool) or not 1000 <= total <= 12000
            or not isinstance(lanes, dict) or set(lanes) != {"governance", "behavioral", "evidence"}
            or any(not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 12000 for v in lanes.values())
            or sum(lanes.values()) != total or not isinstance(aliases, dict)
            or any(not isinstance(k, str) or not isinstance(v, str) or __import__("re").fullmatch(r"[0-9a-f]{16}", v) is None for k, v in aliases.items())):
        raise ValueError("orchestration config lane budgets or aliases are invalid")
    return value


def _audit_memos_artifact(agent: Path, stack_root: Path, env: dict[str, str], add: Callable[[str, str], None]) -> None:
    code_root = Path(env.get("AGENTIC_MEMOS_CODE_ROOT", agent / "runtime" / "providers"))
    if not code_root.exists():
        add(YELLOW, "MemOS disabled; pinned artifact unavailable (no code root configured)")
        return
    try:
        source_agent = _trusted_source_agent(stack_root)
        if source_agent is None:
            add(YELLOW, "MemOS artifact inspection unavailable: trusted source is unavailable")
            return
        with _trusted_orchestration_module(source_agent, "memos_runtime") as runtime:
            plugin = code_root / "memos-local-plugin" / runtime.MEMOS_PLUGIN_VERSION
            if plugin.exists():
                runtime.validate_pinned_plugin(plugin)
        if not plugin.exists():
            add(YELLOW, "MemOS disabled; pinned artifact invalid/unavailable")
        else:
            add(GREEN, "MemOS pinned artifact valid and disabled; doctor did not start it")
            _audit_owned_memos_processes(
                env.get("AGENTIC_MEMOS_DATA_ROOT", str(agent / "runtime" / "memos")),
                plugin / "node_modules/@memtensor/memos-local-plugin/dist/bridge.cjs", add,
            )
    except Exception as exc:
        add(YELLOW, f"MemOS pinned artifact invalid: {type(exc).__name__}: {exc}")


def _audit_crg(target_root: Path, agent: Path, stack_root: Path, env: dict[str, str], add: Callable[[str, str], None]) -> None:
    try:
        source_agent = _trusted_source_agent(stack_root)
        if source_agent is None:
            add(YELLOW, "CRG inspection unavailable: trusted source is unavailable")
            return
        with _trusted_orchestration_module(source_agent, "providers.crg_evidence") as crg:
            revision = env.get("AGENTIC_CRG_REVISION")
            provider = crg.CrgEvidenceProvider(
                repo_root=target_root, project_id="0" * 16,
                registry_path=env.get("AGENTIC_CRG_REGISTRY"),
                ledger=crg.EvidenceLedger(agent / "memory" / "evidence" / "ledger.jsonl"),
                revision_resolver=(lambda _root: revision) if revision is not None else None,
            )
            health = provider.health()
            supported_schemas = crg.SUPPORTED_GRAPH_SCHEMA_VERSIONS
    except Exception as exc:
        add(YELLOW, f"CRG unavailable: health inspection failed ({type(exc).__name__})")
        return
    level = GREEN if health.get("status") == "healthy" else YELLOW
    warnings = ", ".join(str(item) for item in health.get("warnings", [])) or "none"
    if any("volatile" in str(item) or "graph_schema_version" in str(item) for item in health.get("warnings", [])):
        level = RED
    schema_version = health.get("schema_version")
    schema_checked = bool(health.get("database"))
    schema_note = (
        "not observed" if not schema_checked else
        "supported" if str(schema_version) in supported_schemas else "unsupported"
    )
    if schema_checked and schema_note == "unsupported":
        level = RED
    add(level, "CRG " + str(health.get("status")) + "; "
        f"registry={provider.registry_path}; data={health.get('data_dir')}; database={health.get('database')}; "
        f"durable={health.get('durable')}; revision={health.get('graph_revision')}; "
        f"updated={health.get('graph_updated_at')}; nodes={health.get('nodes')}; files={health.get('files')}; "
        f"schema={schema_version} ({schema_note}; supported {sorted(supported_schemas)}); warnings={warnings}")


def audit_crg_ledger_freshness(target_root: Path, agent_root: Path, stack_root: Path) -> dict[str, object]:
    """Trusted-source, count-only CRG ledger audit for observational callers."""
    source_agent = _trusted_source_agent(stack_root)
    if source_agent is None:
        return {"status": "unavailable", "current": 0, "stale": 0, "malformed": 0,
                "records": 0, "truncated": False, "warnings": ["trusted_source_unavailable"]}
    try:
        with _trusted_orchestration_module(source_agent, "providers.crg_evidence") as crg, _trusted_orchestration_module(source_agent, "identity") as identity_mod:
            identity = identity_mod.derive_project_identity(
                target_root, os.environ.get("AGENTIC_GIT_REMOTE")
            ).project_id
            provider = crg.CrgEvidenceProvider(
                repo_root=target_root, project_id=identity,
                registry_path=os.environ.get("AGENTIC_CRG_REGISTRY"),
                ledger=crg.EvidenceLedger(agent_root / "memory" / "evidence" / "ledger.jsonl"),
                revision_resolver=(lambda _root: os.environ["AGENTIC_CRG_REVISION"])
                if "AGENTIC_CRG_REVISION" in os.environ else None,
            )
            return provider.audit_ledger_freshness()
    except Exception:
        return {"status": "unavailable", "current": 0, "stale": 0, "malformed": 0,
                "records": 0, "truncated": False, "warnings": ["crg_audit_unavailable"]}


def _audit_drift(agent: Path, source_agent: Path, profile: str | None, add: Callable[[str, str], None]) -> None:
    if profile is None:
        return
    if not source_agent.is_dir():
        add(YELLOW, "source infrastructure unavailable; source/deployed drift not checked")
        return
    try:
        paths = upgrade_mod.profile_infrastructure_files(source_agent, profile)
        changed = [rel.as_posix() for rel in paths if upgrade_mod.needs_profile_copy(
            source_agent / rel, agent / rel, profile=profile, relative=rel,
        )]
    except (OSError, ValueError) as exc:
        add(YELLOW, f"source/deployed drift unavailable: {type(exc).__name__}")
        return
    if changed:
        add(RED, "source/deployed drift (missing or changed stack-owned files): " + ", ".join(changed))
    else:
        add(GREEN, "source/deployed stack-owned infrastructure matches recorded profile")


# ---- pre-v0.9.0 migration prompt -------------------------------------

def _audit_pre_v090(target_root: Path, log: Callable[[str], None]) -> int:
    """No install.json. Detect adapters from filesystem and prompt to register.

    Codex UX rule: never silently mutate. Show user what we found, ask Y/N,
    write only on confirmation. On N or non-tty, exit 0 with no write.

    Synthesis requires the distinctive brain layout (.agent/memory/,
    skills/, protocols/) to exist. Without this gate, a random repo
    that happens to contain a common filename like `run.py` or
    `AGENTS.md` would prompt the user and on Enter write a bogus
    install.json for adapters that were never installed.
    """
    if not state_mod.brain_present(target_root):
        log(f"no install.json found at {target_root / '.agent/install.json'}")
        log(f"no agentic-stack brain found at {target_root / '.agent'} either.")
        log("nothing to audit. install an adapter with: ./install.sh <adapter-name>")
        return 0

    detected: list[tuple[str, str]] = []  # (name, signal_strength_summary)
    for name, signals in DETECT_SIGNALS.items():
        present = [(f, strength) for f, strength in signals
                   if (target_root / f).exists()]
        if not present:
            continue
        strength = "strong" if any(s == "strong" for _, s in present) else "weak"
        sig_str = ", ".join(f for f, _ in present)
        detected.append((name, f"{strength} — {sig_str}"))

    if not detected:
        log(f"no install.json found at {target_root / '.agent/install.json'}")
        log("brain is present but no adapter signals detected.")
        log("install an adapter with: ./install.sh <adapter-name>")
        return 0

    log(f"no install.json found at {target_root / '.agent/install.json'}")
    log("but I see these adapters appear to be installed:")
    log("")
    for name, sig in detected:
        log(f"  ✓ {name:18s} ({sig})")
    log("")
    log("register them in install.json so I can audit them in future runs?")

    # Non-interactive (no tty) → don't prompt, just exit 0 cleanly.
    if not sys.stdin.isatty():
        log("(non-interactive shell; skipping. re-run from a terminal to register.)")
        return 0

    try:
        answer = input("[Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        log("")
        log("aborted; no changes written.")
        return 0
    if answer not in ("", "y", "yes"):
        log("ok, leaving install.json absent. you can run `./install.sh <adapter>` "
            "per adapter to register explicitly.")
        return 0

    # Synthesize install.json from detected adapters. Crucially, walk each
    # adapter's manifest and populate files_written / skills_link with what
    # the old install.sh would have written. Without this, `remove` is a
    # no-op for migrated installs (files stay on disk) and a follow-up
    # `./install.sh add <name>` would reclassify our own files as
    # user-owned — codex P1 caught this on the migration path.
    doc = state_mod.empty(target_root, __version__)
    now = state_mod._iso_now()  # type: ignore  # internal helper, fine here

    # Defer the import to avoid circulars.
    from . import schema as schema_mod

    stack_root = Path(__file__).resolve().parent.parent
    for name, _sig in detected:
        manifest_path = stack_root / "adapters" / name / "adapter.json"
        files_written: list[str] = []
        files_alerted: list[str] = []
        skills_link = None
        files_overwritten: list[str] = []
        post_install_results: list[dict] = []
        skills_link_pre_existed = True  # conservative default for migration
        if manifest_path.is_file():
            try:
                manifest = schema_mod.validate(manifest_path)
                # Migration is conservative: we don't know whether the old
                # install.sh adopted user content (overwrite/skip_if_exists
                # paths could have been pre-existing user files like
                # CLAUDE.md or run.py that the old installer just clobbered).
                # Synthesizing those as files_written would let `remove`
                # delete genuinely-user content.
                #
                # Bucketing rule for synthesis:
                #   merge_or_alert → files_alerted   (user-owned by spec)
                #   anything else  → files_overwritten (be conservative,
                #                                       preserve on remove)
                #
                # User can re-run `./install.sh <adapter>` to get strict
                # ownership (files_written) and full remove behavior.
                for entry in manifest.get("files", []):
                    dst = entry.get("dst")
                    if not dst:
                        continue
                    if (target_root / dst).exists():
                        if entry.get("merge_policy") == "merge_or_alert":
                            files_alerted.append(dst)
                        else:
                            files_overwritten.append(dst)
                # Skills_link: same logic. Old install.sh COULD have adopted
                # a pre-existing dir via -L/-d. Conservative synthesis says
                # "user-owned" so remove won't delete it. User re-installs to
                # get strict ownership tracking.
                if "skills_link" in manifest:
                    sl_dst = target_root / manifest["skills_link"]["dst"]
                    if sl_dst.exists() or sl_dst.is_symlink():
                        skills_link = manifest["skills_link"]
                        # already True from default

                # If this is the openclaw adapter and the agent is currently
                # registered in ~/.openclaw/openclaw.json, recover the
                # post_install record so a future `remove` can reverse it.
                if name == "openclaw":
                    from .post_install import _openclaw_agent_name
                    expected_name = _openclaw_agent_name(target_root)
                    check = _check_openclaw_agent(expected_name)
                    if check == "ok":
                        post_install_results.append({
                            "action": "openclaw_register_workspace",
                            "status": "ok",
                            "agent_name": expected_name,
                        })
            except Exception:
                # If the manifest is missing or invalid, fall back to the
                # bare-minimum entry. Doctor will still flag missing files
                # later because there's nothing to check.
                pass

        adapter_entry = {
            "installed_at": now,
            "files_written": files_written,           # always [] for synthesized
            "files_overwritten": files_overwritten,   # all non-merge_or_alert files (conservative)
            "files_alerted": files_alerted,
            "file_results": [],
            "post_install_results": post_install_results,
            "_synthesized": True,  # marker for future migrations
        }
        if skills_link is not None:
            adapter_entry["skills_link"] = skills_link
            adapter_entry["skills_link_pre_existed"] = skills_link_pre_existed
        doc["adapters"][name] = adapter_entry

    state_mod.save(target_root, doc)
    log(f"  ✓ wrote install.json with {len(detected)} synthesized adapter(s)")
    if any(doc["adapters"][n].get("files_alerted") for n in doc["adapters"]):
        log("  ! some AGENTS.md files were marked as alerted (existing content"
            " preserved); next doctor run will check for the .agent/ marker.")
    return 0
