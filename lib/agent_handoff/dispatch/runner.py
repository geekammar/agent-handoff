from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from ..adapters.common import git_info
from ..config import BASE_DIR, all_projects, project_by_name
from ..report import auto_report
from ..store import Store
from ..upload import Uploader
from ..util import FileLock, new_id, safe_title, sanitize_error, utcnow_iso, utcnow_iso_precise
from .adapters.base import ExecutionRequest, ExecutionResult
from .adapters.opencode import OpenCodeAdapter
from .coding.pipeline import CodingOrchestrator, PER_STAGE_ORDER, PUBLICATION_LIMITATIONS
from .coding.publication import TrustedPublisher, build_pr_body
from .coding.report import coding_next_action, coding_outcome, coding_report_sections
from .coding.testrunner import test_evidence_for_report
from .coding.workspace import cleanup_workspace
from .delivery import (
    build_issue_comment,
    delivery_marker,
    merged_upload_attempts,
    publication_backoff,
    publication_due,
    reconcile_verified_delivery,
    rejection_marker,
    upload_backoff,
    upload_due,
)
from .github import GithubError, GithubTransport
from .ledger import Ledger, new_coding_state, new_dispatch_record
from .progress import ProgressReporter
from .security import (
    WorkspaceSecurityError,
    validate_model_auth,
    validate_workspace_filesystem,
    validate_workspace_path,
)
from ..notify.config import notification_token_env_names
from ..notify.service import NotificationService
from .config import resolve_dispatch_project, resolve_effective_isolation
from .requests import RequestError, detect_request, parse_issue_request, resolve_request


class DispatchRunner:
    def __init__(self, config: dict[str, Any], dispatch: dict[str, Any], *, progress: ProgressReporter | None = None):
        self.config = config
        self.dispatch = dispatch
        self.store = Store(config)
        self.ledger = Ledger(config)
        self.transport = GithubTransport(dispatch)
        self.adapter = OpenCodeAdapter(
            dispatch.get("execution") or {},
            secret_env_names=notification_token_env_names(config),
        )
        self.uploader = Uploader(config, self.store)
        self.log_path = self.store.logs_root / "dispatch.log"
        self.progress = progress
        try:
            self.notifications: NotificationService | None = NotificationService(config, log=self._log)
            self._notification_init_error: str | None = None
        except Exception as exc:
            self.notifications = None
            self._notification_init_error = sanitize_error(str(exc))
        self.summary: dict[str, Any] = {}
        self._started_at = time.monotonic()
        if self.progress is not None:
            paths = [
                str(self.store.data_root),
                str(self.store.queue_root),
                str(self.store.state_root),
                str(self.store.locks_root),
                str(self.store.logs_root),
                str(self.store.tmp_root),
                str(Path.home()),
            ]
            for project in all_projects(config):
                paths.extend(str(path) for path in (project.get("paths") or []))
            for item in (dispatch.get("projects") or {}).values():
                if isinstance(item, dict) and item.get("workspace"):
                    paths.append(str(item["workspace"]))
                if isinstance(item, dict):
                    coding = item.get("coding") or {}
                    for key in ("workspace_root", "staging_root", "source_workspace"):
                        if coding.get(key):
                            paths.append(str(coding[key]))
            coding_global = dispatch.get("coding") or {}
            if coding_global.get("staging_root"):
                paths.append(str(coding_global["staging_root"]))
            self.progress.add_redactions(paths)

    def _progress(self, stage: str, message: str, record: dict[str, Any] | None = None) -> None:
        if self.progress is None:
            return
        issue = None
        if record is not None:
            try:
                issue = int(record["issue_number"])
            except (KeyError, TypeError, ValueError):
                issue = None
        self.progress.emit(stage, message, issue=issue)

    def _log(self, message: str) -> None:
        try:
            if self.log_path.exists() and self.log_path.stat().st_size > 2 * 1024 * 1024:
                self.log_path.replace(self.log_path.with_suffix(".log.1"))
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(f"{utcnow_iso()} {sanitize_error(message, 4000)}\n")
            os.chmod(self.log_path, 0o600)
        except OSError:
            pass

    def _notify_completed(self, record: dict[str, Any]) -> None:
        if self.notifications is None:
            return
        try:
            queued = self.notifications.enqueue_completed(record)
            if queued:
                self._progress("notifying", "verified delivery notification queued", record)
        except Exception as exc:
            self._log(f"notification enqueue failed: {exc}")

    def _notify_attention(self, record: dict[str, Any], reason: str, next_action: str) -> None:
        if self.notifications is None:
            return
        try:
            queued = self.notifications.enqueue_needs_attention(record, reason, next_action)
            if queued:
                self._progress("notifying", "operator attention notification queued", record)
        except Exception as exc:
            self._log(f"notification enqueue failed: {exc}")

    def _notify_delayed(self, record: dict[str, Any], *, reason: str, stage: str, attempts: int) -> None:
        if self.notifications is None:
            return
        try:
            queued = self.notifications.enqueue_delivery_delayed(
                record, reason=reason, stage=stage, attempts=attempts
            )
            if queued:
                self._progress("notifying", "delivery delay notification queued", record)
        except Exception as exc:
            self._log(f"notification enqueue failed: {exc}")

    def _reconcile_notifications(self) -> None:
        if self.notifications is None:
            return
        try:
            result = self.notifications.reconcile(self.ledger.all())
        except Exception as exc:
            self.summary["warnings"].append(
                f"notification recovery failed independently: {sanitize_error(str(exc))}"
            )
            return
        if result.get("queued"):
            self._progress("notifying", f"recovered {result['queued']} notification intent(s) after restart")

    def _drain_notifications(self) -> None:
        if self.notifications is None:
            if self._notification_init_error:
                self.summary["warnings"].append(
                    f"notification delivery failed independently: {self._notification_init_error}"
                )
            return
        try:
            result = self.notifications.drain()
        except Exception as exc:
            self.summary["warnings"].append(f"notification delivery failed independently: {sanitize_error(str(exc))}")
            return
        self.summary["notifications"] = result
        if result.get("sent"):
            self._progress("notified", f"sent {result.get('sent')} Telegram notification(s)")
        for error in result.get("errors") or []:
            self.summary["warnings"].append(f"notification: {error}")

    def runner_lock(self) -> FileLock:
        return FileLock(self.ledger.root / "runner.lock", blocking=False, timeout=0)

    def run_once(self, *, force: bool = False) -> dict[str, Any]:
        lock = self.runner_lock()
        if not lock.acquire():
            self._progress("busy", "another dispatcher process already holds the runner lock; nothing was started")
            return {"ok": True, "busy": True, "executed": 0, "published": 0, "errors": []}
        self._started_at = time.monotonic()
        if self.progress is not None:
            self.progress.reset()
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
            self._progress("discovering", "verifying the private issue inbox and dispatcher authorization")
            try:
                inbox = self.transport.verify_inbox()
            except GithubError as exc:
                self.summary["ok"] = False
                self.summary["errors"].append(sanitize_error(str(exc)))
                return self.summary
            self._log(f"poll start repository={inbox.get('repository')} user={inbox.get('user')}")
            self._progress("discovering", "inbox verified; recovering durable records without re-execution")
            self._recover_inflight(force=force)
            self._advance_all(force=force)
            self._progress("discovering", "listing open issue requests")
            try:
                issues = self.transport.list_open_issues()
            except GithubError as exc:
                self.summary["ok"] = False
                self.summary["errors"].append(sanitize_error(str(exc)))
                return self.summary
            self._progress("discovering", f"found {len(issues)} open issue(s) to inspect")
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
            try:
                self._reconcile_notifications()
            finally:
                lock.release()
            try:
                self._drain_notifications()
            finally:
                self.summary["elapsed_seconds"] = round(max(0.0, time.monotonic() - self._started_at), 2)
                self._progress(
                    "finished",
                    f"poll finished executed={self.summary.get('executed', 0)} "
                    f"uploaded={self.summary.get('uploaded', 0)} published={self.summary.get('published', 0)} "
                    f"needs_attention={self.summary.get('needs_attention', 0)}",
                )

    def _recover_inflight(self, *, force: bool) -> None:
        for record in self.ledger.all():
            status = record.get("status")
            if record.get("task_type") == "coding":
                if status in ("CLAIMED", "RUNNING", "EXECUTION_COMPLETED"):
                    self._recover_coding(record, force=force)
                continue
            if status in ("CLAIMED", "RUNNING"):
                self._recover_uncertain_execution(record, force=force)
            elif status == "EXECUTION_COMPLETED":
                self._recover_capture(record, force=force)

    def _recover_uncertain_execution(self, record: dict[str, Any], *, force: bool) -> None:
        session = record.get("session") or {}
        events_path = session.get("events_path")
        if session.get("id") and events_path and Path(events_path).is_file():
            self._progress(
                "recovering",
                "existing session output found; capturing and delivering it without re-running OpenCode",
                record,
            )
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
        updated = self.ledger.set_status(
            record["repository"],
            record["issue_number"],
            "NEEDS_ATTENTION",
            note="uncertain execution after restart",
            updates={
                "attention_reason": reason,
                "execution": {"status": "UNCERTAIN", "last_error": reason},
            },
        )
        self._notify_attention(
            updated,
            reason,
            "Inspect the host and persisted session evidence. Do not rerun automatically; submit a new issue if the work must be repeated.",
        )
        self.summary["needs_attention"] += 1
        self.summary["ok"] = False
        self._progress("needs_attention", reason, record)
        self._log(f"issue #{record.get('issue_number')} needs attention: {reason}")

    def _recover_capture(self, record: dict[str, Any], *, force: bool) -> None:
        capture = record.get("capture") or {}
        if capture.get("status") == "CAPTURED":
            self._progress("recovering", "existing report capture found; continuing upload/publication only", record)
            self._upload_and_publish(record, force=force)
            return
        session = record.get("session") or {}
        events_path = session.get("events_path")
        self._progress("recovering", "rebuilding report capture from persisted session events", record)
        result = self.adapter.recover(events_path) if events_path else None
        if not result:
            reason = "execution finished but no session output was persisted; operator review required"
            updated = self.ledger.set_status(
                record["repository"],
                record["issue_number"],
                "NEEDS_ATTENTION",
                note="capture recovery failed",
                updates={"attention_reason": reason, "execution": {"status": "EXECUTION_COMPLETED", "last_error": reason}},
            )
            self._notify_attention(
                updated,
                reason,
                "Inspect the host and persisted session evidence. The agent will not be rerun automatically.",
            )
            self.summary["needs_attention"] += 1
            self.summary["ok"] = False
            self._progress("needs_attention", reason, record)
            return
        self._capture(record, result, force=force)
        self._upload_and_publish(record, force=force)

    def _advance_all(self, *, force: bool) -> None:
        for record in self.ledger.all():
            status = record.get("status")
            if record.get("task_type") == "coding" and status == "ISSUE_UPDATED":
                self._publish(record, force=force)
                continue
            if status in ("NEW", "CLAIMED", "RUNNING", "REJECTED", "NEEDS_ATTENTION", "ISSUE_UPDATED"):
                continue
            if record.get("task_type") == "coding":
                if status == "EXECUTION_COMPLETED":
                    self._advance_coding(record, force=force)
                elif status in ("REPORT_CAPTURED", "UPLOAD_PENDING"):
                    self._upload_and_publish(record, force=force)
                elif status == "UPLOAD_VERIFIED":
                    self._publish(record, force=force)
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
        self._progress("discovered", "versioned request marker found", {"issue_number": number})
        try:
            request = parse_issue_request(body)
            if request is None:
                return
            resolved = resolve_request(request, self.dispatch)
        except RequestError as exc:
            self._progress("rejected", "validation failed; no agent session was started", {"issue_number": number})
            self._reject(issue, str(exc), comment=self.transport.issue_author_allowed(issue))
            return
        if not self.transport.issue_author_allowed(issue):
            self._progress("rejected", "issue author is not authorized; no agent session was started", {"issue_number": number})
            self._record_rejection(issue, "issue author is not authorized in dispatch.allowed_users", comment=False)
            return
        self._progress(
            "validated",
            f"project={resolved.project.get('name')} agent={request.agent} "
            f"model={resolved.model} session={request.session}",
            {"issue_number": number},
        )

        existing = self.ledger.get(repository, number)
        if existing:
            self._progress(
                "resumed",
                f"durable record already exists with status={existing.get('status')}; no duplicate session will be started",
                existing,
            )
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
                "task": request.task,
                "instruction_bytes": len(request.instructions.encode("utf-8", errors="replace")),
                "body_sha256": request.body_sha256,
                "instructions": request.instructions,
            },
            task_type=request.task,
            coding=new_coding_state(resolved.project) if request.task == "coding" else None,
        )
        record, created = self.ledger.create(record)
        if not created:
            if record.get("status") == "NEW":
                self._reserve_coding_ids(record)
                self._execute(record, force=force)
            return
        self._reserve_coding_ids(record)
        record = self.ledger.get(repository, number) or record
        self._progress("claimed", "durable claim persisted before execution", record)
        self._log(f"claimed issue #{number} project={resolved.project.get('name')} model={resolved.model}")
        self._execute(record, force=force)

    def _reserve_coding_ids(self, record: dict[str, Any]) -> dict[str, Any]:
        if record.get("task_type") != "coding":
            return record
        capture = record.get("capture") or {}
        if capture.get("task_id") and capture.get("run_id"):
            return record
        task_id = capture.get("task_id") or new_id("t-")
        run_id = capture.get("run_id") or new_id("r-")

        def apply(item: dict[str, Any]) -> None:
            item.setdefault("capture", {}).update({"task_id": task_id, "run_id": run_id})

        return self.ledger.update(
            record["repository"],
            int(record["issue_number"]),
            apply,
            note="reserved Agent Handoff task/run ids before coding workspace preparation",
        )

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

    def _dispatch_project_for(self, record: dict[str, Any]) -> dict[str, Any]:
        project = record.get("project") or {}
        name = str(project.get("name") or project.get("core_name") or project.get("core_key") or "")
        resolved_project = resolve_dispatch_project(self.dispatch, name)
        if resolved_project:
            return resolved_project
        return {
            "name": name,
            "workspace": project.get("workspace"),
            "execution": project.get("execution") or {},
        }

    def _effective_execution(self, project: dict[str, Any]) -> dict[str, Any]:
        global_execution = self.dispatch.get("execution") or {}
        policy = project.get("execution") or {}
        profile = str(policy.get("permission_profile") or global_execution.get("permission_profile") or "read_only")
        allow_write = bool(policy.get("allow_write", global_execution.get("allow_write", False)))
        return {
            "timeout_seconds": float(global_execution.get("timeout_seconds", 1200)),
            "max_prompt_bytes": int(global_execution.get("max_prompt_bytes", 50000)),
            "permission_profile": profile,
            "allow_write": allow_write,
            "isolation": resolve_effective_isolation(global_execution.get("isolation"), policy),
        }

    def _adapter_for(self, execution: dict[str, Any]) -> OpenCodeAdapter:
        isolation = execution.get("isolation") or {}
        identity = isolation.get("identity") if isolation.get("enabled") else None
        return OpenCodeAdapter(
            execution,
            secret_env_names=notification_token_env_names(self.config),
            identity=identity,
        )

    @staticmethod
    def _isolation_ledger_state(execution: dict[str, Any]) -> dict[str, Any]:
        isolation = execution.get("isolation") or {}
        enabled = bool(isolation.get("enabled"))
        identity = isolation.get("identity") if enabled else None
        return {
            "enabled": enabled,
            "identity_user": (identity or {}).get("user") if identity else None,
            "workspace_roots": list(isolation.get("workspace_roots") or []) if enabled else [],
        }

    def _validate_isolation(self, record: dict[str, Any], project: dict[str, Any], execution: dict[str, Any]) -> str:
        isolation = execution.get("isolation") or {}
        if not isolation.get("enabled"):
            return str((record.get("project") or {}).get("workspace") or "")
        identity = isolation.get("identity")
        if not identity:
            raise WorkspaceSecurityError(
                "isolated execution identity is unavailable; refusing to fall back to the parent identity"
            )
        workspace = str((record.get("project") or {}).get("workspace") or project.get("workspace") or "")
        project_names = [
            str(project.get("name") or ""),
            str(project.get("core_name") or ""),
            str(project.get("core_key") or ""),
        ]
        safe_workspace = validate_workspace_path(
            workspace,
            workspace_roots=isolation.get("workspace_roots") or [],
            project_names=project_names,
            denied_projects=isolation.get("denied_projects") or [],
            protected_paths=[str(BASE_DIR)],
            check_paths=True,
        )
        validate_workspace_filesystem(
            safe_workspace,
            identity,
            require_write=bool(execution.get("allow_write", False)),
            require_clean=True,
            protected_paths=[str(BASE_DIR)],
        )
        validate_model_auth(identity)
        return safe_workspace

    def _execute(self, record: dict[str, Any], *, force: bool) -> None:
        if record.get("task_type") == "coding":
            self._execute_coding(record, force=force)
            return
        repository = record["repository"]
        number = int(record["issue_number"])
        request_data = record.get("request") or {}
        project = self._dispatch_project_for(record)
        execution = self._effective_execution(project)
        workspace = str((record.get("project") or {}).get("workspace") or project.get("workspace") or "")
        profile = str(execution.get("permission_profile") or "read_only")
        allow_write = bool(execution.get("allow_write", False))
        timeout_seconds = float(execution.get("timeout_seconds", 1200))
        self.ledger.set_status(
            repository,
            number,
            "CLAIMED",
            note="claimed before execution",
            updates={
                "execution": {
                    "status": "CLAIMED",
                    "runner_pid": os.getpid(),
                    "permission_profile": profile,
                    "allow_write": allow_write,
                    "isolation": self._isolation_ledger_state(execution),
                }
            },
        )
        try:
            workspace = self._validate_isolation(record, project, execution)
        except Exception as exc:
            error = sanitize_error(f"isolated coding preflight failed: {exc}")
            updated = self.ledger.set_status(
                repository,
                number,
                "NEEDS_ATTENTION",
                note="isolated coding preflight failed; no execution was started",
                updates={
                    "attention_reason": error,
                    "execution": {
                        "status": "FAILED",
                        "permission_profile": profile,
                        "allow_write": allow_write,
                        "isolation": self._isolation_ledger_state(execution),
                        "last_error": error,
                        "finished_at": utcnow_iso(),
                    },
                },
            )
            self._notify_attention(
                updated,
                error,
                "Fix the dedicated identity, model authentication, or clean workspace state. "
                "The agent was not run and will not fall back to root.",
            )
            self.summary["needs_attention"] += 1
            self.summary["ok"] = False
            self._progress("needs_attention", "isolated coding preflight failed; no agent was started", record)
            self._log(f"issue #{number} isolated preflight failed: {error}")
            return
        adapter = self._adapter_for(execution)
        stamp = utcnow_iso().replace(":", "").replace("-", "")
        events_path = str(self.ledger.event_path(repository, number, stamp))
        self.ledger.set_status(
            repository,
            number,
            "RUNNING",
            note="opencode execution started",
            updates={
                "execution": {
                    "status": "RUNNING",
                    "started_at": utcnow_iso(),
                    "last_error": None,
                    "runner_pid": os.getpid(),
                    "permission_profile": profile,
                    "allow_write": allow_write,
                    "isolation": self._isolation_ledger_state(execution),
                },
                "session": {"events_path": events_path},
            },
        )
        self._progress(
            "executing",
            f"OpenCode started with model={record.get('model')} profile={profile} "
            f"(bounded timeout {timeout_seconds:g}s; no total ETA)",
            record,
        )
        title = safe_title(f"Agent Handoff issue #{number}: {record.get('issue_title') or record.get('project', {}).get('name', '')}")
        try:
            result = adapter.run(
                ExecutionRequest(
                    model=str(record.get("model")),
                    workspace=workspace,
                    title=title,
                    instructions=str(request_data.get("instructions") or ""),
                    permission_profile=profile,
                    allow_write=allow_write,
                    timeout_seconds=timeout_seconds,
                ),
                events_path=events_path,
                on_session=lambda session_id: self.ledger.update(
                    repository,
                    number,
                    lambda item: item.setdefault("session", {}).update({"id": session_id}),
                    note=f"session {session_id} started",
                ),
                on_progress=lambda stage, message: self._progress(stage, message, record),
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
            updated = self.ledger.set_status(
                repository,
                number,
                "NEEDS_ATTENTION",
                note="execution raised before recoverable output",
                updates={
                    "attention_reason": error,
                    "execution": {"status": "FAILED", "last_error": error, "finished_at": utcnow_iso()},
                },
            )
            self._notify_attention(
                updated,
                error,
                "Inspect the host and session evidence. The agent will not be rerun automatically.",
            )
            self.summary["needs_attention"] += 1
            self.summary["ok"] = False
            self._progress("needs_attention", "execution failed without recoverable output; the agent will not be re-run", current)
            self._log(f"issue #{number} execution failed: {error}")
            return
        self.summary["executed"] += 1
        self._progress(
            "executed",
            f"execution finished outcome={result.outcome} exit={result.exit_code} "
            f"events={result.event_count} session={'captured' if result.session_id else 'not captured'}",
            record,
        )
        self._log(
            f"issue #{number} execution finished outcome={result.outcome} exit={result.exit_code} "
            f"session={result.session_id or 'none'} events={result.event_count}"
        )
        self._apply_execution_result(record, result)
        current = self.ledger.get(repository, number) or record
        self._capture(current, result, force=force)
        current = self.ledger.get(repository, number) or current
        self._upload_and_publish(current, force=force)

    def _effective_coding_execution(self, project: dict[str, Any]) -> dict[str, Any]:
        global_execution = self.dispatch.get("execution") or {}
        isolation = resolve_effective_isolation(global_execution.get("isolation"), project.get("execution"))
        return {
            "timeout_seconds": float(global_execution.get("timeout_seconds", 1200)),
            "max_prompt_bytes": int(global_execution.get("max_prompt_bytes", 50000)),
            "permission_profile": "workspace_write",
            "allow_write": True,
            "isolation": isolation,
        }

    def _coding_attention(self, record: dict[str, Any], reason: str, stage: str) -> None:
        safe_reason = sanitize_error(reason, 1200)
        if self.progress is not None:
            safe_reason = self.progress.redact(safe_reason)
        updated = self.ledger.set_status(
            record["repository"],
            int(record["issue_number"]),
            "NEEDS_ATTENTION",
            note=f"coding attention required at {stage}",
            updates={
                "attention_reason": safe_reason,
                "execution": {"status": "FAILED", "last_error": safe_reason, "runner_pid": None},
                "coding": {
                    "blocked": True,
                    "blocked_stage": stage,
                    "blocked_reason": safe_reason,
                    "artifacts_preserved": True,
                    "agent": {"status": "uncertain"},
                },
            },
        )
        self._notify_attention(
            updated,
            safe_reason,
            "Inspect the preserved workspace and session evidence. The coding agent is never re-run automatically.",
        )
        self.summary["needs_attention"] += 1
        self.summary["ok"] = False
        self._progress("needs_attention", "coding task requires operator attention; no automatic re-execution", updated)
        self._log(f"issue #{record.get('issue_number')} coding attention at {stage}: {safe_reason}")

    def _coding_adapter(self, execution: dict[str, Any]) -> OpenCodeAdapter:
        return self._adapter_for(execution)

    def _coding_orchestrator(
        self,
        record: dict[str, Any],
        project: dict[str, Any],
        execution: dict[str, Any],
        adapter: OpenCodeAdapter,
    ) -> CodingOrchestrator:
        isolation = execution.get("isolation") or {}
        identity = isolation.get("identity") or {}
        return CodingOrchestrator(
            config=self.config,
            dispatch=self.dispatch,
            ledger=self.ledger,
            record=record,
            project=project,
            identity=identity,
            adapter=adapter,
            progress=lambda stage, message: self._progress(stage, message, record),
            log=self._log,
        )

    def _run_coding_agent(
        self,
        orchestrator: CodingOrchestrator,
        record: dict[str, Any],
        execution: dict[str, Any],
    ) -> ExecutionResult:
        repository = record["repository"]
        number = int(record["issue_number"])
        current = self.ledger.get(repository, number) or record
        coding = current.get("coding") or {}
        workspace = str(coding.get("workspace") or "")
        if not workspace:
            raise RuntimeError("the disposable coding workspace is not available")
        stamp = utcnow_iso().replace(":", "").replace("-", "")
        events_path = str(self.ledger.event_path(repository, number, stamp))
        orchestrator.mark_agent_running(events_path)
        adapter = orchestrator.adapter
        timeout_seconds = float(execution.get("timeout_seconds", 1200))
        title = safe_title(
            f"Agent Handoff issue #{number}: {current.get('issue_title') or current.get('project', {}).get('name', '')}"
        )
        self._progress(
            "executing",
            f"OpenCode coding session started with model={current.get('model')} profile=workspace_write "
            f"(bounded timeout {timeout_seconds:g}s; no total ETA)",
            current,
        )
        try:
            result = adapter.run(
                ExecutionRequest(
                    model=str(current.get("model")),
                    workspace=workspace,
                    title=title,
                    instructions=str((current.get("request") or {}).get("instructions") or ""),
                    permission_profile="workspace_write",
                    allow_write=True,
                    timeout_seconds=timeout_seconds,
                ),
                events_path=events_path,
                on_session=lambda session_id: self.ledger.update(
                    repository,
                    number,
                    lambda item: item.setdefault("session", {}).update({"id": session_id}),
                    note=f"session {session_id} started",
                ),
                on_progress=lambda stage, message: self._progress(stage, message, current),
            )
        except Exception as exc:
            error = sanitize_error(str(exc))
            recovered = None
            try:
                recovered = adapter.recover(events_path)
            except Exception:
                recovered = None
            if recovered:
                self._log(f"recovered coding session output for issue #{number} without re-execution")
                result = recovered
            else:
                result = ExecutionResult(
                    session_id=None,
                    final_text="",
                    termination_reason=None,
                    exit_code=None,
                    timed_out=False,
                    outcome="failed",
                    events_path=events_path,
                    event_count=0,
                    error=error,
                    started_at=utcnow_iso(),
                    finished_at=utcnow_iso(),
                )
        self.summary["executed"] += 1
        self._progress(
            "executed",
            f"coding execution finished outcome={result.outcome} exit={result.exit_code} "
            f"events={result.event_count}; trusted tests are next",
            current,
        )
        self._log(
            f"issue #{number} coding execution finished outcome={result.outcome} exit={result.exit_code} "
            f"session={result.session_id or 'none'}"
        )
        orchestrator.mark_agent_completed(result)
        return result

    def _coding_result_for_capture(self, record: dict[str, Any]) -> ExecutionResult:
        repository = record["repository"]
        number = int(record["issue_number"])
        current = self.ledger.get(repository, number) or record
        session = current.get("session") or {}
        execution = current.get("execution") or {}
        coding = current.get("coding") or {}
        events_path = session.get("events_path") or coding.get("events_path")
        if events_path and Path(events_path).is_file():
            try:
                recovered = self.adapter.recover(str(events_path))
            except Exception:
                recovered = None
            if recovered:
                return recovered
        return ExecutionResult(
            session_id=session.get("id"),
            final_text="",
            termination_reason=execution.get("termination_reason"),
            exit_code=execution.get("exit_code"),
            timed_out=bool(execution.get("timed_out")),
            outcome=str(execution.get("outcome") or ("blocked" if coding.get("blocked") else "unknown")),
            events_path=events_path,
            event_count=0,
            error=execution.get("last_error") or coding.get("blocked_reason"),
            started_at=execution.get("started_at") or utcnow_iso(),
            finished_at=execution.get("finished_at") or utcnow_iso(),
        )

    def _finish_coding(
        self,
        record: dict[str, Any],
        result: ExecutionResult | None,
        *,
        force: bool,
    ) -> None:
        current = self.ledger.get(record["repository"], int(record["issue_number"])) or record
        coding = current.get("coding") or {}
        publication = coding.get("publication") or {}
        agent_done = bool((coding.get("milestones") or {}).get("AGENT_COMPLETED"))
        if result is None:
            result = self._coding_result_for_capture(current)
        terminal_for_report = bool(coding.get("blocked")) or bool(publication.get("pr_url"))
        if not terminal_for_report and agent_done and not publication.get("next_attempt_at"):
            terminal_for_report = True
        if not terminal_for_report:
            if publication.get("next_attempt_at"):
                self._progress(
                    "waiting",
                    f"coding publication retry is not due before {publication.get('next_attempt_at')}; "
                    "the agent will not be re-executed",
                    current,
                )
            return
        capture = current.get("capture") or {}
        if capture.get("status") == "CAPTURED":
            self._upload_and_publish(current, force=force)
            return
        if self._capture(current, result, force=force):
            current = self.ledger.get(record["repository"], int(record["issue_number"])) or current
            self._upload_and_publish(current, force=force)

    def _execute_coding(self, record: dict[str, Any], *, force: bool) -> None:
        repository = record["repository"]
        number = int(record["issue_number"])
        current = self.ledger.get(repository, number) or record
        project = self._dispatch_project_for(current)
        project_coding = project.get("coding") or {}
        if not project_coding.get("enabled"):
            self._coding_attention(
                current,
                "coding is not enabled for this project in trusted configuration; no agent was started",
                "REQUEST_ACCEPTED",
            )
            return
        execution = self._effective_coding_execution(project)
        isolation = execution.get("isolation") or {}
        identity = isolation.get("identity") if isolation.get("enabled") else None
        if not isolation.get("enabled") or not identity:
            self._coding_attention(
                current,
                "coding requires effective isolation with a validated non-root identity; "
                "root fallback is refused and no agent was started",
                "REQUEST_ACCEPTED",
            )
            return
        current = self.ledger.set_status(
            repository,
            number,
            "CLAIMED",
            note="coding request claimed before workspace preparation",
            updates={
                "execution": {
                    "status": "CLAIMED",
                    "runner_pid": os.getpid(),
                    "permission_profile": "workspace_write",
                    "allow_write": True,
                    "isolation": self._isolation_ledger_state(execution),
                },
                "coding": {
                    "limits": dict(project_coding.get("limits") or {}),
                    "repository": project_coding.get("repository"),
                    "base_branch": project_coding.get("base_branch"),
                    "source_workspace": project_coding.get("source_workspace"),
                    "workspace_root": project_coding.get("workspace_root"),
                    "test_profile": project_coding.get("test_profile"),
                    "allowed_model_aliases": list(project_coding.get("allowed_model_aliases") or []),
                },
            },
        )
        adapter = self._coding_adapter(execution)
        orchestrator = self._coding_orchestrator(current, project, execution, adapter)
        try:
            current = orchestrator.prepare()
            current = self.ledger.get(repository, number) or current
            coding = current.get("coding") or {}
            if coding.get("blocked"):
                self._finish_coding(current, None, force=force)
                return
            stage = str(coding.get("stage") or "")
            if stage in ("REQUEST_ACCEPTED", "CLAIMED", "WORKSPACE_PREPARED"):
                result = self._run_coding_agent(orchestrator, current, execution)
            else:
                result = self._coding_result_for_capture(current)
            current = self.ledger.get(repository, number) or current
            current = orchestrator.continue_after_agent(force=force)
            self._finish_coding(current, result, force=force)
        except Exception as exc:
            error = sanitize_error(str(exc))
            self._coding_attention(
                self.ledger.get(repository, number) or current,
                f"coding pipeline raised an unexpected error: {error}",
                "INTERNAL",
            )

    def _recover_coding(self, record: dict[str, Any], *, force: bool) -> None:
        repository = record["repository"]
        number = int(record["issue_number"])
        current = self.ledger.get(repository, number) or record
        status = current.get("status")
        project = self._dispatch_project_for(current)
        execution = self._effective_coding_execution(project)
        adapter = self._coding_adapter(execution)
        orchestrator = self._coding_orchestrator(current, project, execution, adapter)
        if status == "CLAIMED":
            self._progress(
                "recovering",
                "coding was interrupted before agent start; resuming workspace preparation without duplicate execution",
                current,
            )
            self._execute_coding(current, force=force)
            return
        if status == "RUNNING":
            session = current.get("session") or {}
            events_path = session.get("events_path")
            if events_path and Path(events_path).is_file():
                recovered = adapter.recover(str(events_path))
                if recovered:
                    self._progress(
                        "recovering",
                        "coding session output found; resuming tests and publication without re-running OpenCode",
                        current,
                    )
                    orchestrator.mark_agent_completed(recovered)
                    current = self.ledger.get(repository, number) or current
                    current = orchestrator.continue_after_agent(force=force)
                    self._finish_coding(current, recovered, force=force)
                    return
            self._coding_attention(
                current,
                "dispatcher restarted during coding execution without recoverable session output; "
                "the agent was not re-run",
                "AGENT_RUNNING",
            )
            return
        if status == "EXECUTION_COMPLETED":
            self._advance_coding(current, force=force)
            return
        if status in ("REPORT_CAPTURED", "UPLOAD_PENDING"):
            self._upload_and_publish(current, force=force)
            return
        if status == "UPLOAD_VERIFIED":
            self._publish(current, force=force)

    def _advance_coding(self, record: dict[str, Any], *, force: bool) -> None:
        repository = record["repository"]
        number = int(record["issue_number"])
        current = self.ledger.get(repository, number) or record
        coding = current.get("coding") or {}
        publication = coding.get("publication") or {}
        capture = current.get("capture") or {}
        if capture.get("status") == "CAPTURED":
            self._upload_and_publish(current, force=force)
            return
        if coding.get("blocked") or publication.get("pr_url"):
            result = self._coding_result_for_capture(current)
            self._finish_coding(current, result, force=force)
            return
        if publication.get("next_attempt_at") and not force:
            from datetime import datetime

            from ..util import utcnow

            try:
                due = datetime.fromisoformat(str(publication["next_attempt_at"]).replace("Z", "+00:00")) <= utcnow()
            except ValueError:
                due = True
            if not due:
                self._progress(
                    "waiting",
                    f"coding publication retry is not due before {publication.get('next_attempt_at')}; "
                    "OpenCode is not re-executed",
                    current,
                )
                return
        project = self._dispatch_project_for(current)
        execution = self._effective_coding_execution(project)
        adapter = self._coding_adapter(execution)
        orchestrator = self._coding_orchestrator(current, project, execution, adapter)
        result = self._coding_result_for_capture(current)
        current = orchestrator.continue_after_agent(force=force)
        self._finish_coding(current, result, force=force)

    def _cleanup_coding_after_capture(self, record: dict[str, Any]) -> None:
        project = self._dispatch_project_for(record)
        project_coding = project.get("coding") or {}
        if not project_coding.get("cleanup_on_success", True):
            return
        execution = self._effective_coding_execution(project)
        isolation = execution.get("isolation") or {}
        identity = isolation.get("identity") if isolation.get("enabled") else None
        coding = record.get("coding") or {}
        workspace = str(coding.get("workspace") or "")
        root = str(project_coding.get("workspace_root") or "")
        if not identity or not workspace or not root:
            return
        try:
            removed = cleanup_workspace(root, workspace, identity=identity)
        except OSError as exc:
            self.summary["warnings"].append(f"coding workspace cleanup failed: {sanitize_error(str(exc))}")
            return
        if removed:
            self.ledger.update(
                record["repository"],
                int(record["issue_number"]),
                lambda item: item.setdefault("coding", {}).update(
                    {"workspace": None, "cleaned_up": True, "artifacts_preserved": False}
                ),
                note="disposable coding workspace removed after verified report capture",
            )

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
                    "runner_pid": None,
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
        self._progress("capturing", "creating the local structured report and upload queue item", current)
        project = self._project_for(current)
        if not project:
            reason = "Agent Handoff project is no longer configured"
            updated = self.ledger.set_status(
                repository,
                number,
                "NEEDS_ATTENTION",
                note="capture failed",
                updates={"attention_reason": reason, "capture": {"status": "FAILED", "last_error": reason}},
            )
            self._notify_attention(
                updated,
                reason,
                "Restore the Agent Handoff project configuration, then inspect the run manually.",
            )
            self.summary["needs_attention"] += 1
            self.summary["ok"] = False
            self._progress("needs_attention", "report capture failed because the Agent Handoff project is not configured", current)
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
        coding = current.get("coding") or {}
        is_coding = current.get("task_type") == "coding"
        if is_coding:
            tests_payload = test_evidence_for_report(coding.get("tests") or {})
            deployment_payload = {
                "status": "not_applicable",
                "details": "C2 does not deploy. A human owner must review and merge the Draft PR.",
            }
            publication = coding.get("publication") or {}
            report_repository = coding.get("repository")
            report_branch = publication.get("branch")
            report_commit = publication.get("commit")
            report_pr_url = publication.get("pr_url")
            report_remaining = coding_next_action(current)
            report_blockers = coding.get("blocked_reason") if coding.get("blocked") else None
            report_error = result.error or coding.get("blocked_reason")
            report_outcome = result.outcome
        else:
            tests_payload = {
                "status": "not_verified",
                "details": "Task executed through the Agent Handoff dispatcher; no independent test evidence.",
            }
            deployment_payload = {
                "status": "not_verified",
                "details": "No independent deployment evidence was captured.",
            }
            report_repository = None
            report_branch = None
            report_commit = None
            report_pr_url = None
            report_remaining = None
            report_blockers = None
            report_error = result.error
            report_outcome = result.outcome
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
                    if is_coding:
                        git = {
                            "repository": report_repository,
                            "branch": report_branch,
                            "commit": report_commit,
                        }
                    else:
                        git = git_info(str((current.get("project") or {}).get("workspace") or ""))
                    summary_text = result.final_text or (
                        coding.get("blocked_reason") if is_coding else None
                    ) or "No final assistant output was captured."
                    report_text = auto_report(
                        task_id=task_id,
                        run_id=run_id,
                        agent="opencode",
                        session_id=session.get("id"),
                        model=str(current.get("model")),
                        outcome=report_outcome,
                        summary=summary_text,
                        repository=report_repository or git.get("repository"),
                        branch=report_branch or git.get("branch"),
                        commit=report_commit or git.get("commit"),
                        pr_url=report_pr_url,
                        tests=tests_payload,
                        deployment=deployment_payload,
                        error=report_error,
                        termination_reason=result.termination_reason,
                        remaining_issues=report_remaining,
                        blockers=report_blockers,
                        run_dir=str(run_path),
                        remote_report_path=f"TASKS/{meta['task_folder']}/RUNS/{run_id}/REPORT.md",
                    )
                    if is_coding:
                        report_text += coding_report_sections(current)
                    dispatch_context = [
                        "",
                        "## Dispatch context",
                        "",
                        f"- Dispatch repository: `{repository}`",
                        f"- Issue: {current.get('issue_url') or f'#{number}'}",
                        f"- Route: project `{(current.get('project') or {}).get('name')}`, agent `opencode`, model `{current.get('model')}`",
                        f"- Session: `{session.get('id') or 'not captured'}`",
                        f"- Execution outcome: `{result.outcome}`",
                        f"- Task type: `{current.get('task_type') or 'read_only'}`",
                    ]
                    if is_coding:
                        dispatch_context.extend(
                            [
                                f"- Coding outcome: `{coding_outcome(current)}`",
                                f"- Draft PR: {report_pr_url or 'not created'}",
                            ]
                        )
                    run_path, complete = self.store.add_run(
                        project,
                        task_path,
                        meta,
                        outcome=report_outcome,
                        report_text=report_text + "\n".join(dispatch_context) + "\n",
                        run_prompt_text=instructions,
                        tests=tests_payload,
                        deployment=deployment_payload,
                        summary=summary_text[:50000],
                        error=report_error,
                        termination_reason=result.termination_reason,
                        remaining_issues=report_remaining,
                        blockers=report_blockers,
                        repository=report_repository or git.get("repository"),
                        branch=report_branch or git.get("branch"),
                        commit=report_commit or git.get("commit"),
                        pr_url=report_pr_url,
                        session_id=session.get("id"),
                        model=str(current.get("model")),
                        run_id=run_id,
                        completed_at=result.finished_at,
                        auto_generated=True,
                        event_key=f"dispatch:{repository}#{number}:{run_id}",
                    )
                    dispatch_metadata = {
                        "repository": repository,
                        "issue_number": number,
                        "issue_url": current.get("issue_url"),
                        "dispatch_id": current.get("dispatch_id"),
                        "workspace": (current.get("project") or {}).get("workspace"),
                        "events_path": session.get("events_path"),
                        "task_type": current.get("task_type") or "read_only",
                    }
                    complete_updates: dict[str, Any] = {"dispatch": dispatch_metadata}
                    if is_coding:
                        complete_updates["coding"] = {
                            "outcome": coding_outcome(current),
                            "stage": coding.get("stage"),
                            "repository": coding.get("repository"),
                            "base_branch": coding.get("base_branch"),
                            "base_commit": coding.get("base_commit"),
                            "workspace": coding.get("workspace"),
                            "branch": report_branch,
                            "commit": report_commit,
                            "pr_url": report_pr_url,
                            "pr_number": (coding.get("publication") or {}).get("pr_number"),
                            "draft": (coding.get("publication") or {}).get("draft"),
                            "tests": {"status": (coding.get("tests") or {}).get("status")},
                            "diff": {
                                "status": (coding.get("diff") or {}).get("status"),
                                "changed_count": (coding.get("diff") or {}).get("changed_count"),
                            },
                            "blocked": bool(coding.get("blocked")),
                            "blocked_stage": coding.get("blocked_stage"),
                            "blocked_reason": coding.get("blocked_reason"),
                            "milestones": coding.get("milestones"),
                        }
                    self.store.update_complete(run_path, complete_updates)
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
                self._progress("retryable", "report capture failed; the next poll retries capture without re-running OpenCode", current)
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
        self._progress("captured", f"report captured as task={task_id} run={run_id}; upload is pending", current)
        if is_coding and (coding.get("publication") or {}).get("pr_url") and not coding.get("blocked"):
            self._cleanup_coding_after_capture(current)
        return True

    def _update_coding_pr_report_link(self, record: dict[str, Any]) -> None:
        if record.get("task_type") != "coding":
            return
        coding = record.get("coding") or {}
        publication = coding.get("publication") or {}
        upload = record.get("upload") or {}
        report_url = upload.get("report_url") if upload.get("verified") else None
        pr_number = publication.get("pr_number")
        pr_url = publication.get("pr_url")
        if not report_url or not pr_number or not pr_url or publication.get("report_link_updated"):
            return
        next_at = publication.get("report_link_next_attempt_at")
        if next_at:
            from datetime import datetime

            from ..util import utcnow

            try:
                due = datetime.fromisoformat(str(next_at).replace("Z", "+00:00")) <= utcnow()
            except ValueError:
                due = True
            if not due:
                return
        project = self._dispatch_project_for(record)
        project_coding = project.get("coding") or {}
        project_publication = project_coding.get("publication") or {}
        global_coding = self.dispatch.get("coding") or {}
        try:
            publisher = TrustedPublisher(
                repository=str(project_coding.get("repository") or ""),
                base_branch=str(project_coding.get("base_branch") or ""),
                branch_prefix=str(project_publication.get("branch_prefix") or "agent-handoff/"),
                staging_root=str(global_coding.get("staging_root") or ""),
                gh_command=str((self.dispatch.get("github") or {}).get("command") or "gh"),
                token_env=project_publication.get("token_env"),
                max_file_bytes=int((project_coding.get("limits") or {}).get("max_file_bytes", 200_000)),
            )
            capture = record.get("capture") or {}
            body = build_pr_body(
                task_id=str(capture.get("task_id") or ""),
                run_id=str(capture.get("run_id") or ""),
                title=str(record.get("issue_title") or f"Agent Handoff issue #{record.get('issue_number')}"),
                issue_url=record.get("issue_url"),
                base_commit=str(coding.get("base_commit") or ""),
                branch=str(publication.get("branch") or ""),
                validation=coding.get("diff") or {},
                tests=coding.get("tests") or {},
                report_url=report_url,
                limitations=PUBLICATION_LIMITATIONS,
            )
            publisher.update_pr_body(pr_number=int(pr_number), body=body)
        except Exception as exc:
            message = sanitize_error(f"could not add the verified report link to the Draft PR: {exc}", 500)
            attempts = int(publication.get("report_link_attempts", 0) or 0) + 1
            next_at = publication_backoff(attempts, 60)
            self.ledger.update(
                record["repository"],
                int(record["issue_number"]),
                lambda item: item.setdefault("coding", {}).setdefault("publication", {}).update(
                    {
                        "report_link_attempts": attempts,
                        "report_link_last_error": message,
                        "report_link_next_attempt_at": next_at,
                    }
                ),
                note="Draft PR report-link update failed; bounded retry scheduled",
            )
            self.summary.setdefault("warnings", []).append(
                message + f" (retry after {next_at}; report delivery is unaffected)"
            )
            self._log(message)
            return
        self.ledger.update(
            record["repository"],
            int(record["issue_number"]),
            lambda item: item.setdefault("coding", {}).setdefault("publication", {}).update(
                {
                    "report_link_updated": True,
                    "report_link_updated_at": utcnow_iso_precise(),
                    "report_link_attempts": int(publication.get("report_link_attempts", 0) or 0),
                    "report_link_last_error": None,
                    "report_link_next_attempt_at": None,
                }
            ),
            note="verified report link added to the Draft PR body",
        )

    def _mark_coding_milestone(self, record: dict[str, Any], stage: str, milestone: str | None = None, note: str = "") -> None:
        if record.get("task_type") != "coding":
            return

        def apply(item: dict[str, Any]) -> None:
            coding = item.setdefault("coding", {})
            current = str(coding.get("stage") or "")
            if PER_STAGE_ORDER.get(stage, -1) >= PER_STAGE_ORDER.get(current, -1):
                coding["stage"] = stage
            if milestone:
                coding.setdefault("milestones", {})[milestone] = True
            if stage == "UPLOAD_VERIFIED":
                coding["upload_verified_at"] = utcnow_iso_precise()
                coding.setdefault("milestones", {})["REPORT_VERIFIED"] = True
            if stage == "ISSUE_UPDATED":
                coding["report_delivered_at"] = utcnow_iso_precise()
                coding.setdefault("milestones", {})["ISSUE_DELIVERED"] = True

        self.ledger.update(record["repository"], int(record["issue_number"]), apply, note=note or f"coding milestone {stage}")

    def _upload_and_publish(self, record: dict[str, Any], *, force: bool) -> None:
        current = self.ledger.get(record["repository"], int(record["issue_number"])) or record
        if current.get("status") in ("NEEDS_ATTENTION", "REJECTED", "ISSUE_UPDATED"):
            return
        capture = current.get("capture") or {}
        if capture.get("status") != "CAPTURED":
            return
        if not upload_due(current, force=force):
            next_at = (current.get("upload") or {}).get("next_attempt_at")
            self._progress(
                "waiting",
                f"upload retry is not due before {next_at}; no agent session will be re-executed",
                current,
            )
            return
        project = self._project_for(current)
        if not project:
            reason = "Agent Handoff project is no longer configured"
            updated = self.ledger.set_status(
                current["repository"],
                int(current["issue_number"]),
                "NEEDS_ATTENTION",
                note="upload failed: missing project",
                updates={"attention_reason": reason, "upload": {"status": "FAILED", "last_error": reason}},
            )
            self._notify_attention(
                updated,
                reason,
                "Restore the Agent Handoff project configuration; the captured report is preserved.",
            )
            self.summary["needs_attention"] += 1
            self.summary["ok"] = False
            self._progress("needs_attention", reason, current)
            return
        task_id = str(capture.get("task_id"))
        run_id = str(capture.get("run_id"))
        upload_state = current.get("upload") or {}
        attempts = int(upload_state.get("attempts", 0) or 0) + 1
        self._progress(
            "uploading",
            f"starting verified upload attempt {attempts} "
            f"(bounded external operations; no total ETA)",
            current,
        )

        def upload_progress(stage: str, message: str) -> None:
            self._progress(stage, message, current)

        result = reconcile_verified_delivery(
            self.config,
            self.store,
            self.uploader,
            project,
            task_id,
            run_id,
            force=force,
            on_progress=upload_progress,
        )
        if not result.get("ok"):
            error = sanitize_error(result.get("error") or "upload reconciliation failed")
            recorded_attempts = merged_upload_attempts(upload_state, result)
            next_attempt_at = upload_backoff(max(attempts, recorded_attempts))
            updated = self.ledger.set_status(
                current["repository"],
                int(current["issue_number"]),
                "UPLOAD_PENDING",
                note="verified upload pending retry",
                updates={
                    "upload": {
                        "status": "FAILED",
                        "verified": False,
                        "attempts": recorded_attempts,
                        "last_error": error,
                        "last_failed_at": utcnow_iso_precise(),
                        "next_attempt_at": next_attempt_at,
                    }
                },
            )
            self._notify_delayed(
                updated,
                reason=error,
                stage="upload",
                attempts=recorded_attempts,
            )
            self.summary["ok"] = False
            self.summary["errors"].append(f"issue #{current['issue_number']}: {error}")
            self._progress(
                "retryable",
                f"upload attempt {attempts} failed; retry scheduled for {next_attempt_at}. "
                "The claimed session will not be re-executed (retry upload only).",
                current,
            )
            self._log(f"issue #{current['issue_number']} upload failed: {error}")
            return
        recorded_attempts = merged_upload_attempts(upload_state, result)
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
                    "attempts": recorded_attempts,
                    "last_error": None,
                    "last_failed_at": None,
                    "next_attempt_at": None,
                },
                "attention_reason": None,
            },
        )
        self.summary["uploaded"] += 1
        current = self.ledger.get(current["repository"], int(current["issue_number"])) or current
        self._mark_coding_milestone(current, "UPLOAD_VERIFIED", note="coding report upload verified")
        current = self.ledger.get(current["repository"], int(current["issue_number"])) or current
        self._progress("verified", "report upload and receipt passed MD5/read-back verification", current)
        self._publish(current, force=force)

    def _publish(self, record: dict[str, Any], *, force: bool) -> None:
        current = self.ledger.get(record["repository"], int(record["issue_number"])) or record
        if current.get("task_type") == "coding":
            self._update_coding_pr_report_link(current)
            current = self.ledger.get(current["repository"], int(current["issue_number"])) or current
        if current.get("status") == "ISSUE_UPDATED":
            return
        publication = current.get("publication") or {}
        if publication.get("status") == "PUBLISHED":
            return
        if publication.get("status") == "EXHAUSTED" and not force:
            return
        if not publication_due(current, force=force):
            return
        capture = current.get("capture") or {}
        if not (current.get("upload") or {}).get("report_url") and capture.get("status") == "CAPTURED":
            project = self._project_for(current)
            if project:
                def hydrate_progress(stage: str, message: str) -> None:
                    self._progress(stage, message, current)

                reconciled = reconcile_verified_delivery(
                    self.config,
                    self.store,
                    self.uploader,
                    project,
                    str(capture.get("task_id")),
                    str(capture.get("run_id")),
                    force=force,
                    on_progress=hydrate_progress,
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
                                "attempts": merged_upload_attempts(item.get("upload"), reconciled),
                                "last_error": None,
                                "next_attempt_at": None,
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
        self._progress(
            "publishing",
            "checking for an existing idempotent delivery comment, then publishing the verified report link",
            current,
        )
        try:
            existing = self.transport.find_comment(int(current["issue_number"]), marker)
            if existing:
                updated = self.ledger.set_status(
                    current["repository"],
                    int(current["issue_number"]),
                    "ISSUE_UPDATED",
                    note="existing verified delivery comment found",
                    updates={
                        "publication": {
                            "status": "PUBLISHED",
                            "published_at": existing.get("created_at") or utcnow_iso_precise(),
                            "comment_url": existing.get("html_url"),
                            "last_error": None,
                            "last_failed_at": None,
                            "exhausted_at": None,
                            "next_attempt_at": None,
                        },
                        "attention_reason": None,
                    },
                )
                self._mark_coding_milestone(updated, "ISSUE_UPDATED", milestone="REPORT_DELIVERED", note="coding report delivery confirmed")
                updated = self.ledger.get(updated["repository"], int(updated["issue_number"])) or updated
                self._notify_completed(updated)
                self._progress("completed", "existing delivery comment was already present; no duplicate was posted", current)
                return
            self._progress("publishing", "no existing delivery marker; posting one verified report comment", current)
            response = self.transport.comment(int(current["issue_number"]), build_issue_comment(current))
            updated = self.ledger.set_status(
                current["repository"],
                int(current["issue_number"]),
                "ISSUE_UPDATED",
                note="verified report URL published to the originating issue",
                updates={
                    "publication": {
                        "status": "PUBLISHED",
                        "published_at": utcnow_iso_precise(),
                        "comment_url": response.get("url"),
                        "last_error": None,
                        "last_failed_at": None,
                        "exhausted_at": None,
                        "next_attempt_at": None,
                    },
                    "attention_reason": None,
                },
            )
            self._mark_coding_milestone(updated, "ISSUE_UPDATED", milestone="REPORT_DELIVERED", note="coding report delivered to the originating issue")
            updated = self.ledger.get(updated["repository"], int(updated["issue_number"])) or updated
            self._notify_completed(updated)
            self.summary["published"] += 1
            self._progress("completed", "verified report URL published to the originating issue", current)
            self._log(f"issue #{current['issue_number']} verified report published")
        except GithubError as exc:
            attempts = int(publication.get("attempts", 0)) + 1
            was_exhausted = publication.get("status") == "EXHAUSTED"
            error = sanitize_error(str(exc))
            max_attempts = int((self.dispatch.get("delivery") or {}).get("max_attempts", 0))
            exhausted = bool(max_attempts and attempts >= max_attempts)
            next_at = None if exhausted else publication_backoff(
                attempts, int((self.dispatch.get("delivery") or {}).get("retry_seconds", 60))
            )
            updated = self.ledger.set_status(
                current["repository"],
                int(current["issue_number"]),
                "UPLOAD_VERIFIED",
                note="GitHub publication retries exhausted; manual delivery required"
                if exhausted
                else "GitHub publication failed; retryable",
                updates={
                    "publication": {
                        "status": "EXHAUSTED" if exhausted else "FAILED",
                        "attempts": attempts,
                        "last_error": error,
                        "last_failed_at": utcnow_iso_precise(),
                        "exhausted_at": utcnow_iso_precise() if exhausted and not was_exhausted else publication.get("exhausted_at"),
                        "next_attempt_at": next_at,
                    },
                    "attention_reason": error if exhausted else current.get("attention_reason"),
                },
            )
            if exhausted and not was_exhausted:
                self._notify_attention(
                    updated,
                    error,
                    "Use `agent-handoff dispatch deliver` to publish the verified report manually; "
                    "the upload is reused and the agent is not re-executed.",
                )
            elif not exhausted:
                self._notify_delayed(
                    updated,
                    reason=error,
                    stage="publication",
                    attempts=attempts,
                )
            self.summary["ok"] = False
            self.summary["errors"].append(f"issue #{current['issue_number']} publication: {error}")
            if exhausted and not was_exhausted:
                self.summary["needs_attention"] += 1
            if exhausted:
                self._progress(
                    "needs_attention",
                    f"GitHub publication failed {attempts} time(s) and dispatch.delivery.max_attempts was reached. "
                    "The verified upload is preserved; use agent-handoff dispatch deliver for a manual retry. "
                    "The agent is not re-executed.",
                    current,
                )
            else:
                self._progress(
                    "retryable",
                    f"GitHub publication failed; retry scheduled for {next_at}. "
                    "Only the comment is retried; the verified upload is reused.",
                    current,
                )
            self._log(f"issue #{current['issue_number']} publication failed: {error}")
