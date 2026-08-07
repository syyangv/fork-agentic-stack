"""Argparse dispatcher. install.sh and install.ps1 invoke this.

Verbs (subcommands): add, remove, doctor, status, manage, dashboard,
mission-control (beta), brain, transfer, upgrade, sync-manifest.
Anything else in first position → treated as an adapter name (existing
`./install.sh <adapter>` UX preserved).
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

from . import doctor as doctor_mod
from . import install as install_mod
from . import profiles as profiles_mod
from . import remove as remove_mod
from . import schema as schema_mod
from . import skill_manifest as skill_manifest_mod
from . import state as state_mod
from . import status as status_mod
from . import upgrade as upgrade_mod
from . import __version__


VERBS = {
    "add",
    "remove",
    "doctor",
    "status",
    "manage",
    "dashboard",
    "dash",
    "mission-control",
    "mission",
    "mc",
    "brain",
    "transfer",
    "upgrade",
    "sync-manifest",
    "scheduler",
}


def _stack_root() -> Path:
    """Path to the agentic-stack source root.

    Honors AGENTIC_STACK_ROOT env override (CI / non-standard installs).
    Otherwise: walk up from this file (.../harness_manager/cli.py) two
    levels.
    """
    env = os.environ.get("AGENTIC_STACK_ROOT")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parent.parent


def _adapter_dir(adapter_name: str) -> Path:
    return _stack_root() / "adapters" / adapter_name


def _adapter_manifest(adapter_name: str) -> dict:
    """Load and validate adapter.json for adapter_name."""
    p = _adapter_dir(adapter_name) / "adapter.json"
    if not p.is_file():
        raise SystemExit(
            f"error: adapter '{adapter_name}' has no adapter.json at {p}\n"
            f"available adapters: {_list_adapters()}"
        )
    return schema_mod.validate(p)


def _list_adapters() -> str:
    root = _stack_root() / "adapters"
    if not root.is_dir():
        return "(adapters dir missing)"
    names = sorted(p.name for p in root.iterdir() if p.is_dir())
    return ", ".join(names)


def _existing_profile(target: Path) -> str:
    """Keep an existing project on its recorded profile when adding adapters."""
    document = state_mod.load(target) or {}
    orchestration = document.get("orchestration")
    if isinstance(orchestration, dict) and isinstance(orchestration.get("profile"), str):
        return orchestration["profile"]
    return profiles_mod.STANDARD


def _maybe_run_onboard(target: Path, wizard_flags: list[str]) -> int:
    """Run onboard.py against target after install (mirrors install.sh:249).

    Returns the wizard's exit code so cmd_install can propagate failures
    (Ctrl-C in the wizard, exception in onboard.py, etc.). Pre-v0.9.0
    install.sh did `exec python3 onboard.py` so failures naturally
    flowed up — this preserves that contract for CI / scripted users.

    Returns 0 if onboard.py or python3 is missing (matches bash tip-and-skip).
    """
    onboard = _stack_root() / "onboard.py"
    if not onboard.is_file():
        print(
            f"tip: customize {target}/.agent/memory/personal/PREFERENCES.md "
            "with your conventions."
        )
        return 0
    cmd = [sys.executable, str(onboard), str(target), *wizard_flags]
    try:
        result = subprocess.run(cmd, check=False)
        return result.returncode
    except FileNotFoundError:
        print(
            "tip: python3 not found — edit "
            ".agent/memory/personal/PREFERENCES.md manually."
        )
        return 0
    except KeyboardInterrupt:
        # User Ctrl-C'd the wizard. Treat as a real failure so callers
        # know the install is incomplete.
        print()
        print("onboarding cancelled by user; install state may be partial.")
        return 130


# ---- subcommands -----------------------------------------------------

def cmd_install(
    adapter_name: str,
    target: Path,
    wizard_flags: list[str],
    profile: str | None = None,
    scheduled_python: str | None = None,
) -> int:
    """Install one adapter into target. Existing `./install.sh <adapter>` UX.

    Refuses on pre-v0.9 projects (no install.json) when STRONG adapter
    signals are already on disk — without this guard, the install would
    create a fresh install.json containing only the newly-installed
    adapter, orphaning every pre-v0.9 install (they'd vanish from
    status/doctor/remove even though their files remain on disk). The
    same gate cmd_add uses. Weak signals (plain CLAUDE.md, AGENTS.md,
    run.py) are ignored to avoid false-refusing clean repos that happen
    to contain one of those common files.
    """
    detected = state_mod.legacy_unregistered_adapters(target)
    if detected:
        # Pre-v0.9 project: brain is present AND adapter signals exist.
        # Refuse so doctor can synthesize install.json first and
        # preserve the prior install(s). Brain-without-signals is NOT
        # gated — cloning agentic-stack itself, or copying the brain
        # template before first install, shouldn't deadlock the user.
        print(
            f"error: {target}/.agent/ exists but install.json does not.\n"
            f"this looks like a pre-v0.9 install. detected adapters: {detected}\n"
            f"\n"
            f"run this first to register them safely:\n"
            f"  ./install.sh doctor\n"
            f"\n"
            f"proceeding would otherwise create a fresh install.json with only\n"
            f"the new adapter, leaving the existing ones invisible to\n"
            f"status/doctor/remove.",
            file=sys.stderr,
        )
        return 2
    profile = profiles_mod.validate_profile(profile or _existing_profile(target))
    try:
        manifest = _adapter_manifest(adapter_name)
        install_mod.install(
            manifest=manifest,
            target_root=target,
            adapter_dir=_adapter_dir(adapter_name),
            stack_root=_stack_root(),
            profile=profile,
            scheduled_python=scheduled_python,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    # Propagate the onboarding wizard's exit code: Ctrl-C, exception, or
    # explicit failure inside onboard.py should fail the install command,
    # matching the pre-v0.9.0 `exec python3 onboard.py` semantics.
    rc = _maybe_run_onboard(target, wizard_flags)
    if rc != 0:
        return rc
    # Post-install: offer the manage TUI so users who installed one
    # adapter can immediately add others without re-running install.sh.
    # Skip if --yes (scripted) or non-TTY (CI safety).
    if "--yes" not in wizard_flags and sys.stdin.isatty() and sys.stdout.isatty():
        _maybe_offer_manage(target)
    return 0


def _maybe_offer_manage(target: Path) -> None:
    """Offer the manage TUI after a single-adapter install.

    Only invoked from cmd_install when the shell is interactive and the
    user didn't pass --yes. If every available adapter is already
    installed, skip the prompt — nothing useful to do in the TUI. Default
    is no, so just hitting enter dismisses without entering the TUI.
    """
    doc = state_mod.load(target) or {}
    installed = set(doc.get("adapters") or {})
    available = set()
    root = _stack_root() / "adapters"
    if root.is_dir():
        for p in root.iterdir():
            if p.is_dir() and (p / "adapter.json").is_file():
                available.add(p.name)
    not_installed = sorted(available - installed)
    if not not_installed:
        return
    sys.path.insert(0, str(_stack_root()))
    import onboard_widgets as widgets  # noqa: E402
    print()
    try:
        choice = widgets.ask_confirm(
            f"install or manage other adapters? ({len(not_installed)} available)",
            default=False,
        )
    except KeyboardInterrupt:
        # Install already succeeded; treat Ctrl-C at the offer as "no."
        print()
        return
    if choice:
        from . import manage_tui
        manage_tui.run(target_root=target, stack_root=_stack_root())


def cmd_add(
    adapter_name: str, target: Path, profile: str | None = None,
    scheduled_python: str | None = None,
) -> int:
    """Append one adapter to an existing project (no onboard wizard re-run).

    Refuses on pre-v0.9 projects (no install.json yet). Without this check,
    `add` would create a fresh install.json with ONLY the new adapter, and
    every adapter previously installed via the old install.sh would
    disappear from status/doctor/remove tracking even though their files
    are still on disk.
    """
    detected = state_mod.legacy_unregistered_adapters(target)
    if detected:
        print(
            f"error: {target}/.agent/ exists but install.json does not.\n"
            f"this looks like a pre-v0.9 install. detected adapters: {detected}\n"
            f"\n"
            f"run this first to register them safely:\n"
            f"  ./install.sh doctor\n"
            f"\n"
            f"`add` would otherwise create a fresh install.json with only\n"
            f"the new adapter, leaving the existing ones invisible to\n"
            f"status/doctor/remove.",
            file=sys.stderr,
        )
        return 2
    profile = profiles_mod.validate_profile(profile or _existing_profile(target))
    try:
        manifest = _adapter_manifest(adapter_name)
        install_mod.install(
            manifest=manifest,
            target_root=target,
            adapter_dir=_adapter_dir(adapter_name),
            stack_root=_stack_root(),
            profile=profile,
            scheduled_python=scheduled_python,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


def cmd_remove(adapter_name: str, target: Path, yes: bool) -> int:
    return remove_mod.remove(target_root=target, adapter_name=adapter_name, yes=yes)


def cmd_doctor(target: Path, *, scheduler_home: Path | None = None,
               scheduler_runner: object | None = None) -> int:
    result = doctor_mod.audit(target_root=target)
    if (scheduler_home is None
            and os.path.abspath(target) == os.path.abspath(Path.home())):
        scheduler_home = Path.home()
    if scheduler_home is None:
        return result
    from . import scheduler_control, scheduler_doctor
    try:
        status, lines, _evidence = scheduler_control.collect_doctor(
            target=target, home=scheduler_home, runner=scheduler_runner,
            now=dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        )
    except (OSError, ValueError, scheduler_control.scheduler_lifecycle.LifecycleError) as exc:
        print(f"[RED] scheduler: {exc}")
        return 1
    for line in lines:
        print(f"[scheduler] {line}")
    return 1 if status == scheduler_doctor.RED else result


def cmd_scheduler(args: list[str], *, yes: bool, runner: object | None = None,
                  plist_validator: object | None = None) -> int:
    """Manage the user-level scheduler only through an explicit home."""
    if yes is not True:
        print("error: scheduler lifecycle requires explicit --yes", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(prog="./install.sh scheduler")
    parser.add_argument("action", choices=("install", "upgrade", "rollback", "uninstall"))
    parser.add_argument("target", nargs="?")
    parser.add_argument("--home", required=True)
    ns = parser.parse_args(args)
    target = Path(ns.target) if ns.target else Path(ns.home)
    from . import scheduler_control
    try:
        scheduler_control.run_lifecycle(
            ns.action, target=target, home=Path(ns.home), yes=yes,
            runner=runner, plist_validator=plist_validator,
        )
    except (OSError, ValueError, scheduler_control.scheduler_lifecycle.LifecycleError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"scheduler {ns.action} complete for {ns.home}")
    return 0


def cmd_status(target: Path) -> int:
    return status_mod.show(target_root=target)


def cmd_manage(target: Path) -> int:
    """Open the persistent TUI menu for ongoing adapter management."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(
            "error: manage is an interactive TUI; this shell is not a TTY.\n"
            "use the verb-style subcommands instead:\n"
            "  ./install.sh add <adapter>\n"
            "  ./install.sh remove <adapter>\n"
            "  ./install.sh doctor\n"
            "  ./install.sh status",
            file=sys.stderr,
        )
        return 2
    from . import manage_tui
    return manage_tui.run(target_root=target, stack_root=_stack_root())


def cmd_dashboard(target: Path, plain: bool = False) -> int:
    """Open the project dashboard.

    `plain` is primarily for non-TTY terminals and tests. The dashboard
    runner also falls back to plain output automatically when stdin/stdout
    are not interactive.
    """
    from . import dashboard_tui
    return dashboard_tui.run(target_root=target, stack_root=_stack_root(), plain=plain)


def cmd_mission_control(args: list[str]) -> int:
    """Serve the beta local web Mission Control dashboard."""
    parser = argparse.ArgumentParser(
        prog="./install.sh mission-control",
        description=(
            "Beta local web dashboard. It runs only while this command is active; "
            "turn it off with Ctrl-C and clear local events by removing "
            ".agent/runtime/mission-control-events.jsonl."
        ),
    )
    parser.add_argument("target", nargs="?", default=str(Path.cwd()))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--snapshot")
    parser.add_argument("--no-open", action="store_true")
    ns = parser.parse_args(args)
    from . import mission_control
    return mission_control.run(
        target_root=Path(ns.target),
        stack_root=_stack_root(),
        host=ns.host,
        port=ns.port,
        snapshot=ns.snapshot,
        open_browser=not ns.no_open,
    )


def cmd_brain(args: list[str]) -> int:
    from . import brain as brain_mod
    return brain_mod.run(args, target_root=Path.cwd(), stack_root=_stack_root())


def cmd_transfer(args: list[str], target: Path) -> int:
    from . import transfer_tui
    return transfer_tui.run(args, target_root=target, stack_root=_stack_root())


def cmd_sync_manifest(target: Path) -> int:
    try:
        skill_manifest_mod.sync_manifest(target)
        return 0
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


def cmd_upgrade(args: list[str], yes: bool) -> int:
    dry_run = False
    target_args: list[str] = []
    for arg in args:
        if arg == "--dry-run":
            dry_run = True
        elif arg in ("--yes", "-y"):
            yes = True
        else:
            target_args.append(arg)
    if len(target_args) > 1:
        print("usage: ./install.sh upgrade [target-dir] [--dry-run] [--yes]", file=sys.stderr)
        return 2
    target = Path(target_args[0]) if target_args else Path.cwd()
    return upgrade_mod.upgrade(
        target_root=target,
        stack_root=_stack_root(),
        dry_run=dry_run,
        yes=yes,
    )


def cmd_bare(target: Path, wizard_flags: list[str], profile: str | None = None) -> int:
    """`./install.sh` with no args.

    Behavior:
      - install.json present  → list what's still installable
      - no install.json + TTY → enter the onboarding wizard (multi-select
        harness step, then per-adapter install, then PREFERENCES.md flow)
      - no install.json + non-TTY → print usage and exit 2 (CI safety)
    """
    doc = state_mod.load(target)
    if doc is not None:
        if profile is not None:
            print(
                "error: --profile cannot change an installed project; "
                "use its recorded profile or a fresh installation",
                file=sys.stderr,
            )
            return 2
        if "--yes" not in wizard_flags and sys.stdin.isatty() and sys.stdout.isatty():
            return cmd_dashboard(target)
        installed = set(doc.get("adapters", {}).keys())
        available = set()
        root = _stack_root() / "adapters"
        if root.is_dir():
            for p in root.iterdir():
                if p.is_dir() and (p / "adapter.json").is_file():
                    available.add(p.name)
        not_installed = sorted(available - installed)
        if not not_installed:
            print(f"all available adapters already installed: {sorted(installed)}")
            print("open dashboard: ./install.sh dashboard")
            print("run `./install.sh status` for a summary.")
            return 0
        print(f"already installed: {sorted(installed)}")
        print(f"available to add:  {not_installed}")
        print()
        print(f"to add one: ./install.sh add <name>")
        print(f"open dashboard: ./install.sh dashboard")
        print(f"or adapter manager only: ./install.sh manage")
        return 0

    # No install.json. Pre-v0.9 migration gate: if this is a legacy
    # install (brain AND adapter signals present) we must register
    # via doctor first. Brain-only (no signals) falls through to the
    # wizard — someone dropped the template here but never installed
    # anything, so there's nothing to migrate.
    detected = state_mod.legacy_unregistered_adapters(target)
    if detected:
        print(
            f"pre-v0.9 install detected at {target}.",
            file=sys.stderr,
        )
        print(
            f".agent/ exists but install.json does not. detected adapters: "
            f"{detected}",
            file=sys.stderr,
        )
        print(file=sys.stderr)
        print(
            "run this first to register them safely:", file=sys.stderr,
        )
        print("  ./install.sh doctor", file=sys.stderr)
        print(file=sys.stderr)
        print(
            "then re-run ./install.sh to add more adapters or use "
            "./install.sh manage.",
            file=sys.stderr,
        )
        return 2

    # No install.json and not a legacy install — fresh project. Two paths.
    if sys.stdin.isatty() and sys.stdout.isatty():
        return _run_install_wizard(target, wizard_flags, profile or profiles_mod.STANDARD)

    # Non-TTY (CI, scripted) → print usage, exit 2.
    print("usage: ./install.sh <adapter-name> [target-dir] [--profile standard|minimal]")
    print(f"adapters: {_list_adapters()}")
    print()
    print("on a project that's already installed, run:")
    print("  ./install.sh doctor      # audit")
    print("  ./install.sh status      # quick read-only view")
    print("  ./install.sh add <name>  # install another adapter")
    print("  ./install.sh remove <name>  # remove an adapter (with confirm)")
    print("  ./install.sh dashboard   # interactive project dashboard")
    print("  ./install.sh mission-control --port 8787  # beta local web dashboard; Ctrl-C turns it off")
    print("  ./install.sh brain status  # optional external Brain CLI integration")
    print("  ./install.sh manage      # interactive TUI for adapter management")
    print("  ./install.sh transfer    # onboarding-style memory transfer wizard")
    print("  ./install.sh upgrade     # safely refresh .agent infrastructure")
    print("  ./install.sh sync-manifest  # repair skills/_manifest.jsonl")
    return 2


def _run_install_wizard(target: Path, wizard_flags: list[str], profile: str) -> int:
    """Onboarding wizard: detect → multi-select → install each → PREFERENCES.md.

    The original "give people options like Claude Code and all of that"
    flow. Reuses the manage TUI's multi-select widget for harness pick.
    Auto-checks adapters whose detection signals are present in `target`,
    so a user who already has CLAUDE.md / .cursor/ in their repo gets
    those pre-selected.
    """
    # Lazy imports — wizard path only.
    sys.path.insert(0, str(_stack_root()))
    import onboard_widgets as widgets  # noqa: E402
    from onboard_ui import print_banner, intro, R, MUTED, GREEN  # noqa: E402
    from . import doctor as doctor_mod

    print_banner()
    intro("agentic-stack onboarding")

    # Discover all available adapters.
    available = sorted(n for n, _ in schema_mod.discover_all(_stack_root()))
    if not available:
        print(f"  {MUTED}no adapters available — repo seems empty{R}")
        return 1

    # Auto-detect what's already on disk in the target. Only STRONG
    # signals count for default-check — a weak signal like a generic
    # CLAUDE.md, AGENTS.md, or run.py can belong to any project, so
    # pre-checking one and hitting Enter at the multiselect would act
    # on a file we have no evidence we own. Weak-only matches still
    # surface to the user via the adapter list but stay unchecked
    # until toggled.
    #
    # Adapters that install only shared filenames (claude-code, codex,
    # opencode) are weak-only by necessity and never pre-check here.
    # That is the honest outcome: nothing on disk distinguishes "we
    # installed it" from "the user wrote their own CLAUDE.md".
    detected = set()
    for name in available:
        signals = doctor_mod.DETECT_SIGNALS.get(name, [])
        if any(
            (target / f).exists()
            for f, strength in signals
            if strength == "strong"
        ):
            detected.add(name)
    defaults = [available.index(n) for n in available if n in detected]

    if detected:
        print(f"  {GREEN}detected{R}: {sorted(detected)} — pre-checked below.")
        print()

    chosen = widgets.ask_multiselect(
        "which harnesses are you using?",
        available,
        defaults=defaults,
    )
    if not chosen:
        print(f"  {MUTED}no adapters selected; brain not installed.{R}")
        print(f"  {MUTED}you can run `./install.sh <adapter>` later.{R}")
        return 0

    # Install each selected adapter via the manifest backend.
    for name in chosen:
        manifest_path = _stack_root() / "adapters" / name / "adapter.json"
        manifest = schema_mod.validate(manifest_path)
        try:
            install_mod.install(
                manifest=manifest,
                target_root=target,
                adapter_dir=_stack_root() / "adapters" / name,
                stack_root=_stack_root(),
                profile=profile,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    # Continue to existing PREFERENCES.md flow.
    return _maybe_run_onboard(target, wizard_flags)


# ---- main ------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    # Extract --yes / --reconfigure / --force into wizard_flags; these
    # pass through to onboard.py for back-compat with the bash flow.
    wizard_flags: list[str] = []
    rest: list[str] = []
    i = 0
    yes = False
    profile: str | None = None
    scheduled_python: str | None = None
    while i < len(argv):
        a = argv[i]
        if a in ("--yes", "-y"):
            wizard_flags.append("--yes")
            yes = True
        elif a == "--reconfigure":
            wizard_flags.append("--reconfigure")
        elif a == "--force":
            wizard_flags.append("--force")
        elif a == "--profile":
            if i + 1 >= len(argv):
                print("error: --profile requires standard or minimal", file=sys.stderr)
                return 2
            i += 1
            profile = argv[i]
        elif a.startswith("--profile="):
            profile = a.split("=", 1)[1]
        elif a == "--python":
            if i + 1 >= len(argv):
                print("error: --python requires an absolute interpreter path", file=sys.stderr)
                return 2
            i += 1
            scheduled_python = argv[i]
        elif a.startswith("--python="):
            scheduled_python = a.split("=", 1)[1]
        else:
            rest.append(a)
        i += 1

    if profile is not None:
        try:
            profiles_mod.validate_profile(profile)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    if not rest:
        if scheduled_python is not None:
            print("error: --python is only supported when installing an adapter", file=sys.stderr)
            return 2
        target = Path.cwd()
        return cmd_bare(target, wizard_flags, profile)

    first = rest[0]

    if profile is not None and first in VERBS and first != "add":
        print(
            "error: --profile is only supported when installing an adapter or fresh project",
            file=sys.stderr,
        )
        return 2
    if scheduled_python is not None and first in VERBS and first != "add":
        print("error: --python is only supported when installing an adapter", file=sys.stderr)
        return 2

    if first in VERBS:
        verb = first
        if verb == "add":
            if len(rest) < 2:
                print("usage: ./install.sh add <adapter-name> [target-dir]", file=sys.stderr)
                return 2
            adapter = rest[1]
            target = Path(rest[2]) if len(rest) >= 3 else Path.cwd()
            return cmd_add(adapter, target, profile, scheduled_python)
        if verb == "remove":
            if len(rest) < 2:
                print("usage: ./install.sh remove <adapter-name> [target-dir] [--yes]", file=sys.stderr)
                return 2
            adapter = rest[1]
            target = Path(rest[2]) if len(rest) >= 3 else Path.cwd()
            return cmd_remove(adapter, target, yes=yes)
        if verb == "doctor":
            doctor_args = list(rest[1:])
            scheduler_home = None
            if "--scheduler-home" in doctor_args:
                index = doctor_args.index("--scheduler-home")
                if index + 1 >= len(doctor_args):
                    print("error: --scheduler-home requires an absolute path", file=sys.stderr)
                    return 2
                scheduler_home = Path(doctor_args[index + 1])
                del doctor_args[index:index + 2]
            if len(doctor_args) > 1:
                print("usage: ./install.sh doctor [target-dir] [--scheduler-home HOME]", file=sys.stderr)
                return 2
            target = Path(doctor_args[0]) if doctor_args else Path.cwd()
            return cmd_doctor(target, scheduler_home=scheduler_home)
        if verb == "status":
            target = Path(rest[1]) if len(rest) >= 2 else Path.cwd()
            return cmd_status(target)
        if verb == "manage":
            target = Path(rest[1]) if len(rest) >= 2 else Path.cwd()
            return cmd_manage(target)
        if verb in ("dashboard", "dash"):
            plain = False
            target_args: list[str] = []
            for arg in rest[1:]:
                if arg == "--plain":
                    plain = True
                else:
                    target_args.append(arg)
            if len(target_args) > 1:
                print("usage: ./install.sh dashboard [target-dir] [--plain]", file=sys.stderr)
                return 2
            target = Path(target_args[0]) if target_args else Path.cwd()
            return cmd_dashboard(target, plain=plain)
        if verb in ("mission-control", "mission", "mc"):
            return cmd_mission_control(rest[1:])
        if verb == "brain":
            brain_args = list(rest[1:])
            brain_command = brain_args[0] if brain_args else "status"
            if brain_command == "onboard":
                if yes and "--yes" not in brain_args and "-y" not in brain_args:
                    brain_args.append("--yes")
                if "--reconfigure" in wizard_flags and "--reconfigure" not in brain_args:
                    brain_args.append("--reconfigure")
            return cmd_brain(brain_args)
        if verb == "transfer":
            return cmd_transfer(rest[1:], Path.cwd())
        if verb == "upgrade":
            return cmd_upgrade(rest[1:], yes=yes)
        if verb == "sync-manifest":
            target = Path(rest[1]) if len(rest) >= 2 else Path.cwd()
            return cmd_sync_manifest(target)
        if verb == "scheduler":
            return cmd_scheduler(rest[1:], yes=yes)

    # Treat as adapter name (existing UX)
    adapter = first
    target = Path(rest[1]) if len(rest) >= 2 else Path.cwd()
    return cmd_install(adapter, target, wizard_flags, profile, scheduled_python)


if __name__ == "__main__":
    sys.exit(main())
