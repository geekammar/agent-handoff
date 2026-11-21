from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from ..config import BASE_DIR, all_projects, project_by_name
from .security import (
    DEFAULT_DENIED_PROJECTS,
    IdentityError,
    IsolationError,
    WorkspaceSecurityError,
    canonical_path,
    resolve_child_identity,
    validate_model_auth,
    validate_workspace_filesystem,
    validate_workspace_path,
)


class DispatchConfigError(ValueError):
    pass


REPOSITORY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")
MODEL_ALIAS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._:/-]*$")
DRIVE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")
PERMISSION_PROFILES = {"read_only", "workspace_write"}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def _add(errors: list[str], message: str) -> None:
    if message not in errors:
        errors.append(message)


def _int_value(value: Any, default: int, minimum: int, maximum: int, label: str, errors: list[str]) -> int:
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        _add(errors, f"{label} must be an integer")
        return default
    if number < minimum or number > maximum:
        _add(errors, f"{label} must be between {minimum} and {maximum}")
        return default
    return number


def _bool_value(value: Any, default: bool, label: str, errors: list[str]) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        _add(errors, f"{label} must be true or false")
        return default
    return value


def _resolve_project_execution(
    raw: Any,
    global_execution: dict[str, Any],
    label: str,
    errors: list[str],
    *,
    global_isolation_enabled: bool = False,
) -> dict[str, Any]:
    """Resolve one project's effective execution policy.

    A missing override inherits the global policy exactly. A project-level
    workspace_write profile requires its own explicit allow_write=true, so a
    project can never become write-capable implicitly through global state.
    Write-capable execution additionally requires effective isolation; there is
    no root-write fallback.
    """

    global_profile = str(global_execution.get("permission_profile") or "read_only")
    global_allow_write = bool(global_execution.get("allow_write", False))
    default_isolation = {
        "enabled": bool(global_isolation_enabled),
        "inherited": True,
    }
    if raw is not None and not isinstance(raw, dict):
        _add(errors, f"{label} must be an object")
    if raw is None or not isinstance(raw, dict):
        if global_allow_write and not global_isolation_enabled:
            _add(
                errors,
                f"{label} inherits a write-capable policy but isolation is not enabled; "
                "write-capable root execution is refused. Enable isolation for this project "
                "with a validated non-root identity.",
            )
        return {
            "permission_profile": global_profile,
            "allow_write": global_allow_write,
            "inherited": True,
            "isolation": default_isolation,
        }
    unknown = sorted(set(raw) - {"permission_profile", "allow_write", "isolation"})
    if unknown:
        _add(errors, f"{label} contains unsupported field(s): {', '.join(unknown)}")

    isolation_explicit = "isolation" in raw
    isolation_enabled = bool(global_isolation_enabled)
    if isolation_explicit:
        isolation_raw = raw.get("isolation")
        if not isinstance(isolation_raw, dict):
            _add(errors, f"{label}.isolation must be an object")
        else:
            unknown_isolation = sorted(set(isolation_raw) - {"enabled"})
            if unknown_isolation:
                _add(
                    errors,
                    f"{label}.isolation contains unsupported field(s): "
                    + ", ".join(unknown_isolation),
                )
            if "enabled" in isolation_raw:
                if not isinstance(isolation_raw["enabled"], bool):
                    _add(errors, f"{label}.isolation.enabled must be true or false")
                else:
                    isolation_enabled = isolation_raw["enabled"]

    profile_explicit = "permission_profile" in raw
    allow_explicit = "allow_write" in raw
    profile = _text(raw.get("permission_profile")) if profile_explicit else global_profile
    if profile_explicit and profile not in PERMISSION_PROFILES:
        _add(errors, f"{label}.permission_profile must be one of {sorted(PERMISSION_PROFILES)}")
        profile = global_profile
    allow_write = _bool_value(raw.get("allow_write"), False, f"{label}.allow_write", errors) if allow_explicit else global_allow_write

    if profile_explicit and profile == "workspace_write":
        if not (allow_explicit and raw.get("allow_write") is True):
            _add(
                errors,
                f"{label}.permission_profile 'workspace_write' requires an explicit allow_write=true "
                "in the same project execution override",
            )
            allow_write = False
    elif profile_explicit and profile == "read_only":
        if allow_explicit and raw.get("allow_write") is True:
            _add(errors, f"{label}.allow_write=true requires permission_profile 'workspace_write'")
            allow_write = False
        else:
            allow_write = False
    else:
        if allow_explicit and raw.get("allow_write") is True and profile != "workspace_write":
            _add(errors, f"{label}.allow_write=true requires permission_profile 'workspace_write'")
            allow_write = False

    if allow_write and profile != "workspace_write":
        _add(errors, f"{label}.allow_write requires permission_profile 'workspace_write'")
    if profile == "workspace_write" and not allow_write:
        _add(errors, f"{label} permission_profile 'workspace_write' requires allow_write=true")
    if allow_write and not isolation_enabled:
        _add(
            errors,
            f"{label} is write-capable but isolation is not enabled; write-capable root execution "
            "is refused. Enable isolation for this project with a validated non-root identity.",
        )

    return {
        "permission_profile": profile,
        "allow_write": allow_write,
        "inherited": not (profile_explicit or allow_explicit),
        "isolation": {
            "enabled": isolation_enabled,
            "inherited": not (isolation_explicit and isinstance(raw.get("isolation"), dict) and "enabled" in raw["isolation"]),
        },
    }


def _parse_isolation(
    raw: Any,
    errors: list[str],
    warnings: list[str],
    *,
    check_paths: bool,
) -> dict[str, Any]:
    """Parse the global isolation definition without resolving the OS identity.

    The global `enabled` value remains the legacy default for every project.
    A project may opt in or opt out with `execution.isolation.enabled`. The
    identity is resolved later only when at least one effective project policy
    uses isolation, so a disabled example/staging block never requires a real
    Unix account.
    """

    parsed = {
        "enabled": False,
        "environment": "allowlist",
        "identity": None,
        "identity_raw": None,
        "child_path": None,
        "workspace_roots": [],
        "denied_projects": list(DEFAULT_DENIED_PROJECTS),
    }
    if raw is None:
        return parsed
    if not isinstance(raw, dict):
        _add(errors, "dispatch.execution.isolation must be an object")
        return parsed
    unknown = sorted(
        set(raw) - {"enabled", "environment", "identity", "workspace_roots", "denied_projects", "child_path"}
    )
    if unknown:
        _add(
            errors,
            "dispatch.execution.isolation contains unsupported field(s): " + ", ".join(unknown),
        )

    enabled = _bool_value(raw.get("enabled"), False, "dispatch.execution.isolation.enabled", errors)
    environment = _text(raw.get("environment")) or "allowlist"
    if environment != "allowlist":
        _add(errors, "dispatch.execution.isolation.environment must be 'allowlist'")

    workspace_roots: list[str] = []
    roots_raw = raw.get("workspace_roots")
    if roots_raw is None:
        roots_raw = []
    if not isinstance(roots_raw, list) or any(not isinstance(item, str) or not item.strip() for item in roots_raw):
        _add(errors, "dispatch.execution.isolation.workspace_roots must be a list of absolute paths")
    else:
        for item in roots_raw:
            try:
                root = canonical_path(item, "dispatch.execution.isolation.workspace_roots entry")
            except WorkspaceSecurityError as exc:
                _add(errors, str(exc))
                continue
            if root == "/" or root == "/root" or root.startswith("/root/"):
                _add(errors, "dispatch.execution.isolation.workspace_roots must not include / or /root")
                continue
            if root not in workspace_roots:
                workspace_roots.append(root)

    denied_projects = list(DEFAULT_DENIED_PROJECTS)
    denied_raw = raw.get("denied_projects")
    if denied_raw is not None:
        if not isinstance(denied_raw, list) or any(not isinstance(item, str) or not item.strip() for item in denied_raw):
            _add(errors, "dispatch.execution.isolation.denied_projects must be a list of project names")
        else:
            for item in denied_raw:
                name = item.strip()
                if name.lower() not in {existing.lower() for existing in denied_projects}:
                    denied_projects.append(name)

    identity_raw = raw.get("identity")
    if identity_raw is not None:
        if not isinstance(identity_raw, dict):
            _add(errors, "dispatch.execution.isolation.identity must be an object")
            identity_raw = None
        else:
            unknown_identity = sorted(set(identity_raw) - {"user", "group", "home"})
            if unknown_identity:
                _add(
                    errors,
                    "dispatch.execution.isolation.identity contains unsupported field(s): "
                    + ", ".join(unknown_identity),
                )

    parsed.update(
        {
            "enabled": enabled,
            "environment": environment,
            "identity_raw": identity_raw,
            "child_path": _text(raw.get("child_path")) or None,
            "workspace_roots": workspace_roots,
            "denied_projects": denied_projects,
        }
    )
    return parsed


def resolve_effective_isolation(
    global_isolation: dict[str, Any] | None,
    project_execution: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return the one effective isolation policy used by config, runner, and adapter."""

    isolation = dict(global_isolation or {})
    override = (project_execution or {}).get("isolation") or {}
    enabled = override.get("enabled")
    isolation["enabled"] = enabled if isinstance(enabled, bool) else bool(isolation.get("enabled", False))
    if not isolation["enabled"]:
        isolation["identity"] = None
    return isolation


def _finalize_isolation(
    isolation: dict[str, Any],
    *,
    required: bool,
    errors: list[str],
    warnings: list[str],
    check_paths: bool,
) -> dict[str, Any]:
    if not required:
        isolation["identity"] = None
        isolation.pop("identity_raw", None)
        isolation.pop("child_path", None)
        return isolation

    workspace_roots = isolation.get("workspace_roots") or []
    if not workspace_roots:
        _add(
            errors,
            "dispatch.execution.isolation.workspace_roots must contain at least one safe root "
            "when isolation is enabled for any project",
        )
    elif check_paths:
        for root in workspace_roots:
            if not os.path.isdir(root):
                _add(errors, f"dispatch.execution.isolation.workspace_roots entry does not exist: {root}")

    identity_raw = isolation.get("identity_raw")
    identity = None
    if not isinstance(identity_raw, dict):
        _add(
            errors,
            "dispatch.execution.isolation.identity must be an object when isolation is enabled for any project",
        )
    else:
        try:
            identity = resolve_child_identity(
                identity_raw,
                check_paths=check_paths,
                child_path=isolation.get("child_path"),
            )
        except IdentityError as exc:
            _add(errors, str(exc))
    if identity is not None and check_paths:
        try:
            validate_model_auth(identity)
        except IsolationError as exc:
            _add(errors, f"{exc}; isolated execution will fail closed until this is provisioned")
        if os.geteuid() != 0:
            warnings.append(
                "dedicated execution identity requires the dispatcher to run as root; "
                "isolated execution will fail closed under a non-root dispatcher"
            )
    isolation["identity"] = identity
    isolation.pop("identity_raw", None)
    isolation.pop("child_path", None)
    return isolation


def validate_dispatch_config(
    config: dict[str, Any],
    *,
    require_enabled: bool = False,
    check_paths: bool = True,
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Validate and normalize the optional dispatch section.

    Returns (resolved, errors, warnings). The resolved mapping contains only
    trusted values and never reads any field from an issue body.
    """

    raw = config.get("dispatch") or {}
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(raw, dict):
        raise DispatchConfigError("dispatch configuration must be an object")

    enabled = bool(raw.get("enabled", False))
    if require_enabled and not enabled:
        _add(errors, "dispatch.enabled must be true to run the dispatcher")

    transport = _text(raw.get("transport")) or "github_issues"
    if transport != "github_issues":
        _add(errors, f"dispatch.transport must be 'github_issues' (got {transport!r})")

    repository = _text(raw.get("repository"))
    if not repository:
        if enabled or require_enabled:
            _add(errors, "dispatch.repository is required")
    elif not REPOSITORY_RE.fullmatch(repository):
        _add(errors, "dispatch.repository must use the owner/name format")

    allowed_raw = raw.get("allowed_users")
    allowed_users: list[str] = []
    if allowed_raw is None:
        allowed_raw = []
    if not isinstance(allowed_raw, list) or any(not isinstance(item, str) or not item.strip() for item in allowed_raw):
        _add(errors, "dispatch.allowed_users must be a list of GitHub logins")
    else:
        allowed_users = [item.strip() for item in allowed_raw]
        if any(item == "*" for item in allowed_users):
            _add(errors, "dispatch.allowed_users must not contain a wildcard")
    if (enabled or require_enabled) and not allowed_users:
        _add(errors, "dispatch.allowed_users must contain at least one GitHub login")

    agent = _text(raw.get("agent")) or "opencode"
    if agent != "opencode":
        _add(errors, f"unsupported dispatch.agent {agent!r}; only 'opencode' is supported")

    models_raw = raw.get("models")
    models: dict[str, str] = {}
    if models_raw is None:
        models_raw = {}
    if not isinstance(models_raw, dict):
        _add(errors, "dispatch.models must be an object mapping aliases to model IDs")
    else:
        for alias, model_id in models_raw.items():
            alias_text = _text(alias)
            model_text = _text(model_id)
            if not MODEL_ALIAS_RE.fullmatch(alias_text):
                _add(errors, f"invalid dispatch model alias {alias!r}; use letters, digits, _ or -")
                continue
            if not MODEL_ID_RE.fullmatch(model_text) or ".." in model_text:
                _add(errors, f"dispatch model {alias_text!r} must use a provider/model ID")
                continue
            models[alias_text] = model_text
    if (enabled or require_enabled) and not models:
        _add(errors, "dispatch.models must define at least one alias")

    default_model = _text(raw.get("default_model"))
    if default_model and default_model not in models:
        _add(errors, f"dispatch.default_model {default_model!r} is not defined in dispatch.models")

    execution_value = raw.get("execution")
    if execution_value is not None and not isinstance(execution_value, dict):
        _add(errors, "dispatch.execution must be an object")
    execution_raw = execution_value if isinstance(execution_value, dict) else {}
    unknown_execution = sorted(
        set(execution_raw)
        - {"timeout_seconds", "max_prompt_bytes", "permission_profile", "allow_write", "isolation"}
    )
    if unknown_execution:
        _add(
            errors,
            "dispatch.execution contains unsupported field(s): " + ", ".join(unknown_execution),
        )
    timeout_seconds = _int_value(
        execution_raw.get("timeout_seconds"), 1200, 30, 86400, "dispatch.execution.timeout_seconds", errors
    )
    max_prompt_bytes = _int_value(
        execution_raw.get("max_prompt_bytes"), 50000, 1, 1000000, "dispatch.execution.max_prompt_bytes", errors
    )
    permission_profile = _text(execution_raw.get("permission_profile")) or "read_only"
    if permission_profile not in PERMISSION_PROFILES:
        _add(errors, f"dispatch.execution.permission_profile must be one of {sorted(PERMISSION_PROFILES)}")
    allow_write = _bool_value(
        execution_raw.get("allow_write"), False, "dispatch.execution.allow_write", errors
    )
    if allow_write and permission_profile != "workspace_write":
        _add(errors, "dispatch.execution.allow_write requires permission_profile 'workspace_write'")
    if permission_profile == "workspace_write" and not allow_write:
        _add(errors, "permission_profile 'workspace_write' requires dispatch.execution.allow_write=true")
    isolation = _parse_isolation(
        execution_raw.get("isolation"), errors, warnings, check_paths=check_paths
    )
    global_execution = {
        "timeout_seconds": timeout_seconds,
        "max_prompt_bytes": max_prompt_bytes,
        "permission_profile": permission_profile,
        "allow_write": allow_write,
        "isolation": isolation,
    }

    projects_raw = raw.get("projects")
    projects: dict[str, dict[str, Any]] = {}
    core_keys: dict[str, str] = {}
    if projects_raw is None:
        projects_raw = {}
    if not isinstance(projects_raw, dict):
        _add(errors, "dispatch.projects must be an object")
    else:
        for name, value in projects_raw.items():
            dispatch_name = _text(name)
            if not dispatch_name:
                _add(errors, "dispatch project names must not be empty")
                continue
            if not isinstance(value, dict):
                _add(errors, f"dispatch project {dispatch_name!r} must be an object")
                continue
            core_project = project_by_name(config, dispatch_name)
            if not core_project:
                _add(
                    errors,
                    f"dispatch project {dispatch_name!r} is not configured in the core projects mapping; "
                    "add it with `agent-handoff project add` first",
                )
                continue
            workspace = _text(value.get("workspace"))
            if not workspace:
                _add(errors, f"dispatch project {dispatch_name!r} requires a workspace path")
                continue
            workspace_real = _real(workspace)
            core_paths = {_real(item) for item in core_project.get("paths", [])}
            if check_paths and not os.path.isdir(workspace_real):
                _add(errors, f"dispatch project {dispatch_name!r} workspace does not exist: {workspace}")
                continue
            if workspace_real not in core_paths:
                _add(
                    errors,
                    f"dispatch project {dispatch_name!r} workspace is not one of the paths recorded for "
                    f"Agent Handoff project {core_project['name']!r}; refusing cross-project routing",
                )
                continue
            folder_id = _text(core_project.get("drive_folder_id"))
            if not folder_id or not DRIVE_ID_RE.fullmatch(folder_id):
                _add(errors, f"Agent Handoff project {core_project['name']!r} needs a valid Drive folder id for dispatch")
                continue
            if core_project.get("key") in core_keys:
                _add(
                    errors,
                    f"dispatch projects {core_keys[core_project.get('key')]!r} and {dispatch_name!r} "
                    "point at the same Agent Handoff project; use one dispatch entry per project",
                )
                continue
            project_label = f"dispatch project {dispatch_name!r} execution"
            project_execution = _resolve_project_execution(
                value.get("execution"),
                global_execution,
                project_label,
                errors,
                global_isolation_enabled=bool(isolation.get("enabled")),
            )
            core_keys[str(core_project.get("key"))] = dispatch_name
            projects[dispatch_name] = {
                "name": dispatch_name,
                "core_key": core_project.get("key"),
                "core_name": core_project.get("name"),
                "workspace": workspace_real,
                "drive_folder_id": folder_id,
                "execution": project_execution,
            }

    any_project_isolation = any(
        bool(resolve_effective_isolation(isolation, project.get("execution")).get("enabled"))
        for project in projects.values()
    )
    _finalize_isolation(
        isolation,
        required=bool(isolation.get("enabled")) or any_project_isolation,
        errors=errors,
        warnings=warnings,
        check_paths=check_paths,
    )
    for dispatch_name in list(projects):
        project = projects[dispatch_name]
        effective_isolation = resolve_effective_isolation(isolation, project.get("execution"))
        if not effective_isolation.get("enabled"):
            continue
        identity = effective_isolation.get("identity")
        if identity is None:
            _add(
                errors,
                f"dispatch project {dispatch_name!r} enables isolation but no validated non-root "
                "identity is available; refusing root fallback",
            )
            projects.pop(dispatch_name)
            continue
        core_project = project_by_name(config, str(project.get("core_name") or dispatch_name)) or {}
        project_names = [
            dispatch_name,
            str(project.get("core_name") or ""),
            str(project.get("core_key") or ""),
            *[str(item) for item in (core_project.get("aliases") or [])],
        ]
        try:
            workspace_real = validate_workspace_path(
                str(project.get("workspace") or ""),
                workspace_roots=effective_isolation.get("workspace_roots") or [],
                project_names=project_names,
                denied_projects=effective_isolation.get("denied_projects") or [],
                protected_paths=[str(BASE_DIR)],
                check_paths=check_paths,
            )
            if check_paths:
                validate_workspace_filesystem(
                    workspace_real,
                    identity,
                    require_write=bool((project.get("execution") or {}).get("allow_write", False)),
                    require_clean=False,
                    protected_paths=[str(BASE_DIR)],
                )
        except WorkspaceSecurityError as exc:
            _add(errors, f"dispatch project {dispatch_name!r}: {exc}")
            projects.pop(dispatch_name)
            continue
        project["workspace"] = workspace_real
    if (enabled or require_enabled) and not projects:
        _add(errors, "dispatch.projects must configure at least one project")

    poll_interval = _int_value(raw.get("poll_interval_seconds"), 30, 5, 86400, "dispatch.poll_interval_seconds", errors)

    delivery_raw = raw.get("delivery") if isinstance(raw.get("delivery"), dict) else {}
    retry_seconds = _int_value(
        delivery_raw.get("retry_seconds"), 60, 5, 3600, "dispatch.delivery.retry_seconds", errors
    )
    max_attempts = _int_value(
        delivery_raw.get("max_attempts"), 0, 0, 100000, "dispatch.delivery.max_attempts", errors
    )

    github_raw = raw.get("github") if isinstance(raw.get("github"), dict) else {}
    gh_command = _text(github_raw.get("command")) or "gh"
    if "\n" in gh_command or "\r" in gh_command or "\x00" in gh_command:
        _add(errors, "dispatch.github.command must be a single executable path")
    require_private = github_raw.get("require_private", True)
    if require_private is not True:
        _add(errors, "dispatch.github.require_private must remain true for the v1.1 dispatcher")

    resolved = {
        "enabled": enabled,
        "transport": transport,
        "repository": repository,
        "allowed_users": allowed_users,
        "agent": agent,
        "models": models,
        "default_model": default_model or None,
        "projects": projects,
        "poll_interval_seconds": poll_interval,
        "execution": global_execution,
        "delivery": {"retry_seconds": retry_seconds, "max_attempts": max_attempts},
        "github": {"command": gh_command, "require_private": True},
    }
    return resolved, errors, warnings


def resolve_dispatch_project(resolved: dict[str, Any], value: str) -> dict[str, Any] | None:
    needle = (value or "").strip().lower()
    if not needle:
        return None
    for name, project in (resolved.get("projects") or {}).items():
        candidates = {str(name).lower(), str(project.get("core_name", "")).lower(), str(project.get("core_key", "")).lower()}
        if needle in candidates:
            return project
    return None


def resolve_model(resolved: dict[str, Any], alias: str | None) -> tuple[str | None, str | None]:
    models = resolved.get("models") or {}
    selected = _text(alias)
    if not selected:
        selected = resolved.get("default_model") or ""
    if not selected:
        return None, "a model alias is required because no default_model is configured"
    model = models.get(selected)
    if not model:
        return None, f"unknown model alias {selected!r}; configure it in dispatch.models"
    return model, None


def validate_core_config(config: dict[str, Any], *, check_paths: bool = True) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    drive = config.get("drive") or {}
    for key in ("parent_folder_id", "root_folder_id"):
        value = _text(drive.get(key))
        if value and not DRIVE_ID_RE.fullmatch(value):
            _add(errors, f"drive.{key} is not a valid Google Drive folder id")
    if not _text(drive.get("root_folder_id")):
        _add(errors, "drive.root_folder_id is required for uploads")
    projects = all_projects(config)
    if not projects:
        warnings.append("no Agent Handoff projects are configured")
    for project in projects:
        folder_id = _text(project.get("drive_folder_id"))
        if folder_id and not DRIVE_ID_RE.fullmatch(folder_id):
            _add(errors, f"project {project['name']!r} has an invalid drive_folder_id")
        if not folder_id:
            warnings.append(f"project {project['name']!r} has no Drive folder id; uploads will not work")
        paths = [Path(item).expanduser() for item in project.get("paths", [])]
        if check_paths and not paths:
            warnings.append(f"project {project['name']!r} has no local path configured")
        for path in paths:
            if check_paths and not path.is_dir():
                warnings.append(f"project {project['name']!r} path does not exist: {path}")
    return errors, warnings
