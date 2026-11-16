from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..config import BASE_DIR, load_config, resolve_project
from ..report import auto_report
from ..store import Store
from ..util import (
    first_line,
    new_id,
    run_command,
    sanitize_error,
    sha256_text,
    utc_from_timestamp,
    utcnow_iso,
)

SUPPORTED_AGENTS = {"opencode", "agy"}


def integration_log(message: str) -> None:
    try:
        log_dir = BASE_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "integration.log"
        if path.exists() and path.stat().st_size > 2 * 1024 * 1024:
            path.replace(path.with_suffix(".log.1"))
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{utcnow_iso()} {sanitize_error(message, 4000)}\n")
        os.chmod(path, 0o600)
    except OSError:
        pass


def git_info(cwd: str | None) -> dict[str, Any]:
    if not cwd or not os.path.isdir(cwd):
        return {"repository": None, "branch": None, "commit": None, "root": None}
    root = run_command(["git", "-C", cwd, "rev-parse", "--show-toplevel"], timeout=5)
    if root.returncode != 0:
        return {"repository": None, "branch": None, "commit": None, "root": None}
    root_path = (root.stdout or "").strip() or None
    remote = run_command(["git", "-C", cwd, "remote", "get-url", "origin"], timeout=5)
    branch = run_command(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"], timeout=5)
    commit = run_command(["git", "-C", cwd, "rev-parse", "HEAD"], timeout=5)
    return {
        "repository": (remote.stdout or "").strip() or None if remote.returncode == 0 else None,
        "branch": (branch.stdout or "").strip() or None if branch.returncode == 0 else None,
        "commit": (commit.stdout or "").strip() or None if commit.returncode == 0 else None,
        "root": root_path,
    }


def normalize_outcome(value: str | None) -> str:
    value = (value or "").strip().lower()
    if value in ("succeeded", "failed", "blocked", "cancelled"):
        return value
    return "blocked"


def normalize_status(value: str | None, allowed: set[str], default: str) -> str:
    value = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return value if value in allowed else default


def spawn_upload(config: dict[str, Any]) -> None:
    upload_cfg = config.get("upload") or {}
    if not upload_cfg.get("enabled", True) or not upload_cfg.get("auto_upload", True):
        return
    binary = shutil.which("agent-handoff")
    if not binary:
        candidate = BASE_DIR / "bin" / "agent-handoff"
        binary = str(candidate) if candidate.exists() else None
    if not binary:
        return
    try:
        subprocess.Popen(
            [binary, "retry-upload", "--quiet"],
            cwd=str(BASE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env={**os.environ, "AGENT_HANDOFF_HOME": str(BASE_DIR)},
        )
    except OSError as exc:
        integration_log(f"could not start upload worker: {exc}")


def _completed_at(value: Any) -> str:
    if isinstance(value, (int, float)) and value > 0:
        if value > 10_000_000_000:
            value = value / 1000.0
        return utc_from_timestamp(value)
    if isinstance(value, str) and value.strip():
        value = value.strip()
        if value.endswith("Z") or "+" in value:
            return value
        return value
    return utcnow_iso()


def auto_complete(payload: dict[str, Any]) -> dict[str, Any]:
    agent = (payload.get("agent") or "").strip().lower()
    if agent not in SUPPORTED_AGENTS:
        return {"ok": False, "skipped": True, "reason": f"unsupported agent: {agent!r}"}
    cwd = payload.get("cwd") or ""
    session_id = (payload.get("session_id") or "").strip()
    if not session_id:
        return {"ok": False, "skipped": True, "reason": "session_id is required"}
    summary = (payload.get("summary") or "").strip()
    task_prompt = (payload.get("task_prompt") or "").strip()
    run_prompt = (payload.get("run_prompt") or "").strip()
    title = (payload.get("title") or "").strip()
    outcome = normalize_outcome(payload.get("outcome"))
    error = (payload.get("error") or "").strip()
    termination_reason = payload.get("termination_reason")
    model = payload.get("model")
    event_key = (payload.get("event_key") or "").strip()
    if not event_key:
        event_key = sha256_text("|".join([agent, session_id, run_prompt, summary, error, str(termination_reason)]))[:24]
    tests = payload.get("tests") or {"status": "not_verified", "details": "Captured automatically; no independent test evidence."}
    deployment = payload.get("deployment") or {"status": "not_verified", "details": "Captured automatically; no independent deployment evidence."}
    tests = {"status": normalize_status(tests.get("status"), {"passed", "failed", "partial", "not_run", "not_verified"}, "not_verified"), "details": tests.get("details", "")}
    deployment = {"status": normalize_status(deployment.get("status"), {"deployed", "failed", "not_applicable", "not_verified", "unknown"}, "not_verified"), "details": deployment.get("details", "")}
    config = load_config()
    store = Store(config)
    with store.lock():
        config = load_config()
        store.config = config
        project = resolve_project(config, None, cwd, register=True)
        if not project:
            reason = f"no project matched cwd {cwd!r} and the directory is not an auto-registerable git project under the workspace roots"
            integration_log(f"auto-complete skipped: {reason}")
            return {"ok": True, "skipped": True, "reason": reason}
        git = git_info(cwd)
        repository = payload.get("repository") or git.get("repository")
        branch = payload.get("branch") or git.get("branch")
        commit = payload.get("commit") or git.get("commit")
        session = store.session_entry(agent, session_id)
        task_path = None
        meta = None
        if session:
            try:
                _, task_path, meta = store.find_task(session.get("task_id", ""), project["name"])
            except Exception:
                task_path = None
                meta = None
        if task_path is None:
            if not title:
                title = first_line(task_prompt or run_prompt or summary or f"{agent} session")
            task_path, meta = store.create_task(
                project,
                agent=agent,
                title=title,
                prompt_text=task_prompt or run_prompt,
                prompt_captured=bool(task_prompt or run_prompt),
                session_id=session_id,
                repository=repository,
                branch=branch,
                source="auto",
                model=model,
            )
        else:
            updates = {
                "repository": repository,
                "branch": branch,
                "session_id": session_id,
                "model": model,
            }
            if title:
                updates["title"] = title.strip()
            meta = store.update_task_meta(task_path, updates)
        auto_state = store.task_auto_state(task_path)
        if event_key in (auto_state.get("seen") or {}):
            existing = auto_state["seen"][event_key]
            return {
                "ok": True,
                "duplicate": True,
                "project": project["name"],
                "task_id": meta["task_id"],
                "run_id": existing.get("run_id"),
                "task_dir": str(task_path),
            }
        run_id = new_id("r-")
        run_path = task_path / "RUNS" / run_id
        report_text = auto_report(
            task_id=meta["task_id"],
            run_id=run_id,
            agent=agent,
            session_id=session_id,
            model=model,
            outcome=outcome,
            summary=summary,
            repository=repository,
            branch=branch,
            commit=commit,
            pr_url=payload.get("pr_url"),
            tests=tests,
            deployment=deployment,
            error=error,
            termination_reason=termination_reason,
            remaining_issues=payload.get("remaining_issues"),
            blockers=payload.get("blockers"),
            run_dir=str(run_path),
            remote_report_path=f"TASKS/{meta['task_folder']}/RUNS/{run_id}/REPORT.md",
        )
        run_path, complete = store.add_run(
            project,
            task_path,
            meta,
            outcome=outcome,
            report_text=report_text,
            run_prompt_text=run_prompt or task_prompt,
            tests=tests,
            deployment=deployment,
            summary=summary,
            error=error,
            termination_reason=termination_reason,
            remaining_issues=payload.get("remaining_issues"),
            blockers=payload.get("blockers"),
            repository=repository,
            branch=branch,
            commit=commit,
            pr_url=payload.get("pr_url"),
            session_id=session_id,
            model=model,
            completed_at=_completed_at(payload.get("completed_at")),
            auto_generated=True,
            event_key=event_key,
            run_id=run_id,
        )
        run_id = complete["run_id"]
        store.record_seen(task_path, event_key, run_id)
        store.set_session_entry(agent, session_id, {"project_key": project.get("key"), "task_id": meta["task_id"], "last_seen_at": utcnow_iso(), "last_run_id": run_id})
        store.write_local_index(project)
        store.queue_run(project, task_path, meta, run_path, run_id)
        result = {
            "ok": True,
            "duplicate": False,
            "skipped": False,
            "project": project["name"],
            "task_id": meta["task_id"],
            "run_id": run_id,
            "outcome": outcome,
            "task_dir": str(task_path),
            "run_dir": str(run_path),
            "report": str(run_path / "REPORT.md"),
            "queue_pending": len(store.queue_items()),
        }
    spawn_upload(config)
    return result
