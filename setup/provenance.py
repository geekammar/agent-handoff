from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from managed import is_managed_relative, source_commit, walk_files

PROVENANCE_VERIFIED = "verified"
PROVENANCE_UNKNOWN = "unknown"
DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_CHUNK = 1024 * 1024


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def managed_content_digest(root: Path, files: Iterable[Path | PurePosixPath | str]) -> str:
    normalized = sorted({PurePosixPath(item).as_posix() for item in files})
    for item in normalized:
        if not is_managed_relative(item):
            raise ValueError(f"unsafe managed path: {item}")
    digest = hashlib.sha256()
    for relative in normalized:
        path = root / relative
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_sha256(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def git_root(root: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    if not value:
        return None
    try:
        return Path(value).resolve()
    except OSError:
        return None


def _head_managed_files(root: Path) -> set[str] | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-tree", "-r", "-z", "--name-only", "HEAD"],
            capture_output=True,
            text=False,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    files: set[str] = set()
    for item in result.stdout.split(b"\0"):
        if not item:
            continue
        relative = item.decode("utf-8", "surrogateescape")
        if is_managed_relative(relative):
            files.add(relative)
    return files


def _head_blob(root: Path, relative: str) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "cat-file", "blob", f"HEAD:{relative}"],
            capture_output=True,
            text=False,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _git_quiet(root: Path, args: list[str], paths: list[str]) -> int | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args, "--", *paths],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.returncode


def _unknown(reason: str, repository: str | None = None) -> dict[str, Any]:
    return {
        "provenance": PROVENANCE_UNKNOWN,
        "commit": None,
        "repository": repository,
        "reason": reason,
    }


def assess_source_provenance(source: Path, selected: list[Path]) -> dict[str, Any]:
    root = git_root(source)
    if root is None:
        return _unknown("not_a_git_repository")
    try:
        source_root = source.resolve()
    except OSError:
        return _unknown("source_unresolvable")
    if root != source_root:
        return _unknown("source_not_git_root")
    head = source_commit(source)
    if not head:
        return _unknown("git_head_missing", str(source))
    head_files = _head_managed_files(source)
    if head_files is None:
        return _unknown("git_tree_unavailable", str(source))
    selected_files = {item.as_posix() for item in selected}
    if head_files != selected_files:
        return _unknown("managed_file_set_differs", str(source))
    paths = sorted(selected_files)
    staged = _git_quiet(source, ["diff", "--cached", "--quiet", "HEAD"], paths)
    if staged is None:
        return _unknown("git_diff_unavailable", str(source))
    if staged != 0:
        return _unknown("staged_changes", str(source))
    worktree = _git_quiet(source, ["diff", "--quiet", "HEAD"], paths)
    if worktree is None:
        return _unknown("git_diff_unavailable", str(source))
    if worktree != 0:
        return _unknown("working_tree_changes", str(source))
    for relative in paths:
        committed = _head_blob(source, relative)
        if committed is None:
            return _unknown("committed_file_unreadable", str(source))
        try:
            working = (source / relative).read_bytes()
        except OSError:
            return _unknown("working_file_unreadable", str(source))
        if hashlib.sha256(committed).digest() != hashlib.sha256(working).digest():
            return _unknown("working_tree_content_differs", str(source))
    disk_managed = {item.as_posix() for item in walk_files(source)}
    if disk_managed - selected_files:
        return _unknown("untracked_managed_files", str(source))
    return {
        "provenance": PROVENANCE_VERIFIED,
        "commit": head,
        "repository": str(source),
        "reason": None,
    }


def trusted_manifest_source(
    manifest: dict[str, Any] | None,
    home: Path,
    *,
    content_sha256: str | None = None,
) -> dict[str, Any]:
    if not manifest:
        return _unknown("legacy_manifest_missing_provenance")
    if manifest.get("source_provenance") != PROVENANCE_VERIFIED:
        reason = str(manifest.get("source_commit_reason") or "manifest_provenance_unknown")
        return _unknown(reason)
    commit = manifest.get("source_commit")
    if not isinstance(commit, str) or not commit.strip():
        return _unknown("manifest_missing_commit")
    recorded = manifest.get("content_sha256")
    if not isinstance(recorded, str) or not DIGEST_RE.fullmatch(recorded):
        return _unknown("manifest_missing_content_digest")
    files = manifest.get("files")
    if not isinstance(files, list):
        return _unknown("manifest_missing_files")
    try:
        actual = content_sha256 or managed_content_digest(home, files)
    except (OSError, ValueError):
        return _unknown("manifest_content_unreadable")
    if actual != recorded:
        return _unknown("manifest_content_digest_mismatch")
    repository = manifest.get("source_repository")
    return {
        "provenance": PROVENANCE_VERIFIED,
        "commit": commit.strip(),
        "repository": repository if isinstance(repository, str) else None,
        "reason": None,
        "content_sha256": actual,
    }


def trusted_release_source(
    manifest: dict[str, Any],
    snapshot_code: Path,
    files: list[str],
    *,
    content_sha256: str | None = None,
) -> dict[str, Any]:
    if manifest.get("installed_source_provenance") != PROVENANCE_VERIFIED:
        reason = str(
            manifest.get("installed_source_commit_reason")
            or "legacy_snapshot_missing_provenance"
        )
        return _unknown(reason)
    commit = manifest.get("installed_source_commit")
    if not isinstance(commit, str) or not commit.strip():
        return _unknown("snapshot_missing_commit")
    recorded = manifest.get("installed_content_sha256")
    if not isinstance(recorded, str) or not DIGEST_RE.fullmatch(recorded):
        return _unknown("snapshot_missing_content_digest")
    try:
        actual = content_sha256 or managed_content_digest(snapshot_code, files)
    except (OSError, ValueError):
        return _unknown("snapshot_content_unreadable")
    if actual != recorded:
        return _unknown("snapshot_content_digest_mismatch")
    repository = manifest.get("installed_source_repository")
    return {
        "provenance": PROVENANCE_VERIFIED,
        "commit": commit.strip(),
        "repository": repository if isinstance(repository, str) else None,
        "reason": None,
        "content_sha256": actual,
    }
