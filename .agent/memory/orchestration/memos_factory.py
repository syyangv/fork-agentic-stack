"""Construction boundary for the isolated Phase 3 MemOS provider."""
from __future__ import annotations

import os
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path

from .memos_bridge import BridgeConfig, MemOSBridgeClient
from .memos_journal import MemosDeliveryJournal
from .memos_runtime import (
    bridge_command,
    prepare_project_runtime,
    runtime_environment,
)
from .providers.memos_local import MemosLocalProvider


@dataclass(slots=True)
class MemosProviderSession:
    provider: MemosLocalProvider
    client: MemOSBridgeClient | None
    _worker: AbstractContextManager | None = field(default=None, init=False)
    _entered: bool = field(default=False, init=False)
    assist_deadline: float | None = None

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
        self.provider._assist_deadline = self.assist_deadline
        self._entered = True
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
    assist_deadline: float | None = None,
) -> MemosProviderSession:
    """Provision private state and attach a client only to a pinned install."""
    agent_root = Path(agent_root).expanduser().resolve(strict=False)
    code_root = Path(code_root or agent_root / "runtime" / "providers")
    data_root = Path(data_root or agent_root / "runtime" / "memos")
    paths = prepare_project_runtime(code_root, data_root, project_id)
    initialize_timeout = None
    if mode == "assist" and assist_deadline is not None:
        initialize_timeout = max(0.0, assist_deadline - time.monotonic())
    journal = MemosDeliveryJournal(
        paths.project_root / "delivery.sqlite3",
        initialize_timeout=initialize_timeout,
    )
    bridge = paths.plugin_dir / "node_modules/@memtensor/memos-local-plugin/dist/bridge.cjs"
    client = None
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
        ))
    provider = MemosLocalProvider(
        project_id=project_id, journal=journal, client=client, mode=mode,
    )
    return MemosProviderSession(
        provider=provider, client=client, assist_deadline=assist_deadline,
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


__all__ = ["MemosProviderSession", "create_memos_provider"]
