from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from . import report as report_mod
from .config import storage_path
from .util import (
    FileLock,
    atomic_write_json,
    atomic_write_text,
    ensure_dir,
    new_id,
    read_json,
    read_text,
    safe_title,
    slugify,
    utcnow_iso,
)

TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,120}$")
RUN_ID_RE = TASK_ID_RE


def _component(value: str, label: str) -> str:
    value = (value or "").strip()
    if not value or value in (".", "..") or "/" in value or "\\" in value or "\x00" in value:
        raise ValueError(f"invalid {label}: {value!r}")
    return value


class Store:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.data_root = ensure_dir(storage_path(config, "data"))
        self.queue_root = ensure_dir(storage_path(config, "queue"))
        self.state_root = ensure_dir(storage_path(config, "state"))
        self.locks_root = ensure_dir(storage_path(config, "locks"))
        self.logs_root = ensure_dir(storage_path(config, "logs"))
        self.tmp_root = ensure_dir(storage_path(config, "tmp"))
        ensure_dir(self.data_root / "projects")

    def lock(self, name: str = "global") -> FileLock:
        return FileLock(self.locks_root / f"{name}.lock", blocking=True, timeout=120)

    def worker_lock(self) -> FileLock:
        return FileLock(self.locks_root / "upload-worker.lock", blocking=False, timeout=0)

    def project_dir(self, project: dict[str, Any]) -> Path:
        return ensure_dir(self.data_root / "projects" / _component(project["name"], "project name"))

    def tasks_root(self, project: dict[str, Any]) -> Path:
        return ensure_dir(self.project_dir(project) / "TASKS")

    def task_dir(self, project: dict[str, Any], task_folder: str) -> Path:
        return self.tasks_root(project) / _component(task_folder, "task folder")

    def sessions_path(self) -> Path:
        return self.state_root / "sessions.json"

    def index_upload_path(self) -> Path:
        return self.state_root / "index_upload.json"

    def load_sessions(self) -> dict[str, Any]:
        return read_json(self.sessions_path(), default={}) or {}

    def save_sessions(self, data: dict[str, Any]) -> None:
        atomic_write_json(self.sessions_path(), data)

    def create_task(
        self,
        project: dict[str, Any],
        *,
        agent: str,
        title: str,
        prompt_text: str | None,
        prompt_captured: bool = True,
        session_id: str | None = None,
        repository: str | None = None,
        branch: str | None = None,
        pr_url: str | None = None,
        task_id: str | None = None,
        source: str = "manual",
        model: str | None = None,
    ) -> tuple[Path, dict[str, Any]]:
        task_id = task_id or new_id("t-")
        if not TASK_ID_RE.match(task_id):
            raise ValueError(f"invalid task_id: {task_id!r}")
        title = safe_title(title)
        folder = f"{task_id}-{slugify(title)}"
        task_path = self.task_dir(project, folder)
        try:
            _, existing_path, existing_meta = self.find_task(task_id)
            if existing_path.name == folder and existing_path.parent.parent.name.lower() == project["name"].lower():
                return existing_path, existing_meta
            raise ValueError(f"task_id {task_id} already exists in {existing_path.parent.parent.name}")
        except FileNotFoundError:
            pass
        if task_path.exists():
            meta = read_json(task_path / "META.json", default=None)
            if not meta:
                raise RuntimeError(f"task directory exists without META.json: {task_path}")
            return task_path, meta
        ensure_dir(task_path)
        ensure_dir(task_path / "RUNS")
        prompt_value = (prompt_text or "").strip("\n")
        if prompt_captured and prompt_value:
            atomic_write_text(task_path / "PROMPT.md", prompt_value + ("\n" if not prompt_value.endswith("\n") else ""))
            prompt_hash = __import__("hashlib").sha256(prompt_value.encode("utf-8")).hexdigest()
        else:
            atomic_write_text(
                task_path / "PROMPT.md",
                "PROMPT NOT CAPTURED\n\nThe reporting system could not capture the exact original prompt before execution. "
                "No prompt was fabricated from conversation history.\n",
            )
            prompt_hash = None
        meta = {
            "task_id": task_id,
            "project": project["name"],
            "project_key": project.get("key"),
            "title": title,
            "agent": agent,
            "created_at": utcnow_iso(),
            "session_id": session_id,
            "repository": repository,
            "branch": branch,
            "prompt_sha256": prompt_hash,
            "prompt_captured": bool(prompt_captured and prompt_value),
            "source": source,
            "model": model,
            "pr_url": pr_url,
            "task_folder": folder,
        }
        atomic_write_json(task_path / "META.json", meta)
        return task_path, meta

    def update_task_meta(self, task_path: Path, updates: dict[str, Any]) -> dict[str, Any]:
        meta = read_json(task_path / "META.json", default={}) or {}
        for key, value in updates.items():
            if value not in (None, ""):
                meta[key] = value
        atomic_write_json(task_path / "META.json", meta)
        return meta

    def find_task(self, task_id: str, project_name: str | None = None) -> tuple[dict[str, Any], Path, dict[str, Any]]:
        if not TASK_ID_RE.match(task_id or ""):
            raise ValueError(f"invalid task_id: {task_id!r}")
        matches = []
        projects_root = self.data_root / "projects"
        for pdir in sorted(projects_root.iterdir()) if projects_root.exists() else []:
            if not pdir.is_dir():
                continue
            if project_name and pdir.name.lower() != project_name.lower():
                continue
            tdir = pdir / "TASKS"
            if not tdir.exists():
                continue
            for candidate in sorted(tdir.iterdir()):
                if not candidate.is_dir():
                    continue
                meta = read_json(candidate / "META.json", default=None)
                if meta and meta.get("task_id") == task_id:
                    matches.append((pdir.name, candidate, meta))
        if not matches:
            raise FileNotFoundError(f"task not found: {task_id}")
        if len(matches) > 1:
            names = ", ".join(m[0] for m in matches)
            raise RuntimeError(f"task id {task_id} is ambiguous across projects: {names}")
        project_name, task_path, meta = matches[0]
        project = None
        for item in self.config.get("projects", {}).values():
            if item.get("name") == project_name:
                project = dict(item)
                break
        if project is None:
            project = {"name": project_name, "key": project_name.lower(), "drive_folder_id": None}
        project["key"] = project.get("key") or project_name.lower()
        return project, task_path, meta

    def latest_task(self, project: dict[str, Any] | None = None) -> tuple[dict[str, Any], Path, dict[str, Any]]:
        candidates = []
        projects = [project] if project else [dict(p, key=k) for k, p in (self.config.get("projects") or {}).items()]
        for item in projects:
            tdir = self.tasks_root(item)
            for path in tdir.iterdir() if tdir.exists() else []:
                meta = read_json(path / "META.json", default=None)
                if meta:
                    candidates.append((meta.get("created_at", ""), item, path, meta))
        if not candidates:
            raise FileNotFoundError("no tasks found")
        _, project, path, meta = max(candidates, key=lambda x: x[0])
        return project, path, meta

    def add_run(
        self,
        project: dict[str, Any],
        task_path: Path,
        meta: dict[str, Any],
        *,
        outcome: str,
        report_text: str,
        run_prompt_text: str | None,
        tests: dict[str, Any],
        deployment: dict[str, Any],
        summary: str | None,
        error: str | None,
        termination_reason: str | None,
        remaining_issues: str | None,
        blockers: str | None,
        repository: str | None,
        branch: str | None,
        commit: str | None,
        pr_url: str | None,
        session_id: str | None,
        model: str | None,
        run_id: str | None = None,
        completed_at: str | None = None,
        auto_generated: bool = False,
        event_key: str | None = None,
    ) -> tuple[Path, dict[str, Any]]:
        run_id = run_id or new_id("r-")
        if not RUN_ID_RE.match(run_id):
            raise ValueError(f"invalid run_id: {run_id!r}")
        runs_root = ensure_dir(task_path / "RUNS")
        run_path = runs_root / run_id
        if run_path.exists():
            complete = read_json(run_path / "COMPLETE.json", default=None)
            if complete:
                return run_path, complete
        ensure_dir(run_path)
        completed_at = completed_at or utcnow_iso()
        if run_prompt_text is not None:
            atomic_write_text(run_path / "PROMPT.md", run_prompt_text.strip("\n") + "\n")
        report_remote = f"TASKS/{meta['task_folder']}/RUNS/{run_id}/REPORT.md"
        normalized_report = report_mod.ensure_sections(
            report_text,
            {
                "Executive Summary": (summary or "").strip() or "Not provided.",
                "Task ID and Run ID": f"- Task ID: `{meta['task_id']}`\n- Run ID: `{run_id}`",
                "Agent and Session": f"- Agent: `{meta.get('agent')}`\n- Session ID: `{session_id or 'not available'}`\n- Model: `{model or 'not captured'}`",
                "GitHub Branch, Commit and PR": f"- Repository: `{repository or 'not captured'}`\n- Branch: `{branch or 'not captured'}`\n- Commit: `{commit or 'not captured'}`\n- PR: {pr_url or 'not captured'}",
                "Remaining Issues": (remaining_issues or "").strip() or "None reported.",
                "Blockers": (blockers or "").strip() or "None reported.",
                "Evidence links": f"- Local run directory: `{run_path}`\n- Drive report: `{report_remote}`",
            },
        )
        atomic_write_text(run_path / "REPORT.md", normalized_report)
        complete = {
            "task_id": meta["task_id"],
            "run_id": run_id,
            "outcome": outcome,
            "completed_at": completed_at,
            "tests": {
                "status": (tests or {}).get("status", "not_verified"),
                "details": (tests or {}).get("details", ""),
            },
            "deployment": {
                "status": (deployment or {}).get("status", "not_verified"),
                "details": (deployment or {}).get("details", ""),
            },
            "report": {
                "available": True,
                "local_path": str(run_path / "REPORT.md"),
                "remote_path": report_remote,
            },
            "upload": {
                "status": "pending",
                "verified": False,
                "verified_at": None,
                "attempts": 0,
                "last_error": None,
                "remote_dir": f"TASKS/{meta['task_folder']}/RUNS/{run_id}",
                "files": [],
                "redactions": 0,
            },
            "agent": {
                "name": meta.get("agent"),
                "session_id": session_id or meta.get("session_id"),
                "model": model or meta.get("model"),
            },
            "source": "auto" if auto_generated else "manual",
            "auto_generated": bool(auto_generated),
            "event_key": event_key,
            "summary": (summary or "").strip()[:50000],
            "error": (error or "").strip(),
            "termination_reason": termination_reason,
            "repository": repository,
            "branch": branch,
            "commit": commit,
            "pr_url": pr_url,
            "remaining_issues": remaining_issues,
            "blockers": blockers,
        }
        atomic_write_json(run_path / "COMPLETE.json", complete)
        return run_path, complete

    def update_complete(self, run_path: Path, updates: dict[str, Any]) -> dict[str, Any]:
        complete = read_json(run_path / "COMPLETE.json", default={}) or {}
        for key, value in updates.items():
            if isinstance(value, dict) and isinstance(complete.get(key), dict):
                complete[key].update(value)
            else:
                complete[key] = value
        atomic_write_json(run_path / "COMPLETE.json", complete)
        return complete

    def task_records(self, project: dict[str, Any]) -> list[dict[str, Any]]:
        records = []
        tdir = self.tasks_root(project)
        for task_path in sorted(tdir.iterdir()) if tdir.exists() else []:
            if not task_path.is_dir():
                continue
            meta = read_json(task_path / "META.json", default=None)
            if not meta:
                continue
            runs = []
            rdir = task_path / "RUNS"
            for run_path in sorted(rdir.iterdir()) if rdir.exists() else []:
                complete = read_json(run_path / "COMPLETE.json", default=None)
                if complete:
                    runs.append({"path": run_path, "complete": complete})
            records.append({"path": task_path, "meta": meta, "runs": runs})
        return records

    def generate_index(self, project: dict[str, Any], verified_only: bool = False, include_local_paths: bool = True) -> str:
        records = self.task_records(project)
        rows = []
        open_issues = []
        total_runs = 0
        verified_runs = 0
        for record in records:
            meta = record["meta"]
            for run in record["runs"]:
                complete = run["complete"]
                total_runs += 1
                upload = complete.get("upload") or {}
                verified = bool(upload.get("verified"))
                if verified:
                    verified_runs += 1
                if verified_only and not verified:
                    continue
                completed = complete.get("completed_at") or ""
                tests = (complete.get("tests") or {}).get("status", "not_verified")
                deployment = (complete.get("deployment") or {}).get("status", "not_verified")
                outcome = complete.get("outcome", "unknown")
                report_rel = f"TASKS/{meta['task_folder']}/RUNS/{complete.get('run_id')}/REPORT.md"
                report_cell = f"[REPORT.md]({report_rel})"
                upload_cell = "verified" if verified else "pending"
                rows.append(
                    {
                        "completed": completed,
                        "task_id": meta.get("task_id", ""),
                        "title": meta.get("title", ""),
                        "run_id": complete.get("run_id", ""),
                        "agent": (complete.get("agent") or {}).get("name") or meta.get("agent", ""),
                        "outcome": outcome,
                        "tests": tests,
                        "deployment": deployment,
                        "report": report_cell,
                        "upload": upload_cell,
                        "report_local": str(run["path"] / "REPORT.md"),
                    }
                )
                if outcome in ("failed", "blocked") or (complete.get("remaining_issues") or "").strip() or (complete.get("blockers") or "").strip():
                    labels = []
                    if complete.get("remaining_issues"):
                        labels.append(str(complete["remaining_issues"]).strip())
                    if complete.get("blockers"):
                        labels.append(str(complete["blockers"]).strip())
                    if not labels:
                        labels.append(f"Run {complete.get('run_id')} ended as {outcome}.")
                    open_issues.append(f"- {meta.get('task_id')} / {complete.get('run_id')}: " + " ".join(labels))
        rows.sort(key=lambda item: (item["completed"], item["run_id"]), reverse=True)
        open_issues = open_issues[: int((self.config.get("index") or {}).get("max_open_issues", 20))]
        display_runs = len(rows) if verified_only else total_runs
        display_tasks = len({row["task_id"] for row in rows}) if verified_only else len(records)
        lines = []
        lines.append(f"# {project['name']} — Agent Execution Reports")
        lines.append("")
        lines.append("> Report index only. Product roadmap and chat-recovery documents live elsewhere.")
        lines.append(f"> Drive location: `03 — Agent Execution Reports/{project['name']}`")
        lines.append("")
        lines.append(f"Generated: {utcnow_iso()}")
        lines.append("")
        lines.append(f"- Tasks: {display_tasks}")
        lines.append(f"- Runs: {display_runs}")
        lines.append(f"- Upload verified: {verified_runs}")
        if verified_only:
            lines.append("- Scope: upload-verified runs only")
        else:
            lines.append(f"- Upload pending: {max(total_runs - verified_runs, 0)}")
        lines.append("")
        if rows:
            lines.append("| Completed (UTC) | Task ID | Title | Run ID | Agent | Outcome | Tests | Deployment | Upload | Report |")
            lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
            for row in rows:
                title = str(row["title"]).replace("|", "\\|")
                lines.append(
                    f"| {row['completed']} | `{row['task_id']}` | {title} | `{row['run_id']}` | {row['agent']} | {row['outcome']} | {row['tests']} | {row['deployment']} | {row['upload']} | {row['report']} |"
                )
            if include_local_paths:
                lines.append("")
                lines.append("Local report paths:")
                lines.append("")
                for row in rows:
                    lines.append(f"- `{row['run_id']}`: `{row['report_local']}`")
        else:
            lines.append("No runs recorded yet.")
        lines.append("")
        lines.append("## Open issues and blockers")
        lines.append("")
        if open_issues:
            lines.extend(open_issues)
        else:
            lines.append("None recorded.")
        lines.append("")
        lines.append("A task/agent outcome does not imply that tests or deployment succeeded. Check each report for evidence.")
        lines.append("")
        return "\n".join(lines)

    def write_local_index(self, project: dict[str, Any]) -> Path:
        path = self.project_dir(project) / "PROJECT-STATUS.md"
        atomic_write_text(path, self.generate_index(project, verified_only=False, include_local_paths=True))
        return path

    def enqueue(self, kind: str, project: dict[str, Any], *, task_id: str | None = None, run_id: str | None = None, files: list[dict[str, str]] | None = None) -> Path:
        ensure_dir(self.queue_root)
        for item in self.queue_items():
            data = item.get("data") or {}
            if data.get("kind") == kind and data.get("project_key") == project.get("key") and data.get("task_id") == task_id and data.get("run_id") == run_id:
                return item["path"]
        item_id = new_id("q-")
        data = {
            "id": item_id,
            "kind": kind,
            "project": project["name"],
            "project_key": project.get("key"),
            "task_id": task_id,
            "run_id": run_id,
            "files": files or [],
            "created_at": utcnow_iso(),
            "attempts": 0,
            "last_error": None,
            "next_attempt_at": utcnow_iso(),
        }
        path = self.queue_root / f"{item_id}.json"
        atomic_write_json(path, data)
        return path

    def queue_items(self) -> list[dict[str, Any]]:
        items = []
        for path in sorted(self.queue_root.glob("*.json")):
            data = read_json(path, default=None)
            if data:
                items.append({"path": path, "data": data})
        return items

    def remove_queue_item(self, path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def task_auto_state(self, task_path: Path) -> dict[str, Any]:
        return read_json(task_path / "AUTO.json", default={"seen": {}, "order": []}) or {"seen": {}, "order": []}

    def save_task_auto_state(self, task_path: Path, state: dict[str, Any]) -> None:
        order = state.get("order", [])
        if len(order) > 120:
            drop = order[:-120]
            state["order"] = order[-120:]
            for key in drop:
                state.get("seen", {}).pop(key, None)
        atomic_write_json(task_path / "AUTO.json", state)

    def record_seen(self, task_path: Path, event_key: str, run_id: str) -> bool:
        if not event_key:
            return False
        state = self.task_auto_state(task_path)
        if event_key in state.get("seen", {}):
            return True
        state.setdefault("seen", {})[event_key] = {"run_id": run_id, "at": utcnow_iso()}
        order = state.setdefault("order", [])
        if event_key in order:
            order.remove(event_key)
        order.append(event_key)
        self.save_task_auto_state(task_path, state)
        return False

    def session_entry(self, agent: str, session_id: str) -> dict[str, Any] | None:
        return (self.load_sessions() or {}).get(f"{agent}:{session_id}")

    def set_session_entry(self, agent: str, session_id: str, entry: dict[str, Any]) -> None:
        sessions = self.load_sessions()
        sessions[f"{agent}:{session_id}"] = entry
        self.save_sessions(sessions)

    def queue_index(self, project: dict[str, Any]) -> Path:
        return self.enqueue("index", project)

    def queue_run(self, project: dict[str, Any], task_path: Path, meta: dict[str, Any], run_path: Path, run_id: str) -> Path:
        files = [
            {"local": str(task_path / "PROMPT.md"), "remote": f"TASKS/{meta['task_folder']}/PROMPT.md"},
            {"local": str(task_path / "META.json"), "remote": f"TASKS/{meta['task_folder']}/META.json"},
        ]
        if (run_path / "PROMPT.md").exists():
            files.append({"local": str(run_path / "PROMPT.md"), "remote": f"TASKS/{meta['task_folder']}/RUNS/{run_id}/PROMPT.md"})
        files.extend(
            [
                {"local": str(run_path / "REPORT.md"), "remote": f"TASKS/{meta['task_folder']}/RUNS/{run_id}/REPORT.md"},
                {"local": str(run_path / "COMPLETE.json"), "remote": f"TASKS/{meta['task_folder']}/RUNS/{run_id}/COMPLETE.json"},
            ]
        )
        return self.enqueue("run", project, task_id=meta["task_id"], run_id=run_id, files=files)

    def run_record(self, project: dict[str, Any], task_id: str, run_id: str) -> tuple[Path, Path, dict[str, Any], dict[str, Any], dict[str, Any]]:
        found_project, task_path, meta = self.find_task(task_id, project.get("name"))
        run_path = task_path / "RUNS" / run_id
        complete = read_json(run_path / "COMPLETE.json", default=None)
        if not complete:
            raise FileNotFoundError(f"run not found: {task_id}/{run_id}")
        return found_project, task_path, meta, run_path, complete

    def cleanup_tmp(self, max_age_seconds: int = 86400) -> None:
        import time

        now = time.time()
        for path in self.tmp_root.iterdir() if self.tmp_root.exists() else []:
            try:
                if now - path.stat().st_mtime > max_age_seconds:
                    if path.is_dir():
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        path.unlink(missing_ok=True)
            except OSError:
                pass
