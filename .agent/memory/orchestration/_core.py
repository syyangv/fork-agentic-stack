"""Standard-library primitives shared by orchestration modules."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any


REDACTED = "[REDACTED]"
_SENSITIVE_KEYS = re.compile(
    r"^(api[_-]?key|access[_-]?token|auth(?:orization)?|bearer|password|passwd|secret|private[_-]?key)$",
    re.IGNORECASE,
)
_SECRET_VALUES = (
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[opusr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b", re.IGNORECASE),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
_CREDENTIAL_PATHS = (
    re.compile(r"(?:~|/[^\s]+)?/\.aws/credentials\b", re.IGNORECASE),
    re.compile(r"(?:~|/[^\s]+)?/\.config/gcloud/application_default_credentials\.json\b", re.IGNORECASE),
    re.compile(r"(?:^|[\\/])\.env(?:\.[A-Za-z0-9_-]+)?\b", re.IGNORECASE),
    re.compile(r"(?:^|[\\/])(?:credentials|secrets|tokens?)(?:\.[A-Za-z0-9_-]+)?\b", re.IGNORECASE),
)


def deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): deep_freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(deep_freeze(item) for item in value)
    return value


def thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        thaw(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def redact(value: Any, key: str | None = None) -> Any:
    """Return a recursively redacted copy suitable for contract creation."""
    if key and _SENSITIVE_KEYS.match(key):
        return REDACTED
    if isinstance(value, Mapping):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if not isinstance(value, str):
        return value
    result = value
    for pattern in _SECRET_VALUES + _CREDENTIAL_PATHS:
        result = pattern.sub(REDACTED, result)
    return result


def contains_sensitive_plaintext(value: Any, key: str | None = None) -> bool:
    if key and _SENSITIVE_KEYS.match(key) and value != REDACTED:
        return True
    if isinstance(value, Mapping):
        return any(contains_sensitive_plaintext(v, str(k)) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(contains_sensitive_plaintext(item) for item in value)
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in _SECRET_VALUES + _CREDENTIAL_PATHS)
    return False


class SchemaValidationError(ValueError):
    pass


_SCHEMA_DIR = Path(__file__).resolve().parents[2] / "protocols" / "tool_schemas" / "memory"


def validate_schema(instance: Any, schema_name: str) -> None:
    """Validate against the checked-in, dependency-free JSON Schema subset.

    Phase 1 schemas intentionally use only the subset implemented here. This
    keeps project installs standard-library-only while still making the schema
    documents the executable external-boundary contract.
    """
    schema = json.loads((_SCHEMA_DIR / schema_name).read_text(encoding="utf-8"))
    _validate(instance, schema, "$")


def _validate(value: Any, schema: Mapping[str, Any], path: str) -> None:
    expected = schema.get("type")
    if isinstance(expected, list):
        if value is None and "null" in expected:
            return
        candidates = [kind for kind in expected if kind != "null"]
        if not any(_is_type(value, kind) for kind in candidates):
            raise SchemaValidationError(f"{path}: expected {' or '.join(expected)}")
    elif expected and not _is_type(value, expected):
        raise SchemaValidationError(f"{path}: expected {expected}")

    if "const" in schema and value != schema["const"]:
        raise SchemaValidationError(f"{path}: expected {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaValidationError(f"{path}: unsupported value {value!r}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise SchemaValidationError(f"{path}: string below minLength")
        if len(value) > schema.get("maxLength", len(value)):
            raise SchemaValidationError(f"{path}: string exceeds maxLength")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise SchemaValidationError(f"{path}: value does not match required pattern")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema.get("minimum", value):
            raise SchemaValidationError(f"{path}: value below minimum")
        if value > schema.get("maximum", value):
            raise SchemaValidationError(f"{path}: value above maximum")
    if isinstance(value, Mapping):
        required = schema.get("required", [])
        missing = [name for name in required if name not in value]
        if missing:
            raise SchemaValidationError(f"{path}: missing fields {', '.join(missing)}")
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties")
        if additional is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                raise SchemaValidationError(f"{path}: unknown fields {', '.join(unknown)}")
        for name, child in value.items():
            if name in properties:
                _validate(child, properties[name], f"{path}.{name}")
            elif isinstance(additional, Mapping):
                _validate(child, additional, f"{path}.{name}")
    if isinstance(value, (list, tuple)) and "items" in schema:
        for index, child in enumerate(value):
            _validate(child, schema["items"], f"{path}[{index}]")


def _is_type(value: Any, kind: str) -> bool:
    return {
        "object": isinstance(value, Mapping),
        "array": isinstance(value, (list, tuple)),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(kind, False)
