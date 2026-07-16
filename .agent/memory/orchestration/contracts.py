"""Immutable v1 contracts for federated memory orchestration."""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ._core import (
    SchemaValidationError,
    canonical_json,
    contains_sensitive_plaintext,
    deep_freeze,
    redact,
    thaw,
    validate_schema,
)


class ContractError(ValueError):
    pass


def _external(data: Mapping[str, Any], schema: str) -> dict[str, Any]:
    plain = thaw(data)
    try:
        validate_schema(plain, schema)
    except (SchemaValidationError, OSError, ValueError) as exc:
        raise ContractError(str(exc)) from exc
    if contains_sensitive_plaintext(plain):
        raise ContractError("external contract contains plaintext secret or credential path")
    return plain


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    schema: str
    event_id: str
    idempotency_key: str
    timestamp: str
    event_type: str
    project_id: str
    repo_root: str
    revision: str | None
    harness: str
    run_id: str
    session_id: str
    actor: str
    intent: str
    payload: Mapping[str, Any] = field(repr=False)
    privacy: str = "internal"
    code_refs: tuple[Any, ...] = ()
    parent_event_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", deep_freeze(self.payload))
        object.__setattr__(self, "code_refs", deep_freeze(self.code_refs))
        object.__setattr__(self, "parent_event_ids", tuple(self.parent_event_ids))
        if contains_sensitive_plaintext(self.to_dict()):
            raise ContractError("event contains plaintext secret or credential path")
        for path, value in _walk_strings(self.payload):
            if len(value) > 2_000:
                raise ContractError(f"payload string at {path} exceeds 2,000 characters")
        if len(canonical_json(self.payload).encode("utf-8")) > 16 * 1024:
            raise ContractError("payload exceeds 16 KiB")

    @classmethod
    def create(cls, **values: Any) -> "EventEnvelope":
        values = dict(values)
        values.setdefault("schema", "agentic.memory.event.v1")
        values.setdefault("privacy", "internal")
        values.setdefault("code_refs", ())
        values.setdefault("parent_event_ids", ())
        original = {
            "intent": values.get("intent", ""),
            "payload": values.get("payload", {}),
            "code_refs": values.get("code_refs", ()),
        }
        for name, raw in original.items():
            values[name] = redact(raw)
        if canonical_json(original) != canonical_json(
            {name: values[name] for name in original}
        ):
            values["privacy"] = "sensitive-redacted"
        content = {name: values[name] for name in _EVENT_FIELDS if name != "event_id"}
        values["event_id"] = "evt_" + hashlib.sha256(
            canonical_json(content).encode("utf-8")
        ).hexdigest()
        event = cls(**values)
        event._validate_id()
        validate_schema(event.to_dict(), "event-envelope-v1.schema.json")
        return event

    @classmethod
    def from_external(cls, data: Mapping[str, Any]) -> "EventEnvelope":
        event = cls(**_external(data, "event-envelope-v1.schema.json"))
        event._validate_id()
        return event

    def _validate_id(self) -> None:
        content = {name: value for name, value in self.to_dict().items() if name != "event_id"}
        expected = "evt_" + hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()
        if self.event_id != expected:
            raise ContractError("event_id does not match canonical event content")

    def to_dict(self) -> dict[str, Any]:
        return {name: thaw(getattr(self, name)) for name in _EVENT_FIELDS}

    def canonical_json(self) -> str:
        return canonical_json(self.to_dict())


_EVENT_FIELDS = tuple(EventEnvelope.__dataclass_fields__)


@dataclass(frozen=True, slots=True)
class ProvenanceRef:
    kind: str
    provider: str
    source_id: str
    project_id: str
    repository_revision: str | None
    source_hash: str
    observed_at: str
    confidence: float
    freshness: str
    locator: Mapping[str, Any] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "locator", deep_freeze(self.locator))

    @classmethod
    def from_external(cls, data: Mapping[str, Any]) -> "ProvenanceRef":
        return cls(**_external(data, "provenance-ref-v1.schema.json"))

    def to_dict(self) -> dict[str, Any]:
        return {name: thaw(getattr(self, name)) for name in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class RetrievalItem:
    item_id: str
    lane: str
    type: str
    summary: str
    scope: Mapping[str, Any]
    status: str
    provider_score: float
    selection_reason: str
    provenance: tuple[Mapping[str, Any], ...]
    token_estimate: int
    expires_at: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", deep_freeze(self.scope))
        object.__setattr__(self, "provenance", deep_freeze(self.provenance))

    @classmethod
    def from_external(cls, data: Mapping[str, Any]) -> "RetrievalItem":
        plain = _external(data, "retrieval-item-v1.schema.json")
        plain["provenance"] = tuple(
            ProvenanceRef.from_external(item).to_dict() for item in plain["provenance"]
        )
        return cls(**plain)

    def to_dict(self) -> dict[str, Any]:
        return {name: thaw(getattr(self, name)) for name in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class ContextPacket:
    schema: str
    intent: str
    project_id: str
    routing: Mapping[str, bool]
    sections: tuple[Mapping[str, Any], ...]
    warnings: tuple[str, ...]
    health: Mapping[str, Any]
    token_estimate: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "routing", deep_freeze(self.routing))
        object.__setattr__(self, "sections", deep_freeze(self.sections))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "health", deep_freeze(self.health))

    @classmethod
    def from_external(cls, data: Mapping[str, Any]) -> "ContextPacket":
        plain = _external(data, "context-packet-v1.schema.json")
        sections = []
        for section in plain["sections"]:
            sections.append(
                {
                    "lane": section["lane"],
                    "items": [
                        RetrievalItem.from_external(item).to_dict()
                        for item in section["items"]
                    ],
                }
            )
        plain["sections"] = sections
        return cls(**plain)

    def to_dict(self) -> dict[str, Any]:
        return {name: thaw(getattr(self, name)) for name in self.__dataclass_fields__}


class IdempotencyRegistry:
    """Process-local replay guard; persistent delivery arrives in Phase 3."""

    def __init__(self) -> None:
        self._keys: set[str] = set()

    def accept(self, event: EventEnvelope) -> bool:
        if event.idempotency_key in self._keys:
            return False
        self._keys.add(event.idempotency_key)
        return True

    def __len__(self) -> int:
        return len(self._keys)


def _walk_strings(value: Any, path: str = "payload"):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, Mapping):
        for key, child in value.items():
            yield from _walk_strings(child, f"{path}.{key}")
    elif isinstance(value, tuple):
        for index, child in enumerate(value):
            yield from _walk_strings(child, f"{path}[{index}]")
