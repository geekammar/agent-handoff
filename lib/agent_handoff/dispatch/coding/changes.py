from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ...util import sanitize_error
from ..security import DEFAULT_CHILD_PATH

GIT_TIMEOUT_SECONDS = 60
MAX_PATH_LENGTH = 4096

SECRET_PATTERNS = (
    ("private-key", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("aws-access-key", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("github-pat", re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("slack-token", re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("telegram-token", re.compile(rb"\b[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}\b")),
    ("google-api-key", re.compile(rb"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("openai-key", re.compile(rb"\bsk-[A-Za-z0-9]{20,}\b")),
    (
        "generic-secret-assignment",
        re.compile(
            rb"(?i)\b(?:api[_-]?key|client[_-]?secret|access[_-]?token|auth[_-]?token|password|passwd)\b"
            rb"\s*[:=]\s*[\"'][^\"'\s]{8,}[\"']"
        ),
    ),
)


class ChangeValidationError(ValueError):
    pass


class SafeGit:
    """Run Git with repository hooks, filters, replace refs, and prompts disabled."""

    def __init__(
        self,
        *,
        identity: Mapping[str, Any] | None = None,
        child_path: str | None = None,
        timeout: float = GIT_TIMEOUT_SECONDS,
        spawn: Any = subprocess.run,
        allow_file_protocol: bool = False,
    ) -> None:
        self.identity = dict(identity) if identity else None
        self.child_path = child_path or (self.identity or {}).get("child_path") or DEFAULT_CHILD_PATH
        self.timeout = timeout
        self.spawn = spawn
        self.allow_file_protocol = bool(allow_file_protocol)

    def environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        env = {
            "PATH": str(self.child_path),
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_ADVICE": "0",
            "GIT_ATTR_NOSYSTEM": "1",
        }
        if self.identity:
            env["HOME"] = str(self.identity.get("home") or "/")
        if extra:
            env.update({str(key): str(value) for key, value in extra.items()})
        return env

    def command(self, args: Sequence[str]) -> list[str]:
        return [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-c",
            "core.useReplaceRefs=false",
            "-c",
            "core.pager=cat",
            "-c",
            "advice.detachedHead=false",
            "-c",
            "credential.helper=",
            *(["-c", "protocol.file.allow=always"] if self.allow_file_protocol else []),
            *args,
        ]

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: str,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        text: bool = True,
    ) -> subprocess.CompletedProcess:
        command = self.command(args)
        kwargs: dict[str, Any] = {}
        if self.identity:
            from ..security import popen_isolation_kwargs

            kwargs = popen_isolation_kwargs(self.identity)
        try:
            result = self.spawn(
                command,
                cwd=cwd,
                env=self.environment(env),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=text,
                timeout=timeout or self.timeout,
                **kwargs,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ChangeValidationError(f"Git operation failed: {sanitize_error(str(exc))}") from exc
        if check and result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace") if isinstance(result.stderr, bytes) else result.stderr
            stdout = result.stdout.decode("utf-8", errors="replace") if isinstance(result.stdout, bytes) else result.stdout
            detail = sanitize_error((stderr or stdout or f"git exited {result.returncode}"), 600)
            raise ChangeValidationError(f"Git operation failed: {detail}")
        return result


@dataclass(frozen=True)
class ChangeEntry:
    path: str
    status: str
    old_path: str | None = None


def safe_relative_path(value: str) -> str:
    text = str(value or "")
    if not text or "\x00" in text or "\n" in text or "\r" in text:
        raise ChangeValidationError("changed path is empty or contains control characters")
    if len(text) > MAX_PATH_LENGTH or text.startswith("/"):
        raise ChangeValidationError("changed path must be a bounded relative path")
    normalized = text.replace("\\", "/")
    parts = normalized.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ChangeValidationError(f"changed path is not canonical: {text[:120]!r}")
    if parts[0] == ".git" or ".git" in parts:
        raise ChangeValidationError(f"changed path targets Git metadata: {text[:120]!r}")
    return normalized


def qualified_path(root: str, relative: str) -> str:
    safe = safe_relative_path(relative)
    candidate = os.path.realpath(os.path.join(root, safe))
    root_real = os.path.realpath(root)
    if not (candidate == root_real or candidate.startswith(root_real.rstrip("/") + "/")):
        raise ChangeValidationError(f"changed path escapes the task workspace: {relative[:120]!r}")
    return candidate


def parse_status(output: str) -> list[ChangeEntry]:
    fields = [item for item in output.split("\x00") if item]
    entries: list[ChangeEntry] = []
    index = 0
    while index < len(fields):
        item = fields[index]
        index += 1
        if len(item) < 4:
            continue
        code = item[:2]
        path = item[3:]
        old_path = None
        if code[0] in ("R", "C") or code[1] in ("R", "C"):
            if index < len(fields):
                old_path = fields[index]
                index += 1
        entries.append(ChangeEntry(path=path, status=code, old_path=old_path))
    return entries


def collect_status(git: SafeGit, workspace: str) -> list[ChangeEntry]:
    result = git.run(
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=workspace,
    )
    return parse_status(result.stdout or "")


def _index_mode(git: SafeGit, workspace: str, path: str) -> str | None:
    result = git.run(
        ["ls-files", "-s", "-z", "--", path],
        cwd=workspace,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        return None
    first = result.stdout.split("\x00", 1)[0]
    mode = first.split(" ", 1)[0]
    return mode or None


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalized_publication_mode(raw_mode: int) -> str:
    return "0755" if int(raw_mode) & 0o111 else "0644"


def read_regular_file(path: str, max_bytes: int) -> tuple[int, bytes, int, str]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ChangeValidationError("changed file is not a regular file")
        if info.st_size > max_bytes:
            raise ChangeValidationError(
                f"changed file exceeds the configured size limit ({info.st_size} > {max_bytes})"
            )
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > max_bytes:
            raise ChangeValidationError("changed file exceeds the configured size limit")
        return info.st_size, data, stat.S_IMODE(info.st_mode), sha256_bytes(data)
    finally:
        os.close(descriptor)


def scan_bytes_for_secrets(data: bytes) -> list[str]:
    findings: list[str] = []
    sample = data[:500_000]
    for name, pattern in SECRET_PATTERNS:
        if pattern.search(sample):
            findings.append(name)
    return findings


def _workspace_size(root: str) -> int:
    total = 0
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in files:
            try:
                info = os.lstat(os.path.join(current, name))
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                total += int(info.st_size)
    return total


def validate_changes(
    workspace: str,
    *,
    git: SafeGit,
    limits: Mapping[str, Any],
    protected_paths: Sequence[str],
    allowed_paths: Sequence[str] = (),
    secret_scan: bool = True,
) -> dict[str, Any]:
    """Inspect the disposable workspace without modifying it.

    The result is evidence, not a proof that the diff is safe. Secret scanning
    and path rules are defense in depth; review remains mandatory.
    """

    from .config import is_protected_path

    workspace = os.path.realpath(workspace)
    entries = collect_status(git, workspace)
    max_files = int(limits.get("max_files_changed", 25))
    max_file_bytes = int(limits.get("max_file_bytes", 200_000))
    max_diff_bytes = int(limits.get("max_diff_bytes", 500_000))
    max_workspace_bytes = int(limits.get("max_workspace_bytes", 2_000_000_000))

    blocked: list[str] = []
    files: list[dict[str, Any]] = []
    seen: set[str] = set()
    secret_findings: list[str] = []
    symlinks: list[str] = []
    submodules: list[str] = []
    total_content = 0

    if len(entries) > max_files:
        blocked.append(f"changed file count {len(entries)} exceeds the limit {max_files}")

    for entry in entries:
        code = entry.status
        if "U" in code or code == "AA" or code == "DD":
            blocked.append("unmerged or conflicting workspace state is not publishable")
            continue
        paths = [entry.path]
        if entry.old_path:
            paths.append(entry.old_path)
        for raw_path in paths:
            try:
                path = safe_relative_path(raw_path)
            except ChangeValidationError as exc:
                blocked.append(str(exc))
                continue
            if path in seen:
                continue
            seen.add(path)
            exists = entry.path == raw_path and "D" not in code
            if not exists and entry.old_path == raw_path:
                exists = False
            entry_status = "deleted" if not exists else ("added" if "A" in code or code == "??" else "modified")
            if entry.old_path and entry.path == raw_path:
                entry_status = "renamed"
            mode = _index_mode(git, workspace, path)
            if mode == "160000":
                submodules.append(path)
                blocked.append(f"submodule changes are not supported: {path}")
                files.append(
                    {"path": path, "status": entry_status, "size": 0, "mode": None, "raw_mode": None, "sha256": None, "git_mode": mode}
                )
                continue
            if mode == "120000":
                symlinks.append(path)
                blocked.append(f"symlink changes are not supported: {path}")
                files.append(
                    {"path": path, "status": entry_status, "size": 0, "mode": None, "raw_mode": None, "sha256": None, "git_mode": mode}
                )
                continue
            if mode and mode.startswith("100") and mode not in ("100644", "100755"):
                blocked.append(f"unsupported Git file mode {mode} for {path}")
            if is_protected_path(path, protected_paths):
                blocked.append(f"protected path was changed: {path}")
            if allowed_paths and not is_protected_path(path, allowed_paths):
                blocked.append(f"changed path is outside the configured allowlist: {path}")
            absolute = qualified_path(workspace, path)
            if not exists:
                files.append(
                    {"path": path, "status": entry_status, "size": 0, "mode": None, "raw_mode": None, "sha256": None, "git_mode": mode}
                )
                continue
            try:
                info = os.lstat(absolute)
            except OSError:
                blocked.append(f"changed path is not accessible: {path}")
                continue
            if stat.S_ISLNK(info.st_mode):
                symlinks.append(path)
                blocked.append(f"symlink changes are not supported: {path}")
                continue
            if not stat.S_ISREG(info.st_mode):
                blocked.append(f"changed path is not a regular file: {path}")
                continue
            try:
                size, data, file_mode, digest = read_regular_file(absolute, max_file_bytes)
            except (OSError, ChangeValidationError) as exc:
                blocked.append(f"{path}: {sanitize_error(str(exc), 300)}")
                continue
            if b"\x00" in data[:8192]:
                blocked.append(f"binary file changes are not supported: {path}")
            findings = scan_bytes_for_secrets(data) if secret_scan else []
            if findings:
                secret_findings.extend(f"{path}:{name}" for name in findings)
                blocked.append(f"possible secret material detected in {path}; values are redacted")
            total_content += size
            files.append(
                {
                    "path": path,
                    "status": entry_status,
                    "size": size,
                    "mode": normalized_publication_mode(file_mode),
                    "raw_mode": f"{file_mode:04o}",
                    "sha256": digest,
                }
            )

    diff_command = ["diff", "--no-color", "--no-ext-diff", "--no-textconv", "HEAD", "--"]
    try:
        diff_result = git.run(diff_command, cwd=workspace, check=False)
        diff_bytes = len((diff_result.stdout or "").encode("utf-8", errors="replace"))
    except ChangeValidationError as exc:
        blocked.append(sanitize_error(str(exc), 300))
        diff_bytes = 0
    for item in files:
        if item.get("status") == "added":
            diff_bytes += int(item.get("size") or 0)
    if diff_bytes > max_diff_bytes:
        blocked.append(f"diff size {diff_bytes} exceeds the limit {max_diff_bytes}")
    try:
        workspace_bytes = _workspace_size(workspace)
    except OSError as exc:
        blocked.append(f"workspace size could not be measured: {sanitize_error(str(exc), 200)}")
        workspace_bytes = 0
    if workspace_bytes > max_workspace_bytes:
        blocked.append(f"workspace size {workspace_bytes} exceeds the limit {max_workspace_bytes}")

    if not entries:
        blocked.append("the agent produced no changes; refusing to publish an empty coding result")
    if total_content > max_diff_bytes:
        blocked.append(f"changed content {total_content} exceeds the diff size limit {max_diff_bytes}")

    ordered_files = sorted(files, key=lambda item: str(item.get("path")))
    manifest = {
        "schema": 1,
        "digest": "sha256",
        "files": [
            {
                "path": item.get("path"),
                "status": item.get("status"),
                "size": item.get("size"),
                "mode": item.get("mode"),
                "raw_mode": item.get("raw_mode"),
                "sha256": item.get("sha256"),
            }
            for item in ordered_files
        ],
    }
    manifest_sha256 = sha256_bytes(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    return {
        "status": "blocked" if blocked else "passed",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": ordered_files,
        "manifest": manifest,
        "manifest_sha256": manifest_sha256,
        "changed_count": len(files),
        "diff_bytes": diff_bytes,
        "workspace_bytes": workspace_bytes,
        "blocked": blocked,
        "secret_findings": secret_findings,
        "symlinks": symlinks,
        "submodules": submodules,
        "secret_scan": bool(secret_scan),
    }
