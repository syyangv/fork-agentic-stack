"""Stable project identity and explicit alias resolution."""
from __future__ import annotations

import hashlib
import ntpath
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit


_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
_SCP_REMOTE = re.compile(r"^(?:[^@/]+@)?([^:/]+):(.+)$")


@dataclass(frozen=True, slots=True)
class ProjectIdentity:
    project_id: str
    canonical_source: str
    repo_root: str
    remote: str | None


def derive_project_identity(repo_root: str | Path, remote: str | None = None) -> ProjectIdentity:
    root = canonicalize_repo_root(str(repo_root))
    source = canonicalize_remote(remote) if remote else root
    project_id = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
    return ProjectIdentity(project_id, source, root, remote)


def canonicalize_repo_root(repo_root: str) -> str:
    expanded = str(Path(repo_root).expanduser())
    if _WINDOWS_ABSOLUTE.match(repo_root):
        return ntpath.normcase(ntpath.normpath(repo_root)).replace("\\", "/")
    return str(Path(expanded).resolve(strict=False))


def canonicalize_remote(remote: str) -> str:
    value = remote.strip()
    match = _SCP_REMOTE.match(value) if "://" not in value else None
    if match:
        host, path = match.groups()
    else:
        parsed = urlsplit(value)
        if not parsed.hostname:
            raise ValueError(f"unsupported git remote: {remote!r}")
        host, path = parsed.hostname, parsed.path
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not path:
        raise ValueError(f"git remote has no repository path: {remote!r}")
    normalized_host = host.lower()
    if normalized_host in {"github.com", "www.github.com"}:
        normalized_host = "github.com"
        path = path.lower()
    return f"{normalized_host}/{path}"


class ProjectIdentityResolver:
    def __init__(self, aliases: dict[str, str] | None = None) -> None:
        resolved = dict(aliases or {})
        invalid = [name for name, value in resolved.items() if not re.fullmatch(r"[0-9a-f]{16}", value)]
        if invalid:
            raise ValueError(f"aliases must target 16-character project IDs: {', '.join(invalid)}")
        self.aliases = MappingProxyType(resolved)

    def resolve(self, project_or_alias: str) -> str:
        if re.fullmatch(r"[0-9a-f]{16}", project_or_alias):
            return project_or_alias
        try:
            return self.aliases[project_or_alias]
        except KeyError as exc:
            raise KeyError(f"unknown project identity or alias: {project_or_alias}") from exc
