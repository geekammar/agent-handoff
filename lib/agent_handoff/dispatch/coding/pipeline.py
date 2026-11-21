from __future__ import annotations

import os
from typing import Any, Callable, Mapping

from ...config import BASE_DIR
from ...util import sanitize_error, utcnow_iso
from ..delivery import publication_backoff
from ..security import validate_model_auth
from .changes import ChangeValidationError, SafeGit, validate_changes
from .publication import (
    PublicationError,
    TrustedPublisher,
    build_branch,
    build_pr_body,
)
from .testrunner import TestExecutionError, run_test_profile
from .workspace import (
    WorkspacePreparationError,
    cleanup_workspace,
    prepare_workspace,
    validate_coding_root,
    verify_source_repository,
)

CODING_STAGES = (
    "REQUEST_ACCEPTED",
    "WORKSPACE_PREPARED",
    "AGENT_RUNNING",
    "AGENT_COMPLETED",
    "TESTING",
    "VALIDATING_DIFF",
    "COMMIT_PREPARED",
    "BRANCH_PUBLISHED",
    "PR_CREATED",
    "REPORT_CAPTURED",
    "UPLOAD_VERIFIED",
    "ISSUE_UPDATED",
    "NOTIFICATION_SENT",
)
PER_STAGE_ORDER = {name: index for index, name in enumerate(CODING_STAGES)}
PUBLICATION_LIMITATIONS = (
    "Unix UID/GID isolation is not a kernel or network sandbox.",
    "The dispatcher parent still owns GitHub, Drive, and Telegram credentials.",
    "Secret scanning and path rules are defense in depth, not proof of safety.",
    "Tests may access the network unless the host adds a separate policy.",
    "Draft PRs are never auto-merged; a human owner must review.",
)


class CodingPipelineError(RuntimeError):
    pass


class CodingOrchestrator:
    """Durable, resumable coding pipeline that never re-runs OpenCode blindly."""

    def __init__(
        self,
        *,
        config: dict[str, Any],
        dispatch: dict[str, Any],
        ledger: Any,
        record: dict[str, Any],
        project: dict[str, Any],
        identity: Mapping[str, Any],
        adapter: Any,
        progress: Callable[[str, str], None] | None = None,
        log: Callable[[str], None] | None = None,
        git: SafeGit | None = None,
        publisher: TrustedPublisher | None = None,
        spawn: Any = None,
    ) -> None:
        self.config = config
        self.dispatch = dispatch
        self.ledger = ledger
        self.record = record
        self.project = project
        self.coding = dict((record.get("coding") or {}))
        self.identity = dict(identity)
        self.adapter = adapter
        self.progress = progress
        self.log = log or (lambda _message: None)
        self.git = git or SafeGit(identity=self.identity)
        self.spawn = spawn
        publication = (project.get("coding") or {}).get("publication") or {}
        self.publication = dict(publication)
        self._publisher = publisher
        self.workspace_root = str((project.get("coding") or {}).get("workspace_root") or "")
        self.source_workspace = str((project.get("coding") or {}).get("source_workspace") or "")
        self.repository = str((project.get("coding") or {}).get("repository") or "")
        self.base_branch = str((project.get("coding") or {}).get("base_branch") or "")
        self.task_id = str((record.get("capture") or {}).get("task_id") or "")
        self.run_id = str((record.get("capture") or {}).get("run_id") or "")

    def _emit(self, stage: str, message: str) -> None:
        if self.progress:
            try:
                self.progress(stage, message)
            except Exception:
                pass

    def _fresh(self) -> dict[str, Any]:
        return self.ledger.get(self.record["repository"], int(self.record["issue_number"])) or self.record

    def _safe_text(self, value: Any, limit: int = 2000) -> str:
        text = sanitize_error(str(value or ""), 4000)
        known_paths = [
            self.workspace_root,
            self.source_workspace,
            str((self._fresh().get("coding") or {}).get("workspace") or ""),
            str(((self.project.get("coding") or {}).get("publication") or {}).get("staging_root") or ""),
            str(((self.dispatch.get("coding") or {}).get("staging_root")) or ""),
        ]
        for path in sorted((item for item in known_paths if item and len(item) > 1), key=len, reverse=True):
            text = text.replace(path, "<local>")
        return text[:limit]

    def _set_coding(
        self,
        mutator: Callable[[dict[str, Any]], None],
        *,
        note: str,
        top_status: str | None = None,
        updates: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        repository = self.record["repository"]
        number = int(self.record["issue_number"])

        def apply(record: dict[str, Any]) -> None:
            if top_status:
                record["status"] = top_status
                if top_status != "NEEDS_ATTENTION":
                    record["attention_reason"] = None
            coding = record.setdefault("coding", {})
            mutator(coding)
            def merge(target: dict[str, Any], values: dict[str, Any]) -> None:
                for key, value in values.items():
                    if isinstance(value, dict) and isinstance(target.get(key), dict):
                        merge(target[key], value)
                    else:
                        target[key] = value

            merge(record, updates or {})

        return self.ledger.update(repository, number, apply, note=note)

    def _stage(
        self,
        stage: str,
        *,
        note: str,
        top_status: str | None = None,
        updates: dict[str, Any] | None = None,
        milestone: str | None = None,
    ) -> dict[str, Any]:
        def mutate(coding: dict[str, Any]) -> None:
            current = str(coding.get("stage") or "REQUEST_ACCEPTED")
            if PER_STAGE_ORDER.get(stage, -1) < PER_STAGE_ORDER.get(current, -1):
                stage_value = current
            else:
                stage_value = stage
            coding["stage"] = stage_value
            history = coding.setdefault("stage_history", [])
            history.append({"at": utcnow_iso(), "stage": stage_value, "note": note})
            if len(history) > 120:
                coding["stage_history"] = history[-120:]
            if milestone:
                coding.setdefault("milestones", {})[milestone] = True

        updated = self._set_coding(mutate, note=note, top_status=top_status, updates=updates)
        self.record = updated
        return updated

    def _block(self, stage: str, reason: str, *, preserve: bool = True) -> dict[str, Any]:
        safe_reason = self._safe_text(reason, 1500)

        def mutate(coding: dict[str, Any]) -> None:
            coding["blocked"] = True
            coding["blocked_stage"] = stage
            coding["blocked_reason"] = safe_reason
            coding["artifacts_preserved"] = bool(preserve)
            coding["stage"] = stage
            history = coding.setdefault("stage_history", [])
            history.append({"at": utcnow_iso(), "stage": stage, "note": f"blocked: {safe_reason[:300]}"})
            if len(history) > 120:
                coding["stage_history"] = history[-120:]

        updated = self._set_coding(
            mutate,
            note=f"coding blocked at {stage}: {safe_reason}",
            top_status="EXECUTION_COMPLETED",
            updates={"attention_reason": safe_reason},
        )
        self._emit("coding_blocked", f"coding stopped at {stage}; evidence will be preserved")
        self.log(f"coding blocked at {stage}: {safe_reason}")
        return updated

    def _adopt_publisher(self) -> TrustedPublisher:
        if self._publisher is None:
            coding = (self.project.get("coding") or {})
            publication = coding.get("publication") or {}
            global_coding = self.dispatch.get("coding") or {}
            source_staging = str((self.project.get("coding") or {}).get("staging_root") or "")
            staging_root = source_staging or str(global_coding.get("staging_root") or "")
            self._publisher = TrustedPublisher(
                repository=self.repository,
                base_branch=self.base_branch,
                branch_prefix=str(publication.get("branch_prefix") or "agent-handoff/"),
                staging_root=staging_root,
                gh_command=str((self.dispatch.get("github") or {}).get("command") or "gh"),
                token_env=publication.get("token_env"),
                max_file_bytes=int(
                    ((self.project.get("coding") or {}).get("limits") or {}).get("max_file_bytes", 200_000)
                ),
            )
        return self._publisher

    def prepare(self) -> dict[str, Any]:
        current = self._fresh()
        coding = current.get("coding") or {}
        if coding.get("stage") not in (None, "REQUEST_ACCEPTED", "CLAIMED", "WORKSPACE_PREPARED"):
            return current
        if coding.get("workspace") and coding.get("base_commit"):
            return current
        self._emit("coding_workspace", "validating the disposable coding workspace policy")
        try:
            validate_coding_root(
                self.workspace_root,
                identity=self.identity,
                workspace_roots=list(
                    ((self.dispatch.get("execution") or {}).get("isolation") or {}).get("workspace_roots") or []
                ),
                source_workspace=self.source_workspace,
                base_dir=str(BASE_DIR),
                check_paths=True,
            )
            validate_model_auth(self.identity)
            publisher = self._adopt_publisher()
            publisher.validate_staging_root()
            publisher.token()
            source = verify_source_repository(
                self.source_workspace,
                repository=self.repository,
                base_branch=self.base_branch,
                git=self.git,
            )
            prepared = prepare_workspace(
                workspace_root=self.workspace_root,
                repository=self.repository,
                base_branch=self.base_branch,
                base_commit=source["base_commit"],
                source=source["source"],
                issue_number=int(self.record["issue_number"]),
                task_id=self.task_id,
                identity=self.identity,
                git=self.git,
            )
        except Exception as exc:
            reason = self._safe_text(str(exc), 1200)
            return self._block("WORKSPACE_PREPARED", reason)
        updated = self._stage(
            "WORKSPACE_PREPARED",
            note="disposable clean workspace prepared at the verified base commit",
            top_status="CLAIMED",
            updates={
                "coding": {
                    "workspace": prepared["workspace"],
                    "workspace_name": prepared["workspace_name"],
                    "base_commit": prepared["base_commit"],
                    "base_branch": prepared["base_branch"],
                    "repository": prepared["repository"],
                    "source_workspace": source["source"],
                }
            },
        )
        self._emit(
            "coding_workspace",
            f"clean disposable workspace prepared at base commit {prepared['base_commit'][:12]}",
        )
        return updated

    def mark_agent_running(self, events_path: str) -> dict[str, Any]:
        return self._stage(
            "AGENT_RUNNING",
            note="OpenCode coding execution started",
            top_status="RUNNING",
            updates={
                "coding": {
                    "agent": {"status": "running", "started_at": utcnow_iso()},
                    "events_path": events_path,
                },
                "execution": {"status": "RUNNING", "started_at": utcnow_iso()},
                "session": {"events_path": events_path},
            },
        )

    def mark_agent_completed(self, result: Any) -> dict[str, Any]:
        coding = self._fresh().get("coding") or {}
        agent = {
            "status": "completed",
            "outcome": getattr(result, "outcome", None),
            "exit_code": getattr(result, "exit_code", None),
            "timed_out": bool(getattr(result, "timed_out", False)),
            "finished_at": getattr(result, "finished_at", utcnow_iso()),
            "error": self._safe_text(getattr(result, "error", None), 1000) or None,
        }
        milestone = "AGENT_COMPLETED"
        return self._stage(
            "AGENT_COMPLETED",
            note=f"agent completed with outcome={agent['outcome']}",
            top_status="EXECUTION_COMPLETED",
            updates={
                "coding": {"agent": agent},
                "session": {"id": getattr(result, "session_id", None), "events_path": getattr(result, "events_path", None)},
                "execution": {
                    "status": "COMPLETED",
                    "finished_at": getattr(result, "finished_at", utcnow_iso()),
                    "exit_code": getattr(result, "exit_code", None),
                    "timed_out": bool(getattr(result, "timed_out", False)),
                    "outcome": getattr(result, "outcome", None),
                    "termination_reason": getattr(result, "termination_reason", None),
                    "last_error": self._safe_text(getattr(result, "error", None), 1000) or None,
                    "runner_pid": None,
                },
            },
            milestone=milestone if getattr(result, "outcome", None) == "succeeded" else None,
        )

    def continue_after_agent(self, *, force: bool = False) -> dict[str, Any]:
        current = self._fresh()
        coding = current.get("coding") or {}
        if coding.get("blocked"):
            return current
        resumable_stages = {"TESTING", "VALIDATING_DIFF", "COMMIT_PREPARED", "BRANCH_PUBLISHED", "PR_CREATED"}
        if str(coding.get("stage") or "") in resumable_stages:
            recovery = coding.setdefault("recovery", {})
            attempts = int(recovery.get("attempts", 0) or 0) + 1 if not force else int(recovery.get("attempts", 0) or 0)
            limits = (self.project.get("coding") or {}).get("limits") or {}
            maximum = max(5, int(limits.get("max_publication_attempts", 3) or 3) * 2)
            if attempts > maximum:
                return self._block(
                    "RECOVERY",
                    f"the coding pipeline resumed more than {maximum} times without completing; "
                    "operator review is required and the agent will not be rerun",
                )
            current = self._set_coding(
                lambda item: item.setdefault("recovery", {}).update(
                    {"attempts": attempts, "last_resumed_at": utcnow_iso()}
                ),
                note=f"resumed coding pipeline at stage {coding.get('stage')}",
            )
            coding = current.get("coding") or {}
        agent = coding.get("agent") or {}
        if agent.get("status") != "completed":
            return current
        if str(agent.get("outcome") or "") != "succeeded":
            return self._block(
                "AGENT_COMPLETED",
                f"the coding agent did not report success (outcome={agent.get('outcome')}); "
                "tests and publication were not started",
            )
        current = self._ensure_tests(current)
        coding = current.get("coding") or {}
        if coding.get("blocked"):
            return current
        current = self._ensure_validation(current)
        coding = current.get("coding") or {}
        if coding.get("blocked"):
            return current
        current = self._ensure_commit(current)
        coding = current.get("coding") or {}
        if coding.get("blocked"):
            return current
        current = self._ensure_push(current, force=force)
        coding = current.get("coding") or {}
        publication = coding.get("publication") or {}
        if coding.get("blocked") or not publication.get("branch_pushed"):
            return current
        current = self._ensure_pr(current, force=force)
        return current

    def _ensure_tests(self, current: dict[str, Any]) -> dict[str, Any]:
        coding = current.get("coding") or {}
        tests = coding.get("tests") or {}
        if tests.get("status") in ("passed", "failed", "timed_out"):
            return current
        definition = (self.project.get("coding") or {}).get("test_definition") or {}
        workspace = str(coding.get("workspace") or "")
        limits = (self.project.get("coding") or {}).get("limits") or {}
        self._stage("TESTING", note="running the trusted test profile in the isolated workspace")
        self._emit("coding_tests", "running the trusted test profile under the dedicated non-root identity")
        try:
            evidence = run_test_profile(
                definition,
                workspace=workspace,
                identity=self.identity,
                limits=limits,
                spawn=self.spawn,
            )
        except (TestExecutionError, OSError) as exc:
            return self._block("TESTING", f"trusted test execution could not start: {exc}")
        stored = {
            "status": evidence.get("status"),
            "profile": evidence.get("profile"),
            "timeout_seconds": evidence.get("timeout_seconds"),
            "duration_seconds": evidence.get("duration_seconds"),
            "failed_command": evidence.get("failed_command"),
            "commands": [
                {
                    "argv": item.get("argv"),
                    "status": item.get("status"),
                    "exit_code": item.get("exit_code"),
                    "timed_out": item.get("timed_out"),
                    "duration_seconds": item.get("duration_seconds"),
                    "stdout": self._safe_text(item.get("stdout"), 3000),
                    "stderr": self._safe_text(item.get("stderr"), 3000),
                    "output_truncated": item.get("output_truncated"),
                }
                for item in evidence.get("commands") or []
            ],
        }
        if evidence.get("status") != "passed":
            updated = self._stage(
                "TESTING",
                note="trusted tests failed; publication is refused",
                top_status="EXECUTION_COMPLETED",
                updates={"coding": {"tests": stored}},
            )
            return self._block(
                "TESTING",
                f"trusted tests did not pass (status={evidence.get('status')}); "
                "no commit, push, or Draft PR was created",
            )
        updated = self._stage(
            "TESTING",
            note="trusted test profile passed",
            updates={"coding": {"tests": stored}},
            milestone="TESTS_PASSED",
        )
        self._emit("coding_tests", "trusted tests passed")
        return updated

    def _ensure_validation(self, current: dict[str, Any]) -> dict[str, Any]:
        coding = current.get("coding") or {}
        if (coding.get("diff") or {}).get("status") in ("passed", "blocked"):
            return current
        self._stage("VALIDATING_DIFF", note="validating the workspace diff, paths, and secret scan")
        self._emit("coding_validation", "validating changed files, protected paths, and secret material")
        project_coding = self.project.get("coding") or {}
        try:
            evidence = validate_changes(
                str(coding.get("workspace") or ""),
                git=self.git,
                limits=project_coding.get("limits") or {},
                protected_paths=project_coding.get("protected_paths") or [],
                allowed_paths=project_coding.get("allowed_paths") or [],
                secret_scan=bool(project_coding.get("secret_scan", True)),
            )
        except (ChangeValidationError, OSError) as exc:
            return self._block("VALIDATING_DIFF", f"diff validation failed: {exc}")
        stored = {
            "status": evidence.get("status"),
            "files": evidence.get("files"),
            "changed_count": evidence.get("changed_count"),
            "diff_bytes": evidence.get("diff_bytes"),
            "workspace_bytes": evidence.get("workspace_bytes"),
            "blocked": evidence.get("blocked"),
            "secret_findings": evidence.get("secret_findings"),
            "symlinks": evidence.get("symlinks"),
            "submodules": evidence.get("submodules"),
            "secret_scan": evidence.get("secret_scan"),
        }
        if evidence.get("status") != "passed":
            self._stage(
                "VALIDATING_DIFF",
                note="diff validation blocked publication",
                updates={"coding": {"diff": stored}},
            )
            return self._block(
                "VALIDATING_DIFF",
                "diff validation blocked publication: " + "; ".join(evidence.get("blocked") or [])[:1200],
            )
        updated = self._stage(
            "VALIDATING_DIFF",
            note="diff validation passed",
            updates={"coding": {"diff": stored}},
        )
        self._emit("coding_validation", "diff validation passed")
        return updated

    def _ensure_commit(self, current: dict[str, Any]) -> dict[str, Any]:
        coding = current.get("coding") or {}
        publication = coding.get("publication") or {}
        if publication.get("commit"):
            return current
        project_coding = self.project.get("coding") or {}
        branch = str(
            publication.get("branch")
            or build_branch(
                str((project_coding.get("publication") or {}).get("branch_prefix") or "agent-handoff/"),
                str(current.get("project", {}).get("name") or "project"),
                int(self.record["issue_number"]),
                self.task_id,
            )
        )
        self._stage(
            "COMMIT_PREPARED",
            note="preparing one commit in a parent-owned publication repository",
            updates={
                "coding": {
                    "publication": {
                        "branch": branch,
                        "status": "COMMITTING",
                    }
                }
            },
        )
        self._emit("coding_commit", "preparing a single commit from validated files")
        publisher = self._adopt_publisher()
        try:
            publisher.prepare(
                base_commit=str(coding.get("base_commit") or ""),
                task_id=self.task_id,
                run_id=self.run_id,
            )
            committed = publisher.stage_and_commit(
                validation=coding.get("diff") or {},
                task_workspace=str(coding.get("workspace") or ""),
                project=str((current.get("project") or {}).get("name") or "project"),
                title=str(current.get("issue_title") or f"Agent Handoff issue #{self.record['issue_number']}"),
                issue_number=int(self.record["issue_number"]),
                task_id=self.task_id,
                run_id=self.run_id,
                base_commit=str(coding.get("base_commit") or ""),
            )
        except (PublicationError, OSError) as exc:
            return self._block("COMMIT_PREPARED", f"preparing the publication commit failed: {exc}")
        updated = self._stage(
            "COMMIT_PREPARED",
            note=f"commit prepared: {committed['commit']}",
            updates={
                "coding": {
                    "publication": {
                        "branch": branch,
                        "commit": committed["commit"],
                        "staging": committed.get("staging"),
                        "status": "COMMITTED",
                        "draft": True,
                    }
                }
            },
        )
        self._emit("coding_commit", f"commit prepared: {committed['commit'][:12]}")
        return updated

    def _publication_retry(self, current: dict[str, Any], *, stage: str, error: str, force: bool) -> dict[str, Any]:
        coding = current.get("coding") or {}
        publication = coding.get("publication") or {}
        attempts = int(publication.get("attempts", 0) or 0) + 1
        limits = (self.project.get("coding") or {}).get("limits") or {}
        max_attempts = int(
            self.publication.get("max_attempts")
            or limits.get("max_publication_attempts")
            or 3
        )
        exhausted = attempts >= max_attempts
        next_at = None if exhausted else publication_backoff(
            attempts, 60
        )
        updated = self._stage(
            stage,
            note=("publication retries exhausted" if exhausted else "publication failed; retry scheduled"),
            updates={
                "coding": {
                    "publication": {
                        "attempts": attempts,
                        "last_error": self._safe_text(error, 1200),
                        "last_failed_at": utcnow_iso(),
                        "next_attempt_at": next_at,
                    }
                }
            },
        )
        if exhausted:
            return self._block(stage, f"publication failed after {attempts} attempt(s): {error}")
        self._emit(
            "coding_retryable",
            f"publication attempt {attempts} failed; retry is scheduled without re-running OpenCode",
        )
        return updated

    def _publication_due(self, current: dict[str, Any], force: bool) -> bool:
        publication = ((current.get("coding") or {}).get("publication") or {})
        if force or not publication.get("next_attempt_at"):
            return True
        from datetime import datetime

        from ...util import utcnow

        try:
            return datetime.fromisoformat(str(publication["next_attempt_at"]).replace("Z", "+00:00")) <= utcnow()
        except ValueError:
            return True

    def _ensure_push(self, current: dict[str, Any], *, force: bool) -> dict[str, Any]:
        coding = current.get("coding") or {}
        publication = coding.get("publication") or {}
        if publication.get("branch_pushed"):
            return current
        if not self._publication_due(current, force):
            self._emit("coding_waiting", "branch publication retry is not due yet")
            return current
        publisher = self._adopt_publisher()
        staging = publication.get("staging")
        if staging and os.path.isdir(staging):
            publisher.adopt_staging(str(staging))
        branch = str(publication.get("branch") or "")
        commit = str(publication.get("commit") or "")
        if not staging:
            return self._block("BRANCH_PUBLISHED", "the publication staging repository is missing")
        self._stage("BRANCH_PUBLISHED", note="pushing one task branch to the trusted repository")
        self._emit("coding_branch", "pushing the task branch to the trusted repository")
        try:
            result = publisher.push(branch=branch, commit=commit)
        except (PublicationError, OSError) as exc:
            return self._publication_retry(
                current, stage="BRANCH_PUBLISHED", error=str(exc), force=force
            )
        updated = self._stage(
            "BRANCH_PUBLISHED",
            note=f"branch published: {branch}",
            updates={
                "coding": {
                    "publication": {
                        "branch": branch,
                        "commit": commit,
                        "branch_pushed": True,
                        "last_error": None,
                        "next_attempt_at": None,
                        "status": "BRANCH_PUBLISHED",
                    }
                }
            },
        )
        self._emit("coding_branch", "task branch published")
        return updated

    def _ensure_pr(self, current: dict[str, Any], *, force: bool) -> dict[str, Any]:
        coding = current.get("coding") or {}
        publication = coding.get("publication") or {}
        if publication.get("pr_url"):
            return current
        if not self._publication_due(current, force):
            self._emit("coding_waiting", "Draft PR creation retry is not due yet")
            return current
        publisher = self._adopt_publisher()
        staging = publication.get("staging")
        if staging and os.path.isdir(staging):
            publisher.adopt_staging(str(staging))
        branch = str(publication.get("branch") or "")
        commit = str(publication.get("commit") or "")
        project_name = str((current.get("project") or {}).get("name") or "project")
        title_prefix = str(self.publication.get("pr_title_prefix") or "Agent Handoff: ")
        title = title_prefix + str(current.get("issue_title") or f"issue #{self.record['issue_number']}")
        body = build_pr_body(
            task_id=self.task_id,
            run_id=self.run_id,
            title=str(current.get("issue_title") or f"Agent Handoff issue #{self.record['issue_number']}"),
            issue_url=current.get("issue_url"),
            base_commit=str(coding.get("base_commit") or ""),
            branch=branch,
            validation=coding.get("diff") or {},
            tests=coding.get("tests") or {},
            report_url=(current.get("upload") or {}).get("report_url"),
            limitations=PUBLICATION_LIMITATIONS,
        )
        self._stage("PR_CREATED", note="creating or reusing exactly one Draft Pull Request")
        self._emit("coding_pr", "creating or recovering the Draft Pull Request")
        try:
            result = publisher.create_or_reuse_pr(
                branch=branch,
                commit=commit,
                task_id=self.task_id,
                run_id=self.run_id,
                title=title[:200],
                body=body,
            )
        except (PublicationError, OSError) as exc:
            return self._publication_retry(current, stage="PR_CREATED", error=str(exc), force=force)
        updated = self._stage(
            "PR_CREATED",
            note=f"Draft Pull Request recorded: {result.get('pr_url')}",
            updates={
                "coding": {
                    "publication": {
                        "pr_number": result.get("pr_number"),
                        "pr_url": result.get("pr_url"),
                        "pr_created": bool(result.get("created")),
                        "draft": bool(result.get("draft", True)),
                        "last_error": None,
                        "next_attempt_at": None,
                        "status": "PR_CREATED",
                    }
                }
            },
            milestone="PR_PUBLISHED",
        )
        publisher.cleanup(preserve=False)
        self._emit("coding_pr", "Draft Pull Request is ready")
        return updated

    def cleanup_success(self) -> bool:
        project_coding = self.project.get("coding") or {}
        if not project_coding.get("cleanup_on_success", True):
            return False
        current = self._fresh()
        coding = current.get("coding") or {}
        workspace = str(coding.get("workspace") or "")
        if not workspace:
            return False
        ok = cleanup_workspace(self.workspace_root, workspace, identity=self.identity)
        if ok:
            self._set_coding(
                lambda item: item.update({"workspace": None, "cleaned_up": True}),
                note="disposable coding workspace removed after verified capture",
            )
        return ok
