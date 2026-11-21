from __future__ import annotations

import fnmatch
import os
import re
from typing import Any, Iterable, Sequence

from ..security import WorkspaceSecurityError, canonical_path

REPOSITORY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")
MODEL_ALIAS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,39}/$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
PROFILE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
DEFAULT_LIMITS = {
    "test_timeout_seconds": 900,
    "max_files_changed": 25,
    "max_diff_bytes": 500_000,
    "max_file_bytes": 200_000,
    "max_output_bytes": 50_000,
    "max_workspace_bytes": 2_000_000_000,
    "max_coding_tasks": 1,
    "max_publication_attempts": 3,
}
LIMIT_BOUNDS = {
    "test_timeout_seconds": (1, 7200, 900),
    "max_files_changed": (1, 500, 25),
    "max_diff_bytes": (1, 5_000_000, 500_000),
    "max_file_bytes": (1, 5_000_000, 200_000),
    "max_output_bytes": (1000, 1_000_000, 50_000),
    "max_workspace_bytes": (1_000_000, 20_000_000_000, 2_000_000_000),
    "max_coding_tasks": (1, 4, 1),
    "max_publication_attempts": (1, 10, 3),
}
GLOBAL_KEYS = {
    "enabled",
    "staging_root",
    "limits",
    "publication",
    "test_profiles",
    "protected_paths",
    "secret_scan",
}
PROJECT_KEYS = {
    "enabled",
    "repository",
    "base_branch",
    "workspace_root",
    "source_workspace",
    "test_profile",
    "allowed_model_aliases",
    "allowed_paths",
    "protected_paths",
    "limits",
    "publication",
    "cleanup_on_success",
}
PUBLICATION_KEYS = {"base_branch", "branch_prefix", "draft", "token_env", "max_attempts", "pr_title_prefix"}
PROFILE_KEYS = {"commands", "timeout_seconds", "env", "description"}
DEFAULT_BRANCH_PREFIX = "agent-handoff/"
DEFAULT_PROTECTED_PATHS = (
    ".git",
    ".git/**",
    ".gitmodules",
    ".github/workflows/**",
    ".github/actions/**",
    ".github/CODEOWNERS",
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa",
    "id_rsa*",
    "**/id_rsa*",
    "auth.json",
    "**/auth.json",
    "rclone.conf",
    "**/rclone.conf",
    "config/config.json",
    "**/credentials/**",
    "**/.ssh/**",
)
SECRET_ENV_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "PRIVATE_KEY")
FORBIDDEN_PROFILE_ENV = {
    "PATH",
    "HOME",
    "PWD",
    "USER",
    "LOGNAME",
    "SHELL",
    "SSH_AUTH_SOCK",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "PYTHONSTARTUP",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
}
PROTECTED_BRANCHES = {"main", "master", "head", "default"}


class CodingConfigError(ValueError):
    pass


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _add(errors: list[str], message: str) -> None:
    if message not in errors:
        errors.append(message)


def _bool_value(value: Any, default: bool, label: str, errors: list[str]) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        _add(errors, f"{label} must be true or false")
        return default
    return value


def _int_value(value: Any, label: str, bounds: tuple[int, int, int], errors: list[str]) -> int:
    minimum, maximum, default = bounds
    if value is None:
        return default
    if isinstance(value, bool):
        _add(errors, f"{label} must be an integer")
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


def validate_base_branch(value: Any, label: str = "base branch") -> str:
    branch = _text(value)
    if not branch or not BRANCH_RE.fullmatch(branch):
        raise CodingConfigError(f"{label} must be a safe Git branch name")
    if ".." in branch or branch.startswith("-") or branch.endswith("/") or branch.endswith(".lock"):
        raise CodingConfigError(f"{label} must be a safe Git branch name")
    return branch


def prefix_valid(value: Any) -> bool:
    prefix = _text(value)
    return bool(prefix and PREFIX_RE.fullmatch(prefix) and ".." not in prefix)


def validate_branch_name(value: Any, label: str = "branch") -> str:
    branch = validate_base_branch(value, label)
    if branch.lower() in PROTECTED_BRANCHES:
        raise CodingConfigError(f"{label} must not be the protected default branch {branch!r}")
    return branch


def _normalize_patterns(value: Any, label: str, errors: list[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        _add(errors, f"{label} must be a list of relative path patterns")
        return []
    result: list[str] = []
    for item in value:
        pattern = _text(item).replace("\\", "/")
        if not pattern:
            _add(errors, f"{label} entries must not be empty")
            continue
        if pattern.startswith("/") or "\x00" in pattern or "\n" in pattern:
            _add(errors, f"{label} entries must be relative patterns")
            continue
        parts = [part for part in pattern.split("/") if part not in ("", ".")]
        if any(part == ".." for part in parts):
            _add(errors, f"{label} entries must not contain '..'")
            continue
        normalized = "/".join(parts)
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def pattern_matches(path: str, patterns: Sequence[str]) -> bool:
    for pattern in patterns:
        if fnmatch.fnmatchcase(path, pattern):
            return True
        if path.startswith(pattern.rstrip("/") + "/"):
            return True
    return False


def _merge_limits(raw: Any, base: dict[str, int], label: str, errors: list[str]) -> dict[str, int]:
    if raw is None:
        return dict(base)
    if not isinstance(raw, dict):
        _add(errors, f"{label} must be an object")
        return dict(base)
    unknown = sorted(set(raw) - set(LIMIT_BOUNDS))
    if unknown:
        _add(errors, f"{label} contains unsupported field(s): {', '.join(unknown)}")
    merged = dict(base)
    for key, bounds in LIMIT_BOUNDS.items():
        if key in raw:
            merged[key] = _int_value(raw.get(key), f"{label}.{key}", bounds, errors)
    return merged


def _parse_publication(
    raw: Any,
    base: dict[str, Any],
    label: str,
    errors: list[str],
) -> dict[str, Any]:
    merged = dict(base)
    if raw is None:
        return merged
    if not isinstance(raw, dict):
        _add(errors, f"{label} must be an object")
        return merged
    unknown = sorted(set(raw) - PUBLICATION_KEYS)
    if unknown:
        _add(errors, f"{label} contains unsupported field(s): {', '.join(unknown)}")
    if "base_branch" in raw:
        try:
            merged["base_branch"] = validate_base_branch(raw.get("base_branch"), f"{label}.base_branch")
        except CodingConfigError as exc:
            _add(errors, str(exc))
    if "branch_prefix" in raw:
        prefix = _text(raw.get("branch_prefix"))
        if not PREFIX_RE.fullmatch(prefix) or ".." in prefix:
            _add(errors, f"{label}.branch_prefix must end with '/' and use a safe branch namespace")
        else:
            merged["branch_prefix"] = prefix
    if "draft" in raw:
        draft = _bool_value(raw.get("draft"), True, f"{label}.draft", errors)
        if draft is not True:
            _add(errors, f"{label}.draft must remain true; C2 only publishes Draft Pull Requests")
        merged["draft"] = True
    if "token_env" in raw:
        token_env = _text(raw.get("token_env"))
        if token_env and not ENV_NAME_RE.fullmatch(token_env):
            _add(errors, f"{label}.token_env must be an environment variable name")
        merged["token_env"] = token_env or None
    if "max_attempts" in raw:
        merged["max_attempts"] = _int_value(
            raw.get("max_attempts"), f"{label}.max_attempts", (1, 10, 3), errors
        )
    if "pr_title_prefix" in raw:
        prefix = _text(raw.get("pr_title_prefix")) or "Agent Handoff: "
        if "\n" in prefix or "\r" in prefix or len(prefix) > 80:
            _add(errors, f"{label}.pr_title_prefix must be a single short line")
        else:
            merged["pr_title_prefix"] = prefix
    return merged


def _parse_test_profiles(raw: Any, errors: list[str]) -> dict[str, dict[str, Any]]:
    profiles: dict[str, dict[str, Any]] = {}
    if raw is None:
        return profiles
    if not isinstance(raw, dict):
        _add(errors, "dispatch.coding.test_profiles must be an object")
        return profiles
    for name, value in raw.items():
        profile_name = _text(name)
        label = f"dispatch.coding.test_profiles.{profile_name or '<empty>'}"
        if not PROFILE_NAME_RE.fullmatch(profile_name):
            _add(errors, "dispatch.coding.test_profiles names must use letters, digits, _ or -")
            continue
        if not isinstance(value, dict):
            _add(errors, f"{label} must be an object")
            continue
        unknown = sorted(set(value) - PROFILE_KEYS)
        if unknown:
            _add(errors, f"{label} contains unsupported field(s): {', '.join(unknown)}")
        commands_raw = value.get("commands")
        commands: list[list[str]] = []
        if not isinstance(commands_raw, list) or not commands_raw:
            _add(errors, f"{label}.commands must be a non-empty list of argument arrays")
        else:
            for index, command in enumerate(commands_raw):
                if not isinstance(command, list) or not command:
                    _add(errors, f"{label}.commands[{index}] must be a non-empty argument array")
                    continue
                argv: list[str] = []
                valid = True
                for arg in command:
                    if not isinstance(arg, str) or not arg or "\x00" in arg or "\n" in arg or "\r" in arg:
                        _add(errors, f"{label}.commands[{index}] arguments must be safe non-empty strings")
                        valid = False
                        break
                    if len(arg) > 4000:
                        _add(errors, f"{label}.commands[{index}] contains an oversized argument")
                        valid = False
                        break
                    argv.append(arg)
                if valid:
                    commands.append(argv)
        env_raw = value.get("env") or {}
        env: dict[str, str] = {}
        if not isinstance(env_raw, dict):
            _add(errors, f"{label}.env must be an object of string values")
        else:
            for key, item in env_raw.items():
                key_text = _text(key)
                value_text = item if isinstance(item, str) else ""
                if not ENV_NAME_RE.fullmatch(key_text):
                    _add(errors, f"{label}.env contains an invalid variable name")
                    continue
                if key_text in FORBIDDEN_PROFILE_ENV or key_text.startswith(("AGENT_HANDOFF_", "GH_", "GITHUB_", "RCLONE_")):
                    _add(errors, f"{label}.env must not override {key_text}")
                    continue
                upper = key_text.upper()
                if any(marker in upper for marker in SECRET_ENV_MARKERS):
                    _add(errors, f"{label}.env must not contain credential-like variables")
                    continue
                if "\x00" in value_text or "\n" in value_text or "\r" in value_text or len(value_text) > 4000:
                    _add(errors, f"{label}.env values must be single safe lines")
                    continue
                env[key_text] = value_text
        timeout = _int_value(
            value.get("timeout_seconds"),
            f"{label}.timeout_seconds",
            LIMIT_BOUNDS["test_timeout_seconds"],
            errors,
        )
        description = _text(value.get("description"))
        profiles[profile_name] = {
            "name": profile_name,
            "commands": commands,
            "env": env,
            "timeout_seconds": timeout,
            "description": description[:300],
        }
    return profiles


def _effective_isolation(project: dict[str, Any], global_isolation: dict[str, Any]) -> dict[str, Any]:
    isolation = dict(global_isolation or {})
    override = ((project.get("execution") or {}).get("isolation") or {})
    enabled = override.get("enabled")
    isolation["enabled"] = enabled if isinstance(enabled, bool) else bool(isolation.get("enabled", False))
    if not isolation["enabled"]:
        isolation["identity"] = None
    return isolation


def parse_coding_config(
    raw_global: Any,
    projects_raw: dict[str, Any],
    resolved_projects: dict[str, dict[str, Any]],
    models: dict[str, str],
    global_isolation: dict[str, Any],
    *,
    workspace_roots: Sequence[str],
    base_dir: str,
    check_paths: bool,
    errors: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    """Parse trusted C2 coding policy. Coding is disabled unless explicitly opted in twice."""

    result = {
        "enabled": False,
        "staging_root": None,
        "limits": _merge_limits(None, DEFAULT_LIMITS, "dispatch.coding.limits", errors),
        "publication": _parse_publication(None, {
            "base_branch": "main",
            "branch_prefix": DEFAULT_BRANCH_PREFIX,
            "draft": True,
            "token_env": None,
            "max_attempts": 3,
            "pr_title_prefix": "Agent Handoff: ",
        }, "dispatch.coding.publication", errors),
        "test_profiles": {},
        "protected_paths": list(DEFAULT_PROTECTED_PATHS),
        "secret_scan": True,
    }
    if raw_global is None:
        raw_global = {}
    if not isinstance(raw_global, dict):
        _add(errors, "dispatch.coding must be an object")
        raw_global = {}
    unknown = sorted(set(raw_global) - GLOBAL_KEYS)
    if unknown:
        _add(errors, "dispatch.coding contains unsupported field(s): " + ", ".join(unknown))

    result["enabled"] = _bool_value(raw_global.get("enabled"), False, "dispatch.coding.enabled", errors)
    result["secret_scan"] = _bool_value(
        raw_global.get("secret_scan"), True, "dispatch.coding.secret_scan", errors
    )
    staging_raw = _text(raw_global.get("staging_root"))
    if staging_raw:
        try:
            staging = canonical_path(staging_raw, "dispatch.coding.staging_root")
            if staging in workspace_roots or any(
                staging == root or staging.startswith(root.rstrip("/") + "/") for root in workspace_roots
            ):
                _add(errors, "dispatch.coding.staging_root must be outside every coding workspace root")
            elif staging == os.path.realpath(base_dir) or staging.startswith(os.path.realpath(base_dir).rstrip("/") + "/"):
                _add(errors, "dispatch.coding.staging_root must not be inside the Agent Handoff source tree")
            else:
                result["staging_root"] = staging
        except WorkspaceSecurityError as exc:
            _add(errors, str(exc))
    result["limits"] = _merge_limits(raw_global.get("limits"), result["limits"], "dispatch.coding.limits", errors)
    result["publication"] = _parse_publication(
        raw_global.get("publication"), result["publication"], "dispatch.coding.publication", errors
    )
    result["test_profiles"] = _parse_test_profiles(raw_global.get("test_profiles"), errors)
    extra_protected = _normalize_patterns(
        raw_global.get("protected_paths"), "dispatch.coding.protected_paths", errors
    )
    for pattern in extra_protected:
        if pattern not in result["protected_paths"]:
            result["protected_paths"].append(pattern)

    for name, project in resolved_projects.items():
        raw_project = projects_raw.get(name) if isinstance(projects_raw, dict) else None
        raw_coding = (raw_project or {}).get("coding") if isinstance(raw_project, dict) else None
        project_coding: dict[str, Any] = {
            "enabled": False,
            "repository": None,
            "base_branch": None,
            "workspace_root": None,
            "source_workspace": None,
            "test_profile": None,
            "allowed_model_aliases": [],
            "allowed_paths": [],
            "protected_paths": list(result["protected_paths"]),
            "limits": dict(result["limits"]),
            "publication": dict(result["publication"]),
            "cleanup_on_success": True,
            "secret_scan": bool(result["secret_scan"]),
        }
        project["coding"] = project_coding
        if raw_coding is None:
            continue
        label = f"dispatch project {name!r} coding"
        if not isinstance(raw_coding, dict):
            _add(errors, f"{label} must be an object")
            continue
        unknown_project = sorted(set(raw_coding) - PROJECT_KEYS)
        if unknown_project:
            _add(errors, f"{label} contains unsupported field(s): {', '.join(unknown_project)}")

        enabled = _bool_value(raw_coding.get("enabled"), False, f"{label}.enabled", errors)
        project_coding["enabled"] = enabled
        project_coding["cleanup_on_success"] = _bool_value(
            raw_coding.get("cleanup_on_success"),
            True,
            f"{label}.cleanup_on_success",
            errors,
        )
        project_coding["limits"] = _merge_limits(
            raw_coding.get("limits"), result["limits"], f"{label}.limits", errors
        )
        project_coding["publication"] = _parse_publication(
            raw_coding.get("publication"), result["publication"], f"{label}.publication", errors
        )
        project_protected = _normalize_patterns(
            raw_coding.get("protected_paths"), f"{label}.protected_paths", errors
        )
        project_coding["protected_paths"] = list(result["protected_paths"])
        for pattern in project_protected:
            if pattern not in project_coding["protected_paths"]:
                project_coding["protected_paths"].append(pattern)
        project_coding["allowed_paths"] = _normalize_patterns(
            raw_coding.get("allowed_paths"), f"{label}.allowed_paths", errors
        )

        repository = _text(raw_coding.get("repository"))
        if repository:
            if not REPOSITORY_RE.fullmatch(repository):
                _add(errors, f"{label}.repository must use the owner/name format")
            else:
                project_coding["repository"] = repository
        base_branch = _text(raw_coding.get("base_branch"))
        if base_branch:
            try:
                project_coding["base_branch"] = validate_base_branch(base_branch, f"{label}.base_branch")
            except CodingConfigError as exc:
                _add(errors, str(exc))
        if not project_coding.get("base_branch"):
            project_coding["base_branch"] = project_coding["publication"].get("base_branch")

        aliases_raw = raw_coding.get("allowed_model_aliases")
        aliases: list[str] = []
        if aliases_raw is not None:
            if not isinstance(aliases_raw, list) or any(not isinstance(item, str) or not item.strip() for item in aliases_raw):
                _add(errors, f"{label}.allowed_model_aliases must be a list of aliases")
            else:
                for item in aliases_raw:
                    alias = item.strip()
                    if not MODEL_ALIAS_RE.fullmatch(alias) or alias not in models:
                        _add(errors, f"{label}.allowed_model_aliases contains an unknown or invalid alias {alias!r}")
                    elif alias not in aliases:
                        aliases.append(alias)
        project_coding["allowed_model_aliases"] = aliases

        source_raw = _text(raw_coding.get("source_workspace"))
        source_value = source_raw or str(project.get("workspace") or "")
        try:
            source = canonical_path(source_value, f"{label}.source_workspace")
            project_coding["source_workspace"] = source
        except WorkspaceSecurityError as exc:
            _add(errors, str(exc))

        root_raw = _text(raw_coding.get("workspace_root"))
        if root_raw:
            try:
                root = canonical_path(root_raw, f"{label}.workspace_root")
                project_coding["workspace_root"] = root
            except WorkspaceSecurityError as exc:
                _add(errors, str(exc))

        profile_name = _text(raw_coding.get("test_profile"))
        if profile_name:
            if profile_name not in result["test_profiles"]:
                _add(errors, f"{label}.test_profile {profile_name!r} is not defined in dispatch.coding.test_profiles")
            else:
                project_coding["test_profile"] = profile_name
                project_coding["test_definition"] = result["test_profiles"][profile_name]

        if not enabled:
            continue

        if not result["enabled"]:
            _add(errors, f"{label}.enabled requires dispatch.coding.enabled=true")
        effective = _effective_isolation(project, global_isolation)
        if not effective.get("enabled") or not effective.get("identity"):
            _add(
                errors,
                f"{label}.enabled requires effective isolation with a validated non-root identity; "
                "write-capable coding never falls back to the parent identity",
            )
        if not result.get("staging_root"):
            _add(errors, "dispatch.coding.staging_root is required when any project enables coding")
        if not project_coding.get("repository"):
            _add(errors, f"{label}.repository is required when coding is enabled")
        if not project_coding.get("base_branch"):
            _add(errors, f"{label}.base_branch is required when coding is enabled")
        if not project_coding.get("workspace_root"):
            _add(errors, f"{label}.workspace_root is required when coding is enabled")
        if not project_coding.get("allowed_model_aliases"):
            _add(errors, f"{label}.allowed_model_aliases must list at least one trusted alias")
        if not project_coding.get("test_profile") or not project_coding.get("test_definition"):
            _add(errors, f"{label}.test_profile must select a trusted test profile")
        source = project_coding.get("source_workspace")
        root = project_coding.get("workspace_root")
        if source and root:
            if source == root or source.startswith(root.rstrip("/") + "/") or root.startswith(source.rstrip("/") + "/"):
                _add(errors, f"{label}.workspace_root must be a separate disposable root outside source_workspace")
        if root and workspace_roots:
            if not any(root == item or root.startswith(item.rstrip("/") + "/") for item in workspace_roots):
                _add(errors, f"{label}.workspace_root must be inside an isolation workspace_roots entry")
        if root and check_paths and not os.path.isdir(root):
            _add(errors, f"{label}.workspace_root does not exist: {root}")
        if check_paths and source and not os.path.isdir(source):
            _add(errors, f"{label}.source_workspace does not exist: {source}")
    return result


def coding_enabled(project: dict[str, Any]) -> bool:
    coding = project.get("coding") or {}
    return bool(coding.get("enabled"))


def is_protected_path(path: str, patterns: Iterable[str]) -> bool:
    return pattern_matches(path, list(patterns or []))
