from __future__ import annotations

import copy
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .config import BASE_DIR, CONFIG_PATH, DEFAULT_CONFIG, deep_merge, storage_path
from .dispatch.config import DispatchConfigError, validate_core_config, validate_dispatch_config
from .notify.config import NotificationConfigError, validate_notification_config
from .util import (
    atomic_write_json,
    atomic_write_text,
    ensure_dir,
    read_json,
    sanitize_error,
    sha256_text,
    utcnow,
    utcnow_iso,
)

SETUP_SCHEMA_VERSION = 1
STATUS_READY = "ready"
STATUS_MISSING = "missing"
STATUS_OPTIONAL = "optional"
STATUS_BLOCKED = "blocked"

STEP_COMPLETE = "complete"
STEP_PENDING = "pending"
STEP_OPTIONAL = "optional"
STEP_BLOCKED = "blocked"

DRIVE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
FORBIDDEN_ACTIVATIONS = (
    "dispatch.enabled",
    "dispatch.coding.enabled",
    "dispatch.execution.permission_profile=workspace_write",
    "dispatch.execution.allow_write",
    "dispatch.execution.isolation.enabled",
    "notifications.telegram.enabled",
    "systemd service activation",
)
DISPATCH_UNIT_NAME = "agent-handoff-dispatch.service"
JOURNAL_SUFFIX = ".setup-apply-journal.json"
BACKUP_MARKER = ".backup-setup-"

# Only fields that the guided setup foundation may propose are allowed here.
# Project mappings, dispatcher policy, notification credentials, coding policy,
# runtime storage, and security settings are deliberately excluded.
PATCH_FIELDS = (
    "drive.parent_folder_id",
    "drive.parent_folder_name",
    "drive.root_folder_id",
    "drive.root_folder_name",
    "workspace_roots",
    "auto_register_projects",
)
FORBIDDEN_PATCH_PREFIXES = (
    "dispatch",
    "notifications",
    "security",
    "projects",
    "storage",
    "integrations",
    "upload",
    "index",
    "version",
)
SECRET_KEY_RE = re.compile(r"(?i)(token|secret|password|passwd|credential|private[_-]?key)")


class SetupError(ValueError):
    pass


class SetupApprovalError(SetupError):
    pass


class SetupValidationError(SetupError):
    pass


class SetupRecoveryError(SetupError):
    pass


@dataclass(frozen=True)
class ConfigState:
    path: Path
    exists: bool
    valid_json: bool
    raw: dict[str, Any]
    view: dict[str, Any]
    text: str | None = None
    mode: int | None = None
    is_symlink: bool = False
    error: str | None = None


@dataclass
class SetupPatch:
    values: dict[str, Any] = field(default_factory=dict)
    paths: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def requested(self) -> bool:
        return bool(self.paths)

    @property
    def valid(self) -> bool:
        return not self.errors


def config_path_from_env() -> Path:
    configured = os.environ.get("AGENT_HANDOFF_CONFIG")
    if configured:
        return Path(os.path.abspath(os.path.expanduser(configured)))
    return BASE_DIR / "config" / "config.json"


def journal_path_for(config_path: Path) -> Path:
    return config_path.with_name(f".{config_path.name}{JOURNAL_SUFFIX}")


def read_config_state(config_path: Path | str | None = None, *, defaults: Mapping[str, Any] | None = None) -> ConfigState:
    candidate = str(config_path) if config_path is not None else str(config_path_from_env())
    path = Path(os.path.abspath(os.path.expanduser(candidate)))
    base_defaults = copy.deepcopy(dict(defaults or DEFAULT_CONFIG))
    if not path.exists() and not path.is_symlink():
        return ConfigState(
            path=path,
            exists=False,
            valid_json=True,
            raw={},
            view=deep_merge(base_defaults, {}),
        )
    try:
        info = path.lstat()
        is_symlink = stat.S_ISLNK(info.st_mode)
        mode = stat.S_IMODE(info.st_mode)
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        try:
            info = path.lstat()
            mode = stat.S_IMODE(info.st_mode)
            is_symlink = stat.S_ISLNK(info.st_mode)
        except OSError:
            mode = None
            is_symlink = True
        return ConfigState(
            path=path,
            exists=True,
            valid_json=False,
            raw={},
            view={},
            mode=mode,
            is_symlink=is_symlink,
            error=sanitize_error(f"cannot read configuration: {exc}"),
        )
    try:
        raw = json.loads(text)
    except ValueError as exc:
        return ConfigState(
            path=path,
            exists=True,
            valid_json=False,
            raw={},
            view={},
            text=text,
            mode=mode,
            is_symlink=is_symlink,
            error=sanitize_error(f"configuration is not valid JSON: {exc}"),
        )
    if not isinstance(raw, dict):
        return ConfigState(
            path=path,
            exists=True,
            valid_json=False,
            raw={},
            view={},
            text=text,
            mode=mode,
            is_symlink=is_symlink,
            error="configuration root must be a JSON object",
        )
    return ConfigState(
        path=path,
        exists=True,
        valid_json=True,
        raw=raw,
        view=deep_merge(base_defaults, raw),
        text=text,
        mode=mode,
        is_symlink=is_symlink,
    )


def _normalize_text(value: Any, label: str, errors: list[str], *, maximum: int = 200) -> str:
    if not isinstance(value, str):
        errors.append(f"{label} must be a string")
        return ""
    text = value.strip()
    if not text:
        errors.append(f"{label} must not be empty")
        return ""
    if CONTROL_RE.search(text):
        errors.append(f"{label} contains control characters")
        return ""
    if len(text) > maximum:
        errors.append(f"{label} is longer than {maximum} characters")
        return ""
    return text


def normalize_setup_patch(patch: Mapping[str, Any] | None) -> SetupPatch:
    result = SetupPatch()
    if not patch:
        return result
    if not isinstance(patch, Mapping):
        result.errors.append("configuration patch must be an object")
        return result
    for key in patch:
        key_text = str(key)
        if SECRET_KEY_RE.search(key_text):
            result.errors.append(f"refusing to store a secret-like field in configuration: {key_text}")
            continue
        if any(key_text == prefix or key_text.startswith(prefix + ".") for prefix in FORBIDDEN_PATCH_PREFIXES):
            result.errors.append(f"field is not managed by guided setup: {key_text}")
            continue
        if key_text not in PATCH_FIELDS:
            result.errors.append(f"unsupported guided setup field: {key_text}")
            continue
        value = patch[key]
        if key_text in ("drive.parent_folder_id", "drive.root_folder_id"):
            text = _normalize_text(value, key_text, result.errors, maximum=200)
            if text and not DRIVE_ID_RE.fullmatch(text):
                result.errors.append(f"{key_text} is not a valid Google Drive folder id")
            result.values[key_text] = text
        elif key_text in ("drive.parent_folder_name", "drive.root_folder_name"):
            result.values[key_text] = _normalize_text(value, key_text, result.errors, maximum=200)
        elif key_text == "auto_register_projects":
            if not isinstance(value, bool):
                result.errors.append(f"{key_text} must be true or false")
            else:
                result.values[key_text] = value
        elif key_text == "workspace_roots":
            if not isinstance(value, (list, tuple)):
                result.errors.append("workspace_roots must be a list of paths")
                continue
            roots: list[str] = []
            for item in value:
                text = _normalize_text(item, "workspace_roots entry", result.errors, maximum=4096)
                if not text:
                    continue
                expanded = Path(text).expanduser()
                if not expanded.is_absolute():
                    result.errors.append(f"workspace root must be an absolute path: {text}")
                    continue
                resolved = os.path.realpath(str(expanded))
                if resolved == "/":
                    result.errors.append("workspace root must not be /")
                    continue
                if not os.path.isdir(resolved):
                    result.errors.append(f"workspace root does not exist or is not a directory: {text}")
                    continue
                if resolved not in roots:
                    roots.append(resolved)
            result.values[key_text] = roots
        result.paths.append(key_text)
    if result.errors:
        result.paths = []
    return result


def patch_from_values(
    *,
    drive_parent_folder_id: str | None = None,
    drive_parent_folder_name: str | None = None,
    drive_root_folder_id: str | None = None,
    drive_root_folder_name: str | None = None,
    workspace_roots: list[str] | None = None,
    auto_register_projects: bool | None = None,
) -> dict[str, Any]:
    patch: dict[str, Any] = {}
    if drive_parent_folder_id is not None:
        patch["drive.parent_folder_id"] = drive_parent_folder_id
    if drive_parent_folder_name is not None:
        patch["drive.parent_folder_name"] = drive_parent_folder_name
    if drive_root_folder_id is not None:
        patch["drive.root_folder_id"] = drive_root_folder_id
    if drive_root_folder_name is not None:
        patch["drive.root_folder_name"] = drive_root_folder_name
    if workspace_roots:
        patch["workspace_roots"] = list(workspace_roots)
    if auto_register_projects is not None:
        patch["auto_register_projects"] = auto_register_projects
    return patch


def _set_nested(target: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    current = target
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = value


def _get_nested(source: Any, dotted: str) -> Any:
    current = source
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _proposed_raw(state: ConfigState, patch: SetupPatch, defaults: Mapping[str, Any]) -> dict[str, Any]:
    if state.exists:
        proposed = copy.deepcopy(state.raw)
    else:
        proposed = copy.deepcopy(dict(defaults))
    for dotted in patch.paths:
        value = patch.values[dotted]
        if dotted == "workspace_roots":
            existing = _get_nested(proposed, dotted)
            merged: list[str] = []
            if isinstance(existing, list):
                merged.extend(str(item) for item in existing)
            for item in value:
                if item not in merged:
                    merged.append(item)
            _set_nested(proposed, dotted, merged)
        else:
            _set_nested(proposed, dotted, value)
    return proposed


def config_diff(before: Mapping[str, Any] | None, after: Mapping[str, Any], paths: list[str]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for dotted in sorted(paths):
        old = _get_nested(before or {}, dotted)
        new = _get_nested(after, dotted)
        if old != new:
            changes.append({"path": dotted, "before": old, "after": new})
    return changes


def validate_setup_view(view: Mapping[str, Any], *, check_paths: bool = False) -> dict[str, Any]:
    core_errors, core_warnings = validate_core_config(dict(view), check_paths=check_paths)
    try:
        _dispatch, dispatch_errors, dispatch_warnings = validate_dispatch_config(dict(view), check_paths=check_paths)
    except DispatchConfigError as exc:
        dispatch_errors = [str(exc)]
        dispatch_warnings = []
    try:
        _notifications, notification_errors, notification_warnings = validate_notification_config(dict(view))
    except NotificationConfigError as exc:
        notification_errors = [str(exc)]
        notification_warnings = []
    return {
        "errors": [sanitize_error(item) for item in core_errors + dispatch_errors + notification_errors],
        "warnings": [sanitize_error(item) for item in core_warnings + dispatch_warnings + notification_warnings],
    }


def _is_true(value: Any) -> bool:
    return value is True


def _dispatch_raw(config: Mapping[str, Any]) -> dict[str, Any]:
    raw = config.get("dispatch")
    return raw if isinstance(raw, dict) else {}


def dispatch_enabled(config: Mapping[str, Any]) -> bool:
    return _is_true(_dispatch_raw(config).get("enabled"))


def coding_enabled(config: Mapping[str, Any]) -> bool:
    raw = _dispatch_raw(config)
    coding = raw.get("coding")
    if isinstance(coding, dict) and _is_true(coding.get("enabled")):
        return True
    projects = raw.get("projects")
    if isinstance(projects, dict):
        for value in projects.values():
            if isinstance(value, dict):
                project_coding = value.get("coding")
                if isinstance(project_coding, dict) and _is_true(project_coding.get("enabled")):
                    return True
    return False


def _check(
    check_id: str,
    scope: str,
    status: str,
    summary: str,
    *,
    detail: str | None = None,
    next_action: str | None = None,
    blocking: bool = False,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "scope": scope,
        "status": status,
        "summary": summary,
        "detail": detail,
        "next_action": next_action,
        "blocking": bool(blocking),
    }


def _default_runner(command: list[str], timeout: float) -> tuple[int, str, str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        return int(result.returncode), result.stdout or "", result.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except OSError as exc:
        return 127, "", str(exc)


def _binary_version(binary: str, args: list[str] | None, runner: Callable[[list[str], float], tuple[int, str, str]]) -> tuple[str | None, str | None]:
    path = shutil.which(binary)
    if not path:
        return None, None
    code, stdout, stderr = runner([path, *(args or ["--version"])], 10)
    output = (stdout or stderr or "").strip()
    if code != 0:
        return path, None
    return path, (output.splitlines()[0].strip() if output else "installed")


def _count_verified_capture(config: Mapping[str, Any], *, limit: int = 2000) -> bool:
    try:
        data_root = storage_path(dict(config), "data")
    except (TypeError, ValueError):
        return False
    if not data_root.is_dir():
        return False
    inspected = 0
    for root, dirs, files in os.walk(data_root):
        dirs.sort()
        files.sort()
        if "COMPLETE.json" in files:
            inspected += 1
            payload = read_json(Path(root) / "COMPLETE.json", default=None)
            if isinstance(payload, dict) and _is_true((payload.get("upload") or {}).get("verified")):
                return True
            if inspected >= limit:
                break
    return False


def _drive_remote_state(config: Mapping[str, Any], runner: Callable[[list[str], float], tuple[int, str, str]]) -> dict[str, Any]:
    upload = config.get("upload") if isinstance(config.get("upload"), dict) else {}
    enabled = upload.get("enabled", True) is not False
    remote_name = str((config.get("drive") or {}).get("remote") or "gdrive")
    rclone = shutil.which("rclone")
    if not enabled:
        return {"enabled": False, "rclone": rclone, "config_present": False, "remote_present": False, "remote": remote_name, "error": None}
    if not rclone:
        return {"enabled": True, "rclone": None, "config_present": False, "remote_present": False, "remote": remote_name, "error": "rclone is not installed"}
    config_file = Path(os.environ.get("RCLONE_CONFIG", str(Path.home() / ".config" / "rclone" / "rclone.conf"))).expanduser()
    if not config_file.exists():
        return {"enabled": True, "rclone": rclone, "config_present": False, "remote_present": False, "remote": remote_name, "error": "rclone config is missing"}
    code, stdout, stderr = runner([rclone, "listremotes"], 10)
    if code != 0:
        return {
            "enabled": True,
            "rclone": rclone,
            "config_present": True,
            "remote_present": False,
            "remote": remote_name,
            "error": sanitize_error(stderr or stdout or "rclone listremotes failed"),
        }
    remotes = sorted({line.strip().rstrip(":") for line in stdout.splitlines() if line.strip()})
    return {
        "enabled": True,
        "rclone": rclone,
        "config_present": True,
        "remote_present": remote_name in remotes,
        "remote": remote_name,
        "remotes": remotes,
        "error": None,
    }


def dispatch_service_unit_path(home: Path | None = None) -> Path:
    override = os.environ.get("AGENT_HANDOFF_DISPATCH_UNIT")
    if override:
        return Path(override).expanduser()
    return (home or Path.home()) / ".config" / "systemd" / "user" / DISPATCH_UNIT_NAME


def inspect_setup_recovery(config_path: Path | str | None = None) -> dict[str, Any]:
    path = Path(config_path or CONFIG_PATH)
    journal_path = journal_path_for(path)
    if not journal_path.exists():
        return {"required": False, "status": "none", "journal": str(journal_path), "backup": None, "reason": None}
    journal = read_json(journal_path, default=None)
    if not isinstance(journal, dict):
        return {
            "required": True,
            "status": "unreadable",
            "journal": str(journal_path),
            "backup": None,
            "reason": "setup journal is not valid JSON; manual review is required",
        }
    before = journal.get("before_sha256")
    after = journal.get("after_sha256")
    backup = journal.get("backup")
    try:
        current_text = path.read_text(encoding="utf-8") if path.exists() else None
    except OSError as exc:
        return {
            "required": True,
            "status": "unreadable",
            "journal": str(journal_path),
            "backup": backup,
            "reason": sanitize_error(f"cannot read configuration during recovery inspection: {exc}"),
        }
    current = sha256_text(current_text) if current_text is not None else None
    if current is not None and current == after:
        status = "complete_cleanup_required"
        reason = "the proposed configuration was written; finalize by removing the setup journal"
    elif current == before:
        status = "rollback_cleanup_required"
        reason = "the previous configuration is already present; finalize by removing the setup journal"
    else:
        status = "ambiguous"
        reason = "configuration does not match either recorded state; manual review is required"
    return {
        "required": True,
        "status": status,
        "journal": str(journal_path),
        "backup": backup if isinstance(backup, str) else None,
        "reason": reason,
    }


def recover_setup(config_path: Path | str | None = None, *, approve: bool = False) -> dict[str, Any]:
    if not approve:
        raise SetupApprovalError("setup recovery requires explicit approval (--approve)")
    info = inspect_setup_recovery(config_path)
    if not info.get("required"):
        return {"ok": True, "changed": False, "recovery": info}
    if info.get("status") not in ("complete_cleanup_required", "rollback_cleanup_required"):
        raise SetupRecoveryError(
            f"setup recovery cannot proceed automatically ({info.get('status')}): {info.get('reason')}"
        )
    journal = Path(info["journal"])
    try:
        journal.unlink()
    except FileNotFoundError:
        pass
    return {"ok": True, "changed": True, "recovery": info}


def detect_environment(
    state: ConfigState,
    *,
    base_dir: Path | None = None,
    home: Path | None = None,
    runner: Callable[[list[str], float], tuple[int, str, str]] | None = None,
    platform_system: str | None = None,
    version_info: tuple[int, ...] | None = None,
    verified_capture: bool | None = None,
) -> dict[str, Any]:
    runner = runner or _default_runner
    base = Path(base_dir or BASE_DIR)
    system = platform_system or platform.system()
    python_version = version_info or sys.version_info[:3]
    checks: list[dict[str, Any]] = []
    config = state.view if state.valid_json else {}
    dispatch_is_enabled = dispatch_enabled(config) if config else False

    linux_ready = system == "Linux"
    checks.append(
        _check(
            "os.compatibility",
            "core",
            STATUS_READY if linux_ready else STATUS_BLOCKED,
            "Operating system is supported" if linux_ready else "Operating system is not supported",
            detail=system,
            next_action=None if linux_ready else "Use a Linux host. Other operating systems are not verified for dispatch or coding.",
            blocking=not linux_ready,
        )
    )

    python_ok = tuple(python_version) >= (3, 10)
    checks.append(
        _check(
            "python.version",
            "core",
            STATUS_READY if python_ok else STATUS_BLOCKED,
            "Python version is supported" if python_ok else "Python is too old",
            detail=".".join(str(part) for part in python_version),
            next_action=None if python_ok else "Install Python 3.10 or newer.",
            blocking=not python_ok,
        )
    )

    git_path, git_version = _binary_version("git", ["--version"], runner)
    checks.append(
        _check(
            "git",
            "install",
            STATUS_READY if git_path else STATUS_MISSING,
            "git is available" if git_path else "git is missing",
            detail=git_version or (str(git_path) if git_path else None),
            next_action=None if git_path else "Install git to clone, upgrade, or roll back the source checkout.",
            blocking=False,
        )
    )

    rclone_path, rclone_version = _binary_version("rclone", ["version"], runner)
    checks.append(
        _check(
            "rclone",
            "core",
            STATUS_READY if rclone_path else STATUS_MISSING,
            "rclone is available" if rclone_path else "rclone is missing",
            detail=rclone_version or (str(rclone_path) if rclone_path else None),
            next_action=None if rclone_path else "Install rclone. It is required for upload-verified reports.",
            blocking=False,
        )
    )

    gh_path, gh_version = _binary_version("gh", ["--version"], runner)
    if not gh_path:
        checks.append(
            _check(
                "github.cli",
                "dispatch",
                STATUS_OPTIONAL,
                "GitHub CLI is not installed; dispatch is optional",
                next_action="Install GitHub CLI only if you want the optional Issue dispatcher.",
                blocking=False,
            )
        )
    else:
        checks.append(
            _check(
                "github.cli",
                "dispatch",
                STATUS_READY,
                "GitHub CLI is available",
                detail=gh_version or str(gh_path),
                blocking=False,
            )
        )
    if not gh_path:
        checks.append(
            _check(
                "github.auth",
                "dispatch",
                STATUS_OPTIONAL,
                "GitHub authorization is not available; dispatch is optional",
                next_action="Run gh auth login after installing GitHub CLI if you want dispatch.",
                blocking=False,
            )
        )
    else:
        code, _stdout, _stderr = runner([gh_path, "auth", "status"], 10)
        if code == 0:
            checks.append(
                _check(
                    "github.auth",
                    "dispatch",
                    STATUS_READY,
                    "GitHub CLI is authenticated",
                    detail="token value is never read or displayed",
                    blocking=False,
                )
            )
        else:
            checks.append(
                _check(
                    "github.auth",
                    "dispatch",
                    STATUS_MISSING,
                    "GitHub CLI is not authenticated",
                    detail="gh auth status failed; its output and token are not shown",
                    next_action="Run gh auth login with your own account before enabling dispatch.",
                    blocking=dispatch_is_enabled,
                )
            )

    opencode_path, opencode_version = _binary_version("opencode", ["--version"], runner)
    checks.append(
        _check(
            "opencode",
            "capture",
            STATUS_READY if opencode_path else STATUS_OPTIONAL,
            "OpenCode is available" if opencode_path else "OpenCode is not installed; automatic capture and dispatch are optional",
            detail=opencode_version or (str(opencode_path) if opencode_path else None),
            next_action=None if opencode_path else "Install OpenCode if you want automatic capture or dispatch.",
            blocking=False,
        )
    )

    manifest = base / ".agent-handoff-manifest.json"
    installed = manifest.is_file() and (base / "bin" / "agent-handoff").is_file()
    checks.append(
        _check(
            "agent_handoff.installation",
            "core",
            STATUS_READY if installed else STATUS_MISSING,
            "Agent Handoff installation detected" if installed else "Agent Handoff is not installed at the expected home",
            detail=str(base),
            next_action=None
            if installed
            else "Run the installer from a trusted source checkout with an explicit --home.",
            blocking=False,
        )
    )

    if not state.exists:
        checks.append(
            _check(
                "config.file",
                "core",
                STATUS_MISSING,
                "Runtime configuration does not exist yet",
                detail=str(state.path),
                next_action="Run agent-handoff setup plan, then apply an approved configuration step.",
                blocking=False,
            )
        )
    elif state.is_symlink:
        checks.append(
            _check(
                "config.file",
                "core",
                STATUS_BLOCKED,
                "Runtime configuration is a symbolic link",
                detail=str(state.path),
                next_action="Replace the symlink with a regular mode-600 file before using guided setup.",
                blocking=True,
            )
        )
    else:
        checks.append(
            _check(
                "config.file",
                "core",
                STATUS_READY,
                "Runtime configuration exists",
                detail=f"{state.path} (mode {oct(state.mode or 0)})",
                blocking=False,
            )
        )

    if not state.exists:
        checks.append(
            _check(
                "config.json",
                "core",
                STATUS_MISSING,
                "Configuration JSON is not present",
                next_action="Apply an approved setup configuration step to initialize it.",
                blocking=False,
            )
        )
    elif not state.valid_json:
        checks.append(
            _check(
                "config.json",
                "core",
                STATUS_BLOCKED,
                "Configuration is invalid",
                detail=state.error,
                next_action="Fix or restore the configuration file. Guided setup will not overwrite an invalid file.",
                blocking=True,
            )
        )
    else:
        checks.append(
            _check(
                "config.json",
                "core",
                STATUS_READY,
                "Configuration JSON is valid",
                blocking=False,
            )
        )

    validation = {"errors": [], "warnings": []}
    if state.exists and state.valid_json:
        validation = validate_setup_view(state.view, check_paths=False)
        if validation["errors"]:
            checks.append(
                _check(
                    "config.validation",
                    "core",
                    STATUS_BLOCKED,
                    "Configuration validation failed",
                    detail="; ".join(validation["errors"]),
                    next_action="Fix the reported configuration errors before applying guided setup changes.",
                    blocking=True,
                )
            )
        else:
            detail = f"{len(validation['warnings'])} warning(s)" if validation["warnings"] else None
            checks.append(
                _check(
                    "config.validation",
                    "core",
                    STATUS_READY,
                    "Configuration validation passed",
                    detail=detail,
                    blocking=False,
                )
            )
    else:
        checks.append(
            _check(
                "config.validation",
                "core",
                STATUS_MISSING if not state.exists else STATUS_BLOCKED,
                "Configuration validation is pending" if not state.exists else "Configuration validation is unavailable",
                blocking=False,
            )
        )

    drive = _drive_remote_state(config, runner) if config else {
        "enabled": True,
        "rclone": rclone_path,
        "config_present": False,
        "remote_present": False,
        "remote": "gdrive",
        "error": "configuration is not available",
    }
    if not drive["enabled"]:
        checks.append(
            _check(
                "drive.authorization",
                "core",
                STATUS_OPTIONAL,
                "Upload is disabled in configuration",
                detail="Reports will remain local.",
                blocking=False,
            )
        )
    elif drive["remote_present"]:
        checks.append(
            _check(
                "drive.authorization",
                "core",
                STATUS_READY,
                "Google Drive remote is configured",
                detail=f"remote {drive['remote']} (authorization is verified without printing tokens)",
                blocking=False,
            )
        )
    else:
        checks.append(
            _check(
                "drive.authorization",
                "core",
                STATUS_MISSING,
                "Google Drive is not authorized",
                detail=drive.get("error") or f"remote {drive['remote']} is not configured",
                next_action="Run agent-handoff auth, or import a mode-600 token file produced by rclone authorize \"drive\".",
                blocking=False,
            )
        )

    projects = []
    if state.valid_json:
        from .config import all_projects

        projects = all_projects(state.view)
    projects_with_drive = [item for item in projects if str(item.get("drive_folder_id") or "")]
    if not projects:
        checks.append(
            _check(
                "projects.mapping",
                "core",
                STATUS_MISSING,
                "No project mappings are configured",
                next_action="Run agent-handoff project add after Drive authorization.",
                blocking=False,
            )
        )
    elif not projects_with_drive:
        checks.append(
            _check(
                "projects.mapping",
                "core",
                STATUS_MISSING,
                "Projects exist but none has a Drive folder id",
                detail=f"{len(projects)} project(s) configured",
                next_action="Run agent-handoff project add to create or verify a Drive folder for one project.",
                blocking=False,
            )
        )
    else:
        checks.append(
            _check(
                "projects.mapping",
                "core",
                STATUS_READY,
                "At least one project has a Drive folder id",
                detail=f"{len(projects)} project(s) configured; {len(projects_with_drive)} ready for upload",
                blocking=False,
            )
        )

    dispatch = _dispatch_raw(config)
    unit_path = dispatch_service_unit_path(home)
    unit_present = unit_path.is_file()
    if not dispatch_is_enabled:
        checks.append(
            _check(
                "dispatcher.service",
                "dispatch",
                STATUS_OPTIONAL,
                "Dispatcher service is disabled (optional)",
                detail="Core capture does not need the dispatcher.",
                blocking=False,
            )
        )
    else:
        status = STATUS_READY if unit_present else STATUS_MISSING
        checks.append(
            _check(
                "dispatcher.service",
                "dispatch",
                status,
                "Dispatcher service unit file is present" if unit_present else "Dispatcher is enabled but no user service unit file was found",
                detail=str(unit_path),
                next_action=None if unit_present else "Run dispatch manually, or install a systemd user service only with explicit owner consent.",
                blocking=False,
            )
        )

    coding_is_enabled = coding_enabled(config) if config else False
    checks.append(
        _check(
            "coding.policy",
            "coding",
            STATUS_OPTIONAL,
            "Coding policy is configured; guided setup does not change it" if coding_is_enabled else "Coding is disabled (safe default)",
            next_action=None if coding_is_enabled else "Leave coding disabled until a separate owner-approved acceptance.",
            blocking=False,
        )
    )

    recovery = inspect_setup_recovery(state.path)
    if recovery.get("required"):
        checks.append(
            _check(
                "setup.recovery",
                "core",
                STATUS_BLOCKED,
                "Recovery is required from an interrupted setup",
                detail=recovery.get("reason"),
                next_action="Run agent-handoff setup recover --approve --json after reviewing the configuration.",
                blocking=True,
            )
        )
    else:
        checks.append(
            _check(
                "setup.recovery",
                "core",
                STATUS_READY,
                "No interrupted setup was detected",
                blocking=False,
            )
        )

    if verified_capture is None:
        verified_capture = _count_verified_capture(config) if config else False

    return {
        "checks": checks,
        "versions": {
            "python": ".".join(str(part) for part in python_version),
            "git": git_version,
            "rclone": rclone_version,
            "gh": gh_version,
            "opencode": opencode_version,
        },
        "platform": system,
        "state": {
            "config_exists": state.exists,
            "config_valid": state.exists and state.valid_json and not validation["errors"],
            "config_mode": oct(state.mode) if state.mode is not None else None,
            "drive_remote_present": bool(drive.get("remote_present")),
            "drive_remote": drive.get("remote"),
            "upload_enabled": bool(drive.get("enabled")),
            "project_count": len(projects),
            "projects_with_drive": len(projects_with_drive),
            "verified_capture": bool(verified_capture),
            "dispatch_enabled": dispatch_is_enabled,
            "dispatch_service_unit": unit_present,
            "coding_enabled": coding_is_enabled,
        },
        "recovery": recovery,
    }


def _check_status(checks: list[dict[str, Any]], check_id: str) -> str:
    for check in checks:
        if check["id"] == check_id:
            return str(check["status"])
    return STATUS_MISSING


def _step(
    step_id: str,
    title: str,
    scope: str,
    status: str,
    *,
    description: str,
    command: list[str] | None = None,
    required: bool = True,
    automated: bool = False,
    requires_approval: bool = False,
    manual_input_required: bool = False,
    mutates: str = "none",
) -> dict[str, Any]:
    return {
        "id": step_id,
        "title": title,
        "scope": scope,
        "status": status,
        "required": required,
        "description": description,
        "command": command,
        "automated": automated,
        "requires_approval": requires_approval,
        "manual_input_required": manual_input_required,
        "mutates": mutates,
    }


def _map_status(check_status: str) -> str:
    if check_status == STATUS_READY:
        return STEP_COMPLETE
    if check_status == STATUS_BLOCKED:
        return STEP_BLOCKED
    if check_status == STATUS_OPTIONAL:
        return STEP_OPTIONAL
    return STEP_PENDING


def build_setup_plan(
    state: ConfigState | None = None,
    *,
    config_path: Path | str | None = None,
    patch: Mapping[str, Any] | None = None,
    defaults: Mapping[str, Any] | None = None,
    base_dir: Path | None = None,
    home: Path | None = None,
    runner: Callable[[list[str], float], tuple[int, str, str]] | None = None,
    platform_system: str | None = None,
    version_info: tuple[int, ...] | None = None,
    verified_capture: bool | None = None,
) -> dict[str, Any]:
    base_defaults = copy.deepcopy(dict(defaults or DEFAULT_CONFIG))
    state = state or read_config_state(config_path, defaults=base_defaults)
    normalized_patch = normalize_setup_patch(patch)
    environment = detect_environment(
        state,
        base_dir=base_dir,
        home=home,
        runner=runner,
        platform_system=platform_system,
        version_info=version_info,
        verified_capture=verified_capture,
    )
    checks = environment["checks"]
    state_info = environment["state"]

    proposed_raw: dict[str, Any] | None = None
    changes: list[dict[str, Any]] = []
    config_errors: list[str] = []
    config_warnings: list[str] = []
    state_errors: list[str] = []
    if state.exists and not state.valid_json:
        state_errors = [state.error or "configuration is not valid JSON"]
    elif state.exists and state.is_symlink:
        state_errors = ["refusing to overwrite a symbolic-link configuration file"]
    config_errors.extend(state_errors)
    if state.exists and state.valid_json:
        proposed_raw = _proposed_raw(state, normalized_patch, base_defaults)
        if normalized_patch.valid:
            changes = config_diff(state.raw, proposed_raw, normalized_patch.paths)
            if changes:
                validation = validate_setup_view(deep_merge(base_defaults, proposed_raw), check_paths=False)
                config_errors = state_errors + validation["errors"]
                config_warnings = validation["warnings"]
        else:
            config_errors = state_errors + list(normalized_patch.errors)
    elif not state.exists and normalized_patch.requested:
        proposed_raw = _proposed_raw(state, normalized_patch, base_defaults)
        if normalized_patch.valid:
            changes = config_diff({}, proposed_raw, normalized_patch.paths)
            validation = validate_setup_view(deep_merge(base_defaults, proposed_raw), check_paths=False)
            config_errors = validation["errors"]
            config_warnings = validation["warnings"]
        else:
            config_errors = list(normalized_patch.errors)

    changes_required = bool(changes) and normalized_patch.valid and not config_errors
    if normalized_patch.errors and not config_errors:
        config_errors = list(normalized_patch.errors)

    steps: list[dict[str, Any]] = []
    install_status = _map_status(_check_status(checks, "agent_handoff.installation"))
    code_root = Path(__file__).resolve().parents[2]
    install_script = code_root / "setup" / "install.py"
    if not install_script.is_file():
        install_script = Path(base_dir or BASE_DIR) / "setup" / "install.py"
    steps.append(
        _step(
            "install.source",
            "Install or refresh Agent Handoff",
            "install",
            install_status,
            description="Synchronize managed source files and the PATH link. Runtime data and configuration are preserved.",
            command=[sys.executable, str(install_script), "--home", str(home or "~/agent-handoff")],
            required=True,
            automated=True,
            mutates="host",
        )
    )

    config_check = _check_status(checks, "config.json")
    config_validation_blocked = _check_status(checks, "config.validation") == STATUS_BLOCKED
    if state.exists and state.is_symlink:
        config_step_status = STEP_BLOCKED
    elif state.exists and state.valid_json:
        if changes_required:
            config_step_status = STEP_PENDING
        elif config_errors or config_validation_blocked:
            config_step_status = STEP_BLOCKED
        else:
            config_step_status = STEP_COMPLETE
    else:
        config_step_status = _map_status(config_check)
    config_command = ["agent-handoff", "setup", "apply", "--approve", "--json"]
    if normalized_patch.requested:
        flag_for_path = {
            "drive.parent_folder_id": "--drive-parent-folder-id",
            "drive.parent_folder_name": "--drive-parent-folder-name",
            "drive.root_folder_id": "--drive-root-folder-id",
            "drive.root_folder_name": "--drive-root-folder-name",
        }
        for path in normalized_patch.paths:
            value = normalized_patch.values.get(path)
            if path == "workspace_roots" and isinstance(value, list):
                for root in value:
                    config_command.extend(["--workspace-root", str(root)])
            elif path == "auto_register_projects":
                config_command.append("--auto-register-projects" if value is True else "--no-auto-register-projects")
            elif path in flag_for_path:
                config_command.extend([flag_for_path[path], str(value)])
    else:
        config_command.extend(["--drive-root-folder-id", "<ROOT_FOLDER_ID>"])
    steps.append(
        _step(
            "config.write",
            "Write validated runtime configuration",
            "core",
            config_step_status,
            description="Preview a narrowly scoped change, validate it with the existing config contracts, back up the previous file, and write atomically with mode 600.",
            command=config_command,
            required=True,
            automated=True,
            requires_approval=True,
            manual_input_required=not normalized_patch.requested,
            mutates="config",
        )
    )

    drive_root = ""
    if state.valid_json:
        drive_root = str(((state.view.get("drive") or {}).get("root_folder_id")) or "")
    root_complete = bool(drive_root and DRIVE_ID_RE.fullmatch(drive_root))
    steps.append(
        _step(
            "drive.root",
            "Configure the Drive reports root folder id",
            "core",
            STEP_COMPLETE if root_complete else STEP_PENDING,
            description="The reports root folder id is required before uploads and project creation can be verified.",
            command=["agent-handoff", "setup", "apply", "--approve", "--drive-root-folder-id", "<ROOT_FOLDER_ID>", "--json"],
            required=True,
            automated=True,
            requires_approval=True,
            manual_input_required=True,
            mutates="config",
        )
    )

    drive_status = _map_status(_check_status(checks, "drive.authorization"))
    steps.append(
        _step(
            "drive.authorize",
            "Authorize Google Drive",
            "core",
            drive_status,
            description="Authorize rclone with the account that owns the reports folder. The token is stored outside runtime configuration and is never printed.",
            command=["agent-handoff", "auth"],
            required=True,
            automated=False,
            requires_approval=True,
            mutates="credentials",
        )
    )

    project_complete = int(state_info["projects_with_drive"]) > 0
    steps.append(
        _step(
            "project.add",
            "Add one project mapping",
            "core",
            STEP_COMPLETE if project_complete else STEP_PENDING,
            description="Create or verify one Drive project folder and record its local workspace path.",
            command=["agent-handoff", "project", "add", "--name", "<PROJECT_NAME>", "--path", "<PROJECT_PATH>"],
            required=True,
            automated=False,
            requires_approval=True,
            manual_input_required=True,
            mutates="external",
        )
    )

    capture_complete = bool(state_info["verified_capture"])
    steps.append(
        _step(
            "verify.capture",
            "Prove one upload-verified capture",
            "core",
            STEP_COMPLETE if capture_complete else STEP_PENDING,
            description="A setup is not verified until at least one report shows upload.verified=true after MD5/read-back verification.",
            command=["agent-handoff", "doctor", "--json"],
            required=True,
            automated=False,
            requires_approval=True,
            mutates="external",
        )
    )

    if state_info["dispatch_enabled"]:
        dispatch_status = STEP_COMPLETE if not any(
            item["blocking"] and item["scope"] == "dispatch" for item in checks
        ) else STEP_BLOCKED
    else:
        dispatch_status = STEP_OPTIONAL
    steps.append(
        _step(
            "dispatch.configure",
            "Configure the optional private Issue dispatcher",
            "dispatch",
            dispatch_status,
            description="Owner-only step. Guided setup never enables the dispatcher or a service automatically.",
            command=["agent-handoff", "config", "validate", "--json"],
            required=False,
            automated=False,
            requires_approval=True,
            manual_input_required=True,
            mutates="config",
        )
    )
    steps.append(
        _step(
            "dispatch.verify",
            "Verify one dispatcher request",
            "dispatch",
            STEP_OPTIONAL if not state_info["dispatch_enabled"] else STEP_PENDING,
            description="Submit a request only after dispatch configuration and GitHub authorization are valid.",
            command=["agent-handoff", "dispatch", "run", "--once", "--json"],
            required=False,
            automated=False,
            requires_approval=True,
            mutates="external",
        )
    )
    steps.append(
        _step(
            "notifications.configure",
            "Configure optional Telegram notifications",
            "dispatch",
            STEP_OPTIONAL,
            description="Remains disabled unless the owner explicitly configures it. Credentials live only in an environment variable.",
            command=["agent-handoff", "config", "validate", "--json"],
            required=False,
            automated=False,
            requires_approval=True,
            manual_input_required=True,
            mutates="config",
        )
    )
    steps.append(
        _step(
            "coding.review",
            "Review optional coding policy separately",
            "coding",
            STEP_OPTIONAL,
            description="P1-B1 never enables coding, workspace_write, isolation, or a dedicated execution identity.",
            command=None,
            required=False,
            automated=False,
            requires_approval=True,
            manual_input_required=True,
            mutates="none",
        )
    )

    blockers: list[dict[str, Any]] = []
    for check in checks:
        if check["blocking"]:
            blockers.append(
                {
                    "id": check["id"],
                    "scope": check["scope"],
                    "summary": check["summary"],
                    "detail": check.get("detail"),
                    "next_action": check.get("next_action"),
                }
            )
    if config_errors and (normalized_patch.requested or not state.valid_json):
        blockers.append(
            {
                "id": "config.patch_invalid",
                "scope": "core",
                "summary": "Proposed configuration is invalid",
                "detail": "; ".join(config_errors),
                "next_action": "Correct the requested values or restore a valid configuration file.",
            }
        )

    required_steps = [step for step in steps if step["required"]]
    pending_required = [step["id"] for step in required_steps if step["status"] == STEP_PENDING]
    blocked_required = [step["id"] for step in required_steps if step["status"] == STEP_BLOCKED]
    completed_steps = [step["id"] for step in steps if step["status"] == STEP_COMPLETE]
    optional_steps = [step["id"] for step in steps if not step["required"]]
    core_blockers = [item for item in blockers if item["scope"] in ("core", "install")]
    core_ready = not core_blockers
    all_ready = not blockers

    if blockers:
        plan_status = "blocked"
    elif pending_required:
        plan_status = "action_required"
    else:
        plan_status = "ready"

    next_actions = [
        {
            "step": step["id"],
            "command": step["command"],
            "requires_approval": step["requires_approval"],
            "manual_input_required": step["manual_input_required"],
        }
        for step in required_steps
        if step["status"] == STEP_PENDING
    ]

    return {
        "schema_version": SETUP_SCHEMA_VERSION,
        "command": "setup.plan",
        "status": plan_status,
        "ok": core_ready,
        "core_ready": core_ready,
        "all_scopes_ready": all_ready,
        "platform": environment["platform"],
        "versions": environment["versions"],
        "installation": {
            "installed": _check_status(checks, "agent_handoff.installation") == STATUS_READY,
            "home": str(Path(base_dir or BASE_DIR)),
            "config_path": str(state.path),
            "config_exists": state.exists,
            "config_valid": bool(state_info["config_valid"]),
            "config_mode": state_info["config_mode"],
        },
        "existing_installation": _check_status(checks, "agent_handoff.installation") == STATUS_READY,
        "state": state_info,
        "checks": checks,
        "steps": steps,
        "completed_steps": completed_steps,
        "remaining_required_steps": pending_required,
        "blocked_required_steps": blocked_required,
        "optional_steps": optional_steps,
        "blockers": blockers,
        "next_actions": next_actions,
        "recovery": environment["recovery"],
        "config_plan": {
            "requested": normalized_patch.requested,
            "field_paths": list(normalized_patch.paths),
            "changes_required": changes_required,
            "would_mutate_config": changes_required,
            "requires_approval": changes_required,
            "changes": changes,
            "errors": config_errors,
            "warnings": config_warnings,
            "backup_required": changes_required and state.exists,
            "atomic_write": True,
            "forbidden_activations": list(FORBIDDEN_ACTIVATIONS),
        },
        "approval_required": changes_required,
    }


def _validate_patch_activation_boundaries(
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
) -> None:
    for field_name in ("dispatch", "notifications", "security", "projects", "storage", "integrations", "upload"):
        before_value = before.get(field_name) if isinstance(before, Mapping) else None
        after_value = after.get(field_name) if isinstance(after, Mapping) else None
        if before_value != after_value:
            raise SetupValidationError(f"guided setup must not modify {field_name} configuration")


def _atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    ensure_dir(path.parent, 0o700)
    import tempfile
    import contextlib

    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def apply_setup_patch(
    config_path: Path | str | None = None,
    *,
    patch: Mapping[str, Any] | None = None,
    defaults: Mapping[str, Any] | None = None,
    approve: bool = False,
    post_write_validator: Callable[[Mapping[str, Any]], None] | None = None,
    config_lock: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    if not approve:
        raise SetupApprovalError("configuration changes require explicit approval (--approve)")
    path = Path(config_path or CONFIG_PATH)
    base_defaults = copy.deepcopy(dict(defaults or DEFAULT_CONFIG))
    lock_context = config_lock
    if lock_context is None:
        from .config import config_lock as lock_context  # type: ignore[assignment]

    with lock_context():
        recovery = inspect_setup_recovery(path)
        if recovery.get("required"):
            if recovery.get("status") in ("complete_cleanup_required", "rollback_cleanup_required"):
                recover_setup(path, approve=True)
            else:
                raise SetupRecoveryError(
                    f"setup recovery must be completed first ({recovery.get('status')}): {recovery.get('reason')}"
                )
        state = read_config_state(path, defaults=base_defaults)
        if state.exists and state.is_symlink:
            raise SetupValidationError("refusing to overwrite a symbolic-link configuration file")
        if not state.valid_json:
            raise SetupValidationError(state.error or "configuration is not valid JSON")
        normalized = normalize_setup_patch(patch)
        if not normalized.valid:
            raise SetupValidationError("; ".join(normalized.errors))
        if not normalized.requested:
            return {
                "ok": True,
                "changed": False,
                "config": str(path),
                "backup": None,
                "changes": [],
                "recovery": None,
                "message": "no configuration changes were requested",
            }
        proposed = _proposed_raw(state, normalized, base_defaults)
        if state.exists:
            _validate_patch_activation_boundaries(state.raw, proposed)
        proposed_view = deep_merge(base_defaults, proposed)
        validation = validate_setup_view(proposed_view, check_paths=False)
        if validation["errors"]:
            raise SetupValidationError("; ".join(validation["errors"]))
        changes = config_diff(state.raw if state.exists else {}, proposed, normalized.paths)
        if state.exists and proposed == state.raw:
            return {
                "ok": True,
                "changed": False,
                "config": str(path),
                "backup": None,
                "changes": [],
                "recovery": None,
                "message": "configuration already matches the requested setup values",
            }

        previous_text = state.text if state.exists else None
        previous_sha = sha256_text(previous_text) if previous_text is not None else None
        new_text = json.dumps(proposed, indent=2, ensure_ascii=False) + "\n"
        new_sha = sha256_text(new_text)
        stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
        backup_path: Path | None = None
        if state.exists:
            backup_path = path.with_name(f"{path.name}{BACKUP_MARKER}{stamp}-{os.getpid()}")
            try:
                _atomic_write_bytes(backup_path, previous_text.encode("utf-8"), 0o600)
            except OSError as exc:
                raise SetupError(f"cannot create configuration backup: {sanitize_error(str(exc))}")
        journal_path = journal_path_for(path)
        journal = {
            "schema_version": SETUP_SCHEMA_VERSION,
            "started_at": utcnow_iso(),
            "config_path": str(path),
            "backup": str(backup_path) if backup_path else None,
            "before_sha256": previous_sha,
            "after_sha256": new_sha,
        }
        try:
            atomic_write_json(journal_path, journal, mode=0o600)
        except OSError as exc:
            raise SetupError(f"cannot create setup recovery journal: {sanitize_error(str(exc))}")
        try:
            atomic_write_text(path, new_text, mode=0o600)
            written = read_config_state(path, defaults=base_defaults)
            written_view = deep_merge(base_defaults, written.raw) if written.valid_json else {}
            if not written.valid_json or sha256_text(path.read_text(encoding="utf-8")) != new_sha:
                raise SetupValidationError("written configuration did not round-trip to the proposed content")
            revalidation = validate_setup_view(written_view, check_paths=False)
            if revalidation["errors"]:
                raise SetupValidationError("; ".join(revalidation["errors"]))
            if post_write_validator is not None:
                post_write_validator(written_view)
        except BaseException as exc:
            restore_error = None
            try:
                if previous_text is not None:
                    atomic_write_text(path, previous_text, mode=0o600)
                else:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
            except OSError as restore_exc:
                restore_error = sanitize_error(str(restore_exc))
            try:
                journal_path.unlink()
            except OSError:
                pass
            if restore_error:
                raise SetupError(
                    f"configuration apply failed ({sanitize_error(str(exc))}) and rollback also failed ({restore_error}); "
                    f"backup: {backup_path}. Run agent-handoff setup plan --json before retrying."
                )
            if isinstance(exc, SetupError):
                raise
            raise SetupError(f"configuration apply failed and was rolled back: {sanitize_error(str(exc))}")
        journal_cleanup_error = None
        try:
            journal_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            journal_cleanup_error = sanitize_error(str(exc))
        return {
            "ok": True,
            "changed": True,
            "config": str(path),
            "backup": str(backup_path) if backup_path else None,
            "changes": changes,
            "warnings": validation["warnings"],
            "recovery": None if not journal_cleanup_error else {"required": True, "status": "complete_cleanup_required"},
            "journal_cleanup_error": journal_cleanup_error,
            "atomic_write": True,
            "mode": "600",
        }


def format_setup_plan(plan: Mapping[str, Any]) -> str:
    lines: list[str] = []
    lines.append("Agent Handoff guided setup")
    lines.append(f"Platform: {plan.get('platform')} · Python {plan.get('versions', {}).get('python')}")
    installation = plan.get("installation") or {}
    if plan.get("existing_installation"):
        lines.append(f"Existing installation detected: {installation.get('home')}")
    else:
        lines.append(f"Installation not detected at: {installation.get('home')}")
    lines.append(f"Configuration: {installation.get('config_path')}")
    lines.append("")
    lines.append("Environment checks:")
    for check in plan.get("checks") or []:
        label = {
            STATUS_READY: "READY",
            STATUS_MISSING: "MISSING",
            STATUS_OPTIONAL: "OPTIONAL",
            STATUS_BLOCKED: "BLOCKED",
        }.get(check["status"], check["status"].upper())
        detail = f" — {check['detail']}" if check.get("detail") else ""
        lines.append(f"[{label:8}] ({check['scope']}) {check['summary']}{detail}")
        if check.get("next_action") and check["status"] != STATUS_READY:
            lines.append(f"           Next: {check['next_action']}")
    lines.append("")
    lines.append("Setup steps:")
    for step in plan.get("steps") or []:
        label = {
            STEP_COMPLETE: "COMPLETE",
            STEP_PENDING: "PENDING",
            STEP_OPTIONAL: "OPTIONAL",
            STEP_BLOCKED: "BLOCKED",
        }.get(step["status"], step["status"].upper())
        lines.append(f"[{label:8}] {step['title']}")
        lines.append(f"           {step['description']}")
        if step.get("command"):
            lines.append(f"           Command: {' '.join(str(item) for item in step['command'])}")
        if step.get("status") == STEP_PENDING and step.get("requires_approval"):
            lines.append("           Approval required before this step changes the host or configuration.")
    config_plan = plan.get("config_plan") or {}
    if config_plan.get("errors"):
        lines.append("")
        lines.append("Invalid configuration proposal:")
        for error in config_plan["errors"]:
            lines.append(f"[FAIL] {error}")
    if config_plan.get("changes"):
        lines.append("")
        lines.append("Proposed configuration changes (approval required):")
        for change in config_plan["changes"]:
            lines.append(f"  {change['path']}: {change['before']!r} -> {change['after']!r}")
    elif config_plan.get("requested"):
        lines.append("")
        lines.append("No configuration changes would be written; the requested values already match.")
    else:
        lines.append("")
        lines.append("No configuration changes are proposed by this plan.")
    if plan.get("recovery", {}).get("required"):
        lines.append("")
        lines.append("Recovery required:")
        lines.append(f"  status: {plan['recovery'].get('status')}")
        lines.append(f"  reason: {plan['recovery'].get('reason')}")
        lines.append("  command: agent-handoff setup recover --approve --json")
    blockers = plan.get("blockers") or []
    if blockers:
        lines.append("")
        lines.append("Blockers:")
        for blocker in blockers:
            lines.append(f"[BLOCKED] ({blocker['scope']}) {blocker['summary']}")
            if blocker.get("detail"):
                lines.append(f"           {blocker['detail']}")
            if blocker.get("next_action"):
                lines.append(f"           Next: {blocker['next_action']}")
    lines.append("")
    if plan.get("status") == "ready":
        lines.append("Completed verification: all required setup steps are complete.")
    elif plan.get("status") == "action_required":
        lines.append("Ready for guided setup: some required steps remain.")
    else:
        lines.append("Setup is blocked until the reported issues are resolved.")
    if plan.get("next_actions"):
        lines.append("")
        lines.append("Required next actions:")
        for index, action in enumerate(plan["next_actions"], start=1):
            command = " ".join(str(item) for item in action["command"]) if action.get("command") else "(manual review)"
            lines.append(f"  {index}. {command}")
            if action.get("requires_approval"):
                lines.append("     Approval required.")
            if action.get("manual_input_required"):
                lines.append("     Replace placeholder values with your own host values.")
    return "\n".join(lines)
