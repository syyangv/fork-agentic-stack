"""Pure ownership-attestation decisions for the loopback review service.

The observations are injected by a later bounded lifecycle layer.  Gate 11
never discovers processes, manipulates a PID, or executes a service.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath
import re
from typing import Mapping, Optional, Tuple

from .local_schedule_config import LocalScheduleConfig


_ATTESTATION_SCHEMA = "agentic.memory.review-service.v1"
_LOOPBACK = "127.0.0.1"


@dataclass(frozen=True)
class ReviewServiceExpectation:
    project_id: str
    entrypoint: str
    port: int
    command: Tuple[str, ...]

    def __post_init__(self) -> None:
        entrypoint = PurePath(self.entrypoint)
        if (
            re.fullmatch(r"[0-9a-f]{16}", self.project_id) is None
            or "\x00" in self.entrypoint
            or not entrypoint.is_absolute()
            or ".." in entrypoint.parts
            or not 1024 <= self.port <= 65535
        ):
            raise ValueError("review service expectation is invalid")
        if not self.command or any(not isinstance(part, str) or not part for part in self.command):
            raise ValueError("review service command is invalid")


@dataclass(frozen=True)
class ReviewServiceObservation:
    host: str
    port: int
    pid: int
    healthy: bool
    attestation: Mapping[str, object]
    command: Tuple[str, ...]


@dataclass(frozen=True)
class ReviewServiceOutcome:
    status: str
    authority: str = "no_auto_accept"
    action: str = ""


def expectation_from_config(
    config: LocalScheduleConfig, project_id: str, entrypoint: str, command: Tuple[str, ...],
) -> ReviewServiceExpectation:
    """Bind the local loopback port to a versioned, non-executable expectation."""
    if config.review_server_host != _LOOPBACK:
        raise ValueError("review service configuration must be loopback-only")
    if (
        command.count(entrypoint) != 1
        or len(command) < 2
        or command[1] != entrypoint
    ):
        raise ValueError("review service command must include the exact versioned entrypoint")
    if command.count("--host") != 1 or command.count("--port") != 1:
        raise ValueError("review service command must contain one host and port")
    try:
        host_index = command.index("--host")
        port_index = command.index("--port")
    except ValueError as exc:
        raise ValueError("review service command must bind host and port") from exc
    if (
        host_index + 1 >= len(command) or port_index + 1 >= len(command)
        or command[host_index + 1] != _LOOPBACK
        or command[port_index + 1] != str(config.review_server_port)
    ):
        raise ValueError("review service command does not match configured loopback port")
    return ReviewServiceExpectation(project_id, entrypoint, config.review_server_port, command)


def assess_review_service(
    expected: ReviewServiceExpectation, observation: Optional[ReviewServiceObservation],
) -> ReviewServiceOutcome:
    """Reuse only an exact healthy project-bound service; never manage a process.

    An observation represents an already-known listener.  Every mismatch is a
    conflict because an unrelated occupant must remain untouched.  A missing
    observation is merely unavailable for a later launchd-owned lifecycle.
    """
    if observation is None:
        return ReviewServiceOutcome("unavailable", action="service_unavailable")
    expected_attestation = {
        "schema": _ATTESTATION_SCHEMA,
        "project_id": expected.project_id,
        "entrypoint": expected.entrypoint,
        "host": _LOOPBACK,
        "port": expected.port,
        "pid": observation.pid,
    }
    if (
        observation.host == _LOOPBACK
        and observation.port == expected.port
        and type(observation.pid) is int
        and observation.pid > 0
        and observation.healthy is True
        and dict(observation.attestation) == expected_attestation
        and observation.command == expected.command
    ):
        return ReviewServiceOutcome("reused", action="reuse_attested_service")
    return ReviewServiceOutcome("port_conflict", action="resolve_port_conflict_without_process_interaction")
