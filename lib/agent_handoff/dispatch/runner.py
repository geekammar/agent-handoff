from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..adapters.common import git_info
from ..config import project_by_name
from ..report import auto_report
from ..store import Store
from ..upload import Uploader
from ..util import FileLock, new_id, safe_title, sanitize_error, utcnow_iso
from .adapters.base import ExecutionRequest, ExecutionResult
from .adapters.opencode import OpenCodeAdapter
from .delivery import (
    build_issue_comment,
    delivery_marker,
    publication_backoff,
    publication_due,
    reconcile_verified_delivery,
    rejection_marker,
)
from .github import GithubError, GithubTransport
from .ledger import Ledger, new_dispatch_record
from .requests import RequestError, detect_request, parse_issue_request, resolve_request


class DispatchRunner:
    def __init__(self, config: dict[str, Any], dispatch: dict[str, Any]):
        self.config = config
        self.dispatch = dispatch
        self.store = Store(config)
        self.ledger = Ledger(config)
        self.transport = GithubTransport(dispatch)
        self.adapter = OpenCodeAdapter(dispatch.get("execution") or {})
        self.uploader = Uploader(config, self.store)
        self.log_path = self.store.logs_root / "dispatch.log"
        self.summary: dict[str, Any] = {}

    def _log(self, message: str) -> None:
        try:
            if self.log_path.exists() and self.log_path.stat().st_size > 2 * 1024 * 1024:
                self.log_path.replace(self.log_path.with_suffix(".log.1"))
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(f"{utcnow_iso()} {sanitize_error(message, 4000)}\n")
            os.chmod(self.log_path, 0o600)
        except OSError:
            pass

    def runner_lock(self) -> FileLock:
        return FileLock(self.ledger.root / "runner.lock", blocking=False, timeout=0)

    def run_once(self, *, force: bool = False) -> dict[str, Any]:
        lock = self.runner_lock()
        if not lock.acquire():
            return {"ok": True, "busy": True, "executed": 0, "published": 0, "errors": []}
        self.summary = {
            "ok": True,
            "busy": False,
            "executed": 0,
            "rejected": 0,
            "needs_attention": 0,
            "uploaded": 0,
            "published": 0,
            "errors": [],
            "warnings": [],
        }
        try:
            try:
                inbox = self.transport.verify_inbox()
            except GithubError as exc:
                self.summary["ok"] = False
                self.summary["errors"].append(sanitize_error(str(exc)))
                return self.summary
            self._log(f"poll start repository={inbox.get('repository')} user={inbox.get('user')}")
            self._recover_inflight(force=force)
            self._advance_all(force=force)
            try:
                issues = self.transport.list_open_issues()
            except GithubError as exc:
                self.summary["ok"] = False
                self.summary["errors"].append(sanitize_error(str(exc)))
                return self.summary
            for issue in issues:
                try:
                    self._process_issue(issue, force=force)
                except Exception as exc:
                    self.summary["ok"] = False
                    self.summary["errors"].append(sanitize_error(f"issue #{issue.get('number')}: {exc}"))
                    self._log(f"issue #{issue.get('number')} failed: {exc}")
            self._advance_all(force=force)
            self.summary["records"] = len(self.ledger.all())
            return self.summary
        finally:
            lock.release()

    def _recover_inflight(self, *, force: bool) -> None:
        for record in self.ledger.all():
            status = record.get("status")
            if status in ("CLAIMED", "RUNNING"):
                self._recover_uncertain_execution(record, force=force)
            elif status == "EXECUTION_COMPLETED":
                self._recover_capture(record, force=force)

    def _recover_uncertain_execution(self, record: dict[str, Any], *, force: bool) -> None:
        session = record.get("session") or {}
        events_path = session.get("events_path")
        if session.get("id") and events_path and Path(events_path).is_file():
            result = self.adapter.recover(events_path)
            if result:
                self._log(f"recovered session output for issue #{record.get('issue_number')} without re-execution")
                self._apply_execution_result(record, result)
                self._capture(record, result, force=force)
                self._upload_and_publish(record, force=force)
                return
        reason = (
            "process restarted after claim/start without recoverable session output; "
            "the agent was not re-run. Inspect the host and submit a new issue if needed."
        )
        self.ledger.set_status(
            record["repository"],
            record["issue_number"],
            "NEEDS_ATTENTION",
            note="uncertain execution after restart",
            updates={
                "attention_reason": reason,
                "execution": {"status": "UNCERTAIN", "last_error": reason},
            },
        )
        self.summary["needs_attention"] += 1
        self.summary["ok"] = False
        self._log(f"issue #{record.get('issue_number')} needs attention: {reason}")

    def _recover_capture(self, record: dict[str, Any], *, force: bool) -> None:
        capture = record.get("capture") or {}
        if capture.get("status") == "CAPTURED":
            self._upload_and_publish(record, force=force)
            return
        session = record.get("session") or {}
        events_path = session.get("events_path")
        result = self.adapter.recover(events_path) if events_path else None
        if not result:
            reason = "execution finished but no session output was persisted; operator review required"
            self.ledger.set_status(
                record["repository"],
                record["issue_number"],
                "NEEDS_ATTENTION",
                note="capture recovery failed",
                updates={"attention_reason": reason, "execution": {"status": "EXECUTION_COMPLETED", "last_error": reason}},
            )
            self.summary["needs_attention"] += 1
            self.summary["ok"] = False
            return
        self._capture(record, result, force=force)
        self._upload_and_publish(record, force=force)

    def _advance_all(self, *, force: bool) -> None:
        for record in self.ledger.all():
            status = record.get("status")
            if status in ("NEW", "CLAIMED", "RUNNING", "REJECTED", "NEEDS_ATTENTION", "ISSUE_UPDATED"):
                continue
            if status in ("EXECUTION_COMPLETED",):
                self._recover_capture(record, force=force)
            elif status in ("REPORT_CAPTURED", "UPLOAD_PENDING"):
                self._upload_and_publish(record, force=force)
            elif status == "UPLOAD_VERIFIED":
                self._publish(record, force=force)

    def _process_issue(self, issue: dict[str, Any], *, force: bool) -> None:
        if not isinstance(issue, dict) or "pull_request" in issue:
            return
        body = str(issue.get("body") or "")
        if not detect_request(body):
            return
        number = int(issue["number"])
        repository = self.dispatch["repository"]
        try:
            request = parse_issue_request(body)
            if request is None:
                return
            resolved = resolve_request(request, self.dispatch)
        except RequestError as exc:
            self._reject(issue, str(exc), comment=self.transport.issue_author_allowed(issue))
            return
        if not self.transport.issue_author_allowed(issue):
            self._record_rejection(issue, "issue author is not authorized in dispatch.allowed_users", comment=False)
            return

        existing = self.ledger.get(repository, number)
        if existing:
            if existing.get("status") == "NEW":
                self._execute(existing, force=force)
            return

        record = new_dispatch_record(
            repository=repository,
            issue=issue,
            project=resolved.project,
            agent=request.agent,
            model=resolved.model,
            request={
                "version": request.version,
                "project": request.project,
                "agent": request.agent,
                "model_alias": request.model_alias,
                "model": resolved.model,
                "session": request.session,
                "instruction_bytes": len(request.instructions.encode("utf-8", errors="replace")),
                "body_sha256": request.body_sha256,
                "instructions": request.instructions,
            },
        )
        record, created = self.ledger.create(record)
        if not created:
            if record.get("status") == "NEW":
                self._execute(record, force=force)
            return
        self._log(f"claimed issue #{number} project={resolved.project.get('name')} model={resolved.model}")
        self._execute(record, force=force)

    def _record_rejection(self, issue: dict[str, Any], reason: str, *, comment: bool) -> None:
        repository = self.dispatch["repository"]
        number = int(issue["number"])
        existing = self.ledger.get(repository, number)
        if existing:
            return
        body = str(issue.get("body") or "")
        request = None
        try:
            request = parse_issue_request(body)
        except RequestError:
            pass
        body_hash = request.body_sha256 if request else __import__("hashlib").sha256(body.encode("utf-8")).hexdigest()
        record = {
            "schema": 1,
            "dispatch_id": f"d-{__import__('hashlib').sha256(f'{repository}#{number}'.encode()).hexdigest()[:24]}",
            "repository": repository,
            "issue_number": number,
            "issue_url": issue.get("html_url") or issue.get("url"),
            "issue_title": issue.get("title") or "",
            "request": {"body_sha256": body_hash},
            "project": {},
            "agent": self.dispatch.get("agent"),
            "model": None,
            "session": {"mode": "new", "id": None, "events_path": None},
            "execution": {"status": "NOT_STARTED", "started_at": None, "finished_at": None, "exit_code": None, "timed_out": False, "outcome": None, "termination_reason": None, "last_error": reason},
            "capture": {"status": "NOT_STARTED", "task_id": None, "run_id": None, "report_path": None, "captured_at": None, "last_error": None},
            "upload": {"status": "NOT_STARTED", "verified": False, "verified_at": None, "report_remote_path": None, "report_url": None, "receipt_remote_path": None, "receipt_url": None, "attempts": 0, "last_error": None},
            "publication": {"status": "NOT_STARTED", "attempts": 0, "published_at": None, "comment_url": None, "last_error": None, "next_attempt_at": None},
            "status": "REJECTED",
            "attention_reason": reason,
            "created_at": utcnow_iso(),
            "updated_at": utcnow_iso(),
            "history": [{"at": utcnow_iso(), "status": "REJECTED", "note": reason}],
        }
        record, _ = self.ledger.create(record)
        self.summary["rejected"] += 1
        self._log(f"rejected issue #{number}: {reason}")
        if comment:
            marker = rejection_marker(number, body_hash)
            try:
                if not self.transport.find_comment(number, marker):
                    self.transport.comment(
                        number,
                        f"{marker}\n**Agent Handoff rejected this request.**\n\nReason: {reason}\n\nNo agent session was started.",
                    )
            except GithubError as exc:
                self._log(f"rejection comment failed for issue #{number}: {exc}")

    def _reject(self, issue: dict[str, Any], reason: str, *, comment: bool) -> None:
        self._record_rejection(issue, reason, comment=comment)

    def _execute(self, record: dict[str, Any], *, force: bool) -> None:
        repository = record["repository"]
        number = int(record["issue_number"])
        request_data = record.get("request") or {}
        workspace = str((record.get("project") or {}).get("workspace") or "")
        self.ledger.set_status(
            repository,
            number,
            "CLAIMED",
            note="claimed before execution",
            updates={"execution": {"status": "CLAIMED"}},
        )
        stamp = utcnow_iso().replace(":", "").replace("-", "")
        events_path = str(self.ledger.event_path(repository, number, stamp))
        self.ledger.set_status(
            repository,
            number,
            "RUNNING",
            note="opencode execution started",
            updates={
                "execution": {"status": "RUNNING", "started_at": utcnow_iso(), "last_error": None},
                "session": {"events_path": events_path},
            },
        )
        title = safe_title(f"Agent Handoff issue #{number}: {record.get('issue_title') or record.get('project', {}).get('name', '')}")
        try:
            result = self.adapter.run(
                ExecutionRequest(
                    model=str(record.get("model")),
                    workspace=workspace,
                    title=title,
                    instructions=str(request_data.get("instructions") or ""),
                    permission_profile=str((self.dispatch.get("execution") or {}).get("permission_profile", "read_only")),
                    allow_write=bool((self.dispatch.get("execution") or {}).get("allow_write", False)),
                    timeout_seconds=float((self.dispatch.get("execution") or {}).get("timeout_seconds", 1200)),
                ),
                events_path=events_path,
                on_session=lambda session_id: self.ledger.update(
                    repository,
                    number,
                    lambda item: item.setdefault("session", {}).update({"id": session_id}),
                    note=f"session {session_id} started",
                ),
            )
        except Exception as exc:
            error = sanitize_error(str(exc))
            current = self.ledger.get(repository, number) or record
            session = current.get("session") or {}
            recovered = None
            if session.get("id") and session.get("events_path"):
                recovered = self.adapter.recover(str(session["events_path"]))
            if recovered:
                self._apply_execution_result(current, recovered)
                self._capture(current, recovered, force=force)
                self._upload_and_publish(current, force=force)
                return
            self.ledger.set_status(
                repository,
                number,
                "NEEDS_ATTENTION",
                note="execution raised before recoverable output",
                updates={
                    "attention_reason": error,
                    "execution": {"status": "FAILED", "last_error": error, "finished_at": utcnow_iso()},
                },
            )
            self.summary["needs_attention"] += 1
            self.summary["ok"] = False
            self._log(f"issue #{number} execution failed: {error}")
            return
        self.summary["executed"] += 1
        self._log(
            f"issue #{number} execution finished outcome={result.outcome} exit={result.exit_code} "
            f"session={result.session_id or 'none'} events={result.event_count}"
        )
        self._apply_execution_result(record, result)
        current = self.ledger.get(repository, number) or record
        self._capture(current, result, force=force)
        current = self.ledger.get(repository, number) or current
        self._upload_and_publish(current, force=force)

    def _apply_execution_result(self, record: dict[str, Any], result: ExecutionResult) -> None:
        self.ledger.set_status(
            record["repository"],
            int(record["issue_number"]),
            "EXECUTION_COMPLETED",
            note=f"execution completed: {result.outcome}",
            updates={
                "execution": {
                    "status": "COMPLETED",
                    "finished_at": result.finished_at,
                    "exit_code": result.exit_code,
                    "timed_out": result.timed_out,
                    "outcome": result.outcome,
                    "termination_reason": result.termination_reason,
                    "last_error": result.error,
                },
                "session": {"id": result.session_id, "events_path": result.events_path},
            },
        )

    def _project_for(self, record: dict[str, Any]) -> dict[str, Any] | None:
        name = (record.get("project") or {}).get("core_name") or (record.get("project") or {}).get("name")
        project = project_by_name(self.config, str(name)) if name else None
        if project:
            project = dict(project)
            project["key"] = project.get("key") or str(project.get("name", "")).lower()
        return project

    def _capture(self, record: dict[str, Any], result: ExecutionResult, *, force: bool) -> bool:
        repository = record["repository"]
        number = int(record["issue_number"])
        current = self.ledger.get(repository, number) or record
        capture = current.get("capture") or {}
        if capture.get("status") == "CAPTURED":
            return True
        project = self._project_for(current)
        if not project:
            reason = "Agent Handoff project is no longer configured"
            self.ledger.set_status(
                repository,
                number,
                "NEEDS_ATTENTION",
                note="capture failed",
                updates={"attention_reason": reason, "capture": {"status": "FAILED", "last_error": reason}},
            )
            self.summary["needs_attention"] += 1
            self.summary["ok"] = False
            return False
        task_id = capture.get("task_id") or new_id("t-")
        run_id = capture.get("run_id") or new_id("r-")
        if not capture.get("task_id") or not capture.get("run_id"):
            current = self.ledger.update(
                repository,
                number,
                lambda item: item.setdefault("capture", {}).update({"task_id": task_id, "run_id": run_id}),
                note="reserved Agent Handoff task/run ids",
            )
        request_data = current.get("request") or {}
        session = current.get("session") or {}
        instructions = str(request_data.get("instructions") or "")
        try:
            project, task_path, meta, run_path, complete = self.store.run_record(project, task_id, run_id)
        except FileNotFoundError:
            task_path = None
            meta = None
            run_path = None
            complete = None
        except Exception:
            task_path = None
            meta = None
            run_path = None
            complete = None
        if not complete:
            try:
                with self.store.lock():
                    try:
                        _, task_path, meta = self.store.find_task(task_id, project["name"])
                    except FileNotFoundError:
                        task_path, meta = self.store.create_task(
                            project,
                            agent="opencode",
                            title=str(current.get("issue_title") or f"Agent Handoff issue #{number}"),
                            prompt_text=instructions,
                            prompt_captured=True,
                            session_id=session.get("id"),
                            repository=None,
                            branch=None,
                            source="dispatch",
                            model=str(current.get("model")),
                            task_id=task_id,
                        )
                    run_path = task_path / "RUNS" / run_id
                    git = git_info(str((current.get("project") or {}).get("workspace") or ""))
                    report_text = auto_report(
                        task_id=task_id,
                        run_id=run_id,
                        agent="opencode",
                        session_id=session.get("id"),
                        model=str(current.get("model")),
                        outcome=result.outcome,
                        summary=result.final_text or "No final assistant output was captured.",
                        repository=git.get("repository") or current.get("repository"),
                        branch=git.get("branch"),
                        commit=git.get("commit"),
                        pr_url=None,
                        tests={
                            "status": "not_verified",
                            "details": "Task executed through the Agent Handoff dispatcher; no independent test evidence.",
                        },
                        deployment={
                            "status": "not_verified",
                            "details": "No independent deployment evidence was captured.",
                        },
                        error=result.error,
                        termination_reason=result.termination_reason,
                        remaining_issues=None,
                        blockers=None,
                        run_dir=str(run_path),
                        remote_report_path=f"TASKS/{meta['task_folder']}/RUNS/{run_id}/REPORT.md",
                    )
                    dispatch_context = [
                        "",
                        "## Dispatch context",
                        "",
                        f"- Repository: `{repository}`",
                        f"- Issue: {current.get('issue_url') or f'#{number}'}",
                        f"- Route: project `{(current.get('project') or {}).get('name')}`, agent `opencode`, model `{current.get('model')}`",
                        f"- Session: `{session.get('id') or 'not captured'}`",
                        f"- Execution outcome: `{result.outcome}`",
                    ]
                    run_path, complete = self.store.add_run(
                        project,
                        task_path,
                        meta,
                        outcome=result.outcome,
                        report_text=report_text + "\n".join(dispatch_context) + "\n",
                        run_prompt_text=instructions,
                        tests={
                            "status": "not_verified",
                            "details": "Task executed through the Agent Handoff dispatcher; no independent test evidence.",
                        },
                        deployment={
                            "status": "not_verified",
                            "details": "No independent deployment evidence was captured.",
                        },
                        summary=result.final_text[:50000] if result.final_text else "No final assistant output was captured.",
                        error=result.error,
                        termination_reason=result.termination_reason,
                        remaining_issues=None,
                        blockers=None,
                        repository=git.get("repository") or current.get("repository"),
                        branch=git.get("branch"),
                        commit=git.get("commit"),
                        pr_url=None,
                        session_id=session.get("id"),
                        model=str(current.get("model")),
                        run_id=run_id,
                        completed_at=result.finished_at,
                        auto_generated=True,
                        event_key=f"dispatch:{repository}#{number}:{run_id}",
                    )
                    self.store.update_complete(
                        run_path,
                        {
                            "dispatch": {
                                "repository": repository,
                                "issue_number": number,
                                "issue_url": current.get("issue_url"),
                                "dispatch_id": current.get("dispatch_id"),
                                "workspace": (current.get("project") or {}).get("workspace"),
                                "events_path": session.get("events_path"),
                            }
                        },
                    )
                    self.store.write_local_index(project)
                    self.store.queue_run(project, task_path, meta, run_path, run_id)
            except Exception as exc:
                error = sanitize_error(str(exc))
                self.ledger.set_status(
                    repository,
                    number,
                    "EXECUTION_COMPLETED",
                    note="report capture failed; retrying without re-execution",
                    updates={"capture": {"status": "FAILED", "last_error": error}},
                )
                self.summary["ok"] = False
                self._log(f"issue #{number} capture failed: {error}")
                return False
        self.ledger.set_status(
            repository,
            number,
            "UPLOAD_PENDING",
            note="report captured and queued for verified upload",
            updates={
                "capture": {
                    "status": "CAPTURED",
                    "task_id": task_id,
                    "run_id": run_id,
                    "report_path": str(run_path / "REPORT.md"),
                    "captured_at": utcnow_iso(),
                    "last_error": None,
                },
                "upload": {"status": "PENDING"},
            },
        )
        return True

    def _upload_and_publish(self, record: dict[str, Any], *, force: bool) -> None:
        current = self.ledger.get(record["repository"], int(record["issue_number"])) or record
        if current.get("status") in ("NEEDS_ATTENTION", "REJECTED", "ISSUE_UPDATED"):
            return
        capture = current.get("capture") or {}
        if capture.get("status") != "CAPTURED":
            return
        project = self._project_for(current)
        if not project:
            reason = "Agent Handoff project is no longer configured"
            self.ledger.set_status(
                current["repository"],
                int(current["issue_number"]),
                "NEEDS_ATTENTION",
                note="upload failed: missing project",
                updates={"attention_reason": reason, "upload": {"status": "FAILED", "last_error": reason}},
            )
            self.summary["needs_attention"] += 1
            self.summary["ok"] = False
            return
        task_id = str(capture.get("task_id"))
        run_id = str(capture.get("run_id"))
        result = reconcile_verified_delivery(
            self.config,
            self.store,
            self.uploader,
            project,
            task_id,
            run_id,
            force=force,
        )
        if not result.get("ok"):
            attempts = int((current.get("upload") or {}).get("attempts", 0)) + 1
            error = sanitize_error(result.get("error") or "upload reconciliation failed")
            self.ledger.set_status(
                current["repository"],
                int(current["issue_number"]),
                "UPLOAD_PENDING",
                note="verified upload pending retry",
                updates={
                    "upload": {
                        "status": "FAILED",
                        "verified": False,
                        "attempts": attempts,
                        "last_error": error,
                    }
                },
            )
            self.summary["ok"] = False
            self.summary["errors"].append(f"issue #{current['issue_number']}: {error}")
            self._log(f"issue #{current['issue_number']} upload failed: {error}")
            return
        self.ledger.set_status(
            current["repository"],
            int(current["issue_number"]),
            "UPLOAD_VERIFIED",
            note="report upload and receipt verified",
            updates={
                "upload": {
                    "status": "VERIFIED",
                    "verified": True,
                    "verified_at": result.get("verified_at") or (current.get("upload") or {}).get("verified_at"),
                    "report_remote_path": result.get("report_remote_path"),
                    "report_url": result.get("report_url"),
                    "receipt_remote_path": result.get("receipt_remote_path"),
                    "receipt_url": result.get("receipt_url"),
                    "last_error": None,
                }
            },
        )
        self.summary["uploaded"] += 1
        current = self.ledger.get(current["repository"], int(current["issue_number"])) or current
        self._publish(current, force=force)

    def _publish(self, record: dict[str, Any], *, force: bool) -> None:
        current = self.ledger.get(record["repository"], int(record["issue_number"])) or record
        if current.get("status") == "ISSUE_UPDATED":
            return
        publication = current.get("publication") or {}
        if publication.get("status") == "PUBLISHED":
            return
        if not publication_due(current, force=force):
            return
        capture = current.get("capture") or {}
        if not (current.get("upload") or {}).get("report_url") and capture.get("status") == "CAPTURED":
            project = self._project_for(current)
            if project:
                reconciled = reconcile_verified_delivery(
                    self.config,
                    self.store,
                    self.uploader,
                    project,
                    str(capture.get("task_id")),
                    str(capture.get("run_id")),
                    force=force,
                )
                if reconciled.get("ok"):
                    current = self.ledger.update(
                        current["repository"],
                        int(current["issue_number"]),
                        lambda item: item.setdefault("upload", {}).update(
                            {
                                "status": "VERIFIED",
                                "verified": True,
                                "verified_at": reconciled.get("verified_at"),
                                "report_remote_path": reconciled.get("report_remote_path"),
                                "report_url": reconciled.get("report_url"),
                                "receipt_remote_path": reconciled.get("receipt_remote_path"),
                                "receipt_url": reconciled.get("receipt_url"),
                                "last_error": None,
                            }
                        ),
                        note="hydrated verified upload metadata before publication",
                    )
                else:
                    error = sanitize_error(reconciled.get("error") or "upload metadata reconciliation failed")
                    self.ledger.update(
                        current["repository"],
                        int(current["issue_number"]),
                        lambda item: item.setdefault("publication", {}).update(
                            {"status": "FAILED", "last_error": error}
                        ),
                        note="publication deferred: upload metadata is not verified",
                    )
                    self.summary["ok"] = False
                    self.summary["errors"].append(f"issue #{current['issue_number']}: {error}")
                    return
        run_id = str(capture.get("run_id") or current.get("dispatch_id"))
        marker = delivery_marker(run_id)
        try:
            existing = self.transport.find_comment(int(current["issue_number"]), marker)
            if existing:
                self.ledger.set_status(
                    current["repository"],
                    int(current["issue_number"]),
                    "ISSUE_UPDATED",
                    note="existing verified delivery comment found",
                    updates={
                        "publication": {
                            "status": "PUBLISHED",
                            "published_at": existing.get("created_at") or utcnow_iso(),
                            "comment_url": existing.get("html_url"),
                            "last_error": None,
                        }
                    },
                )
                return
            response = self.transport.comment(int(current["issue_number"]), build_issue_comment(current))
            self.ledger.set_status(
                current["repository"],
                int(current["issue_number"]),
                "ISSUE_UPDATED",
                note="verified report URL published to the originating issue",
                updates={
                    "publication": {
                        "status": "PUBLISHED",
                        "published_at": utcnow_iso(),
                        "comment_url": response.get("url"),
                        "last_error": None,
                    }
                },
            )
            self.summary["published"] += 1
            self._log(f"issue #{current['issue_number']} verified report published")
        except GithubError as exc:
            attempts = int(publication.get("attempts", 0)) + 1
            error = sanitize_error(str(exc))
            max_attempts = int((self.dispatch.get("delivery") or {}).get("max_attempts", 0))
            exhausted = bool(max_attempts and attempts >= max_attempts)
            self.ledger.set_status(
                current["repository"],
                int(current["issue_number"]),
                "UPLOAD_VERIFIED",
                note="GitHub publication failed; retryable",
                updates={
                    "publication": {
                        "status": "FAILED",
                        "attempts": attempts,
                        "last_error": error,
                        "next_attempt_at": publication_backoff(
                            attempts, int((self.dispatch.get("delivery") or {}).get("retry_seconds", 60))
                        ),
                    },
                    "attention_reason": error if exhausted else current.get("attention_reason"),
                },
            )
            self.summary["ok"] = False
            self.summary["errors"].append(f"issue #{current['issue_number']} publication: {error}")
            self._log(f"issue #{current['issue_number']} publication failed: {error}")
