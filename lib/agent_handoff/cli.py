from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from . import __version__
from .config import (
    BASE_DIR,
    CONFIG_PATH,
    add_project,
    all_projects,
    load_config,
    project_by_name,
    resolve_project,
)
from .adapters import agy_hook, auto_complete, git_info, integration_log, normalize_outcome, normalize_status, spawn_upload
from .dispatch.config import DispatchConfigError, validate_core_config, validate_dispatch_config
from .dispatch.delivery import (
    apply_issue_delivered,
    apply_upload_verified,
    diagnose_historical_run,
    delivery_marker,
    merged_upload_attempts,
    reconcile_verified_delivery,
)
from .dispatch.github import GithubError, GithubTransport
from .dispatch.ledger import Ledger
from .dispatch.progress import ProgressReporter
from .dispatch.runner import DispatchRunner
from .dispatch.status import GROUP_ORDER, format_status_view, record_status_view, status_group
from . import guided_setup
from .notify.config import NotificationConfigError, validate_notification_config
from .notify.service import NotificationService
from .report import manual_report
from .store import Store
from .upload import Uploader
from .util import (
    atomic_write_text,
    graceful_sigterm,
    new_id,
    read_json,
    read_text,
    run_command,
    sanitize_error,
    utcnow_iso,
)

INSTRUCTION_FILE = BASE_DIR / "integrations" / "AGENT-INSTRUCTIONS.md"
OPENCODE_PLUGIN = Path.home() / ".config" / "opencode" / "plugin" / "agent-handoff.js"
OPENCODE_CONFIG = Path.home() / ".config" / "opencode" / "opencode.json"
AGY_HOOKS = Path.home() / ".gemini" / "config" / "hooks.json"
AGY_RULES = Path.home() / ".gemini" / "config" / "AGENTS.md"
OPENCODE_RULES = Path.home() / ".config" / "opencode" / "AGENTS.md"


def emit(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def human_size(value: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def command_version(binary: str, args: list[str] | None = None) -> str | None:
    path = shutil.which(binary)
    if not path:
        return None
    result = run_command([path, *(args or ["--version"])], timeout=10)
    output = (result.stdout or result.stderr or "").strip()
    if not output:
        return "installed"
    return output.splitlines()[0].strip()


def config_mode_warning() -> str | None:
    try:
        mode = stat.S_IMODE(CONFIG_PATH.stat().st_mode)
        if mode & 0o077:
            return f"config permissions are {oct(mode)}; expected 600"
    except OSError:
        return "config file is missing"
    return None


def _check(checks: list[dict[str, str]], component: str, status: str, detail: str) -> None:
    checks.append({"component": component, "status": status, "detail": detail})


def collect_doctor(config: dict[str, Any], strict: bool = False) -> dict[str, Any]:
    store = Store(config)
    uploader = Uploader(config, store)
    checks: list[dict[str, str]] = []
    versions = {
        "python": sys.version.split()[0],
        "rclone": command_version("rclone", ["version"]),
        "opencode": command_version("opencode"),
        "agy": command_version("agy"),
        "gh": command_version("gh"),
        "agent_handoff": __version__,
    }
    py_ok = sys.version_info >= (3, 8)
    _check(checks, "python", "PASS" if py_ok else "FAIL", versions["python"])
    base_ok = BASE_DIR.exists() and os.access(BASE_DIR, os.W_OK)
    _check(checks, "utility home", "PASS" if base_ok else "FAIL", str(BASE_DIR))
    config_ok = CONFIG_PATH.exists()
    config_warn = config_mode_warning()
    _check(checks, "config", "PASS" if config_ok and not config_warn else ("WARN" if config_ok else "FAIL"), config_warn or str(CONFIG_PATH))
    path_bin = shutil.which("agent-handoff")
    _check(checks, "PATH executable", "PASS" if path_bin else "WARN", path_bin or "agent-handoff is not on PATH")
    rclone_ok = bool(versions["rclone"])
    _check(checks, "rclone", "PASS" if rclone_ok else "FAIL", versions["rclone"] or "rclone is not installed")
    if rclone_ok:
        ready, reason = uploader.remote_ready()
        _check(checks, "rclone gdrive remote", "PASS" if ready else "WARN", reason or config.get("drive", {}).get("remote", "gdrive"))
    else:
        _check(checks, "rclone gdrive remote", "FAIL", "rclone is not installed")

    opencode_ok = bool(versions["opencode"])
    _check(checks, "OpenCode", "PASS" if opencode_ok else "WARN", versions["opencode"] or "opencode not found")
    if OPENCODE_PLUGIN.exists():
        plugin_text = read_text(OPENCODE_PLUGIN)
        _check(checks, "OpenCode plugin", "PASS" if "agent-handoff" in plugin_text else "WARN", str(OPENCODE_PLUGIN))
    else:
        _check(checks, "OpenCode plugin", "FAIL", f"missing {OPENCODE_PLUGIN}")
    if OPENCODE_CONFIG.exists():
        try:
            oc = json.loads(read_text(OPENCODE_CONFIG))
            instructions = oc.get("instructions") if isinstance(oc, dict) else None
            has_instruction = isinstance(instructions, list) and str(INSTRUCTION_FILE) in instructions
            _check(checks, "OpenCode instruction", "PASS" if has_instruction else "WARN", str(INSTRUCTION_FILE))
        except ValueError:
            _check(checks, "OpenCode instruction", "WARN", "opencode.json is not valid JSON")
    else:
        _check(checks, "OpenCode instruction", "WARN", f"missing {OPENCODE_CONFIG}")

    agy_ok = bool(versions["agy"])
    _check(checks, "AGY", "PASS" if agy_ok else "WARN", versions["agy"] or "agy not found")
    if AGY_HOOKS.exists():
        try:
            hooks = json.loads(read_text(AGY_HOOKS))
            commands = json.dumps(hooks)
            has_hook = "agent-handoff" in commands and "hook" in commands and "agy" in commands
            _check(checks, "AGY hook", "PASS" if has_hook else "FAIL", str(AGY_HOOKS))
        except ValueError:
            _check(checks, "AGY hook", "FAIL", f"invalid JSON: {AGY_HOOKS}")
    else:
        _check(checks, "AGY hook", "FAIL", f"missing {AGY_HOOKS}")
    if AGY_RULES.exists():
        rules = read_text(AGY_RULES)
        _check(checks, "AGY instruction", "PASS" if "AGENT HANDOFF" in rules.upper() else "WARN", str(AGY_RULES))
    else:
        _check(checks, "AGY instruction", "WARN", f"missing {AGY_RULES}")

    projects = all_projects(config)
    if not projects:
        _check(checks, "projects", "FAIL", "no projects configured")
    for project in projects:
        folder_id = project.get("drive_folder_id")
        paths = [p for p in project.get("paths", []) if os.path.isdir(p)]
        detail = f"folder_id={'yes' if folder_id else 'missing'}, paths={len(paths)}"
        status = "PASS" if folder_id else "WARN"
        if not paths:
            status = "WARN"
        _check(checks, f"project:{project['name']}", status, detail)

    queue_items = store.queue_items()
    queue_public = []
    for item in queue_items:
        data = item.get("data") or {}
        queue_public.append(
            {
                "id": data.get("id"),
                "kind": data.get("kind"),
                "project": data.get("project"),
                "task_id": data.get("task_id"),
                "run_id": data.get("run_id"),
                "attempts": data.get("attempts", 0),
                "last_error": data.get("last_error"),
                "next_attempt_at": data.get("next_attempt_at"),
            }
        )
    max_attempts = max((int((i["data"] or {}).get("attempts", 0)) for i in queue_items), default=0)
    queue_status = "PASS" if not queue_items else "WARN"
    _check(checks, "upload queue", queue_status, f"{len(queue_items)} pending, max attempts {max_attempts}")

    task_count = 0
    run_count = 0
    for project in projects:
        for record in store.task_records(project):
            task_count += 1
            run_count += len(record["runs"])
    _check(checks, "local records", "PASS", f"{task_count} tasks, {run_count} runs")
    try:
        usage = shutil.disk_usage(BASE_DIR)
        free_ok = usage.free > 1024 * 1024 * 1024
        _check(checks, "disk", "PASS" if free_ok else "WARN", f"{human_size(usage.free)} free")
    except OSError as exc:
        _check(checks, "disk", "WARN", sanitize_error(str(exc)))
    try:
        stat_info = os.stat(BASE_DIR)
        _check(checks, "permissions", "PASS" if stat.S_IMODE(stat_info.st_mode) & 0o077 == 0 else "WARN", oct(stat.S_IMODE(stat_info.st_mode)))
    except OSError:
        _check(checks, "permissions", "WARN", "cannot stat utility directory")

    upload_info = uploader.doctor()
    if upload_info.get("connection") and upload_info["connection"].get("connected"):
        _check(checks, "Google Drive access", "PASS", "authorized and reachable")
    elif upload_info.get("remote_ready"):
        _check(checks, "Google Drive access", "FAIL", upload_info.get("remote_error") or upload_info.get("connection", {}).get("error") or "not reachable")
    else:
        _check(checks, "Google Drive access", "WARN", upload_info.get("remote_error") or "authorization required")
    hierarchy = upload_info.get("hierarchy")
    if hierarchy:
        if hierarchy.get("ok"):
            for key, item in (hierarchy.get("projects") or {}).items():
                if item.get("configured"):
                    _check(checks, f"drive:{item.get('name', key)}", "PASS" if item.get("verified") else "FAIL", item.get("found") or "folder id mismatch")
                else:
                    _check(checks, f"drive:{key}", "WARN", "no Drive folder id configured")
        else:
            _check(checks, "drive hierarchy", "FAIL", hierarchy.get("error") or "verification failed")

    dispatch_raw = config.get("dispatch") or {}
    if not dispatch_raw.get("enabled"):
        _check(checks, "dispatcher", "PASS", "disabled (optional)")
    else:
        try:
            resolved, dispatch_errors, dispatch_warnings = validate_dispatch_config(config, require_enabled=True)
            detail = f"{resolved.get('repository')} · {len(resolved.get('projects') or {})} project(s)"
            _check(checks, "dispatcher", "PASS" if not dispatch_errors else "FAIL", detail if not dispatch_errors else "; ".join(dispatch_errors))
            for warning in dispatch_warnings:
                _check(checks, "dispatcher warning", "WARN", warning)
        except DispatchConfigError as exc:
            _check(checks, "dispatcher", "FAIL", sanitize_error(str(exc)))
    notifications_raw = config.get("notifications")
    notification_raw = notifications_raw.get("telegram") if isinstance(notifications_raw, dict) else None
    if notifications_raw is not None and not isinstance(notifications_raw, dict):
        _check(checks, "Telegram notifications", "FAIL", "notifications configuration must be an object")
    elif notification_raw is not None and not isinstance(notification_raw, dict):
        _check(checks, "Telegram notifications", "FAIL", "notifications.telegram configuration must be an object")
    elif not isinstance(notification_raw, dict) or not notification_raw.get("enabled"):
        _check(checks, "Telegram notifications", "PASS", "disabled (optional)")
    else:
        try:
            _notification_resolved, notification_errors, _notification_warnings = validate_notification_config(config)
            _check(
                checks,
                "Telegram notifications",
                "PASS" if not notification_errors else "FAIL",
                "telegram enabled" if not notification_errors else "; ".join(notification_errors),
            )
        except NotificationConfigError as exc:
            _check(checks, "Telegram notifications", "FAIL", sanitize_error(str(exc)))
    _check(checks, "GitHub CLI", "PASS" if versions.get("gh") else "WARN", versions.get("gh") or "gh not found (required only for dispatch)")

    nonzero = [c for c in checks if c["status"] == "FAIL"]
    warnings = [c for c in checks if c["status"] == "WARN"]
    exit_code = 1 if nonzero else (1 if strict and warnings else 0)
    return {
        "ok": not nonzero and not (strict and warnings),
        "checks": checks,
        "warnings": len(warnings),
        "failures": len(nonzero),
        "versions": versions,
        "projects": projects,
        "queue": {"pending": len(queue_items), "items": queue_public},
        "upload": upload_info,
        "paths": {
            "base": str(BASE_DIR),
            "config": str(CONFIG_PATH),
            "data": str(store.data_root),
            "queue": str(store.queue_root),
            "logs": str(store.logs_root),
            "opencode_plugin": str(OPENCODE_PLUGIN),
            "agy_hooks": str(AGY_HOOKS),
            "dispatch_state": str(store.state_root / "dispatch"),
        },
        "exit_code": exit_code,
    }


def cmd_doctor(args: argparse.Namespace) -> int:
    config = load_config()
    data = collect_doctor(config, strict=args.strict)
    if args.json:
        emit(data)
    else:
        print(f"Agent Handoff Doctor — {data['versions']['agent_handoff']}")
        print(f"Home: {data['paths']['base']}")
        print("")
        for check in data["checks"]:
            print(f"[{check['status']:4}] {check['component']}: {check['detail']}")
        print("")
        if data["failures"]:
            print(f"FAIL: {data['failures']} check(s) failed; {data['warnings']} warning(s).")
        elif data["warnings"]:
            print(f"OK with {data['warnings']} warning(s).")
        else:
            print("OK: all checks passed.")
        upload = data.get("upload") or {}
        if not upload.get("remote_ready"):
            print("")
            print("Google Drive is not configured/authorized. Run: agent-handoff auth")
    return int(data["exit_code"])


def _resolve_project_or_error(config: dict[str, Any], name: str | None, cwd: str | None, register: bool = True) -> dict[str, Any]:
    project = resolve_project(config, name, cwd, register=register)
    if not project:
        raise ValueError(
            "project not found. Pass --project, or run from a configured workspace root. "
            "For a new project run: agent-handoff project add --name \"Name\""
        )
    return project


def _task_env(project: dict[str, Any], task_path: Path, meta: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"export AGENT_HANDOFF_PROJECT={project['name']!r}",
            f"export AGENT_HANDOFF_TASK_ID={meta['task_id']!r}",
            f"export AGENT_HANDOFF_TASK_DIR={str(task_path)!r}",
        ]
    )


def cmd_init(args: argparse.Namespace) -> int:
    config = load_config()
    store = Store(config)
    cwd = args.cwd or os.getcwd()
    project = _resolve_project_or_error(config, args.project, cwd, register=True)
    base_cwd = cwd
    if not os.path.isdir(base_cwd) and project.get("paths"):
        base_cwd = project["paths"][0]
    git = git_info(base_cwd)
    if args.prompt_not_captured:
        prompt_text = ""
        captured = False
    else:
        if not args.prompt_file:
            raise ValueError("--prompt-file is required unless --prompt-not-captured is used")
        prompt_path = Path(args.prompt_file).expanduser()
        if not prompt_path.is_file():
            raise ValueError(f"prompt file not found: {prompt_path}")
        prompt_text = read_text(prompt_path)
        captured = bool(prompt_text.strip())
    repository = args.repository or git.get("repository")
    branch = args.branch or git.get("branch")
    with store.lock():
        config = load_config()
        store.config = config
        project = _resolve_project_or_error(config, project["key"], base_cwd, register=False)
        task_path, meta = store.create_task(
            project,
            agent=args.agent,
            title=args.title,
            prompt_text=prompt_text,
            prompt_captured=captured,
            session_id=args.session_id,
            repository=repository,
            branch=branch,
            pr_url=args.pr_url,
            task_id=args.task_id,
            source="manual",
            model=args.model,
        )
        if args.repository or args.session_id or args.model:
            meta = store.update_task_meta(task_path, {"repository": repository, "session_id": args.session_id, "model": args.model})
        store.write_local_index(project)
    data = {
        "ok": True,
        "project": project["name"],
        "task_id": meta["task_id"],
        "title": meta["title"],
        "task_dir": str(task_path),
        "prompt_file": str(task_path / "PROMPT.md"),
        "prompt_captured": bool(meta.get("prompt_captured")),
        "drive_folder_id": project.get("drive_folder_id"),
        "env": _task_env(project, task_path, meta),
        "next": f"agent-handoff prepare-run --task-id {meta['task_id']}",
    }
    if args.json:
        emit(data)
    else:
        print(f"Task created: {data['task_id']}")
        print(f"Project: {data['project']}  Title: {data['title']}")
        print(f"Task dir: {data['task_dir']}")
        if not data["drive_folder_id"]:
            print("WARNING: no Drive folder id is mapped for this project. Local reports work; uploads wait for `agent-handoff project add`.")
        print("")
        print(data["env"])
        print("")
        print(f"Next: {data['next']}")
    return 0


def cmd_prepare_run(args: argparse.Namespace) -> int:
    config = load_config()
    store = Store(config)
    run_id = args.run_id or new_id("r-")
    with store.lock():
        project, task_path, meta = store.find_task(args.task_id)
        run_path = task_path / "RUNS" / run_id
        if run_path.exists() and (run_path / "COMPLETE.json").exists():
            raise ValueError(f"run already completed: {run_id}")
        run_path.mkdir(parents=True, exist_ok=True)
        os.chmod(run_path, 0o700)
        prompt = read_text(task_path / "PROMPT.md")
        if prompt:
            (run_path / "PROMPT.md").write_text(prompt, encoding="utf-8")
            os.chmod(run_path / "PROMPT.md", 0o600)
        instructions = (
            f"# Run {run_id}\n\n"
            "Write the structured report to `REPORT.md` in this directory before finishing.\n"
            "Do not fabricate test or deployment results. Mark anything you did not verify as `Not verified`.\n\n"
            "Then finalize the run:\n\n"
            f"agent-handoff complete --task-id {meta['task_id']} --run-id {run_id} --report-file {run_path / 'REPORT.md'} "
            "--outcome succeeded|failed|blocked|cancelled --tests passed|failed|not_run|not_verified\n"
        )
        (run_path / "RUN-INSTRUCTIONS.md").write_text(instructions, encoding="utf-8")
        os.chmod(run_path / "RUN-INSTRUCTIONS.md", 0o600)
    data = {
        "ok": True,
        "project": project["name"],
        "task_id": meta["task_id"],
        "run_id": run_id,
        "run_dir": str(run_path),
        "report_file": str(run_path / "REPORT.md"),
        "env": f"export AGENT_HANDOFF_TASK_ID={meta['task_id']!r}\nexport AGENT_HANDOFF_RUN_ID={run_id!r}\nexport AGENT_HANDOFF_RUN_DIR={str(run_path)!r}",
    }
    if args.json:
        emit(data)
    else:
        print(f"Run prepared: {run_id}")
        print(f"Run dir: {run_path}")
        print("")
        print(data["env"])
        print("")
        print(f"Report file: {data['report_file']}")
    return 0


def cmd_complete(args: argparse.Namespace) -> int:
    config = load_config()
    store = Store(config)
    outcome = normalize_outcome(args.outcome)
    tests_status = normalize_status(args.tests, {"passed", "failed", "partial", "not_run", "not_verified"}, "not_verified")
    deployment_status = normalize_status(args.deployment, {"deployed", "failed", "not_applicable", "not_verified", "unknown"}, "not_verified")
    summary = args.summary or ""
    if args.summary_file:
        summary = read_text(Path(args.summary_file).expanduser())
    run_id = args.run_id or new_id("r-")
    with store.lock():
        config = load_config()
        store.config = config
        project, task_path, meta = store.find_task(args.task_id)
        run_path = task_path / "RUNS" / run_id
        report_text = ""
        if args.report_file:
            report_path = Path(args.report_file).expanduser()
            if not report_path.is_file():
                raise ValueError(f"report file not found: {report_path}")
            report_text = read_text(report_path)
        elif (run_path / "REPORT.md").is_file():
            report_text = read_text(run_path / "REPORT.md")
        if not report_text.strip():
            report_text = "# Agent Execution Report\n\n## Executive Summary\n\n" + (summary.strip() or "No agent-authored report was supplied.")
        git = git_info(str(task_path))
        if git.get("root") is None and project.get("paths"):
            git = git_info(project["paths"][0])
        repository = args.repository or meta.get("repository") or git.get("repository")
        branch = args.branch or git.get("branch")
        commit = args.commit or git.get("commit")
        pr_url = args.pr_url or meta.get("pr_url")
        report_text = manual_report(
            task_id=meta["task_id"],
            run_id=run_id,
            agent=meta.get("agent"),
            session_id=args.session_id or meta.get("session_id"),
            model=args.model or meta.get("model"),
            outcome=outcome,
            report_text=report_text,
            repository=repository,
            branch=branch,
            commit=commit,
            pr_url=pr_url,
            tests={"status": tests_status, "details": args.tests_details or ""},
            deployment={"status": deployment_status, "details": args.deployment_details or ""},
            remaining_issues=args.remaining_issues,
            blockers=args.blockers,
            run_dir=str(run_path),
            remote_report_path=f"TASKS/{meta['task_folder']}/RUNS/{run_id}/REPORT.md",
        )
        run_path, complete = store.add_run(
            project,
            task_path,
            meta,
            outcome=outcome,
            report_text=report_text,
            run_prompt_text=None,
            tests={"status": tests_status, "details": args.tests_details or ""},
            deployment={"status": deployment_status, "details": args.deployment_details or ""},
            summary=summary,
            error=args.error,
            termination_reason=None,
            remaining_issues=args.remaining_issues,
            blockers=args.blockers,
            repository=repository,
            branch=branch,
            commit=commit,
            pr_url=pr_url,
            session_id=args.session_id or meta.get("session_id"),
            model=args.model or meta.get("model"),
            run_id=run_id,
            auto_generated=False,
        )
        store.write_local_index(project)
        queue_path = None if args.no_upload else store.queue_run(project, task_path, meta, run_path, run_id)
        pending = len(store.queue_items())
    if not args.no_upload:
        spawn_upload(config)
    if outcome == "succeeded" and tests_status == "failed":
        print("WARNING: outcome is succeeded but tests are marked failed; the report and index keep both facts separate.", file=sys.stderr)
    data = {
        "ok": True,
        "project": project["name"],
        "task_id": meta["task_id"],
        "run_id": run_id,
        "outcome": outcome,
        "tests": tests_status,
        "deployment": deployment_status,
        "run_dir": str(run_path),
        "report": str(run_path / "REPORT.md"),
        "complete": str(run_path / "COMPLETE.json"),
        "queued": bool(queue_path),
        "queue_pending": pending,
        "upload": complete.get("upload"),
    }
    if args.json:
        emit(data)
    else:
        print(f"Run completed: {run_id}")
        print(f"Task: {meta['task_id']}  Outcome: {outcome}")
        print(f"Report: {run_path / 'REPORT.md'}")
        print(f"COMPLETE.json: {run_path / 'COMPLETE.json'}")
        print(f"Upload queue: {pending} pending")
    return 0


def _row_for(project: dict[str, Any], meta: dict[str, Any], run: dict[str, Any] | None) -> dict[str, Any]:
    if run is None:
        return {
            "project": project["name"],
            "task_id": meta.get("task_id"),
            "title": meta.get("title"),
            "run_id": "-",
            "agent": meta.get("agent"),
            "outcome": "no-runs",
            "completed_at": meta.get("created_at"),
            "tests": "-",
            "deployment": "-",
            "upload": "-",
            "report": "-",
        }
    complete = run["complete"]
    upload = complete.get("upload") or {}
    return {
        "project": project["name"],
        "task_id": meta.get("task_id"),
        "title": meta.get("title"),
        "run_id": complete.get("run_id"),
        "agent": (complete.get("agent") or {}).get("name") or meta.get("agent"),
        "outcome": complete.get("outcome"),
        "completed_at": complete.get("completed_at") or meta.get("created_at"),
        "tests": (complete.get("tests") or {}).get("status"),
        "deployment": (complete.get("deployment") or {}).get("status"),
        "upload": "verified" if upload.get("verified") else "pending",
        "report": str(run["path"] / "REPORT.md"),
    }


def cmd_list(args: argparse.Namespace) -> int:
    config = load_config()
    store = Store(config)
    projects = all_projects(config)
    if args.project:
        project = project_by_name(config, args.project)
        if not project:
            raise ValueError(f"project not found: {args.project}")
        projects = [project]
    rows = []
    for project in projects:
        for record in store.task_records(project):
            if not record["runs"]:
                rows.append(_row_for(project, record["meta"], None))
            else:
                for run in record["runs"]:
                    rows.append(_row_for(project, record["meta"], run))
    rows.sort(key=lambda r: r.get("completed_at") or "", reverse=True)
    if args.limit and args.limit > 0:
        rows = rows[: args.limit]
    if args.json:
        emit({"ok": True, "count": len(rows), "rows": rows})
        return 0
    if not rows:
        print("No tasks found.")
        return 0
    print(f"{'Project':10} {'Task ID':30} {'Run ID':30} {'Agent':8} {'Outcome':10} {'Completed':20} {'Upload':8} Title")
    for row in rows:
        title = (row.get("title") or "").replace("\n", " ")
        if len(title) > 44:
            title = title[:43] + "…"
        print(
            f"{str(row['project'])[:10]:10} {str(row['task_id'])[:30]:30} {str(row['run_id'])[:30]:30} "
            f"{str(row['agent'])[:8]:8} {str(row['outcome'])[:10]:10} {str(row['completed_at'])[:20]:20} "
            f"{str(row['upload'])[:8]:8} {title}"
        )
    return 0


def cmd_retry_upload(args: argparse.Namespace) -> int:
    config = load_config()
    store = Store(config)
    uploader = Uploader(config, store)
    result = uploader.process_queue(
        force=args.force,
        only_project=args.project,
        only_task=args.task,
        only_run=args.run,
        time_budget=args.time_budget,
    )
    if args.json:
        emit(result)
    elif not args.quiet:
        if result.get("busy"):
            print("Another upload worker is already running; nothing to do.")
        elif result.get("ok"):
            print(f"Upload worker finished: {result.get('processed', 0)} item(s) verified, {result.get('pending', 0)} pending.")
        else:
            print(f"Upload worker could not finish: {'; '.join(result.get('errors') or []) or 'unknown error'}", file=sys.stderr)
            print(f"Pending queue items: {result.get('pending', 0)}", file=sys.stderr)
    if result.get("busy"):
        return 0
    return 0 if result.get("ok") else 1


def cmd_index(args: argparse.Namespace) -> int:
    config = load_config()
    store = Store(config)
    projects = all_projects(config)
    if args.project:
        project = project_by_name(config, args.project)
        if not project:
            raise ValueError(f"project not found: {args.project}")
        projects = [project]
    paths = []
    with store.lock():
        for project in projects:
            paths.append(str(store.write_local_index(project)))
            if args.upload:
                store.queue_index(project)
    if args.upload:
        spawn_upload(config)
    data = {"ok": True, "indexes": paths, "queued": bool(args.upload)}
    if args.json:
        emit(data)
    else:
        for path in paths:
            print(path)
        if args.upload:
            print("Project index upload queued.")
    return 0


def _verify_remote_for_project(uploader: Uploader, project: dict[str, Any]) -> dict[str, Any]:
    return uploader.verify_project(project, create=False)


def cmd_project_add(args: argparse.Namespace) -> int:
    config = load_config()
    store = Store(config)
    uploader = Uploader(config, store)
    name = args.name.strip()
    if not name or "/" in name or "\\" in name:
        raise ValueError("invalid project name")
    key = name.lower().replace(" ", "-")
    paths = [os.path.realpath(os.path.expanduser(p)) for p in (args.path or [])]
    prefixes = [os.path.realpath(os.path.expanduser(p)) for p in (args.path_prefix or [])]
    aliases = args.alias or []
    ready, reason = uploader.remote_ready()
    connected = False
    if ready:
        connection = uploader.check_connection()
        connected = bool(connection.get("connected"))
        if not connected:
            reason = connection.get("error")
    existing = project_by_name(config, name) or project_by_name(config, key)
    if args.folder_id:
        candidate = {"name": name, "key": existing["key"] if existing else key, "drive_folder_id": args.folder_id}
        if connected:
            verification = uploader.verify_project(candidate, create=False)
            if not verification.get("ok"):
                raise ValueError(verification.get("error") or "folder verification failed")
        elif not args.allow_unverified:
            raise ValueError(f"cannot verify folder id because Google Drive is not ready: {reason}. Use --allow-unverified to save it anyway.")
        project = add_project(config, name, folder_id=args.folder_id, paths=paths, aliases=aliases, path_prefixes=prefixes)
    else:
        if not connected:
            raise ValueError(f"cannot create or verify a Drive folder because Google Drive is not ready: {reason}. Run agent-handoff auth first, or pass --folder-id.")
        project = add_project(config, name, folder_id=None, paths=paths, aliases=aliases, path_prefixes=prefixes)
        verification = uploader.verify_project(project, create=True)
        if not verification.get("ok"):
            raise ValueError(verification.get("error") or "folder creation failed")
        project = project_by_name(load_config(), name) or project
    data = {
        "ok": True,
        "project": project.get("name"),
        "key": project.get("key"),
        "folder_id": project.get("drive_folder_id"),
        "paths": project.get("paths"),
        "verified": connected,
    }
    if args.json:
        emit(data)
    else:
        print(f"Project: {data['project']}")
        print(f"Folder ID: {data['folder_id'] or 'not configured'}")
        print(f"Paths: {', '.join(data['paths'] or []) or 'none'}")
    return 0


def cmd_project_list(args: argparse.Namespace) -> int:
    config = load_config()
    store = Store(config)
    uploader = Uploader(config, store)
    hierarchy = None
    if args.check:
        hierarchy = uploader.verify_hierarchy()
    rows = []
    for project in all_projects(config):
        tasks = store.task_records(project)
        runs = sum(len(record["runs"]) for record in tasks)
        row = {
            "name": project["name"],
            "key": project["key"],
            "folder_id": project.get("drive_folder_id"),
            "paths": project.get("paths", []),
            "tasks": len(tasks),
            "runs": runs,
            "auto_registered": project.get("auto_registered", False),
        }
        if hierarchy and hierarchy.get("ok"):
            item = (hierarchy.get("projects") or {}).get(project["key"], {})
            row["drive_verified"] = bool(item.get("verified")) if item.get("configured") else False
        rows.append(row)
    if args.json:
        emit({"ok": True, "projects": rows, "hierarchy": hierarchy})
        return 0
    print(f"{'Project':16} {'Key':16} {'Folder ID':36} {'Tasks':5} {'Runs':5} Paths")
    for row in rows:
        print(f"{row['name'][:16]:16} {row['key'][:16]:16} {str(row['folder_id'] or '-')[:36]:36} {row['tasks']:5} {row['runs']:5} {', '.join(row['paths']) or '-'}")
    if hierarchy and not hierarchy.get("ok"):
        print(f"Drive hierarchy warning: {hierarchy.get('error')}", file=sys.stderr)
    return 0


def _upsert_rclone_section(text: str, name: str, values: dict[str, str]) -> str:
    lines = text.splitlines() if text else []
    start_index = None
    end_index = len(lines)
    for index, line in enumerate(lines):
        if line.strip() == f"[{name}]":
            start_index = index
            break
    if start_index is not None:
        for index in range(start_index + 1, len(lines)):
            if lines[index].strip().startswith("[") and lines[index].strip().endswith("]"):
                end_index = index
                break
        existing: dict[str, str] = {}
        for line in lines[start_index + 1 : end_index]:
            if "=" in line and not line.lstrip().startswith(("#", ";")):
                key, value = line.split("=", 1)
                existing[key.strip()] = value.strip()
        existing.update(values)
        if "client_id" not in values and "client_id" in existing:
            values["client_id"] = existing["client_id"]
        if "client_secret" not in values and "client_secret" in existing:
            values["client_secret"] = existing["client_secret"]
        values = {**existing, **values}
        section = [f"[{name}]"] + [f"{key} = {value}" for key, value in values.items()]
        new_lines = lines[:start_index] + section + lines[end_index:]
    else:
        section = [f"[{name}]"] + [f"{key} = {value}" for key, value in values.items()]
        new_lines = lines + ([] if not lines or not lines[-1].strip() else [""]) + section
    return "\n".join(new_lines).rstrip() + "\n"


def _load_token_file(path: Path) -> dict[str, Any]:
    raw = read_text(path).strip()
    if not raw:
        raise ValueError(f"token file is empty: {path}")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"token file is not valid JSON: {exc}")
    if isinstance(data, str):
        data = json.loads(data)
    if isinstance(data, dict) and isinstance(data.get("token"), dict) and "access_token" not in data:
        data = data["token"]
    if not isinstance(data, dict):
        raise ValueError("token JSON must be an object")
    if not data.get("refresh_token") and not data.get("access_token"):
        raise ValueError("token JSON has neither refresh_token nor access_token")
    return data


def _import_gdrive_token(args: argparse.Namespace, config: dict[str, Any], uploader: Uploader) -> int:
    token_path = Path(args.import_token_file).expanduser()
    if not token_path.is_file():
        raise ValueError(f"token file not found: {token_path}")
    token = _load_token_file(token_path)
    config_path = uploader.config_file()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    old_text = read_text(config_path) if config_path.exists() else ""
    old_backup = None
    if config_path.exists():
        stamp = utcnow_iso().replace(":", "").replace("-", "")
        old_backup = config_path.with_name(f"{config_path.name}.backup-agent-handoff-{stamp}")
        old_backup.write_text(old_text, encoding="utf-8")
        os.chmod(old_backup, 0o600)
    values = {
        "type": "drive",
        "scope": "drive",
        "token": json.dumps(token, ensure_ascii=False, separators=(",", ":")),
    }
    new_text = _upsert_rclone_section(old_text, config.get("drive", {}).get("remote", "gdrive"), values)
    atomic_write_text(config_path, new_text, mode=0o600)
    ready, reason = uploader.remote_ready()
    if not ready:
        if old_backup:
            config_path.write_text(old_text, encoding="utf-8")
            os.chmod(config_path, 0o600)
        else:
            config_path.unlink(missing_ok=True)
        raise ValueError(f"rclone config was written but is not usable: {reason}")
    connection = uploader.check_connection(timeout=45)
    if not connection.get("connected"):
        if old_backup:
            config_path.write_text(old_text, encoding="utf-8")
            os.chmod(config_path, 0o600)
        else:
            config_path.unlink(missing_ok=True)
        raise ValueError(f"authorization was written but Google Drive is not reachable: {connection.get('error')}")
    data = {
        "ok": True,
        "remote": uploader.remote_name,
        "config": str(config_path),
        "token_file": str(token_path),
        "token_has_refresh": bool(token.get("refresh_token")),
        "backup": str(old_backup) if old_backup else None,
        "connection": connection,
    }
    if args.json:
        emit(data)
    else:
        print(f"Google Drive authorized: {uploader.remote_name}")
        print(f"Config: {config_path}")
        print(f"Token file: {token_path} (remove it after verifying; it contains credentials)")
        if old_backup:
            print(f"Previous config backup: {old_backup}")
    return 0


def cmd_auth(args: argparse.Namespace) -> int:
    config = load_config()
    store = Store(config)
    uploader = Uploader(config, store)
    if getattr(args, "import_token_file", None):
        return _import_gdrive_token(args, config, uploader)
    ready, reason = uploader.remote_ready()
    connection = uploader.check_connection() if ready else None
    data = {
        "ok": bool(ready and connection and connection.get("connected")),
        "remote": config.get("drive", {}).get("remote", "gdrive"),
        "ready": ready,
        "reason": reason,
        "connection": connection,
        "config": str(uploader.config_file()),
        "helper": str(BASE_DIR / "setup" / "gdrive-auth.sh"),
    }
    if args.json:
        emit(data)
    else:
        print(f"rclone remote: {data['remote']}")
        print(f"rclone config: {data['config']}")
        if data["ok"]:
            print("Google Drive is authorized and reachable.")
        else:
            print(f"Not ready: {reason or (connection or {}).get('error')}")
            print("")
            print("Minimum manual authorization step (headless VPS):")
            print("1. On a device with a browser (or Termux with rclone), run:")
            print('     rclone authorize "drive"')
            print("2. Sign in with the Google account that owns the AI Project Operations folder.")
            print("3. Copy the token JSON that rclone prints. Do not paste it into an AI chat.")
            print("4. Save it on this host as ~/gdrive-token.json (mode 600), then run:")
            print("     agent-handoff auth --import-token-file ~/gdrive-token.json")
            print("   Alternatively run the official interactive flow: rclone config")
            print("5. Verify with:")
            print("     agent-handoff auth --check && agent-handoff doctor")
    if getattr(args, "check", False):
        return 0 if data["ok"] else 1
    return 0


def cmd_auto_complete(args: argparse.Namespace) -> int:
    payload_path = Path(args.payload_file).expanduser()
    if not payload_path.is_file():
        raise ValueError(f"payload file not found: {payload_path}")
    try:
        payload = json.loads(read_text(payload_path))
    except ValueError as exc:
        raise ValueError(f"invalid payload JSON: {exc}")
    result = auto_complete(payload)
    if args.json or True:
        emit(result)
    return 0 if result.get("ok", False) else 1


def cmd_hook(args: argparse.Namespace) -> int:
    raw = ""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if args.target == "agy":
            result = agy_hook(payload)
        else:
            result = {"ok": False, "error": f"unknown hook target: {args.target}"}
    except Exception as exc:
        result = {"ok": False, "error": sanitize_error(str(exc))}
        try:
            integration_log(f"hook {args.target} failed: {exc}")
        except Exception:
            pass
    if getattr(args, "debug", False):
        print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
    print("{}")
    return 0


def cmd_config_validate(args: argparse.Namespace) -> int:
    config = load_config()
    core_errors, core_warnings = validate_core_config(config)
    try:
        resolved, dispatch_errors, dispatch_warnings = validate_dispatch_config(config)
    except DispatchConfigError as exc:
        resolved = {}
        dispatch_errors = [str(exc)]
        dispatch_warnings = []
    try:
        notification_resolved, notification_errors, notification_warnings = validate_notification_config(config)
    except NotificationConfigError as exc:
        notification_resolved = {}
        notification_errors = [str(exc)]
        notification_warnings = []
    errors = core_errors + dispatch_errors + notification_errors
    warnings = core_warnings + dispatch_warnings + notification_warnings
    token_env = notification_resolved.get("token_env") or ""
    data = {
        "ok": not errors and not (args.strict and warnings),
        "errors": errors,
        "warnings": warnings,
        "config": str(CONFIG_PATH),
        "dispatch": {
            "enabled": bool(resolved.get("enabled")),
            "repository": resolved.get("repository") or None,
            "projects": sorted((resolved.get("projects") or {}).keys()),
            "models": sorted((resolved.get("models") or {}).keys()),
            "transport": resolved.get("transport"),
        },
        "notifications": {
            "enabled": bool(notification_resolved.get("enabled")),
            "chat_id_configured": bool(notification_resolved.get("chat_id")),
            "token_env": token_env or None,
            "token_present": bool(notification_resolved.get("token")),
            "timeout_seconds": notification_resolved.get("timeout_seconds"),
            "retry_seconds": notification_resolved.get("retry_seconds"),
            "max_attempts": notification_resolved.get("max_attempts"),
            "delayed_after_attempts": notification_resolved.get("delayed_after_attempts"),
        },
    }
    if args.json:
        emit(data)
    else:
        print(f"Config: {data['config']}")
        print(f"Dispatcher: {'enabled' if data['dispatch']['enabled'] else 'disabled'}")
        if data["dispatch"]["repository"]:
            print(f"Repository: {data['dispatch']['repository']}")
        print(f"Telegram notifications: {'enabled' if data['notifications']['enabled'] else 'disabled'}")
        if data["notifications"]["enabled"]:
            print(
                f"Telegram chat configured: {data['notifications']['chat_id_configured']} · "
                f"token source: {data['notifications']['token_env']}"
            )
        for warning in warnings:
            print(f"[WARN] {warning}")
        for error in errors:
            print(f"[FAIL] {error}")
        if not errors and not warnings:
            print("OK: configuration is valid.")
        elif not errors:
            print(f"OK with {len(warnings)} warning(s).")
        else:
            print(f"FAIL: {len(errors)} error(s), {len(warnings)} warning(s).")
    return 0 if data["ok"] else 1


def _dispatch_reporter(args: argparse.Namespace) -> ProgressReporter:
    quiet = bool(getattr(args, "quiet", False))
    enabled = not quiet and (bool(getattr(args, "progress", False)) or sys.stderr.isatty())
    return ProgressReporter(enabled=enabled)


def _emit_dispatch_result(result: dict[str, Any], as_json: bool, *, quiet: bool = False) -> None:
    if as_json:
        emit(result)
        return
    if quiet:
        for error in result.get("errors") or []:
            print(f"[ERROR] {error}", file=sys.stderr)
        return
    if result.get("busy"):
        print("Another dispatcher process is already running.")
        return
    elapsed = result.get("elapsed_seconds")
    elapsed_text = f" in {elapsed:g}s" if isinstance(elapsed, (int, float)) else ""
    print(
        f"Dispatch poll{elapsed_text}: "
        f"executed={result.get('executed', 0)} uploaded={result.get('uploaded', 0)} "
        f"published={result.get('published', 0)} rejected={result.get('rejected', 0)} "
        f"needs_attention={result.get('needs_attention', 0)}"
    )
    notifications = result.get("notifications") or {}
    if notifications.get("enabled"):
        print(
            f"Telegram notifications: sent={notifications.get('sent', 0)} "
            f"failed={notifications.get('failed', 0)} "
            f"pending={sum((notifications.get('counts') or {}).get(key, 0) for key in ('PENDING', 'RETRY_WAIT', 'SENDING'))} "
            f"needs_attention={(notifications.get('counts') or {}).get('NEEDS_ATTENTION', 0)}"
        )
    for warning in result.get("warnings") or []:
        print(f"[WARN] {warning}")
    for error in result.get("errors") or []:
        print(f"[ERROR] {error}", file=sys.stderr)


def cmd_dispatch_run(args: argparse.Namespace) -> int:
    config = load_config()
    try:
        resolved, errors, _warnings = validate_dispatch_config(config, require_enabled=True)
    except DispatchConfigError as exc:
        raise ValueError(str(exc))
    if errors:
        raise ValueError("dispatcher configuration is invalid: " + "; ".join(errors))
    reporter = _dispatch_reporter(args)
    runner = DispatchRunner(config, resolved, progress=reporter)
    with graceful_sigterm():
        if not args.watch:
            result = runner.run_once(force=args.force)
            _emit_dispatch_result(result, args.json, quiet=args.quiet)
            return 0 if result.get("ok") else 1
        try:
            while True:
                result = runner.run_once(force=args.force)
                if args.json:
                    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
                else:
                    _emit_dispatch_result(result, args.json, quiet=args.quiet)
                sys.stdout.flush()
                if not result.get("ok") and args.stop_on_error:
                    return 1
                time.sleep(max(5, int(resolved.get("poll_interval_seconds", 30))))
        except KeyboardInterrupt:
            print("dispatcher stopped", file=sys.stderr)
            return 130


def cmd_dispatch_status(args: argparse.Namespace) -> int:
    config = load_config()
    ledger = Ledger(config)
    records = ledger.all()
    if args.issue is not None:
        records = [record for record in records if int(record.get("issue_number", -1)) == int(args.issue)]
    dispatch_cfg = config.get("dispatch") or {}
    notifier = NotificationService(config)
    notifications_by_issue: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
    for item in notifier.list_records(include_sent=True):
        key = (item.get("repository"), item.get("issue_number"))
        notifications_by_issue.setdefault(key, []).append(item)
    views = [
        record_status_view(
            record,
            dispatch_cfg,
            notifications=notifications_by_issue.get((record.get("repository"), record.get("issue_number")), []),
        )
        for record in records
    ]
    notification_summary = {
        "enabled": notifier.enabled,
        "configured_enabled": notifier.configured_enabled,
        "configuration_error": "; ".join(notifier.config_errors) if notifier.config_errors else None,
        "counts": notifier.counts(),
        "next_action": notifier.overall_next_action(),
    }
    if args.json:
        counts: dict[str, int] = {}
        for item in views:
            group = status_group(item)
            counts[group] = counts.get(group, 0) + 1
        emit(
            {
                "ok": True,
                "count": len(views),
                "counts": counts,
                "notifications": notification_summary,
                "records": views,
            }
        )
        return 0
    if not views:
        print("No dispatch records.")
        _print_notification_summary(notification_summary)
        return 0
    counts = {}
    for item in views:
        group = status_group(item)
        counts[group] = counts.get(group, 0) + 1
    summary_text = ", ".join(f"{label.lower()}={counts[group]}" for group, label in GROUP_ORDER if counts.get(group))
    print(f"Dispatch records: {len(views)} ({summary_text})")
    for group, label in GROUP_ORDER:
        items = [item for item in views if status_group(item) == group]
        if not items:
            continue
        print(f"\n{label} ({len(items)})")
        for item in items:
            for line in format_status_view(item):
                print(line)
    _print_notification_summary(notification_summary)
    return 0


def _print_notification_summary(summary: dict[str, Any]) -> None:
    counts = summary.get("counts") or {}
    if not summary.get("configured_enabled"):
        print("\nTelegram notifications: disabled (optional).")
        if counts:
            print(
                f"  stored: sent={counts.get('SENT', 0)} "
                f"pending={sum(counts.get(key, 0) for key in ('PENDING', 'SENDING'))} "
                f"retry_wait={counts.get('RETRY_WAIT', 0)} needs_attention={counts.get('NEEDS_ATTENTION', 0)}"
            )
        return
    print(
        "\nTelegram notifications: "
        f"{'enabled' if summary.get('enabled') else 'configuration error'} · "
        f"sent={counts.get('SENT', 0)} pending={sum(counts.get(key, 0) for key in ('PENDING', 'SENDING'))} "
        f"retry_wait={counts.get('RETRY_WAIT', 0)} needs_attention={counts.get('NEEDS_ATTENTION', 0)} "
        f"superseded={counts.get('SUPERSEDED', 0)}"
    )
    if summary.get("configuration_error"):
        print(f"  configuration: {summary['configuration_error']}")
    if summary.get("next_action") and summary["next_action"] != "None.":
        print(f"  next action: {summary['next_action']}")


def cmd_dispatch_notify(args: argparse.Namespace) -> int:
    config = load_config()
    notifier = NotificationService(config)
    if args.retry:
        result = notifier.retry(
            task_id=args.task,
            run_id=args.run,
            notification_id=args.notification,
            acknowledge_duplicate=args.acknowledge_duplicate,
        )
        if args.json:
            emit(result)
        elif not notifier.enabled:
            print(result.get("error") or "Telegram notifications are disabled or invalid.")
            if result.get("configuration_error"):
                print(f"configuration: {result['configuration_error']}", file=sys.stderr)
        elif result.get("error"):
            print(result.get("error"))
        else:
            print(
                f"Notification retry: retried={result.get('retried', 0)} "
                f"sent={result.get('sent', 0)} failed={result.get('failed', 0)} "
                f"superseded={result.get('superseded', 0)} skipped_ambiguous={result.get('skipped_ambiguous', 0)}"
            )
            for error in result.get("errors") or []:
                print(f"[WARN] {error}", file=sys.stderr)
            if result.get("next_action"):
                print(f"next action: {result['next_action']}")
            counts = result.get("counts") or {}
            print(
                f"Outbox: sent={counts.get('SENT', 0)} pending={sum(counts.get(key, 0) for key in ('PENDING', 'SENDING'))} "
                f"retry_wait={counts.get('RETRY_WAIT', 0)} needs_attention={counts.get('NEEDS_ATTENTION', 0)} "
                f"superseded={counts.get('SUPERSEDED', 0)}"
            )
        return 0 if result.get("ok") else 1
    records = notifier.list_records(task_id=args.task, run_id=args.run, include_sent=args.all)
    counts = notifier.counts()
    data = {
        "ok": True,
        "enabled": notifier.enabled,
        "configured_enabled": notifier.configured_enabled,
        "configuration_error": "; ".join(notifier.config_errors) if notifier.config_errors else None,
        "counts": counts,
        "count": len(records),
        "records": records,
        "next_action": notifier.overall_next_action(),
    }
    if args.json:
        emit(data)
        return 0
    if not notifier.configured_enabled:
        print("Telegram notifications: disabled (optional).")
        if counts:
            print(
                f"stored: sent={counts.get('SENT', 0)} "
                f"pending={sum(counts.get(key, 0) for key in ('PENDING', 'SENDING'))} "
                f"retry_wait={counts.get('RETRY_WAIT', 0)} needs_attention={counts.get('NEEDS_ATTENTION', 0)} "
                f"superseded={counts.get('SUPERSEDED', 0)}"
            )
            print("These records will not be sent until notifications.telegram.enabled is true.")
        return 0
    print(
        f"Telegram notifications: {'enabled' if notifier.enabled else 'configuration error'} · "
        f"sent={counts.get('SENT', 0)} pending={sum(counts.get(key, 0) for key in ('PENDING', 'SENDING'))} "
        f"retry_wait={counts.get('RETRY_WAIT', 0)} needs_attention={counts.get('NEEDS_ATTENTION', 0)} "
        f"superseded={counts.get('SUPERSEDED', 0)}"
    )
    if data["configuration_error"]:
        print(f"configuration: {data['configuration_error']}")
    for item in records:
        line = (
            f"  [{item.get('state')}] {item.get('kind')} · project={item.get('project')} "
            f"task={item.get('task_id')} run={item.get('run_id')} attempts={item.get('attempts', 0)}"
        )
        print(line)
        if item.get("sent_at"):
            print(f"      sent: {item['sent_at']} · telegram_message_id={item.get('telegram_message_id')}")
        if item.get("next_attempt_at"):
            print(f"      next retry: {item['next_attempt_at']}")
        if item.get("last_error"):
            print(f"      last error: {item['last_error']}")
        print(f"      next action: {item.get('next_action')}")
    if not records:
        print("  No notification records.")
    if data["next_action"] != "None.":
        print(f"next action: {data['next_action']}")
    return 0


def _find_run_project(config: dict[str, Any], store: Store, task_id: str, project_name: str | None) -> dict[str, Any]:
    if project_name:
        project = project_by_name(config, project_name)
        if not project:
            raise ValueError(f"project not found: {project_name}")
        return project
    project, _task_path, _meta = store.find_task(task_id)
    return project


def cmd_dispatch_inspect(args: argparse.Namespace) -> int:
    config = load_config()
    store = Store(config)
    project = _find_run_project(config, store, args.task, args.project)
    data = diagnose_historical_run(config, project, args.task, args.run)
    if args.json:
        emit(data)
    else:
        print(f"Run: {args.task} / {args.run}")
        print(f"Local upload verified: {data.get('local_upload_verified')} at {data.get('local_verified_at')}")
        print(f"Local report URL: {data.get('local_report_url') or 'not recorded'}")
        remote = data.get("remote_report") or {}
        print(f"Remote report: {remote.get('url') or remote.get('error')}")
        complete = data.get("remote_complete") or {}
        if complete.get("ok"):
            try:
                remote_json = json.loads(complete.get("text") or "{}")
                upload = remote_json.get("upload") or {}
                print(f"Remote COMPLETE.json upload: verified={upload.get('verified')} status={upload.get('status')}")
            except ValueError:
                print("Remote COMPLETE.json is not valid JSON")
        else:
            print(f"Remote COMPLETE.json: {complete.get('error')}")
    return 0 if data.get("ok") else 1


def cmd_dispatch_reconcile(args: argparse.Namespace) -> int:
    """Explicitly reverify one exhausted remote publication without side effects."""

    config = load_config()
    try:
        resolved, errors, _warnings = validate_dispatch_config(config)
    except DispatchConfigError as exc:
        raise ValueError(str(exc))
    if errors:
        raise ValueError("dispatcher configuration is invalid: " + "; ".join(errors))
    repository = str(args.repository or resolved.get("repository") or "")
    if not repository:
        raise ValueError("--repository is required when dispatch.repository is not configured")
    reporter = _dispatch_reporter(args)
    runner = DispatchRunner(config, resolved, progress=reporter)
    try:
        result = runner.reconcile_remote(repository=repository, issue=int(args.issue))
    except Exception as exc:
        error = sanitize_error(str(exc), 800)
        payload = {
            "ok": False,
            "repository": repository,
            "issue": int(args.issue),
            "remote_state": None,
            "error": error,
        }
        if args.json:
            emit(payload)
        elif not args.quiet:
            print(f"Remote reconciliation failed: {error}", file=sys.stderr)
        return 1
    record = result.get("record") or {}
    coding = record.get("coding") or {}
    publication = coding.get("publication") or {}
    view = record_status_view(record, resolved) if record else {}
    payload = {
        "ok": bool(result.get("ok")),
        "repository": repository,
        "issue": int(args.issue),
        "remote_state": publication.get("remote_state") or result.get("remote_state"),
        "blocked": bool(coding.get("blocked")),
        "error": result.get("error"),
        "next_action": view.get("next_action"),
    }
    if args.json:
        emit(payload)
    elif not args.quiet:
        if payload["ok"]:
            print(
                f"Remote reconciliation verified issue #{payload['issue']} "
                f"state={payload['remote_state']} without executing the agent."
            )
            if payload.get("next_action"):
                print(f"Next action: {payload['next_action']}")
        else:
            print(
                f"Remote reconciliation did not clear the block: {payload.get('error') or 'verification failed'}",
                file=sys.stderr,
            )
    return 0 if payload["ok"] else 1


def cmd_dispatch_deliver(args: argparse.Namespace) -> int:
    with graceful_sigterm():
        return _cmd_dispatch_deliver(args)


def _cmd_dispatch_deliver(args: argparse.Namespace) -> int:
    config = load_config()
    reporter = _dispatch_reporter(args)
    store = Store(config)
    project = _find_run_project(config, store, args.task, args.project)
    uploader = Uploader(config, store)
    ledger = Ledger(config)
    record = ledger.find_by_run(args.task, args.run)
    repository = (args.repository or (record or {}).get("repository") or "").strip()
    issue_number = args.issue if args.issue is not None else (record or {}).get("issue_number")
    if not repository or issue_number is None:
        raise ValueError("--repository and --issue are required when no dispatch record links this run to an issue")
    issue_number = int(issue_number)
    dispatch_cfg = config.get("dispatch") or {}
    transport = GithubTransport(
        {
            "repository": repository,
            "allowed_users": dispatch_cfg.get("allowed_users") or [],
            "github": {"command": ((dispatch_cfg.get("github") or {}).get("command") or "gh"), "require_private": True},
        }
    )
    reporter.emit("discovering", f"verifying the private issue inbox and issue #{issue_number}")
    inbox = transport.verify_repository()
    issue = transport.get_issue(issue_number)
    author = str(((issue.get("user") or {}).get("login")) or "").lower()
    allowed = {item.strip().lower() for item in (dispatch_cfg.get("allowed_users") or [])}
    allowed.add(str(inbox.get("user") or "").lower())
    if author not in allowed:
        raise ValueError("the issue author is not the authenticated user and is not in dispatch.allowed_users")
    reporter.emit("verifying", "reconciling the existing run; this never invokes OpenCode")
    result = reconcile_verified_delivery(
        config,
        store,
        uploader,
        project,
        args.task,
        args.run,
        force=True,
        on_progress=lambda stage, message: reporter.emit(stage, message),
    )
    if not result.get("ok"):
        raise ValueError(result.get("error") or "verified upload reconciliation failed")
    _found_project, _task_path, _meta, _run_path, complete = store.run_record(project, args.task, args.run)
    agent = complete.get("agent") or {}
    project_data = {
        "name": project.get("name"),
        "core_key": project.get("key"),
        "core_name": project.get("name"),
        "workspace": (project.get("paths") or [None])[0],
        "drive_folder_id": project.get("drive_folder_id"),
    }
    from .dispatch.ledger import new_dispatch_record

    now = utcnow_iso()
    record = ledger.get(repository, issue_number)
    if record:
        capture = record.get("capture") or {}
        if capture.get("run_id") and capture.get("run_id") != args.run:
            raise ValueError(f"issue #{issue_number} is already linked to run {capture.get('run_id')}")
    else:
        record = new_dispatch_record(
            repository=repository,
            issue=issue,
            project=project_data,
            agent=str(agent.get("name") or "opencode"),
            model=str(agent.get("model") or "unknown"),
            request={
                "version": "reconcile",
                "project": project.get("name"),
                "agent": str(agent.get("name") or "opencode"),
                "model_alias": None,
                "model": agent.get("model"),
                "session": "new",
                "body_sha256": (complete.get("dispatch") or {}).get("body_sha256"),
                "instructions": "",
            },
        )
        record["status"] = "UPLOAD_VERIFIED"
        record["history"].append({"at": now, "status": "UPLOAD_VERIFIED", "note": "operator reconciliation of an existing run"})
        record, _created = ledger.create(record)
    def apply_reconcile(item: dict[str, Any]) -> None:
        item["capture"] = {
            **(item.get("capture") or {}),
            "status": "CAPTURED",
            "task_id": args.task,
            "run_id": args.run,
            "report_path": str(store.find_task(args.task)[1] / "RUNS" / args.run / "REPORT.md"),
            "captured_at": now,
            "last_error": None,
        }
        item["upload"] = {
            **(item.get("upload") or {}),
            "status": "VERIFIED",
            "verified": True,
            "verified_at": result.get("verified_at") or complete.get("upload", {}).get("verified_at"),
            "report_remote_path": result.get("report_remote_path"),
            "report_url": result.get("report_url"),
            "receipt_remote_path": result.get("receipt_remote_path"),
            "receipt_url": result.get("receipt_url"),
            "attempts": merged_upload_attempts(item.get("upload"), result),
            "last_error": None,
        }
        item["execution"] = {
            **(item.get("execution") or {}),
            "status": "COMPLETED",
            "outcome": complete.get("outcome"),
            "finished_at": complete.get("completed_at"),
            "last_error": None,
        }
        item["session"] = {**(item.get("session") or {}), "mode": "new", "id": agent.get("session_id")}
        item["model"] = agent.get("model")
        item["status"] = "UPLOAD_VERIFIED"
        apply_upload_verified(item)
    record = ledger.update(repository, issue_number, apply_reconcile, note="verified upload metadata reconciled")
    marker = delivery_marker(args.run)
    notifier = NotificationService(config, log=lambda message: reporter.emit("notifying", message))
    with ledger.publication_lock(repository, issue_number):
        record = ledger.get(repository, issue_number) or record
        if record.get("status") == "ISSUE_UPDATED" and (record.get("publication") or {}).get("status") == "PUBLISHED":
            existing_comment = transport.find_comment(issue_number, marker)
            comment_url = (record.get("publication") or {}).get("comment_url") or (existing_comment or {}).get("html_url")
            final_record = record
            coding_record = record.get("coding") or {}
            milestones = coding_record.get("milestones") or {}
            if record.get("task_type") == "coding" and not (
                milestones.get("ISSUE_DELIVERED") or milestones.get("REPORT_DELIVERED")
            ):
                final_record = ledger.update(
                    repository,
                    issue_number,
                    apply_issue_delivered,
                    note="coding Issue delivery milestone reconciled from published state",
                )
        else:
            reporter.emit("publishing", "checking for an existing idempotent delivery comment")
            existing_comment = transport.find_comment(issue_number, marker)
            comment_url = (existing_comment or {}).get("html_url")
            if not existing_comment:
                from .dispatch.delivery import build_issue_comment

                response = transport.comment(issue_number, build_issue_comment(record))
                comment_url = response.get("url")
            published_at = (existing_comment or {}).get("created_at") or utcnow_iso()
            final_record = ledger.set_status(
                repository,
                issue_number,
                "ISSUE_UPDATED",
                note="operator published verified delivery",
                updates={
                    "publication": {
                        "status": "PUBLISHED",
                        "published_at": published_at,
                        "comment_url": comment_url,
                        "last_error": None,
                        "next_attempt_at": None,
                    },
                    "attention_reason": None,
                },
            )
            if final_record.get("task_type") == "coding":
                final_record = ledger.update(
                    repository,
                    issue_number,
                    apply_issue_delivered,
                    note="coding Issue delivery milestone recorded",
                )
    notification_data: dict[str, Any] = {
        "enabled": notifier.enabled,
        "configured_enabled": notifier.configured_enabled,
        "configuration_error": "; ".join(notifier.config_errors) if notifier.config_errors else None,
    }
    try:
        queued = notifier.enqueue_completed(final_record)
        if queued:
            notification_id = str(queued.get("notification_id"))
            drain = notifier.drain(force=True, limit=1, allowed_ids={notification_id})
            current = notifier.store.get(notification_id) or queued
            notification_data.update(
                {
                    "kind": current.get("kind"),
                    "state": current.get("state"),
                    "attempts": current.get("attempts", 0),
                    "sent": current.get("state") == "SENT",
                    "last_error": current.get("last_error"),
                }
            )
            if drain.get("errors"):
                reporter.emit("retryable", f"Telegram notification failed independently: {drain['errors'][0]}")
    except Exception as exc:
        notification_data["error"] = sanitize_error(str(exc))
    data = {
        "ok": True,
        "reused_comment": bool(existing_comment),
        "task_id": args.task,
        "run_id": args.run,
        "issue_url": issue.get("html_url"),
        "comment_url": comment_url,
        "report_url": result.get("report_url"),
        "receipt_url": result.get("receipt_url"),
        "opencode_executed": False,
        "notification": notification_data,
    }
    reporter.emit("completed", "verified report delivery reconciled without re-executing OpenCode")
    if args.json:
        emit(data)
    elif not args.quiet:
        print(f"Verified report: {data['report_url']}")
        print(f"Upload receipt: {data['receipt_url']}")
        print(f"Issue: {data['issue_url']}")
        print(f"Publication: {'reused existing comment' if data['reused_comment'] else 'posted once'}")
        notification = data.get("notification") or {}
        if notification.get("configured_enabled"):
            if notification.get("state") == "SENT":
                print("Telegram notification: sent")
            elif notification.get("state"):
                print(f"Telegram notification: {notification.get('state')} (report delivery is unaffected)")
            elif notification.get("error"):
                print(f"Telegram notification: skipped ({notification.get('error')})")
    return 0


def _setup_config_path() -> Path:
    return guided_setup.config_path_from_env()


def _setup_patch_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return guided_setup.patch_from_values(
        drive_parent_folder_id=getattr(args, "drive_parent_folder_id", None),
        drive_parent_folder_name=getattr(args, "drive_parent_folder_name", None),
        drive_root_folder_id=getattr(args, "drive_root_folder_id", None),
        drive_root_folder_name=getattr(args, "drive_root_folder_name", None),
        workspace_roots=getattr(args, "workspace_root", None),
        auto_register_projects=getattr(args, "auto_register_projects", None),
    )


def _setup_plan(args: argparse.Namespace, patch: dict[str, Any] | None = None) -> dict[str, Any]:
    state = guided_setup.read_config_state(_setup_config_path())
    return guided_setup.build_setup_plan(
        state,
        patch=_setup_patch_from_args(args) if patch is None else patch,
        base_dir=BASE_DIR,
        home=Path.home(),
    )


def _emit_setup_error(args: argparse.Namespace, command: str, exc: Exception) -> int:
    message = sanitize_error(str(exc))
    if getattr(args, "json", False):
        emit({"ok": False, "command": command, "error": message})
    else:
        print(f"error: {message}", file=sys.stderr)
    return 1


def cmd_setup_plan(args: argparse.Namespace) -> int:
    try:
        plan = _setup_plan(args)
    except guided_setup.SetupError as exc:
        return _emit_setup_error(args, "setup.plan", exc)
    if args.json:
        emit(plan)
    else:
        print(guided_setup.format_setup_plan(plan))
    return 0 if plan.get("ok") else 1


def cmd_setup_apply(args: argparse.Namespace) -> int:
    try:
        patch = _setup_patch_from_args(args)
        recovery = guided_setup.inspect_setup_recovery(_setup_config_path())
        if recovery.get("required"):
            if recovery.get("status") not in ("complete_cleanup_required", "rollback_cleanup_required"):
                raise guided_setup.SetupRecoveryError(
                    f"setup recovery must be completed first ({recovery.get('status')}): {recovery.get('reason')}"
                )
            if not args.approve:
                preview = _setup_plan(args, patch)
                if args.json:
                    emit({"ok": False, "command": "setup.apply", "approval_required": True, "recovery": recovery, "plan": preview})
                else:
                    print(guided_setup.format_setup_plan(preview))
                    print("Approval required: rerun with --approve to finalize the interrupted setup first.", file=sys.stderr)
                return 2
            guided_setup.recover_setup(_setup_config_path(), approve=True)
        plan = _setup_plan(args, patch)
        if not plan["config_plan"]["changes_required"]:
            if plan["config_plan"]["errors"]:
                raise guided_setup.SetupValidationError("; ".join(plan["config_plan"]["errors"]))
            if not plan.get("ok"):
                blocking = [item for item in plan.get("blockers") or [] if item.get("scope") in ("core", "install")]
                detail = "; ".join(item["summary"] for item in blocking) or "setup is blocked"
                raise guided_setup.SetupValidationError(detail)
            result = {
                "ok": True,
                "command": "setup.apply",
                "changed": False,
                "approval_required": False,
                "config": str(_setup_config_path()),
                "message": "configuration already satisfies the requested setup values",
                "plan_status": plan["status"],
            }
            if args.json:
                emit(result)
            else:
                print(result["message"])
                if plan["remaining_required_steps"]:
                    print("Required setup steps still remain: " + ", ".join(plan["remaining_required_steps"]))
            return 0
        if not args.approve:
            if args.json:
                emit({"ok": False, "command": "setup.apply", "approval_required": True, "plan": plan})
            else:
                print(guided_setup.format_setup_plan(plan))
            print("Approval required: rerun with --approve to apply the proposed configuration.", file=sys.stderr)
            return 2
        result = guided_setup.apply_setup_patch(
            _setup_config_path(),
            patch=patch,
            defaults=guided_setup.DEFAULT_CONFIG,
            approve=True,
        )
        if args.json:
            emit(result)
        else:
            if result.get("changed"):
                print("Configuration updated after validation.")
                if result.get("backup"):
                    print(f"Previous configuration backup: {result['backup']}")
                for change in result.get("changes") or []:
                    print(f"  {change['path']}: {change['before']!r} -> {change['after']!r}")
            else:
                print(result.get("message") or "No configuration change was necessary.")
        return 0
    except guided_setup.SetupError as exc:
        return _emit_setup_error(args, "setup.apply", exc)


def cmd_setup_recover(args: argparse.Namespace) -> int:
    command = "setup.recover"
    try:
        recovery = guided_setup.inspect_setup_recovery(_setup_config_path())
        if not recovery.get("required"):
            result = {"ok": True, "command": command, "changed": False, "recovery": recovery, "message": "no recovery is required"}
            if args.json:
                emit(result)
            else:
                print(result["message"])
            return 0
        if not args.approve:
            result = {"ok": False, "command": command, "approval_required": True, "recovery": recovery}
            if args.json:
                emit(result)
            else:
                print(f"Recovery status: {recovery.get('status')}")
                print(f"Reason: {recovery.get('reason')}")
                print("Approval required: rerun with --approve to finalize a safe cleanup.", file=sys.stderr)
            return 2
        result = guided_setup.recover_setup(_setup_config_path(), approve=True)
        result["command"] = command
        if args.json:
            emit(result)
        else:
            print("Interrupted setup recovery finalized.")
            if result.get("recovery", {}).get("backup"):
                print(f"Recovery backup retained: {result['recovery']['backup']}")
        return 0
    except guided_setup.SetupError as exc:
        return _emit_setup_error(args, command, exc)


def cmd_version(args: argparse.Namespace) -> int:
    print(__version__)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-handoff",
        description="Structured, upload-verified execution reports for OpenCode and AGY.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  agent-handoff setup plan --json\n"
            "  agent-handoff setup apply --approve --drive-root-folder-id <ROOT_FOLDER_ID>\n"
            "  agent-handoff doctor\n"
            "  agent-handoff config validate\n"
            "  agent-handoff init --project ProjectA --agent opencode --title \"Fix login\" --prompt-file prompt.txt\n"
            "  agent-handoff prepare-run --task-id t-20261116T130000Z-abcd1234\n"
            "  agent-handoff complete --task-id t-... --run-id r-... --report-file REPORT.md --outcome succeeded --tests passed\n"
            "  agent-handoff list --project ProjectA\n"
            "  agent-handoff retry-upload\n"
            "  agent-handoff project add --name ProjectA --path ~/projects/project-a\n"
            "  agent-handoff dispatch run --once\n"
        ),
    )
    parser.add_argument("--debug", action="store_true", help="print tracebacks on errors")
    parser.add_argument("--version", action="version", version=f"agent-handoff {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="check installation, integrations, and Google Drive access")
    p.add_argument("--json", action="store_true")
    p.add_argument("--strict", action="store_true", help="treat warnings as a non-zero exit")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("init", help="create a task and capture the exact prompt before execution")
    p.add_argument("--project", help="project name, alias, or path (default: infer from cwd)")
    p.add_argument("--agent", required=True, help="agent name, e.g. opencode or agy")
    p.add_argument("--title", required=True)
    p.add_argument("--prompt-file", help="file containing the exact original prompt")
    p.add_argument("--prompt-not-captured", action="store_true", help="record explicitly that the prompt was not captured")
    p.add_argument("--repository")
    p.add_argument("--branch")
    p.add_argument("--session-id")
    p.add_argument("--pr-url")
    p.add_argument("--model")
    p.add_argument("--task-id")
    p.add_argument("--cwd")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("prepare-run", help="create an assigned run directory for an agent-authored report")
    p.add_argument("--task-id", required=True)
    p.add_argument("--run-id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_prepare_run)

    p = sub.add_parser("complete", help="finalize a run with a structured report")
    p.add_argument("--task-id", required=True)
    p.add_argument("--run-id")
    p.add_argument("--report-file")
    p.add_argument("--outcome", required=True, choices=["succeeded", "failed", "blocked", "cancelled"])
    p.add_argument("--tests", default="not_verified", choices=["passed", "failed", "partial", "not_run", "not_verified"])
    p.add_argument("--tests-details")
    p.add_argument("--deployment", default="not_verified", choices=["deployed", "failed", "not_applicable", "not_verified", "unknown"])
    p.add_argument("--deployment-details")
    p.add_argument("--summary")
    p.add_argument("--summary-file")
    p.add_argument("--error")
    p.add_argument("--remaining-issues")
    p.add_argument("--blockers")
    p.add_argument("--session-id")
    p.add_argument("--model")
    p.add_argument("--repository")
    p.add_argument("--branch")
    p.add_argument("--commit")
    p.add_argument("--pr-url")
    p.add_argument("--no-upload", action="store_true", help="do not queue or attempt an upload")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_complete)

    p = sub.add_parser("list", help="list tasks and runs")
    p.add_argument("--project")
    p.add_argument("--limit", type=int, default=0, help="0 means all")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("retry-upload", help="process the upload queue with verification")
    p.add_argument("--project")
    p.add_argument("--task")
    p.add_argument("--run")
    p.add_argument("--force", action="store_true", help="ignore backoff")
    p.add_argument("--time-budget", type=float, default=300.0)
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_retry_upload)

    p = sub.add_parser("index", help="rebuild the local PROJECT-STATUS.md index")
    p.add_argument("--project")
    p.add_argument("--upload", action="store_true", help="queue an upload of the verified-run index")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_index)

    project_parser = sub.add_parser("project", help="manage project mappings")
    project_sub = project_parser.add_subparsers(dest="project_command", required=True)
    p = project_sub.add_parser("add", help="add or verify a project and its Drive folder")
    p.add_argument("--name", required=True)
    p.add_argument("--folder-id")
    p.add_argument("--path", action="append")
    p.add_argument("--path-prefix", action="append")
    p.add_argument("--alias", action="append")
    p.add_argument("--allow-unverified", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_project_add)
    p = project_sub.add_parser("list", help="list project mappings")
    p.add_argument("--check", action="store_true", help="verify Drive folder IDs")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_project_list)

    p = sub.add_parser("auth", help="show Google Drive authorization status and steps")
    p.add_argument("--check", action="store_true", help="non-zero exit if Drive is not ready")
    p.add_argument("--import-token-file", help="import a token JSON produced by `rclone authorize \"drive\"`")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_auth)

    p = sub.add_parser("auto-complete", help="internal: ingest an agent session payload")
    p.add_argument("--payload-file", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_auto_complete)

    hook_parser = sub.add_parser("hook", help="internal: AGY lifecycle hook")
    hook_sub = hook_parser.add_subparsers(dest="target", required=True)
    p = hook_sub.add_parser("agy", help=argparse.SUPPRESS)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_hook)

    config_parser = sub.add_parser("config", help="inspect and validate runtime configuration")
    config_sub = config_parser.add_subparsers(dest="config_command", required=True)
    p = config_sub.add_parser("validate", help="validate core and optional dispatcher configuration")
    p.add_argument("--strict", action="store_true", help="treat warnings as a non-zero exit")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_config_validate)

    setup_parser = sub.add_parser("setup", help="guided setup preflight, planning, and safe configuration changes")
    setup_sub = setup_parser.add_subparsers(dest="setup_command", required=True)

    def add_setup_patch_arguments(target: argparse.ArgumentParser) -> None:
        target.add_argument("--drive-parent-folder-id", help="Google Drive parent folder id for the reports root")
        target.add_argument("--drive-parent-folder-name", help="display name of the parent folder")
        target.add_argument("--drive-root-folder-id", help="Google Drive reports root folder id required for uploads")
        target.add_argument("--drive-root-folder-name", help="display name of the reports root folder")
        target.add_argument(
            "--workspace-root",
            action="append",
            default=None,
            help="add an existing local workspace root; existing roots are preserved",
        )
        group = target.add_mutually_exclusive_group()
        group.add_argument(
            "--auto-register-projects",
            dest="auto_register_projects",
            action="store_true",
            default=None,
            help="enable project auto-registration under workspace roots",
        )
        group.add_argument(
            "--no-auto-register-projects",
            dest="auto_register_projects",
            action="store_false",
            help="disable project auto-registration",
        )
        target.add_argument("--json", action="store_true")

    p = setup_sub.add_parser("plan", help="inspect the environment and preview setup steps without changing anything")
    add_setup_patch_arguments(p)
    p.set_defaults(func=cmd_setup_plan)

    p = setup_sub.add_parser("apply", help="validate, back up, and atomically apply approved setup configuration")
    add_setup_patch_arguments(p)
    p.add_argument("--approve", action="store_true", help="explicitly approve the proposed configuration change")
    p.set_defaults(func=cmd_setup_apply)

    p = setup_sub.add_parser("recover", help="finalize a safe cleanup after an interrupted setup apply")
    p.add_argument("--approve", action="store_true", help="explicitly approve the recovery cleanup")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_setup_recover)

    dispatch_parser = sub.add_parser("dispatch", help="optional private GitHub Issues dispatcher")
    dispatch_sub = dispatch_parser.add_subparsers(dest="dispatch_command", required=True)
    p = dispatch_sub.add_parser("run", help="claim and execute dispatched issues once or watch for new ones")
    p.add_argument("--once", action="store_true", help="process one poll and exit (default)")
    p.add_argument("--watch", action="store_true", help="poll continuously")
    p.add_argument("--force", action="store_true", help="ignore upload/publication backoff")
    p.add_argument("--stop-on-error", action="store_true", help="stop watch mode after a fatal poll")
    p.add_argument("--progress", action="store_true", help="write human progress to stderr even when stderr is not a TTY")
    p.add_argument("--quiet", action="store_true", help="suppress human progress and summary; JSON stdout is unchanged")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_dispatch_run)
    p = dispatch_sub.add_parser("status", help="show durable dispatch records without running agents")
    p.add_argument("--issue", type=int)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_dispatch_status)
    p = dispatch_sub.add_parser("notify", help="inspect or safely retry Telegram notifications without running agents")
    p.add_argument(
        "--retry",
        action="store_true",
        help="requeue confirmed failed/pending notifications and send them now; ambiguous sends are excluded",
    )
    p.add_argument("--notification", help="target one notification id; required for an ambiguous send")
    p.add_argument(
        "--acknowledge-duplicate",
        action="store_true",
        help="with --notification, allow one retry when the earlier send may already have reached Telegram",
    )
    p.add_argument("--all", action="store_true", help="include already sent notifications in the listing")
    p.add_argument("--task", help="limit to one Agent Handoff task id")
    p.add_argument("--run", help="limit to one Agent Handoff run id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_dispatch_notify)
    p = dispatch_sub.add_parser("inspect", help="read local and remote metadata for one run without changing anything")
    p.add_argument("--task", required=True)
    p.add_argument("--run", required=True)
    p.add_argument("--project")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_dispatch_inspect)
    p = dispatch_sub.add_parser(
        "reconcile",
        help="reverify one exhausted remote publication without executing an agent or creating artifacts",
    )
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--repository", help="owner/name (defaults to dispatch.repository)")
    p.add_argument("--progress", action="store_true", help="write human progress to stderr even when stderr is not a TTY")
    p.add_argument("--quiet", action="store_true", help="suppress human output; JSON stdout is unchanged")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_dispatch_reconcile)
    p = dispatch_sub.add_parser("deliver", help="reconcile an existing verified run and publish its direct link; never runs an agent")
    p.add_argument("--task", required=True)
    p.add_argument("--run", required=True)
    p.add_argument("--project")
    p.add_argument("--issue", type=int)
    p.add_argument("--repository", help="owner/name (inferred from an existing dispatch record when omitted)")
    p.add_argument("--progress", action="store_true", help="write human progress to stderr even when stderr is not a TTY")
    p.add_argument("--quiet", action="store_true", help="suppress human progress and output; JSON stdout is unchanged")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_dispatch_deliver)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if getattr(args, "debug", False):
            os.environ["AGENT_HANDOFF_DEBUG"] = "1"
        return int(args.func(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"error: {sanitize_error(str(exc))}", file=sys.stderr)
        if getattr(args, "debug", False):
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
