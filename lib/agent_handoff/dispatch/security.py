from __future__ import annotations

import grp
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..util import sanitize_error

DEFAULT_DENIED_PROJECTS = ("Rabet", "Yakfi")
DEFAULT_CHILD_PATH = "/usr/local/bin:/usr/bin:/bin"
PRIVILEGED_GROUP_NAMES = frozenset({"root", "sudo", "wheel", "admin", "adm", "docker", "disk", "lxd"})
CHILD_PATH_UNTRUSTED_WRITE_BITS = 0o022
PROTECTED_PREFIXES = (
    "/bin",
    "/boot",
    "/dev",
    "/etc",
    "/lib",
    "/lib64",
    "/proc",
    "/root",
    "/run",
    "/sbin",
    "/sys",
    "/usr",
    "/var/lib",
    "/var/log",
    "/var/run",
)
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
SAFE_LOCALE_ENV = ("LANG", "LC_ALL", "LC_CTYPE", "TZ")
GIT_CLEAN_CHECK_TIMEOUT = 30


class IdentityError(ValueError):
    pass


class WorkspaceSecurityError(ValueError):
    pass


class IsolationError(ValueError):
    pass


def _is_within(path: str, root: str) -> bool:
    normalized_root = root.rstrip("/") or "/"
    if normalized_root == "/":
        return path.startswith("/")
    return path == normalized_root or path.startswith(normalized_root + "/")


def canonical_path(value: Any, label: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise WorkspaceSecurityError(f"{label} is required")
    expanded = os.path.expanduser(text)
    if not os.path.isabs(expanded):
        raise WorkspaceSecurityError(f"{label} must be an absolute canonical path")
    if "\x00" in expanded or "\n" in expanded or "\r" in expanded:
        raise WorkspaceSecurityError(f"{label} must be a single filesystem path")
    normalized = os.path.normpath(expanded)
    if expanded != normalized:
        raise WorkspaceSecurityError(f"{label} must be a canonical path without '.', '..', or repeated separators")
    real = os.path.realpath(expanded)
    if real != normalized:
        raise WorkspaceSecurityError(f"{label} must not contain symlink components or symlink escapes")
    return real


def _protected_reason(path: str, extra_protected: Sequence[str] = ()) -> str | None:
    for prefix in (*PROTECTED_PREFIXES, *extra_protected):
        if prefix and _is_within(path, prefix):
            return prefix
    return None


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _has_execute_bits(mode: int, uid: int, gid: int, *, owner_uid: int, owner_gid: int) -> bool:
    if owner_uid == uid:
        return bool(mode & stat.S_IXUSR)
    if owner_gid == gid:
        return bool(mode & stat.S_IXGRP)
    return bool(mode & stat.S_IXOTH)


def _directory_access_bits(entry: os.stat_result, uid: int, gid: int) -> int:
    if entry.st_uid == uid:
        return (entry.st_mode >> 6) & 0o7
    if entry.st_gid == gid:
        return (entry.st_mode >> 3) & 0o7
    return entry.st_mode & 0o7


def _check_ancestor_traversal(path: str, identity: dict[str, Any], label: str) -> None:
    uid = int(identity["uid"])
    gid = int(identity["gid"])
    current = Path(path)
    for candidate in reversed([current, *current.parents]):
        try:
            entry = os.stat(candidate, follow_symlinks=False)
        except OSError as exc:
            raise WorkspaceSecurityError(f"{label} is not accessible: {candidate}") from exc
        if not stat.S_ISDIR(entry.st_mode):
            raise WorkspaceSecurityError(f"{label} ancestor is not a directory: {candidate}")
        if not _has_execute_bits(entry.st_mode, uid, gid, owner_uid=entry.st_uid, owner_gid=entry.st_gid):
            raise WorkspaceSecurityError(
                f"{label} is not traversable by the dedicated execution identity: {candidate}"
            )


def _check_trusted_ancestors(path: str, identity: Mapping[str, Any], label: str) -> None:
    """Reject parents writable by users other than root or the dispatcher identity.

    On the production host the dispatcher is root, so this means root-owned.
    Sticky, root-owned directories such as /tmp are allowed as ancestors because
    another user cannot rename or replace a root-owned child entry there. A
    non-sticky group/world-writable ancestor, or one owned by a third UID, could
    be used to swap the trusted path, so it is rejected. This narrowing is not a
    complete TOCTOU guarantee; see docs/SECURE-CODING.md.
    """

    current = Path(path).parent
    for candidate in (current, *current.parents):
        try:
            entry = os.lstat(candidate)
        except OSError as exc:
            raise WorkspaceSecurityError(f"{label} ancestor is not accessible: {candidate}") from exc
        if stat.S_ISLNK(entry.st_mode):
            raise WorkspaceSecurityError(f"{label} ancestor must not be a symlink: {candidate}")
        if not stat.S_ISDIR(entry.st_mode):
            raise WorkspaceSecurityError(f"{label} ancestor is not a directory: {candidate}")
        if entry.st_uid not in {0, os.geteuid()}:
            raise WorkspaceSecurityError(f"{label} ancestor is owned by an untrusted user: {candidate}")
        if entry.st_mode & stat.S_ISVTX and entry.st_uid == 0:
            continue
        if entry.st_mode & 0o022:
            raise WorkspaceSecurityError(f"{label} ancestor is writable by untrusted users: {candidate}")


def _identity_group_ids(identity: Mapping[str, Any]) -> set[int]:
    """Return the child's primary and supplementary group ids, when resolvable."""

    groups: set[int] = set()
    try:
        primary = int(identity.get("gid", -1))
    except (TypeError, ValueError):
        primary = -1
    if primary > 0:
        groups.add(primary)
    for item in identity.get("group_ids") or []:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value > 0:
            groups.add(value)
    for item in identity.get("groups") or []:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value > 0:
            groups.add(value)
    user = str(identity.get("user") or "")
    if user and primary > 0:
        try:
            groups.update(int(item) for item in os.getgrouplist(user, primary) if int(item) > 0)
        except OSError:
            pass
    return groups


def _has_child_write_capability(entry: os.stat_result, identity: Mapping[str, Any]) -> bool:
    try:
        uid = int(identity.get("uid", -1))
    except (TypeError, ValueError):
        uid = -1
    if uid <= 0:
        return True
    if entry.st_uid == uid and entry.st_mode & stat.S_IWUSR:
        return True
    if entry.st_gid in _identity_group_ids(identity) and entry.st_mode & stat.S_IWGRP:
        return True
    return bool(entry.st_mode & stat.S_IWOTH)


def _trusted_path_directory(entry: os.stat_result, identity: Mapping[str, Any]) -> bool:
    """Return True when an untrusted child cannot replace this directory entry."""

    try:
        child_uid = int(identity.get("uid", -1))
    except (TypeError, ValueError):
        child_uid = -1
    if entry.st_uid == child_uid:
        return False
    if entry.st_uid not in {0, os.geteuid()}:
        return False
    if entry.st_mode & CHILD_PATH_UNTRUSTED_WRITE_BITS:
        return False
    return not _has_child_write_capability(entry, identity)


def _check_child_path_directory_chain(path: str, identity: Mapping[str, Any], label: str) -> None:
    current = Path(path)
    for candidate in reversed([current, *current.parents]):
        try:
            entry = os.lstat(candidate)
        except OSError as exc:
            raise IdentityError(f"{label} is not accessible: {candidate}") from exc
        if stat.S_ISLNK(entry.st_mode):
            raise IdentityError(f"{label} must not contain a symlink component: {candidate}")
        if not stat.S_ISDIR(entry.st_mode):
            raise IdentityError(f"{label} ancestor is not a directory: {candidate}")
        is_entry = candidate == current
        if not is_entry and entry.st_uid == 0 and entry.st_mode & stat.S_ISVTX:
            # /tmp-style sticky root directory. The child cannot rename or
            # replace a root-owned entry inside it, including this PATH entry.
            continue
        if not _trusted_path_directory(entry, identity):
            raise IdentityError(
                f"{label} is owned or writable by an untrusted identity: {candidate}"
            )


def _check_child_path_entry(path: str, identity: Mapping[str, Any], label: str) -> str:
    """Validate one PATH entry and return its resolved real directory."""

    try:
        raw_entry = os.lstat(path)
    except OSError as exc:
        raise IdentityError(f"{label} does not exist: {path}") from exc
    parent = os.path.dirname(path) or "/"
    _check_child_path_directory_chain(parent, identity, label)
    if stat.S_ISLNK(raw_entry.st_mode):
        try:
            link_entry = os.lstat(path)
        except OSError as exc:
            raise IdentityError(f"{label} is not accessible: {path}") from exc
        try:
            child_uid = int(identity.get("uid", -1))
        except (TypeError, ValueError):
            child_uid = -1
        if link_entry.st_uid == child_uid or link_entry.st_uid not in {0, os.geteuid()}:
            raise IdentityError(f"{label} symlink is owned by an untrusted identity: {path}")
        resolved = os.path.realpath(path)
        if resolved == path:
            raise IdentityError(f"{label} is an unresolved symlink: {path}")
        _check_child_path_directory_chain(resolved, identity, label)
    else:
        resolved = os.path.realpath(path)
        if resolved != path:
            raise IdentityError(f"{label} must not contain a symlink escape: {path}")
        _check_child_path_directory_chain(resolved, identity, label)
    if not os.path.isdir(resolved):
        raise IdentityError(f"{label} is not a directory: {path}")
    return resolved


def validate_child_path(
    value: Any,
    identity: Mapping[str, Any],
    *,
    workspace: str | None = None,
    check_paths: bool = True,
) -> list[str]:
    """Validate the isolated child PATH and reject untrusted executable directories.

    Every component must be absolute, canonical, and owned by root (or the
    dispatcher identity) with no group/other write permission and no effective
    write capability for the dedicated child UID/GID. A sticky root-owned
    ancestor such as /tmp is allowed when the PATH directory itself is trusted;
    /tmp as a PATH entry and child-owned HOME/cache/TMPDIR/workspace entries
    are unsafe because the child could create or replace an executable there.
    """

    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise IdentityError("dispatch.execution.isolation.child_path must not be empty")
    if "\x00" in text or "\n" in text or "\r" in text:
        raise IdentityError("dispatch.execution.isolation.child_path must be a single PATH value")
    parts = text.split(":")
    if any(not part for part in parts):
        raise IdentityError(
            "dispatch.execution.isolation.child_path must not contain empty or relative components"
        )
    validated: list[str] = []
    for part in parts:
        if not os.path.isabs(part):
            raise IdentityError(
                "dispatch.execution.isolation.child_path must contain absolute directories only"
            )
        normalized = os.path.normpath(part)
        if normalized != part:
            raise IdentityError(
                "dispatch.execution.isolation.child_path entries must be canonical paths"
            )
        if not check_paths:
            validated.append(part)
            continue
        resolved = _check_child_path_entry(part, identity, "dispatch.execution.isolation.child_path entry")
        untrusted_roots = [
            identity.get("home"),
            identity.get("config_home"),
            identity.get("data_home"),
            identity.get("cache_home"),
            identity.get("tmp_dir"),
            workspace,
        ]
        for root in untrusted_roots:
            if not isinstance(root, str) or not root:
                continue
            root_real = os.path.realpath(root)
            if _is_within(resolved, root_real):
                raise IdentityError(
                    "dispatch.execution.isolation.child_path must not contain the child's "
                    f"HOME, XDG, TMPDIR, or workspace path: {part}"
                )
        if part not in validated:
            validated.append(part)
    if not validated:
        raise IdentityError("dispatch.execution.isolation.child_path must contain at least one directory")
    return validated


def validate_trusted_executable(
    binary: str,
    identity: Mapping[str, Any],
    *,
    workspace: str | None = None,
    label: str = "trusted executable",
) -> str:
    """Validate a regular executable used by the privileged parent for the child."""

    try:
        uid = int(identity.get("uid", -1))
        gid = int(identity.get("gid", -1))
    except (TypeError, ValueError) as exc:
        raise IsolationError("the dedicated execution identity is malformed") from exc
    canonical = os.path.realpath(binary)
    try:
        entry = os.stat(canonical)
    except OSError as exc:
        raise IsolationError(f"{label} is not available: {binary}") from exc
    if not stat.S_ISREG(entry.st_mode):
        raise IsolationError(f"{label} must be a regular file")
    if entry.st_uid == uid or entry.st_uid not in {0, os.geteuid()}:
        raise IsolationError(
            f"{label} must be owned by root (or the dispatcher identity) so the dedicated "
            "coding identity cannot replace it"
        )
    if entry.st_mode & 0o022:
        raise IsolationError(f"{label} must not be group/world writable")
    if entry.st_mode & (stat.S_ISUID | stat.S_ISGID):
        raise IsolationError(f"{label} must not be setuid or setgid")
    if not _has_execute_bits(entry.st_mode, uid, gid, owner_uid=entry.st_uid, owner_gid=entry.st_gid):
        raise IsolationError(f"{label} is not executable by the dedicated identity")
    for root in (
        identity.get("home"),
        identity.get("config_home"),
        identity.get("data_home"),
        identity.get("cache_home"),
        identity.get("tmp_dir"),
        workspace,
    ):
        if not isinstance(root, str) or not root:
            continue
        root_real = os.path.realpath(root)
        if _is_within(canonical, root_real):
            raise IsolationError(
                f"{label} must not live under the dedicated child's HOME, workspace, cache, or temporary directories"
            )
    try:
        _check_trusted_ancestors(canonical, identity, label)
    except WorkspaceSecurityError as exc:
        raise IsolationError(str(exc)) from exc
    return canonical


def resolve_trusted_path_executable(
    name: str,
    identity: Mapping[str, Any],
    *,
    workspace: str | None = None,
    label: str = "trusted executable",
) -> str:
    """Resolve one bare executable name against the validated trusted child PATH."""

    if not isinstance(name, str) or not name or "\x00" in name or "\n" in name or "/" in name:
        raise IsolationError(f"{label} name must be a bare executable name")
    entries = validate_child_path(
        identity.get("child_path") or DEFAULT_CHILD_PATH,
        identity,
        workspace=workspace,
        check_paths=True,
    )
    resolved = shutil.which(name, path=":".join(entries))
    if not resolved:
        raise IsolationError(f"{label} {name!r} is not available on the validated trusted child PATH")
    return validate_trusted_executable(
        resolved, identity, workspace=workspace, label=f"{label} {name!r}"
    )


def resolve_child_identity(
    raw: Any,
    *,
    check_paths: bool = True,
    child_path: str | None = None,
) -> dict[str, Any]:
    """Resolve a dedicated unprivileged Unix identity without falling back to root."""

    if not isinstance(raw, dict):
        raise IdentityError("dispatch.execution.isolation.identity must be an object")
    unknown = sorted(set(raw) - {"user", "group", "home"})
    if unknown:
        raise IdentityError(
            "dispatch.execution.isolation.identity contains unsupported field(s): "
            + ", ".join(unknown)
        )
    user = _text(raw.get("user"))
    if not user:
        raise IdentityError("dispatch.execution.isolation.identity.user is required when isolation is enabled")
    if not USER_RE.fullmatch(user):
        raise IdentityError(
            "dispatch.execution.isolation.identity.user must be a local Unix user name "
            "(lowercase letters, digits, '_' or '-')"
        )
    home_raw = _text(raw.get("home"))
    if not home_raw:
        raise IdentityError("dispatch.execution.isolation.identity.home is required when isolation is enabled")

    try:
        account = pwd.getpwnam(user)
    except KeyError as exc:
        raise IdentityError(
            f"dispatch.execution.isolation.identity.user {user!r} does not exist; "
            "create the dedicated account in a separately approved staging operation"
        ) from exc

    uid = int(account.pw_uid)
    primary_gid = int(account.pw_gid)
    if uid == 0:
        raise IdentityError("the dedicated execution identity must not be root (uid 0)")
    if primary_gid == 0:
        raise IdentityError("the dedicated execution identity must not use the root group (gid 0)")

    group_name = _text(raw.get("group"))
    if group_name:
        try:
            group = grp.getgrnam(group_name)
        except KeyError as exc:
            raise IdentityError(
                f"dispatch.execution.isolation.identity.group {group_name!r} does not exist"
            ) from exc
        if int(group.gr_gid) != primary_gid:
            raise IdentityError(
                "dispatch.execution.isolation.identity.group must be the user's primary group in C1"
            )
    else:
        try:
            group = grp.getgrgid(primary_gid)
        except KeyError as exc:
            raise IdentityError(
                "the dedicated execution identity primary group cannot be resolved"
            ) from exc
        group_name = group.gr_name

    gid = int(group.gr_gid)
    if gid == 0:
        raise IdentityError("the dedicated execution identity must not use the root group (gid 0)")
    if group_name in PRIVILEGED_GROUP_NAMES:
        raise IdentityError(
            f"the dedicated execution identity primary group {group_name!r} is privileged"
        )

    try:
        group_ids = os.getgrouplist(user, primary_gid)
    except OSError as exc:
        raise IdentityError("the dedicated execution identity supplementary groups cannot be resolved") from exc
    for group_id in group_ids:
        try:
            name = grp.getgrgid(int(group_id)).gr_name
        except KeyError as exc:
            raise IdentityError("the dedicated execution identity has an unresolvable group") from exc
        if int(group_id) == 0 or name in PRIVILEGED_GROUP_NAMES:
            raise IdentityError(
                f"the dedicated execution identity belongs to a privileged group {name!r}; "
                "remove that membership before enabling isolation"
            )

    try:
        home = canonical_path(home_raw, "dispatch.execution.isolation.identity.home")
    except WorkspaceSecurityError as exc:
        raise IdentityError(str(exc)) from exc
    protected = _protected_reason(home)
    if protected:
        raise IdentityError(
            "dispatch.execution.isolation.identity.home must be outside protected system directories "
            f"and /root (got prefix {protected})"
        )

    config_home = os.path.join(home, ".config")
    data_home = os.path.join(home, ".local", "share")
    cache_home = os.path.join(home, ".cache")
    tmp_dir = os.path.join(home, "tmp")

    path_value = _text(child_path) or DEFAULT_CHILD_PATH

    identity = {
        "user": user,
        "uid": uid,
        "group": group_name,
        "gid": gid,
        "groups": [],
        "group_ids": [int(item) for item in group_ids],
        "home": home,
        "config_home": config_home,
        "data_home": data_home,
        "cache_home": cache_home,
        "tmp_dir": tmp_dir,
        "child_path": path_value,
        "model_auth_path": os.path.join(data_home, "opencode", "auth.json"),
    }

    validated_path = validate_child_path(path_value, identity, check_paths=check_paths)
    identity["path_entries"] = validated_path
    identity["child_path"] = ":".join(validated_path)

    if check_paths:
        for label, directory in (
            ("home", home),
            ("XDG_CONFIG_HOME", config_home),
            ("XDG_DATA_HOME", data_home),
            ("XDG_CACHE_HOME", cache_home),
            ("TMPDIR", tmp_dir),
        ):
            if not os.path.isdir(directory):
                raise IdentityError(
                    f"the dedicated execution identity {label} directory does not exist: {directory}"
                )
            entry = os.stat(directory)
            if entry.st_uid != uid:
                raise IdentityError(
                    f"the dedicated execution identity {label} directory must be owned by {user!r}: {directory}"
                )
            if entry.st_mode & 0o022:
                raise IdentityError(
                    f"the dedicated execution identity {label} directory must not be group/world writable: {directory}"
                )
            _check_ancestor_traversal(directory, identity, label)

    return identity


def validate_model_auth(identity: Mapping[str, Any]) -> None:
    """Require child-owned OpenCode auth and never fall back to parent/root auth."""

    path = str(identity.get("model_auth_path") or "")
    if not path:
        raise IsolationError("the dedicated OpenCode model authentication path is not configured")
    entry = os.lstat(path) if os.path.lexists(path) else None
    if entry is None:
        raise IsolationError(
            "the dedicated OpenCode model authentication file is missing; provision a separate provider "
            "credential for this Unix identity. Root OpenCode authentication is never copied or inherited."
        )
    if stat.S_ISLNK(entry.st_mode):
        raise IsolationError("the dedicated OpenCode model authentication file must not be a symlink")
    if not stat.S_ISREG(entry.st_mode):
        raise IsolationError("the dedicated OpenCode model authentication path must be a regular file")
    uid = int(identity.get("uid", -1))
    if entry.st_uid != uid:
        raise IsolationError(
            "the dedicated OpenCode model authentication file must be owned by the dedicated identity"
        )
    if entry.st_mode & 0o077:
        raise IsolationError(
            "the dedicated OpenCode model authentication file must not be readable or writable by group/other"
        )


def validate_workspace_path(
    workspace: Any,
    *,
    workspace_roots: Sequence[str],
    project_names: Sequence[str],
    denied_projects: Sequence[str],
    protected_paths: Sequence[str] = (),
    check_paths: bool = True,
) -> str:
    try:
        path = canonical_path(workspace, "dispatch workspace")
    except WorkspaceSecurityError:
        raise
    if path == "/":
        raise WorkspaceSecurityError("the filesystem root is not a dispatch workspace")
    protected = _protected_reason(path, protected_paths)
    if protected:
        raise WorkspaceSecurityError(
            f"dispatch workspace is inside a protected path {protected!r}: {path}"
        )
    canonical_roots = []
    for root in workspace_roots:
        try:
            canonical_roots.append(canonical_path(root, "dispatch.execution.isolation.workspace_roots entry"))
        except WorkspaceSecurityError:
            raise
    if not any(_is_within(path, root) for root in canonical_roots):
        raise WorkspaceSecurityError(
            "dispatch workspace is outside every configured dispatch.execution.isolation.workspace_roots entry"
        )
    denied = {name.strip().lower() for name in denied_projects if isinstance(name, str) and name.strip()}
    for name in project_names:
        if isinstance(name, str) and name.strip().lower() in denied:
            raise WorkspaceSecurityError(
                f"project {name!r} is denied for the isolated coding profile; it must remain read_only"
            )
    components = {part.lower() for part in Path(path).parts if part and part != "/"}
    blocked_components = components & denied
    if blocked_components:
        raise WorkspaceSecurityError(
            "dispatch workspace path contains a denied coding project name: "
            + ", ".join(sorted(blocked_components))
        )
    if check_paths:
        if not os.path.isdir(path):
            raise WorkspaceSecurityError(f"dispatch workspace does not exist or is not a directory: {path}")
        if not os.path.lexists(os.path.join(path, ".git")):
            raise WorkspaceSecurityError(
                "isolated coding requires a Git workspace; .git is missing"
            )
        git_entry = os.path.join(path, ".git")
        if os.path.islink(git_entry):
            raise WorkspaceSecurityError("the Git metadata path must not be a symlink")
    return path


def _check_workspace_bits(path: str, identity: Mapping[str, Any], *, require_write: bool) -> None:
    uid = int(identity["uid"])
    gid = int(identity["gid"])
    entry = os.stat(path)
    required = 0o5 | (0o2 if require_write else 0)
    access = _directory_access_bits(entry, uid, gid)
    if access & required != required:
        action = "read/write/execute" if require_write else "read/execute"
        raise WorkspaceSecurityError(
            f"dispatch workspace is not {action}-accessible by the dedicated execution identity"
        )
    _check_ancestor_traversal(path, dict(identity), "dispatch workspace")


def validate_workspace_filesystem(
    workspace: str,
    identity: Mapping[str, Any],
    *,
    require_write: bool,
    require_clean: bool,
    protected_paths: Sequence[str] = (),
) -> None:
    path = canonical_path(workspace, "dispatch workspace")
    entry = os.stat(path)
    if not stat.S_ISDIR(entry.st_mode):
        raise WorkspaceSecurityError("dispatch workspace is not a directory")
    uid = int(identity["uid"])
    gid = int(identity["gid"])
    if entry.st_uid != uid:
        raise WorkspaceSecurityError(
            "dispatch workspace must be owned by the dedicated execution identity"
        )
    if entry.st_gid != gid:
        raise WorkspaceSecurityError(
            "dispatch workspace group must match the dedicated execution identity primary group"
        )
    if entry.st_mode & 0o022:
        raise WorkspaceSecurityError(
            "dispatch workspace must not be group/world writable for isolated execution"
        )
    _check_workspace_bits(path, identity, require_write=require_write)
    _check_trusted_ancestors(path, identity, "dispatch workspace")
    git_entry_path = os.path.join(path, ".git")
    if not os.path.lexists(git_entry_path):
        raise WorkspaceSecurityError("isolated coding requires a Git workspace; .git is missing")
    if os.path.islink(git_entry_path):
        raise WorkspaceSecurityError("the Git metadata path must not be a symlink")
    git_entry = os.lstat(git_entry_path)
    if not stat.S_ISDIR(git_entry.st_mode):
        raise WorkspaceSecurityError(
            "the Git metadata path must be a .git directory; linked worktree metadata is not supported in C1"
        )
    if git_entry.st_uid != uid:
        raise WorkspaceSecurityError("the Git metadata path must be owned by the dedicated execution identity")
    if git_entry.st_mode & 0o022:
        raise WorkspaceSecurityError("the Git metadata path must not be group/world writable")
    model_auth = str(identity.get("model_auth_path") or "")
    if model_auth:
        for candidate in (os.path.dirname(model_auth), model_auth):
            if not os.path.lexists(candidate):
                continue
            if _is_within(os.path.realpath(candidate), path):
                raise WorkspaceSecurityError(
                    "dedicated OpenCode model authentication must not live inside the coding workspace"
                )
    if require_clean:
        check_clean_git_workspace(path, identity=identity)


def check_clean_git_workspace(
    workspace: str,
    *,
    identity: Mapping[str, Any] | None = None,
    timeout: int = GIT_CLEAN_CHECK_TIMEOUT,
) -> None:
    path = canonical_path(workspace, "dispatch workspace")
    if identity:
        try:
            git = resolve_trusted_path_executable(
                "git", identity, workspace=path, label="trusted git executable"
            )
        except IsolationError as exc:
            raise WorkspaceSecurityError(str(exc)) from exc
    else:
        git = shutil.which("git", path=DEFAULT_CHILD_PATH)
    if not git:
        raise WorkspaceSecurityError("git is required to prove the isolated coding workspace is clean")
    env = {
        "PATH": (identity or {}).get("child_path") or DEFAULT_CHILD_PATH,
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    if identity:
        env["HOME"] = str(identity.get("home") or "/")
    command = [
        git,
        "-C",
        path,
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.untrackedCache=false",
        "-c",
        "core.excludesFile=/dev/null",
        "-c",
        "advice.detachedHead=false",
        "status",
        "--porcelain=v1",
        "--untracked-files=normal",
    ]
    kwargs: dict[str, Any] = {}
    if identity:
        kwargs = popen_isolation_kwargs(identity)
    try:
        result = subprocess.run(
            command,
            cwd=path,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            **kwargs,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorkspaceSecurityError(f"could not inspect the workspace Git state: {sanitize_error(str(exc))}") from exc
    if result.returncode != 0:
        detail = sanitize_error(result.stderr or "git status failed", 400)
        raise WorkspaceSecurityError(f"could not prove the workspace is clean: {detail}")
    if (result.stdout or "").strip():
        raise WorkspaceSecurityError(
            "dispatch workspace has uncommitted or untracked changes; refusing to run a coding task "
            "and refusing to reset or overwrite local work"
        )


def build_isolated_environment(
    identity: Mapping[str, Any],
    *,
    workspace: str,
    policy: Mapping[str, Any],
    parent_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a small allowlisted child environment instead of copying the parent."""

    parent = parent_env or {}
    env = {
        "PATH": str(identity["child_path"]),
        "HOME": str(identity["home"]),
        "USER": str(identity["user"]),
        "LOGNAME": str(identity["user"]),
        "XDG_CONFIG_HOME": str(identity["config_home"]),
        "XDG_DATA_HOME": str(identity["data_home"]),
        "XDG_CACHE_HOME": str(identity["cache_home"]),
        "TMPDIR": str(identity["tmp_dir"]),
        "PWD": workspace,
        "OPENCODE_CONFIG_CONTENT": json.dumps(dict(policy)),
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
    }
    for name in SAFE_LOCALE_ENV:
        value = parent.get(name)
        if isinstance(value, str) and value and "\x00" not in value and "\n" not in value and len(value) <= 200:
            env[name] = value
    return env


def _validate_opencode_executable(
    binary: str,
    identity: Mapping[str, Any],
    *,
    workspace: str | None = None,
) -> str:
    """Require a non-tamperable OpenCode executable."""

    try:
        return validate_trusted_executable(
            binary, identity, workspace=workspace, label="the OpenCode executable"
        )
    except IsolationError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise IsolationError("the dedicated execution identity is malformed") from exc


def resolve_opencode_binary(
    identity: Mapping[str, Any],
    parent_env: Mapping[str, str] | None = None,
    *,
    workspace: str | None = None,
) -> str:
    parent = parent_env or {}
    validate_child_path(
        identity.get("child_path") or DEFAULT_CHILD_PATH,
        identity,
        workspace=workspace,
    )
    explicit = _text(parent.get("AGENT_HANDOFF_OPENCODE_BIN"))
    if explicit:
        if not os.path.isabs(explicit) or "\x00" in explicit or "\n" in explicit:
            raise IsolationError("AGENT_HANDOFF_OPENCODE_BIN must be an absolute executable path")
        binary = os.path.realpath(explicit)
    else:
        binary = shutil.which(
            "opencode", path=str(identity.get("child_path") or DEFAULT_CHILD_PATH)
        ) or ""
    if not binary:
        raise IsolationError(
            "the OpenCode executable is not available on the dedicated child PATH; "
            "install it in a reviewed location or set an explicit absolute binary path"
        )
    return _validate_opencode_executable(binary, identity, workspace=workspace)


def popen_isolation_kwargs(identity: Mapping[str, Any] | None) -> dict[str, Any]:
    """Subprocess controls that set the child uid/gid and drop supplementary groups."""

    if not identity:
        return {}
    uid = int(identity.get("uid", -1))
    gid = int(identity.get("gid", -1))
    if uid <= 0 or gid <= 0:
        raise IsolationError("the dedicated execution identity must be non-root")
    if os.geteuid() != 0:
        raise IsolationError(
            "dedicated execution identity requires the dispatcher to run as root so the child uid/gid "
            "and empty supplementary groups can be applied safely; falling back to the parent identity is refused"
        )
    return {"user": uid, "group": gid, "extra_groups": [], "umask": 0o077}
