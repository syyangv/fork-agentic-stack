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
MEMOS_PLUGIN_VERSION = "2.0.14"
MEMOS_PLUGIN_INTEGRITY = (
    "sha512-yEAroCSBfdf7urP47Hyr2MzTg4BPLIWqlno5r0imHb69s8fh7uXZRuPK23IWCzDFIWuPK/SuZfk8u3MdGQOzLg=="
)
MEMOS_PLUGIN_SHASUM = "32639d241918c7da8d536e52eac7e0a7c42c312e"
MEMOS_DISTRIBUTION = "agentic-stack-memos-2.0.14-lexical.1"
MINIMUM_NODE_MAJOR = 20
LOCK_ASSET_DIR = Path(__file__).resolve().parent / "assets" / "memos-2.0.14"
_SAFE_ENV_KEYS = (
    "PATH", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP",
    "SystemRoot", "ComSpec", "PATHEXT",
)


@dataclass(frozen=True)
class VerifiedArtifact:
    path: Path
    integrity: str
    sha1: str
    size: int


@dataclass(frozen=True)
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


def validate_installed_plugin(code_root: str | Path) -> Path:
    """Validate the immutable, pinned installed plugin without running it."""
    plugin_dir = versioned_plugin_dir(code_root)
    package_dir = plugin_dir / "node_modules" / "@memtensor" / "memos-local-plugin"
    _validate_installed_package(
        plugin_dir, package_dir, MEMOS_PLUGIN_SHASUM, MEMOS_PLUGIN_INTEGRITY,
    )
    _validate_tree_immutable(plugin_dir)
    return plugin_dir


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
        _apply_reviewed_lexical_delta(staged_package)
        _prune_model_loader_dependencies(staging, Path(lock_asset_dir))
        (staging / "plugin.tgz").unlink(missing_ok=False)
        marker = {
            "artifact_sha1": artifact.sha1,
            "integrity": artifact.integrity,
            "package": MEMOS_PLUGIN_NAME,
            "version": MEMOS_PLUGIN_VERSION,
            "distribution": MEMOS_DISTRIBUTION,
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
    if not isinstance(package, dict):
        raise RuntimeError("installed MemOS package metadata is missing or invalid")
    if package.get("version") != MEMOS_PLUGIN_VERSION:
        raise RuntimeError(
            f"installed MemOS version mismatch: expected {MEMOS_PLUGIN_VERSION}, "
            f"found {package.get('version')!r}"
        )
    if not (package_dir / "dist" / "bridge.cjs").is_file():
        raise RuntimeError("installed MemOS package is missing dist/bridge.cjs")


_LEXICAL_DELTA_FILES = {
    "core/config/schema.ts": "d4884f1339c00125ae1d7e21b522ee4ce4c4c5920acf3131a9f84b6b2f5995b5",
    "core/config/defaults.ts": "b0992d4dbbeefaf1cb60fe01baa99a9c9eee664d98cd10a7c92aad93d14cff74",
    "core/pipeline/memory-core.ts": "dfe4eb6741686c00ba0433b96da054c658071f87b0bc8f1e83f484a03fcec721",
    "dist/core/config/schema.js": "3ef95230ecfc99b1d9b4b266f6845324f69a63289d0cf89b1b7830aec947af06",
    "dist/core/config/defaults.js": "a0eb9e13f6523a1a09979463405b46af173d68dcb0fc4805ac5cdf406278e2f5",
    "dist/core/pipeline/memory-core.js": "cb811d6b4ebfa0bb395d376a8d743bf780dea1826610209b0f0fa79d5d81428d",
    "core/embedding/embedder.ts": "dfde79b44a4716247f377511b9186a6cba57a3233b8a7fb638494a32fe0755eb",
    "dist/core/embedding/embedder.js": "41d6f1a0af4a5440b79bb9d1081a8b12eacbd20b0ceb9880ab542cefd71c6621",
    "core/embedding/index.ts": "3c8a4618194ab388e943e52cb7251e214ab22fa4ae9dcb650d11822742ef8f4d",
    "dist/core/embedding/index.js": "449d01d449c824d60fd7a612c81cedae12304651e6838e0b8708d204397d495b",
}


def _apply_reviewed_lexical_delta(package_dir: Path) -> None:
    """Apply the deterministic, source-attested lexical-only 2.0.14 delta.

    The npm tarball remains the pristine upstream input.  The installed tree
    is deliberately identified by ``MEMOS_DISTRIBUTION`` and its exact file
    manifest; it must never be described as the pristine upstream package.
    """
    for relative, expected in _LEXICAL_DELTA_FILES.items():
        path = package_dir / relative
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise RuntimeError(f"MemOS lexical delta input is missing: {relative}") from exc
        if not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), expected):
            raise RuntimeError(f"MemOS lexical delta input mismatch: {relative}")
        text = payload.decode("utf-8")
        if relative.endswith("schema.ts"):
            old, new = (
                "const EmbeddingSchema = Type.Object({\n  provider:",
                "const EmbeddingSchema = Type.Object({\n  /** Disable all embedding provider construction and model loading. */\n  enabled: Bool(true),\n  /** Non-model retrieval is represented explicitly for managed profiles. */\n  engine: Type.Optional(Type.Literal(\"sqlite_fts5\")),\n  provider:",
            )
        elif relative.endswith("schema.js"):
            old, new = (
                "const EmbeddingSchema = Type.Object({\n    provider:",
                "const EmbeddingSchema = Type.Object({\n    /** Disable all embedding provider construction and model loading. */\n    enabled: Bool(true),\n    /** Non-model retrieval is represented explicitly for managed profiles. */\n    engine: Type.Optional(Type.Literal(\"sqlite_fts5\")),\n    provider:",
            )
        elif relative.endswith("defaults.ts"):
            old, new = "  embedding: {\n    provider:", "  embedding: {\n    enabled: true,\n    provider:"
        elif relative.endswith("defaults.js"):
            old, new = "    embedding: {\n        provider:", "    embedding: {\n        enabled: true,\n        provider:"
        elif relative.endswith("embedding/index.ts"):
            old = 'export { LocalEmbeddingProvider, __resetLocalExtractorForTests } from "./providers/local.js";'
            new = "// Local embedding exports removed by the reviewed lexical distribution."
        elif relative.endswith("embedding/index.js"):
            old = 'export { LocalEmbeddingProvider, __resetLocalExtractorForTests } from "./providers/local.js";'
            new = "// Local embedding exports removed by the reviewed lexical distribution."
        elif relative.endswith("embedder.ts"):
            old = 'import { LocalEmbeddingProvider } from "./providers/local.js";'
            new = "// Local model loading is removed by the reviewed lexical distribution."
        elif relative.endswith("embedding/embedder.js"):
            old = 'import { LocalEmbeddingProvider } from "./providers/local.js";'
            new = "// Local model loading is removed by the reviewed lexical distribution."
        elif relative.endswith("memory-core.ts"):
            old, new = "  try {\n    embedder = createEmbedder({", "  try {\n    if (config.embedding.enabled === false) {\n      log.info(\"embedder.disabled\", { retrieval: \"lexical_fts\" });\n    } else embedder = createEmbedder({"
        else:
            old, new = "    try {\n        embedder = createEmbedder({", "    try {\n        if (config.embedding.enabled === false) {\n            log.info(\"embedder.disabled\", { retrieval: \"lexical_fts\" });\n        }\n        else embedder = createEmbedder({"
        if text.count(old) != 1:
            raise RuntimeError(f"MemOS lexical delta anchor mismatch: {relative}")
        text = text.replace(old, new)
        if relative.endswith(("schema.ts", "schema.js")):
            anchor = 'Type.Literal("local"),'
            if text.count(anchor) != 1:
                raise RuntimeError(f"MemOS lexical provider anchor mismatch: {relative}")
            text = text.replace(anchor, 'Type.Literal("lexical"),\n        ' + anchor)
        if relative.endswith(("embedder.ts", "embedding/embedder.js")):
            local_case = 'case "local":\n      return new LocalEmbeddingProvider();' if relative.endswith(".ts") else 'case "local":\n            return new LocalEmbeddingProvider();'
            replacement = 'case "local":\n      throw new MemosError(ERROR_CODES.UNSUPPORTED, "Local embedding is removed in the lexical distribution");' if relative.endswith(".ts") else 'case "local":\n            throw new MemosError(ERROR_CODES.UNSUPPORTED, "Local embedding is removed in the lexical distribution");'
            if text.count(local_case) != 1:
                raise RuntimeError(f"MemOS local loader anchor mismatch: {relative}")
            text = text.replace(local_case, replacement)
        path.write_text(text, encoding="utf-8")

    package_json = package_dir / "package.json"
    package = json.loads(package_json.read_text(encoding="utf-8"))
    dependencies = package.get("dependencies")
    if not isinstance(dependencies, dict) or dependencies.pop("@huggingface/transformers", None) is None:
        raise RuntimeError("MemOS lexical delta package dependency anchor mismatch")
    package["agenticStackDistribution"] = MEMOS_DISTRIBUTION
    package_json.write_text(json.dumps(package, indent=2) + "\n", encoding="utf-8")

    # Remove both source and built local loaders. Static imports were removed
    # above, so retaining these files would only preserve an unapproved loader.
    for relative in ("core/embedding/providers/local.ts", "dist/core/embedding/providers/local.js", "dist/core/embedding/providers/local.d.ts"):
        path = package_dir / relative
        if not path.is_file():
            raise RuntimeError(f"MemOS lexical loader input is missing: {relative}")
        path.unlink()


def _prune_model_loader_dependencies(staging: Path, lock_asset_dir: Path) -> None:
    """Make the final installed dependency tree match the lexical lock."""
    lexical_lock_path = lock_asset_dir / "package-lock.lexical.json"
    try:
        lexical = json.loads(lexical_lock_path.read_text(encoding="utf-8"))
        original = json.loads((staging / "package-lock.json").read_text(encoding="utf-8"))
        keep = set(lexical["packages"])
        installed = set(original["packages"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("MemOS lexical dependency lock is invalid") from exc
    for key in sorted(installed - keep, key=lambda value: value.count("/"), reverse=True):
        if not key.startswith("node_modules/"):
            continue
        path = staging / key
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path)
    # npm's hidden lock describes the pre-prune tree and is not authoritative
    # for this immutable distribution. The reviewed top-level lexical lock is.
    (staging / "node_modules" / ".package-lock.json").unlink(missing_ok=True)
    for directory, directories, files in os.walk(staging / "node_modules", topdown=False):
        path = Path(directory)
        if path != staging / "node_modules" and not any(path.iterdir()):
            path.rmdir()
    shutil.copy2(lexical_lock_path, staging / "package-lock.json")


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
        raise RuntimeError("existing MemOS code directory metadata is incomplete or invalid") from exc
    if not isinstance(marker, dict):
        raise RuntimeError("existing MemOS code directory metadata is incomplete or invalid")
    if marker.get("artifact_sha1") != artifact_sha1:
        raise RuntimeError("existing MemOS code directory came from a different artifact")
    if marker.get("version") != MEMOS_PLUGIN_VERSION:
        raise RuntimeError("existing MemOS code directory has an invalid version marker")
    if marker.get("distribution") != MEMOS_DISTRIBUTION:
        raise RuntimeError("existing MemOS code directory lacks the reviewed lexical delta")
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
