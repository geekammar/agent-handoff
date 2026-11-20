from __future__ import annotations

import fnmatch
import json
import os
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

MANAGED_TOP = frozenset({"bin", "lib", "integrations", "setup", "tests", "docs", "config", "README.md", "AGENTS.md", "SKILL.md"})
RUNTIME_TOP = frozenset({"data", "queue", "state", "locks", "logs", "tmp", "releases"})
SKIPPED_PARTS = frozenset({"__pycache__", ".pytest_cache", "TASKS"})
ROOT_ARTIFACTS = frozenset(
    {
        "PROJECT-STATUS.md",
        "AUTO.json",
        "COMPLETE.json",
        "META.json",
        "PROMPT.md",
        "REPORT.md",
    }
)
CREDENTIAL_NAMES = frozenset({"rclone.conf", ".env", "id_rsa", "id_ed25519"})
CREDENTIAL_GLOBS = ("*.pem", "*.key", "*credentials*.json", "*gdrive*token*", "*oauth*token*")
ARTIFACT_GLOBS = ("*.pyc", "*.pyo", "*.log", "*.lock", "*.tmp", "*.temp", "*.bak", "*.backup-agent-handoff-*")
MANIFEST_NAME = ".agent-handoff-manifest.json"
MANIFEST_SCHEMA = 1
MANIFEST_MODE = 0o600


class ManifestError(ValueError):
    pass


def _parts(path: Path | PurePosixPath) -> tuple[str, ...]:
    return tuple(path.parts)


def is_managed_relative(path: Path | PurePosixPath | str) -> bool:
    value = PurePosixPath(path)
    if value.is_absolute():
        return False
    parts = value.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return False
    if parts[0] not in MANAGED_TOP:
        return False
    if any(part in RUNTIME_TOP or part in SKIPPED_PARTS for part in parts):
        return False
    name = parts[-1]
    if name in ROOT_ARTIFACTS:
        return False
    if parts[0] == "config" and len(parts) == 2 and parts[1] == "config.json":
        return False
    if parts[0] == "tests" and len(parts) == 2 and parts[1] in {"live-config.json", "acceptance-results.json"}:
        return False
    if name in CREDENTIAL_NAMES:
        return False
    if name.startswith(".env.") and name != ".env.example":
        return False
    if any(fnmatch.fnmatch(name, pattern) for pattern in CREDENTIAL_GLOBS):
        return False
    if any(fnmatch.fnmatch(name, pattern) for pattern in ARTIFACT_GLOBS):
        return False
    if name.startswith(".") and ".tmp-" in name:
        return False
    return True


def tracked_files(root: Path) -> list[Path] | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True,
            text=False,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return [Path(item.decode("utf-8")) for item in result.stdout.split(b"\0") if item]


def walk_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        dirnames[:] = [
            name
            for name in sorted(dirnames)
            if name != ".git" and not (base / name).is_symlink()
        ]
        for name in sorted(filenames):
            path = base / name
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(root)
            if is_managed_relative(relative):
                files.append(relative)
    return files


def source_files(source: Path) -> list[Path]:
    files = tracked_files(source)
    if files is None:
        files = walk_files(source)
    selected: set[Path] = set()
    try:
        source_root = source.resolve()
    except OSError:
        return []
    for item in files:
        relative = Path(item)
        if not is_managed_relative(relative):
            continue
        candidate = source / relative
        try:
            if candidate.is_symlink() or not candidate.is_file():
                continue
            resolved = candidate.resolve()
        except OSError:
            continue
        if not resolved.is_relative_to(source_root):
            continue
        selected.add(relative)
    return sorted(selected, key=lambda item: item.as_posix())


def safe_target(home: Path, relative: Path | PurePosixPath | str) -> Path | None:
    value = PurePosixPath(relative)
    if not is_managed_relative(value):
        return None
    try:
        base = home.resolve()
    except OSError:
        return None
    target = home
    for part in value.parts[:-1]:
        target = target / part
        if target.is_symlink():
            return None
    try:
        parent = target.resolve(strict=False)
    except OSError:
        return None
    if not parent.is_relative_to(base):
        return None
    return home.joinpath(*value.parts)


def normalize_manifest(relative: Path | PurePosixPath | str) -> str:
    value = PurePosixPath(relative)
    if not is_managed_relative(value):
        raise ManifestError(f"unsafe manifest path: {relative}")
    return value.as_posix()


def load_manifest(path: Path, required: bool = True) -> dict[str, Any] | None:
    if not path.exists():
        if required:
            raise ManifestError(f"missing ownership manifest: {path}")
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestError(f"invalid ownership manifest {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError(f"invalid ownership manifest {path}: root is not an object")
    if data.get("schema") != MANIFEST_SCHEMA:
        raise ManifestError(f"unsupported ownership manifest schema in {path}")
    files = data.get("files")
    if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
        raise ManifestError(f"invalid ownership manifest {path}: files must be a list of strings")
    normalized = sorted({normalize_manifest(item) for item in files})
    result = dict(data)
    result["files"] = normalized
    return result


def write_manifest(path: Path, files: list[str], metadata: dict[str, Any] | None = None) -> None:
    normalized = sorted({normalize_manifest(item) for item in files})
    payload = {"schema": MANIFEST_SCHEMA}
    if metadata:
        for key, value in metadata.items():
            if key in {"schema", "files"}:
                continue
            payload[key] = value
    payload["files"] = normalized
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, MANIFEST_MODE)
    os.replace(temporary, path)
