"""Verified, offline installer for the pinned MemOS local plugin artifact."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


MEMOS_PLUGIN_NAME = "@memtensor/memos-local-plugin"
MEMOS_PLUGIN_VERSION = "2.0.10"
MEMOS_PLUGIN_INTEGRITY = (
    "sha512-Rg2NIjGAObTC3zFQ4wOzB+hxR7qHvHWMVI5Nxc+7QEi5wpBUibkniz3SdHOPrbbCkqhatS0DjZ+aUexl/9Q+EA=="
)
MEMOS_PLUGIN_SHASUM = "d75850ce7340d56b8a255831969950b9fbf96995"
MINIMUM_NODE_MAJOR = 20
LOCK_ASSET_DIR = Path(__file__).resolve().parent / "assets" / "memos-2.0.10"
_SAFE_ENV_KEYS = (
    "PATH", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP",
    "SystemRoot", "ComSpec", "PATHEXT",
)


@dataclass(frozen=True, slots=True)
class VerifiedArtifact:
    path: Path
    integrity: str
    sha1: str
    size: int


@dataclass(frozen=True, slots=True)
class MemosInstallResult:
    plugin_dir: Path
    package_dir: Path
    version: str
    artifact_sha1: str
    already_installed: bool


def versioned_plugin_dir(code_root: str | Path) -> Path:
    return (
        Path(code_root).expanduser().resolve(strict=False)
        / "memos-local-plugin"
        / MEMOS_PLUGIN_VERSION
    )


def verify_tarball(
    tarball: str | Path,
    *,
    integrity: str = MEMOS_PLUGIN_INTEGRITY,
    shasum: str = MEMOS_PLUGIN_SHASUM,
) -> VerifiedArtifact:
    """Verify both npm's SHA-512 SRI value and legacy SHA-1 shasum."""
    path = Path(tarball).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError(f"MemOS artifact is not a regular file: {path}")
    if not integrity.startswith("sha512-"):
        raise ValueError("MemOS artifact integrity must use sha512 SRI")
    try:
        expected_sha512 = base64.b64decode(integrity[7:], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid MemOS sha512 integrity value") from exc
    if len(expected_sha512) != hashlib.sha512().digest_size:
        raise ValueError("invalid MemOS sha512 integrity digest length")
    if re.fullmatch(r"[0-9a-f]{40}", shasum) is None:
        raise ValueError("invalid MemOS SHA-1 shasum")

    sha512 = hashlib.sha512()
    sha1 = hashlib.sha1()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            sha512.update(chunk)
            sha1.update(chunk)
    actual_sha1 = sha1.hexdigest()
    if not hmac.compare_digest(sha512.digest(), expected_sha512):
        raise ValueError("MemOS artifact SHA-512 integrity mismatch")
    if not hmac.compare_digest(actual_sha1, shasum):
        raise ValueError("MemOS artifact SHA-1 shasum mismatch")
    return VerifiedArtifact(path, integrity, actual_sha1, size)


def require_node_20(version: str) -> int:
    match = re.fullmatch(r"v?(\d+)(?:\.\d+){0,2}(?:[-+].*)?", version.strip())
    if match is None:
        raise RuntimeError(f"unable to parse Node.js version: {version!r}")
    major = int(match.group(1))
    if major < MINIMUM_NODE_MAJOR:
        raise RuntimeError(
            f"MemOS {MEMOS_PLUGIN_VERSION} requires Node.js >=20; found {version.strip()}"
        )
    return major


def install_verified_tarball(
    tarball: str | Path,
    code_root: str | Path,
    *,
    integrity: str = MEMOS_PLUGIN_INTEGRITY,
    shasum: str = MEMOS_PLUGIN_SHASUM,
    node_version: str | None = None,
    npm_command: Sequence[str] = ("npm",),
    runner: Callable[..., object] = subprocess.run,
    lock_asset_dir: str | Path = LOCK_ASSET_DIR,
) -> MemosInstallResult:
    """Install one local artifact into an immutable versioned prefix.

    The npm command is injectable for testing and managed environments. The
    root artifact is always the verified local tarball; a committed lockfile
    pins its full npm dependency graph. npm may still need approved registry
    access to fetch the exact integrity-pinned dependency tarballs.
    """
    artifact = verify_tarball(tarball, integrity=integrity, shasum=shasum)
    if not npm_command or any(not isinstance(part, str) or not part for part in npm_command):
        raise ValueError("npm_command must be a non-empty sequence of command arguments")
    if node_version is None:
        result = runner(
            ("node", "--version"),
            check=True,
            capture_output=True,
            text=True,
            env=_minimal_environment(),
        )
        node_version = str(getattr(result, "stdout", "")).strip()
    require_node_20(node_version)

    plugin_dir = versioned_plugin_dir(code_root)
    package_rel = Path("node_modules") / "@memtensor" / "memos-local-plugin"
    package_dir = plugin_dir / package_rel
    if plugin_dir.exists():
        _validate_installed_package(
            plugin_dir, package_dir, artifact.sha1, artifact.integrity,
        )
        _validate_tree_immutable(plugin_dir)
        return MemosInstallResult(
            plugin_dir, package_dir, MEMOS_PLUGIN_VERSION, artifact.sha1, True
        )

    version_parent = plugin_dir.parent
    version_parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{MEMOS_PLUGIN_VERSION}.", dir=version_parent))
    try:
        _prepare_locked_install(staging, artifact, Path(lock_asset_dir))
        command = (
            *npm_command,
            "ci",
            "--prefix",
            str(staging),
            "--omit=dev",
            "--no-audit",
            "--no-fund",
        )
        with tempfile.TemporaryDirectory(
            prefix=".memos-npm-home.", dir=version_parent,
        ) as install_home:
            environment = _minimal_environment(Path(install_home))
            runner(
                command, check=True, capture_output=True, text=True,
                env=environment,
            )
        staged_package = staging / package_rel
        _validate_package_version(staged_package)
        (staging / "plugin.tgz").unlink(missing_ok=False)
        marker = {
            "artifact_sha1": artifact.sha1,
            "integrity": artifact.integrity,
            "package": MEMOS_PLUGIN_NAME,
            "version": MEMOS_PLUGIN_VERSION,
        }
        file_manifest = _build_file_manifest(staging)
        manifest_path = staging / ".agentic-stack-files.json"
        manifest_path.write_text(
            json.dumps(file_manifest, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        marker["files_manifest_sha256"] = hashlib.sha256(
            manifest_path.read_bytes()
        ).hexdigest()
        marker_path = staging / ".agentic-stack-install.json"
        marker_path.write_text(
            json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        try:
            os.rename(staging, plugin_dir)
        except FileExistsError:
            # A concurrent installer won.  Trust it only after full validation.
            _validate_installed_package(
                plugin_dir, package_dir, artifact.sha1, artifact.integrity,
            )
            _remove_staging(staging, version_parent)
            return MemosInstallResult(
                plugin_dir, package_dir, MEMOS_PLUGIN_VERSION, artifact.sha1, True
            )
        # Freeze only after the atomic directory publication.  Some platforms
        # refuse to rename a source directory once its own write bit is gone.
        _make_tree_immutable(plugin_dir)
        _validate_tree_immutable(plugin_dir)
    except BaseException:
        _remove_staging(staging, version_parent)
        raise

    return MemosInstallResult(
        plugin_dir, package_dir, MEMOS_PLUGIN_VERSION, artifact.sha1, False
    )


def _validate_package_version(package_dir: Path) -> None:
    try:
        package = json.loads((package_dir / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("installed MemOS package metadata is missing or invalid") from exc
    if package.get("version") != MEMOS_PLUGIN_VERSION:
        raise RuntimeError(
            f"installed MemOS version mismatch: expected {MEMOS_PLUGIN_VERSION}, "
            f"found {package.get('version')!r}"
        )
    if not (package_dir / "dist" / "bridge.cjs").is_file():
        raise RuntimeError("installed MemOS package is missing dist/bridge.cjs")


def _prepare_locked_install(
    staging: Path, artifact: VerifiedArtifact, lock_asset_dir: Path,
) -> None:
    package_path = lock_asset_dir / "package.json"
    lock_path = lock_asset_dir / "package-lock.json"
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        locked_plugin = lock["packages"]["node_modules/@memtensor/memos-local-plugin"]
    except (OSError, KeyError, json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("MemOS dependency lock assets are missing or invalid") from exc
    expected_spec = "file:plugin.tgz"
    if package.get("dependencies", {}).get(MEMOS_PLUGIN_NAME) != expected_spec:
        raise RuntimeError("MemOS package asset does not reference the local tarball")
    if (
        locked_plugin.get("version") != MEMOS_PLUGIN_VERSION
        or locked_plugin.get("resolved") != expected_spec
        or locked_plugin.get("integrity") != artifact.integrity
    ):
        raise RuntimeError("MemOS dependency lock does not match the verified artifact")
    shutil.copy2(package_path, staging / "package.json")
    shutil.copy2(lock_path, staging / "package-lock.json")
    shutil.copy2(artifact.path, staging / "plugin.tgz")


def _minimal_environment(home: Path | None = None) -> dict[str, str]:
    environment = {
        key: value for key in _SAFE_ENV_KEYS
        if (value := os.environ.get(key)) is not None
    }
    if "PATH" not in environment:
        environment["PATH"] = os.defpath
    if home is not None:
        environment["HOME"] = str(home)
        environment["USERPROFILE"] = str(home)
        environment["npm_config_cache"] = str(home / "npm-cache")
    return environment


def _validate_installed_package(
    plugin_dir: Path, package_dir: Path, artifact_sha1: str, integrity: str,
) -> None:
    _validate_package_version(package_dir)
    try:
        marker = json.loads(
            (plugin_dir / ".agentic-stack-install.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"existing MemOS code directory is incomplete: {plugin_dir}") from exc
    if marker.get("artifact_sha1") != artifact_sha1:
        raise RuntimeError("existing MemOS code directory came from a different artifact")
    if marker.get("version") != MEMOS_PLUGIN_VERSION:
        raise RuntimeError("existing MemOS code directory has an invalid version marker")
    if marker.get("integrity") != integrity or marker.get("package") != MEMOS_PLUGIN_NAME:
        raise RuntimeError("existing MemOS code directory has invalid artifact metadata")
    manifest_path = plugin_dir / ".agentic-stack-files.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("existing MemOS code inventory is missing or invalid") from exc
    digest = marker.get("files_manifest_sha256")
    if (not isinstance(digest, str)
            or not hmac.compare_digest(digest, hashlib.sha256(manifest_bytes).hexdigest())
            or manifest != _build_file_manifest(plugin_dir)):
        raise RuntimeError("existing MemOS code inventory mismatch")


def _build_file_manifest(root: Path) -> dict[str, dict[str, object]]:
    entries: dict[str, dict[str, object]] = {}
    for directory, directories, files in os.walk(root):
        directories.sort()
        files.sort()
        for name in files:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if relative in {".agentic-stack-files.json", ".agentic-stack-install.json"}:
                continue
            if path.is_symlink():
                entries[relative] = {"type": "symlink", "target": os.readlink(path)}
            elif path.is_file():
                entries[relative] = {
                    "type": "file", "size": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            else:
                raise RuntimeError("MemOS code inventory contains a non-regular file")
    return entries


def _make_tree_immutable(root: Path) -> None:
    resolved_root = root.resolve()
    for directory, directories, files in os.walk(root, topdown=False):
        for name in files:
            path = Path(directory) / name
            if path.is_symlink():
                _validate_internal_symlink(path, resolved_root)
            else:
                os.chmod(path, 0o444)
        for name in directories:
            path = Path(directory) / name
            if path.is_symlink():
                _validate_internal_symlink(path, resolved_root)
            else:
                os.chmod(path, 0o555)
    os.chmod(root, 0o555)


def _validate_tree_immutable(root: Path) -> None:
    resolved_root = root.resolve()
    for directory, directories, files in os.walk(root):
        for name in (*directories, *files):
            path = Path(directory) / name
            if path.is_symlink():
                _validate_internal_symlink(path, resolved_root)
            elif path.stat().st_mode & 0o222:
                raise RuntimeError(f"MemOS code tree contains a writable path: {path}")
    if root.stat().st_mode & 0o222:
        raise RuntimeError(f"MemOS code directory is writable: {root}")


def _validate_internal_symlink(path: Path, resolved_root: Path) -> None:
    try:
        target = path.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"MemOS code tree contains a broken symlink: {path}") from exc
    if not target.is_relative_to(resolved_root):
        raise RuntimeError(f"MemOS code tree symlink escapes its immutable root: {path}")


def _remove_staging(staging: Path, expected_parent: Path) -> None:
    """Remove only the private temporary directory allocated above."""
    if not staging.exists():
        return
    if staging.parent != expected_parent or not staging.name.startswith(f".{MEMOS_PLUGIN_VERSION}."):
        raise RuntimeError(f"refusing to remove unexpected staging path: {staging}")
    for directory, directories, files in os.walk(staging):
        for name in directories:
            path = Path(directory) / name
            if path.is_symlink():
                continue
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass
        for name in files:
            path = Path(directory) / name
            if path.is_symlink():
                continue
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
    try:
        os.chmod(staging, 0o700)
    except OSError:
        pass
    shutil.rmtree(staging)
