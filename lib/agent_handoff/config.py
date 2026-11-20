from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

from .util import FileLock, atomic_write_json, ensure_dir, read_json, utcnow_iso

BASE_DIR = Path(os.environ.get("AGENT_HANDOFF_HOME", str(Path(__file__).resolve().parents[2]))).resolve()
CONFIG_PATH = Path(os.environ.get("AGENT_HANDOFF_CONFIG", str(BASE_DIR / "config" / "config.json"))).resolve()

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "drive": {
        "remote": "gdrive",
        "parent_folder_id": "",
        "parent_folder_name": "AI Project Operations",
        "root_folder_id": "",
        "root_folder_name": "03 — Agent Execution Reports",
    },
    "storage": {
        "data": str(BASE_DIR / "data"),
        "queue": str(BASE_DIR / "queue"),
        "state": str(BASE_DIR / "state"),
        "locks": str(BASE_DIR / "locks"),
        "logs": str(BASE_DIR / "logs"),
        "tmp": str(BASE_DIR / "tmp"),
    },
    "projects": {},
    "workspace_roots": [str(Path.home() / "projects")],
    "auto_register_projects": True,
    "upload": {
        "enabled": True,
        "auto_upload": True,
        "verify": True,
        "timeout_seconds": 120,
        "hook_timeout_seconds": 25,
        "max_attempts": 0,
        "verify_interval_minutes": 60,
    },
    "integrations": {
        "opencode": True,
        "agy": True,
    },
    "security": {
        "redact_uploads": True,
        "max_prompt_bytes": 200000,
        "max_summary_bytes": 50000,
    },
    "index": {
        "include_pending_local": True,
        "max_open_issues": 20,
    },
    "notifications": {
        "telegram": {
            "enabled": False,
            "chat_id": "",
            "token_env": "AGENT_HANDOFF_TELEGRAM_TOKEN",
            "timeout_seconds": 10,
            "retry_seconds": 60,
            "max_attempts": 5,
            "delayed_after_attempts": 3,
        }
    },
}


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def config_lock() -> FileLock:
    return FileLock(BASE_DIR / "locks" / "config.lock", blocking=True, timeout=30)


def _migrate(config: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    merged = deep_merge(DEFAULT_CONFIG, config)
    changed = merged != config
    if changed:
        merged["updated_at"] = utcnow_iso()
    return merged, changed


def load_config(create: bool = True) -> dict[str, Any]:
    raw = read_json(CONFIG_PATH, default=None)
    if raw is None:
        raw = {}
    config, changed = _migrate(raw)
    if create and (changed or not CONFIG_PATH.exists()):
        ensure_dir(CONFIG_PATH.parent)
        atomic_write_json(CONFIG_PATH, config, mode=0o600)
    return config


def save_config(config: dict[str, Any]) -> None:
    ensure_dir(CONFIG_PATH.parent)
    config["updated_at"] = utcnow_iso()
    atomic_write_json(CONFIG_PATH, config, mode=0o600)


def storage_path(config: dict[str, Any], key: str) -> Path:
    value = config.get("storage", {}).get(key)
    if not value:
        value = str(BASE_DIR / key)
    return Path(value).expanduser().resolve()


def all_projects(config: dict[str, Any]) -> list[dict[str, Any]]:
    projects = []
    for key, value in (config.get("projects") or {}).items():
        if not isinstance(value, dict):
            continue
        item = dict(value)
        item["key"] = key
        item.setdefault("name", key)
        item.setdefault("aliases", [])
        item.setdefault("paths", [])
        item.setdefault("path_prefixes", [])
        projects.append(item)
    return sorted(projects, key=lambda p: p["name"].lower())


def project_by_name(config: dict[str, Any], name: str) -> dict[str, Any] | None:
    needle = (name or "").strip().lower()
    if not needle:
        return None
    for project in all_projects(config):
        candidates = {project["key"].lower(), project["name"].lower()}
        candidates.update(str(a).lower() for a in project.get("aliases", []))
        if needle in candidates:
            return project
    return None


def _real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def project_by_path(config: dict[str, Any], cwd: str) -> dict[str, Any] | None:
    target = _real(cwd)
    best = None
    best_len = -1
    for project in all_projects(config):
        for item in project.get("paths", []):
            root = _real(item)
            if target == root or target.startswith(root.rstrip("/") + "/"):
                if len(root) > best_len:
                    best = project
                    best_len = len(root)
        for item in project.get("path_prefixes", []):
            prefix = _real(item)
            if target == prefix or target.startswith(prefix):
                if len(prefix) > best_len:
                    best = project
                    best_len = len(prefix)
    return best


def git_root(path: str) -> str | None:
    current = Path(path)
    if current.is_file():
        current = current.parent
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return str(candidate)
    return None


def find_project(config: dict[str, Any], value: str) -> dict[str, Any] | None:
    by_name = project_by_name(config, value)
    if by_name:
        return by_name
    if value and (os.path.isabs(value) or "/" in value or os.path.isdir(value)):
        return project_by_path(config, _real(value))
    return None


def auto_register_project(config: dict[str, Any], cwd: str) -> dict[str, Any] | None:
    if not config.get("auto_register_projects", True):
        return None
    root = git_root(cwd)
    if not root:
        return None
    workspace_roots = [_real(p) for p in config.get("workspace_roots", [])]
    if not any(root == item or root.startswith(item.rstrip("/") + "/") for item in workspace_roots):
        return None
    with config_lock():
        fresh = load_config()
        existing = project_by_path(fresh, cwd)
        if existing:
            return existing
        base = Path(root).name.lower()
        key = base
        suffix = 2
        while key in fresh.get("projects", {}):
            key = f"{base}-{suffix}"
            suffix += 1
        fresh.setdefault("projects", {})[key] = {
            "name": key,
            "drive_folder_id": None,
            "paths": [root],
            "path_prefixes": [],
            "aliases": [key],
            "auto_registered": True,
            "registered_at": utcnow_iso(),
        }
        save_config(fresh)
        return project_by_name(fresh, key)


def resolve_project(config: dict[str, Any], name_or_path: str | None, cwd: str | None = None, register: bool = False) -> dict[str, Any] | None:
    if name_or_path:
        project = find_project(config, name_or_path)
        if project:
            return project
    if cwd:
        project = project_by_path(config, cwd)
        if project:
            return project
        if register:
            return auto_register_project(config, cwd)
    return None


def set_project_drive_id(config: dict[str, Any], key: str, folder_id: str) -> None:
    with config_lock():
        fresh = load_config()
        if key not in fresh.get("projects", {}):
            raise KeyError(key)
        fresh["projects"][key]["drive_folder_id"] = folder_id
        fresh["projects"][key]["auto_registered"] = False
        save_config(fresh)


def add_project(config: dict[str, Any], name: str, folder_id: str | None = None, paths: list[str] | None = None, aliases: list[str] | None = None, path_prefixes: list[str] | None = None) -> dict[str, Any]:
    key = name.strip().lower().replace(" ", "-")
    with config_lock():
        fresh = load_config()
        if key in fresh.get("projects", {}):
            project = fresh["projects"][key]
            project["name"] = name
            if folder_id:
                project["drive_folder_id"] = folder_id
                project["auto_registered"] = False
            for p in paths or []:
                if p not in project.setdefault("paths", []):
                    project["paths"].append(p)
            for p in path_prefixes or []:
                if p not in project.setdefault("path_prefixes", []):
                    project["path_prefixes"].append(p)
            for a in aliases or []:
                if a not in project.setdefault("aliases", []):
                    project["aliases"].append(a)
        else:
            fresh.setdefault("projects", {})[key] = {
                "name": name,
                "drive_folder_id": folder_id,
                "paths": paths or [],
                "path_prefixes": path_prefixes or [],
                "aliases": aliases or [name.lower()],
                "auto_registered": False,
                "registered_at": utcnow_iso(),
            }
        save_config(fresh)
        return project_by_name(fresh, name) or fresh["projects"][key]
