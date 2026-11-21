from __future__ import annotations

import os
import re
import shutil
import stat
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from ...util import sanitize_error
from ..security import (
    WorkspaceSecurityError,
    _check_trusted_ancestors,
    canonical_path,
)
from .changes import ChangeValidationError, SafeGit

WORKSPACE_NAME_RE = re.compile(r"^issue-[0-9]{1,20}-[A-Za-z0-9_-]{1,64}$")
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class WorkspacePreparationError(RuntimeError):
    pass


def normalize_repository_url(value: str) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    if text.startswith("git@github.com:"):
        name = text[len("git@github.com:") :]
        if name.endswith(".git"):
            name = name[:-4]
        parts = name.split("/")
        if len(parts) == 2:
            return f"{parts[0]}/{parts[1]}".lower()
        return None
    parsed = urlparse(text)
    if parsed.scheme not in ("https", "ssh") or parsed.hostname not in ("github.com", "www.github.com"):
        return None
    name = parsed.path.lstrip("/")
    if name.endswith(".git"):
        name = name[:-4]
    parts = name.split("/")
    if len(parts) == 2 and all(parts):
        return f"{parts[0]}/{parts[1]}".lower()
    return None


def validate_coding_root(
    root: str,
    *,
    identity: Mapping[str, Any],
    workspace_roots: Sequence[str],
    source_workspace: str,
    base_dir: str,
    check_paths: bool = True,
) -> str:
    try:
        path = canonical_path(root, "coding workspace_root")
    except WorkspaceSecurityError as exc:
        raise WorkspacePreparationError(str(exc)) from exc
    if path == "/" or path == "/root":
        raise WorkspacePreparationError("the coding workspace_root must not be / or /root")
    if not any(path == item or path.startswith(item.rstrip("/") + "/") for item in workspace_roots):
        raise WorkspacePreparationError("the coding workspace_root is outside every isolation workspace_roots entry")
    source = os.path.realpath(source_workspace)
    if path == source or path.startswith(source.rstrip("/") + "/") or source.startswith(path.rstrip("/") + "/"):
        raise WorkspacePreparationError("the coding workspace_root must be separate from the source workspace")
    base = os.path.realpath(base_dir)
    if path == base or path.startswith(base.rstrip("/") + "/"):
        raise WorkspacePreparationError("the coding workspace_root must not be inside the Agent Handoff source tree")
    if not check_paths:
        return path
    try:
        entry = os.stat(path)
    except OSError as exc:
        raise WorkspacePreparationError(f"the coding workspace_root does not exist: {path}") from exc
    if not stat.S_ISDIR(entry.st_mode):
        raise WorkspacePreparationError("the coding workspace_root is not a directory")
    if entry.st_uid != int(identity.get("uid", -1)) or entry.st_gid != int(identity.get("gid", -1)):
        raise WorkspacePreparationError("the coding workspace_root must be owned by the dedicated execution identity")
    if entry.st_mode & 0o077:
        raise WorkspacePreparationError("the coding workspace_root must not be accessible by group/other")
    try:
        _check_trusted_ancestors(path, identity, "coding workspace_root")
    except WorkspaceSecurityError as exc:
        raise WorkspacePreparationError(str(exc)) from exc
    return path


def verify_source_repository(
    source: str,
    *,
    repository: str,
    base_branch: str,
    git: SafeGit,
) -> dict[str, str]:
    path = os.path.realpath(source)
    if not os.path.isdir(path):
        raise WorkspacePreparationError("the trusted source workspace does not exist")
    git_entry = os.path.join(path, ".git")
    if not os.path.lexists(git_entry):
        raise WorkspacePreparationError("the trusted source workspace is not a Git checkout")
    if os.path.islink(git_entry):
        raise WorkspacePreparationError("the trusted source Git metadata must not be a symlink")
    remote = git.run(["remote", "get-url", "origin"], cwd=path)
    actual = normalize_repository_url(remote.stdout or "")
    if not actual:
        raise WorkspacePreparationError("the trusted source workspace has no recognizable GitHub origin")
    if actual != repository.strip().lower():
        raise WorkspacePreparationError(
            "the trusted source workspace origin does not match the configured coding repository"
        )
    head = git.run(["rev-parse", "--verify", f"refs/heads/{base_branch}^{{commit}}"], cwd=path)
    base_commit = (head.stdout or "").strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", base_commit):
        raise WorkspacePreparationError("the configured base branch does not resolve to a commit")
    return {"source": path, "repository": actual, "base_branch": base_branch, "base_commit": base_commit}


def _assert_no_symlinks(root: str) -> None:
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            candidate = os.path.join(current, name)
            try:
                entry = os.lstat(candidate)
            except OSError as exc:
                raise WorkspacePreparationError(
                    f"the disposable workspace could not be inspected: {sanitize_error(str(exc), 200)}"
                ) from exc
            if stat.S_ISLNK(entry.st_mode):
                relative = os.path.relpath(candidate, root)
                raise WorkspacePreparationError(
                    f"the disposable workspace contains an unexpected symlink: {relative}"
                )


def prepare_workspace(
    *,
    workspace_root: str,
    repository: str,
    base_branch: str,
    base_commit: str,
    source: str,
    issue_number: int,
    task_id: str,
    identity: Mapping[str, Any],
    git: SafeGit,
    check_paths: bool = True,
) -> dict[str, Any]:
    if not TASK_ID_RE.fullmatch(str(task_id)):
        raise WorkspacePreparationError("the Agent Handoff task id is not safe for a workspace name")
    name = f"issue-{int(issue_number)}-{task_id}"
    if not WORKSPACE_NAME_RE.fullmatch(name):
        raise WorkspacePreparationError("the disposable workspace name is not safe")
    target = os.path.join(workspace_root, name)
    if os.path.lexists(target):
        raise WorkspacePreparationError(
            "the disposable workspace already exists; refusing to reuse or clean a dirty task directory"
        )
    if os.geteuid() == 0:
        os.mkdir(target, 0o700)
        try:
            os.chown(target, int(identity["uid"]), int(identity["gid"]))
        except OSError:
            shutil.rmtree(target, ignore_errors=True)
            raise
    else:
        raise WorkspacePreparationError(
            "the dispatcher must run as root to create a child-owned disposable workspace"
        )
    try:
        target = os.path.realpath(target)
        if not target.startswith(os.path.realpath(workspace_root).rstrip("/") + "/"):
            raise WorkspacePreparationError("the disposable workspace escaped the managed coding root")
        clone = git.run(
            [
                "clone",
                "--no-local",
                "--no-hardlinks",
                "--branch",
                base_branch,
                "--single-branch",
                "--quiet",
                "--",
                os.path.realpath(source),
                target,
            ],
            cwd=workspace_root,
            timeout=300,
        )
        del clone
        checkout = git.run(["rev-parse", "HEAD"], cwd=target)
        current = (checkout.stdout or "").strip()
        if current != base_commit:
            git.run(["checkout", "--detach", "--quiet", base_commit, "--"], cwd=target, timeout=120)
            verified = git.run(["rev-parse", "HEAD"], cwd=target)
            if (verified.stdout or "").strip() != base_commit:
                raise WorkspacePreparationError("the disposable workspace is not at the verified base commit")
        if check_paths:
            _assert_no_symlinks(target)
        status = git.run(
            ["status", "--porcelain=v1", "--untracked-files=all"],
            cwd=target,
        )
        if (status.stdout or "").strip():
            raise WorkspacePreparationError("the disposable workspace is not clean after preparation")
        return {
            "workspace": target,
            "workspace_name": name,
            "base_commit": base_commit,
            "base_branch": base_branch,
            "repository": repository,
        }
    except (ChangeValidationError, OSError) as exc:
        raise WorkspacePreparationError(sanitize_error(str(exc), 600)) from exc


def cleanup_workspace(
    workspace_root: str,
    workspace: str,
    *,
    identity: Mapping[str, Any] | None = None,
) -> bool:
    root = os.path.realpath(workspace_root)
    target = os.path.realpath(workspace)
    if target == root or not target.startswith(root.rstrip("/") + "/"):
        return False
    name = os.path.basename(target)
    if not WORKSPACE_NAME_RE.fullmatch(name):
        return False
    if os.path.islink(workspace) or os.path.islink(target):
        return False
    if not os.path.isdir(target):
        return True
    if identity and os.geteuid() == 0:
        try:
            entry = os.lstat(target)
            if entry.st_uid != int(identity.get("uid", -1)):
                return False
        except OSError:
            return False
    shutil.rmtree(target)
    return not os.path.lexists(target)


def repository_git_dir(workspace: str) -> Path:
    return Path(workspace) / ".git"
