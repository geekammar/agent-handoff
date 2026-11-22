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
CLEANUP_DENIED_COMPONENTS = ("rabet", "yakfi", "nokmel")


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


def _remote_matches_source(remote: str, *, repository: str, source: str) -> bool:
    normalized = normalize_repository_url(remote)
    if normalized and normalized == repository.strip().lower():
        return True
    text = (remote or "").strip()
    if not text:
        return False
    try:
        if os.path.exists(text) and os.path.realpath(text) == os.path.realpath(source):
            return True
    except OSError:
        pass
    return False


def _inspect_existing_workspace(
    *,
    target: str,
    expected_name: str,
    workspace_root: str,
    repository: str,
    base_branch: str,
    base_commit: str,
    source: str,
    identity: Mapping[str, Any],
    git: SafeGit,
    require_clean: bool,
) -> dict[str, Any]:
    try:
        entry = os.lstat(target)
    except OSError as exc:
        raise WorkspacePreparationError("the existing workspace is missing") from exc
    if stat.S_ISLNK(entry.st_mode):
        raise WorkspacePreparationError("the existing workspace must not be a symlink")
    if not stat.S_ISDIR(entry.st_mode):
        raise WorkspacePreparationError("the existing workspace is not a directory")
    if os.path.basename(target) != expected_name:
        raise WorkspacePreparationError("the existing workspace name does not match this Issue and task id")
    root = os.path.realpath(workspace_root)
    real_target = os.path.realpath(target)
    if real_target == root or not real_target.startswith(root.rstrip("/") + "/"):
        raise WorkspacePreparationError("the existing workspace escaped the managed coding root")
    if os.path.dirname(real_target) != root:
        raise WorkspacePreparationError("the existing workspace is not a direct child of the managed coding root")
    if not WORKSPACE_NAME_RE.fullmatch(os.path.basename(real_target)):
        raise WorkspacePreparationError("the existing workspace name is not managed by Agent Handoff")
    uid = int(identity.get("uid", -1))
    gid = int(identity.get("gid", -1))
    if entry.st_uid != uid or entry.st_gid != gid:
        raise WorkspacePreparationError("the existing workspace is not owned by the dedicated execution identity")
    if entry.st_mode & 0o077:
        raise WorkspacePreparationError("the existing workspace permissions are not private to the execution identity")
    git_entry = os.path.join(real_target, ".git")
    try:
        git_stat = os.lstat(git_entry)
    except OSError as exc:
        raise WorkspacePreparationError("the existing workspace has unsafe or missing Git metadata") from exc
    if stat.S_ISLNK(git_stat.st_mode) or not stat.S_ISDIR(git_stat.st_mode):
        raise WorkspacePreparationError("the existing workspace Git metadata must be a real directory")
    if git_stat.st_uid != uid or git_stat.st_gid != gid:
        raise WorkspacePreparationError("the existing workspace Git metadata is not child-owned")
    if git_stat.st_mode & 0o022:
        raise WorkspacePreparationError("the existing workspace Git metadata is group/world writable")
    _assert_no_symlinks(real_target)
    remote = git.run(["remote", "get-url", "origin"], cwd=real_target, check=False)
    if remote.returncode != 0 or not _remote_matches_source(
        remote.stdout or "", repository=repository, source=source
    ):
        raise WorkspacePreparationError(
            "the existing workspace origin does not match the configured repository or trusted source"
        )
    head = git.run(["rev-parse", "HEAD"], cwd=real_target)
    if (head.stdout or "").strip() != base_commit:
        raise WorkspacePreparationError("the existing workspace is not at the verified base commit")
    base_ref = git.run(
        ["rev-parse", "--verify", f"refs/remotes/origin/{base_branch}^{{commit}}"],
        cwd=real_target,
        check=False,
    )
    if base_ref.returncode != 0:
        base_ref = git.run(
            ["rev-parse", "--verify", f"refs/heads/{base_branch}^{{commit}}"],
            cwd=real_target,
            check=False,
        )
    if base_ref.returncode != 0 or (base_ref.stdout or "").strip() != base_commit:
        raise WorkspacePreparationError("the existing workspace base branch does not match the verified base commit")
    status = git.run(["status", "--porcelain=v1", "--untracked-files=all"], cwd=real_target)
    modified = bool((status.stdout or "").strip())
    if modified and require_clean:
        raise WorkspacePreparationError(
            "the existing workspace is dirty before the coding agent completed; refusing to adopt it"
        )
    return {
        "workspace": real_target,
        "workspace_name": os.path.basename(real_target),
        "base_commit": base_commit,
        "base_branch": base_branch,
        "repository": repository,
        "recovered": True,
        "modified": modified,
    }


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
    allow_existing: bool = False,
    require_clean: bool = True,
) -> dict[str, Any]:
    if not TASK_ID_RE.fullmatch(str(task_id)):
        raise WorkspacePreparationError("the Agent Handoff task id is not safe for a workspace name")
    name = f"issue-{int(issue_number)}-{task_id}"
    if not WORKSPACE_NAME_RE.fullmatch(name):
        raise WorkspacePreparationError("the disposable workspace name is not safe")
    target = os.path.join(workspace_root, name)
    if os.path.lexists(target):
        if allow_existing:
            return _inspect_existing_workspace(
                target=target,
                expected_name=name,
                workspace_root=workspace_root,
                repository=repository,
                base_branch=base_branch,
                base_commit=base_commit,
                source=source,
                identity=identity,
                git=git,
                require_clean=require_clean,
            )
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
    expected_name: str | None = None,
    source_workspace: str | None = None,
) -> bool:
    try:
        original = os.lstat(workspace)
    except OSError:
        return True
    if stat.S_ISLNK(original.st_mode):
        return False
    root = os.path.realpath(workspace_root)
    target = os.path.realpath(workspace)
    if target == root or not target.startswith(root.rstrip("/") + "/"):
        return False
    if os.path.dirname(target) != root:
        return False
    name = os.path.basename(target)
    if not WORKSPACE_NAME_RE.fullmatch(name):
        return False
    if expected_name is not None and name != expected_name:
        return False
    if source_workspace:
        source = os.path.realpath(source_workspace)
        if (
            target == source
            or target.startswith(source.rstrip("/") + "/")
            or source.startswith(target.rstrip("/") + "/")
        ):
            return False
    components = {part.lower() for part in Path(target).parts if part and part != "/"}
    if components & set(CLEANUP_DENIED_COMPONENTS):
        return False
    if not os.path.isdir(target):
        return True
    try:
        entry = os.lstat(target)
    except OSError:
        return False
    if not stat.S_ISDIR(entry.st_mode):
        return False
    if entry.st_mode & 0o077:
        return False
    if identity:
        try:
            uid = int(identity.get("uid", -1))
            gid = int(identity.get("gid", -1))
        except (TypeError, ValueError):
            return False
        if uid <= 0 or gid <= 0:
            return False
        if entry.st_uid != uid or entry.st_gid != gid:
            return False
    shutil.rmtree(target)
    return not os.path.lexists(target)


def repository_git_dir(workspace: str) -> Path:
    return Path(workspace) / ".git"
