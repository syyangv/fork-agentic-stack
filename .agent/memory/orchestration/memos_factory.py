"""Construction boundary for the isolated Phase 3 MemOS provider."""
from __future__ import annotations

import os
import re
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path

from .memos_bridge import BridgeConfig, MemOSBridgeClient
from .host_evolution import (
    ClaudeOpusNativeAdapter, DailyQuotaStore, MemosOpusHostHandler,
)
from .memos_journal import MemosDeliveryJournal
from .memos_runtime import (
    EvolutionPilotConfig,
    bridge_command,
    load_evolution_pilot_config,
    prepare_project_runtime,
    runtime_paths,
    runtime_environment,
    validate_pinned_plugin,
)
from .providers.memos_local import MemosLocalProvider
from .revalidation import RevalidationIndex


@dataclass(slots=True)
class MemosProviderSession:
    provider: MemosLocalProvider
    client: MemOSBridgeClient | None
    _worker: AbstractContextManager | None = field(default=None, init=False)
    _entered: bool = field(default=False, init=False)
    assist_deadline: float | None = None
    evolution_pilot: EvolutionPilotConfig | None = None

    def close(self) -> None:
        if self._entered:
            try:
                if self.client is not None:
                    self.client.close(deadline=self.assist_deadline)
            finally:
                if self._worker is not None:
                    self._worker.__exit__(None, None, None)
                self._worker = None
                self._entered = False
        elif self.client is not None:
            with self.provider.journal.delivery_worker():
                self.client.close()

    def __enter__(self) -> MemosLocalProvider:
        if self._entered:
            raise RuntimeError("MemOS provider session is already entered")
        timeout = None
        if self.provider.mode == "assist" and self.assist_deadline is not None:
            timeout = max(0.0, self.assist_deadline - time.monotonic())
        self._worker = self.provider.journal.delivery_worker(timeout=timeout)
        try:
            self._worker.__enter__()
        except TimeoutError:
            if self.provider.mode != "assist" or self.assist_deadline is None:
                raise
            self._worker = None
            self.provider._session_lock_error = "behavioral_project_lock_timeout"
            if self.evolution_pilot is not None:
                raise RuntimeError("evolution pilot lifecycle lock timeout")
        self.provider._assist_deadline = self.assist_deadline
        self._entered = True
        if self.evolution_pilot is not None and self.client is not None:
            try:
                self.provider._validated_health = self.client.health(
                    timeout=min(30.0, self.evolution_pilot.timeout_seconds),
                )
            except BaseException:
                self.close()
                raise
        return self.provider

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def create_memos_provider(
    agent_root: str | Path,
    project_id: str,
    *,
    mode: str = "shadow",
    code_root: str | Path | None = None,
    data_root: str | Path | None = None,
    repo_root: str | Path | None = None,
    assist_deadline: float | None = None,
) -> MemosProviderSession:
    """Provision private state and attach a client only to a pinned install."""
    agent_root = Path(agent_root).expanduser().resolve(strict=False)
    code_root = Path(code_root or agent_root / "runtime" / "providers")
    data_root = Path(data_root or agent_root / "runtime" / "memos")
    evolution_pilot = None
    pilot_path = os.environ.get("AGENTIC_EVOLUTION_PILOT_CONFIG")
    if pilot_path:
        if repo_root is None:
            raise ValueError(
                "repo_root is required when AGENTIC_EVOLUTION_PILOT_CONFIG is set"
            )
        evolution_pilot = load_evolution_pilot_config(
            pilot_path, project_id=project_id, repo_root=repo_root,
        )
        prospective = runtime_paths(code_root, data_root, project_id)
        validate_pinned_plugin(prospective.plugin_dir)
        repository_revision = _repository_revision(Path(repo_root))
    else:
        repository_revision = None
    paths = prepare_project_runtime(
        code_root,
        data_root,
        project_id,
        evolution_pilot=evolution_pilot is not None,
        host_model=evolution_pilot.model if evolution_pilot else "gpt",
        min_distinct_episodes=(
            evolution_pilot.min_distinct_episodes if evolution_pilot else 3
        ),
    )
    initialize_timeout = None
    if mode == "assist" and assist_deadline is not None:
        initialize_timeout = max(0.0, assist_deadline - time.monotonic())
    journal = MemosDeliveryJournal(
        paths.project_root / "delivery.sqlite3",
        initialize_timeout=initialize_timeout,
    )
    bridge = paths.plugin_dir / "node_modules/@memtensor/memos-local-plugin/dist/bridge.cjs"
    client = None
    host_handler = None
    if evolution_pilot is not None:
        host_root = paths.project_root / "host-evolution"
        inference_cwd = host_root / "cwd"
        inference_cwd.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(host_root, 0o700)
        os.chmod(inference_cwd, 0o700)
        if any(inference_cwd.iterdir()):
            raise RuntimeError("evolution inference cwd must remain empty")
        adapter = ClaudeOpusNativeAdapter(
            executable=os.environ.get("AGENTIC_CLAUDE_COMMAND", "claude"),
            model=evolution_pilot.model,
            cwd=inference_cwd,
            timeout_seconds=evolution_pilot.timeout_seconds,
            environment=os.environ,
            home=os.environ.get("HOME", str(Path.home())),
        )
        host_handler = MemosOpusHostHandler(
            adapter=adapter,
            quota=DailyQuotaStore(host_root / "quota.sqlite3", evolution_pilot.daily_caps),
            audit_file=host_root / "audit.jsonl",
            expected_model=evolution_pilot.model,
            project_id=project_id,
            repository_revision=repository_revision,
        )
    if bridge.is_file():
        environment = runtime_environment(
            paths,
            {
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "LANG": os.environ.get("LANG", "C.UTF-8"),
                "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
            },
        )
        client = MemOSBridgeClient(BridgeConfig(
            command=bridge_command(paths),
            home=str(paths.home),
            memos_home=str(paths.memos_home),
            memos_config_file=str(paths.config_file),
            cwd=paths.plugin_dir,
            env=environment,
            inherit_environment=False,
            call_timeout=_call_timeout(),
            request_handlers=(
                {"host.llm.complete": host_handler} if host_handler is not None else None
            ),
            request_timeout=(
                evolution_pilot.timeout_seconds + 5.0
                if evolution_pilot is not None else 45.0
            ),
        ))
    provider = MemosLocalProvider(
        project_id=project_id, journal=journal, client=client, mode=mode,
        revalidation_index=RevalidationIndex(
            agent_root / "memory" / "evidence" / "revalidation.sqlite3"
        ),
    )
    return MemosProviderSession(
        provider=provider, client=client, assist_deadline=assist_deadline,
        evolution_pilot=evolution_pilot,
    )


def _call_timeout() -> float:
    raw = os.environ.get("AGENTIC_MEMOS_CALL_TIMEOUT", "2")
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError("AGENTIC_MEMOS_CALL_TIMEOUT must be numeric") from exc
    if not 0.1 <= value <= 30:
        raise ValueError("AGENTIC_MEMOS_CALL_TIMEOUT must be between 0.1 and 30 seconds")
    return value


def _repository_revision(repo_root: Path) -> str:
    try:
        marker = repo_root / ".git"
        if marker.is_file():
            line = marker.read_text("utf-8").strip()
            if not line.startswith("gitdir: "):
                raise ValueError
            git_dir = Path(line[8:])
            if not git_dir.is_absolute():
                git_dir = (repo_root / git_dir).resolve(strict=True)
        else:
            git_dir = marker.resolve(strict=True)
        head = (git_dir / "HEAD").read_text("ascii").strip()
        if head.startswith("ref: "):
            ref = head[5:]
            if not re.fullmatch(r"refs/[A-Za-z0-9._/-]{1,500}", ref) or ".." in ref:
                raise ValueError
            loose = git_dir / ref
            if loose.is_file():
                revision = loose.read_text("ascii").strip()
            else:
                revision = ""
                for line in (git_dir / "packed-refs").read_text("ascii").splitlines():
                    if line and not line.startswith(("#", "^")):
                        candidate, name = line.split(" ", 1)
                        if name == ref:
                            revision = candidate
                            break
        else:
            revision = head
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError("evolution pilot repository revision is unavailable") from exc
    if re.fullmatch(r"[0-9a-f]{40,64}", revision) is None:
        raise RuntimeError("evolution pilot repository revision is invalid")
    return revision


__all__ = ["MemosProviderSession", "create_memos_provider"]
