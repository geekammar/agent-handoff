from __future__ import annotations

import os
from typing import Any, Callable, Mapping

from ...config import BASE_DIR
from ...util import sanitize_error, utcnow_iso
from ..delivery import publication_backoff
from ..security import validate_model_auth
from .changes import ChangeValidationError, SafeGit, validate_changes
from .milestones import CODING_STAGES, PER_STAGE_ORDER
from .publication import (
    PublicationError,
    RemoteStateError,
    TrustedPublisher,
    build_branch,
    build_pr_body,
    build_pr_title,
)
from .testrunner import TestExecutionError, run_test_profile
from .workspace import (
    WorkspacePreparationError,
    cleanup_workspace,
    prepare_workspace,
    validate_coding_root,
    verify_source_repository,
)

REMOTE_REVALIDATE_SECONDS = 300

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
        self.git = git or SafeGit(identity=self.identity, allow_file_protocol=True)
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
        milestones: list[str] | None = None,
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
            for name in [*(milestones or []), *([milestone] if milestone else [])]:
                coding.setdefault("milestones", {})[name] = True

        updated = self._set_coding(mutate, note=note, top_status=top_status, updates=updates)
        self.record = updated
        return updated

    def _block(
        self,
        stage: str,
        reason: str,
        *,
        preserve: bool = True,
        extra: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        safe_reason = self._safe_text(reason, 1500)

        def mutate(coding: dict[str, Any]) -> None:
            if extra is not None:
                extra(coding)
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

    def _remote_check_due(self, publication: Mapping[str, Any], force: bool) -> bool:
        if not (publication.get("branch_pushed") or publication.get("pr_url")):
            return False
        if str(publication.get("remote_state") or "") in ("invalid", "unverified_exhausted"):
            return False
        if force:
            return True
        from datetime import datetime

        from ...util import utcnow

        next_at = publication.get("remote_check_next_at")
        if next_at:
            try:
                if datetime.fromisoformat(str(next_at).replace("Z", "+00:00")) > utcnow():
                    return False
            except ValueError:
                pass
        checked = publication.get("remote_checked_at")
        if not checked:
            return True
        try:
            moment = datetime.fromisoformat(str(checked).replace("Z", "+00:00"))
        except ValueError:
            return True
        return (utcnow() - moment).total_seconds() >= REMOTE_REVALIDATE_SECONDS

    def _remote_max_attempts(self) -> int:
        limits = (self.project.get("coding") or {}).get("limits") or {}
        try:
            return max(1, int(limits.get("max_publication_attempts", 3) or 3))
        except (TypeError, ValueError):
            return 3

    def _remote_health_block(
        self,
        current: dict[str, Any],
        *,
        health_stage: str,
        reason: str,
        state: str,
        publication_updates: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Record a current remote-health problem without rewriting history.

        The durable stage remains monotonic. Historical publication milestones
        stay recorded; ``remote_state`` is the separate current-health view.
        """

        safe_reason = self._safe_text(reason, 1200)

        def mutate(coding: dict[str, Any]) -> None:
            pub = coding.setdefault("publication", {})
            pub.update(dict(publication_updates))
            coding["blocked"] = True
            coding["blocked_stage"] = health_stage
            coding["blocked_reason"] = safe_reason
            coding["artifacts_preserved"] = True
            coding["current_health"] = state
            coding["current_health_stage"] = health_stage
            history = coding.setdefault("stage_history", [])
            history.append(
                {
                    "at": utcnow_iso(),
                    "stage": str(coding.get("stage") or health_stage),
                    "note": f"remote health {state}: {safe_reason[:300]}",
                }
            )
            if len(history) > 120:
                coding["stage_history"] = history[-120:]

        updated = self._set_coding(
            mutate,
            note=f"coding remote health changed to {state}: {safe_reason}",
            top_status="EXECUTION_COMPLETED",
            updates={"attention_reason": safe_reason},
        )
        self._emit("coding_blocked", f"coding remote state is {state}; evidence and history are preserved")
        self.log(f"coding remote state {state}: {safe_reason}")
        return updated

    def _mark_remote_invalid(self, current: dict[str, Any], reason: str) -> dict[str, Any]:
        coding = current.get("coding") or {}
        publication = coding.get("publication") or {}
        stage = "PR_CREATED" if publication.get("pr_url") else "BRANCH_PUBLISHED"
        safe_reason = self._safe_text(reason, 1200)
        return self._remote_health_block(
            current,
            health_stage=stage,
            reason=f"the published remote state is no longer valid: {safe_reason}",
            state="invalid",
            publication_updates={
                "remote_state": "invalid",
                "remote_invalidated_at": utcnow_iso(),
                "remote_invalid_reason": safe_reason,
                "remote_last_error": safe_reason,
                "remote_check_next_at": None,
            },
        )

    def _schedule_remote_check_retry(
        self, current: dict[str, Any], error: str
    ) -> dict[str, Any]:
        coding = current.get("coding") or {}
        publication = coding.get("publication") or {}
        stage = "PR_CREATED" if publication.get("pr_url") else "BRANCH_PUBLISHED"
        attempts = int(publication.get("remote_check_attempts", 0) or 0) + 1
        maximum = self._remote_max_attempts()
        safe_error = self._safe_text(error, 1200)
        if attempts >= maximum:
            return self._remote_health_block(
                current,
                health_stage=stage,
                reason=(
                    f"could not verify the published remote state after {attempts} attempt(s): "
                    f"{safe_error}; use `agent-handoff dispatch reconcile --issue <number>` after GitHub recovers"
                ),
                state="unverified_exhausted",
                publication_updates={
                    "remote_state": "unverified_exhausted",
                    "remote_check_attempts": attempts,
                    "remote_last_error": safe_error,
                    "remote_check_next_at": None,
                },
            )
        next_at = publication_backoff(attempts, 60)

        def update(item: dict[str, Any]) -> None:
            pub = item.setdefault("publication", {})
            pub.update(
                {
                    "remote_state": "unverified",
                    "remote_check_attempts": attempts,
                    "remote_last_error": safe_error,
                    "remote_check_next_at": next_at,
                }
            )

        updated = self._set_coding(
            update,
            note=f"remote publication verification failed transiently; retry after {next_at}",
        )
        self._emit(
            "coding_retryable",
            "could not verify the published remote state; a bounded retry is scheduled without rerunning OpenCode",
        )
        return updated

    def revalidate_remote_publication(
        self, current: dict[str, Any], *, force: bool = False
    ) -> dict[str, Any]:
        """Verify a previously published branch/PR against live GitHub state."""

        coding = current.get("coding") or {}
        if coding.get("blocked"):
            return current
        publication = coding.get("publication") or {}
        if not self._remote_check_due(publication, force):
            return current
        branch = str(publication.get("branch") or "")
        commit = str(publication.get("commit") or "")
        if not branch or not commit:
            return self._mark_remote_invalid(
                current, "the recorded publication branch or commit is missing"
            )
        pr_number = publication.get("pr_number")
        try:
            pr_number = int(pr_number) if pr_number is not None else None
        except (TypeError, ValueError):
            pr_number = None
        if pr_number is None and publication.get("pr_url"):
            pr_number = TrustedPublisher.pr_number_from_url(str(publication.get("pr_url")))
            if pr_number is None:
                return self._mark_remote_invalid(
                    current, "the recorded Draft PR URL has no safe PR number"
                )
        publisher = self._adopt_publisher()
        try:
            publisher.verify_repository()
            verified = publisher.verify_published_state(
                branch=branch,
                commit=commit,
                pr_number=pr_number,
                pr_url=str(publication.get("pr_url") or "") or None,
            )
        except RemoteStateError as exc:
            return self._mark_remote_invalid(current, str(exc))
        except (PublicationError, OSError) as exc:
            return self._schedule_remote_check_retry(current, str(exc))
        now = utcnow_iso()

        def update(item: dict[str, Any]) -> None:
            pub = item.setdefault("publication", {})
            pub.update(
                {
                    "remote_state": "valid",
                    "remote_checked_at": now,
                    "remote_verified_at": now,
                    "remote_check_attempts": 0,
                    "remote_check_next_at": None,
                    "remote_last_error": None,
                    "remote_branch_sha": verified.get("branch_sha"),
                }
            )
            pr = verified.get("pr") or {}
            if pr:
                pub.update(
                    {
                        "remote_pr_state": str(pr.get("state") or ""),
                        "remote_pr_draft": bool(pr.get("draft")),
                        "remote_pr_head_sha": str((pr.get("head") or {}).get("sha") or ""),
                    }
                )

        updated = self._set_coding(update, note="verified published remote branch and Draft PR state")
        self._emit("coding_verified", "published remote branch and Draft PR state are still valid")
        return updated

    def recover_blocked_remote(self) -> dict[str, Any]:
        """Explicitly verify a transiently exhausted remote publication.

        Deterministic ``invalid`` state is never recoverable here. This method
        performs GET verification only: it never executes OpenCode, pushes a
        branch, creates a PR, posts a comment, or sends a notification.
        """

        current = self._fresh()
        coding = current.get("coding") or {}
        publication = coding.get("publication") or {}
        state = str(publication.get("remote_state") or "")
        if not (coding.get("blocked") and state == "unverified_exhausted"):
            if state == "invalid":
                raise CodingPipelineError(
                    "the recorded remote publication state is deterministically invalid; "
                    "it is not recoverable by reconciliation"
                )
            raise CodingPipelineError(
                "the coding record is not blocked by an exhausted transient remote-verification failure"
            )
        branch = str(publication.get("branch") or "")
        commit = str(publication.get("commit") or "")
        if not branch or not commit:
            updated = self._mark_remote_invalid(
                current, "the recorded publication branch or commit is missing"
            )
            return {
                "ok": False,
                "remote_state": "invalid",
                "record": updated,
                "error": "the recorded publication branch or commit is missing",
            }
        pr_number = publication.get("pr_number")
        try:
            pr_number = int(pr_number) if pr_number is not None else None
        except (TypeError, ValueError):
            pr_number = None
        if pr_number is None and publication.get("pr_url"):
            pr_number = TrustedPublisher.pr_number_from_url(str(publication.get("pr_url")))
        publisher = self._adopt_publisher()
        try:
            publisher.verify_repository()
            verified = publisher.verify_published_state(
                branch=branch,
                commit=commit,
                pr_number=pr_number,
                pr_url=str(publication.get("pr_url") or "") or None,
            )
        except RemoteStateError as exc:
            updated = self._mark_remote_invalid(current, str(exc))
            return {
                "ok": False,
                "remote_state": "invalid",
                "record": updated,
                "error": self._safe_text(str(exc), 800),
            }
        except (PublicationError, OSError) as exc:
            error = self._safe_text(str(exc), 800)
            attempts = int(publication.get("remote_check_attempts", 0) or 0)

            def update(item: dict[str, Any]) -> None:
                item.setdefault("publication", {}).update(
                    {
                        "remote_check_attempts": attempts,
                        "remote_last_error": error,
                        "remote_check_next_at": None,
                    }
                )

            updated = self._set_coding(
                update,
                note="explicit remote reconciliation failed transiently; the record remains blocked",
            )
            return {
                "ok": False,
                "remote_state": "unverified_exhausted",
                "record": updated,
                "error": error,
            }
        now = utcnow_iso()
        milestones = coding.get("milestones") or {}
        delivered = bool(
            milestones.get("ISSUE_DELIVERED") or milestones.get("REPORT_DELIVERED")
        )
        upload_verified = bool((current.get("upload") or {}).get("verified"))
        if delivered:
            top_status = "ISSUE_UPDATED"
        elif upload_verified:
            top_status = "UPLOAD_VERIFIED"
        elif milestones.get("AGENT_COMPLETED") or publication.get("pr_url"):
            top_status = "EXECUTION_COMPLETED"
        else:
            top_status = "CLAIMED"

        def update(item: dict[str, Any]) -> None:
            pub = item.setdefault("publication", {})
            pub.update(
                {
                    "remote_state": "valid",
                    "remote_checked_at": now,
                    "remote_verified_at": now,
                    "remote_check_attempts": 0,
                    "remote_check_next_at": None,
                    "remote_last_error": None,
                    "remote_invalid_reason": None,
                    "remote_recovered_at": now,
                    "remote_branch_sha": verified.get("branch_sha"),
                }
            )
            pr = verified.get("pr") or {}
            if pr:
                pub.update(
                    {
                        "remote_pr_state": str(pr.get("state") or ""),
                        "remote_pr_draft": bool(pr.get("draft")),
                        "remote_pr_head_sha": str((pr.get("head") or {}).get("sha") or ""),
                    }
                )
            item["blocked"] = False
            item["blocked_stage"] = None
            item["blocked_reason"] = None
            item["current_health"] = None
            item["current_health_stage"] = None
            history = item.setdefault("stage_history", [])
            history.append(
                {
                    "at": now,
                    "stage": str(item.get("stage") or ""),
                    "note": "remote publication verified by explicit operator reconciliation",
                }
            )
            if len(history) > 120:
                item["stage_history"] = history[-120:]

        updated = self._set_coding(
            update,
            note="explicit remote reconciliation verified the existing branch and Draft PR",
            top_status=top_status,
            updates={"attention_reason": None},
        )
        self._emit(
            "coding_verified",
            "existing remote branch and Draft PR verified by explicit reconciliation; no artifact was created",
        )
        return {"ok": True, "remote_state": "valid", "record": updated, "error": None}

    def _expected_workspace_name(self) -> str:
        return f"issue-{int(self.record['issue_number'])}-{self.task_id}"

    def _agent_started(self, coding: Mapping[str, Any]) -> bool:
        agent = coding.get("agent") or {}
        stage = str(coding.get("stage") or "")
        return str(agent.get("status") or "") in ("running", "completed", "uncertain") or stage in (
            "AGENT_RUNNING",
            "AGENT_COMPLETED",
            "TESTING",
            "VALIDATING_DIFF",
            "COMMIT_PREPARED",
            "BRANCH_PUBLISHED",
            "PR_CREATED",
        )

    def revalidate_workspace(self, current: dict[str, Any]) -> dict[str, Any]:
        """Revalidate persisted workspace identity and artifacts on every resume."""

        coding = current.get("coding") or {}
        if coding.get("blocked"):
            return current
        workspace = str(coding.get("workspace") or "")
        if not workspace:
            return self._block(
                "WORKSPACE_PREPARED",
                "the persisted coding workspace path is missing; publication is refused",
            )
        agent_started = self._agent_started(coding)
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
            source = verify_source_repository(
                self.source_workspace,
                repository=self.repository,
                base_branch=self.base_branch,
                git=self.git,
            )
            base_commit = str(coding.get("base_commit") or "")
            if not base_commit:
                raise WorkspacePreparationError("the persisted base commit is missing")
            exists = self.git.run(
                ["cat-file", "-e", f"{base_commit}^{{commit}}"],
                cwd=source["source"],
                check=False,
            )
            if exists.returncode != 0:
                raise WorkspacePreparationError("the persisted base commit no longer exists in the source repository")
            adopted = prepare_workspace(
                workspace_root=self.workspace_root,
                repository=self.repository,
                base_branch=self.base_branch,
                base_commit=base_commit,
                source=source["source"],
                issue_number=int(self.record["issue_number"]),
                task_id=self.task_id,
                identity=self.identity,
                git=self.git,
                allow_existing=True,
                require_clean=not agent_started,
            )
        except (WorkspacePreparationError, ChangeValidationError, OSError) as exc:
            return self._block(
                "WORKSPACE_PREPARED",
                f"persisted coding workspace revalidation failed: {exc}",
            )
        if adopted["workspace"] != workspace or adopted["workspace_name"] != str(coding.get("workspace_name") or ""):
            if adopted["workspace"] != workspace:
                return self._block(
                    "WORKSPACE_PREPARED",
                    "the persisted coding workspace path does not match the managed task workspace",
                )
        diff = coding.get("diff") or {}
        if diff.get("status") == "passed":
            try:
                evidence = validate_changes(
                    adopted["workspace"],
                    git=self.git,
                    limits=(self.project.get("coding") or {}).get("limits") or {},
                    protected_paths=(self.project.get("coding") or {}).get("protected_paths") or [],
                    allowed_paths=(self.project.get("coding") or {}).get("allowed_paths") or [],
                    secret_scan=bool((self.project.get("coding") or {}).get("secret_scan", True)),
                )
            except (ChangeValidationError, OSError) as exc:
                return self._block(
                    "VALIDATING_DIFF",
                    f"persisted workspace artifacts could not be revalidated: {exc}",
                )
            if (
                evidence.get("status") != "passed"
                or evidence.get("manifest_sha256") != str(diff.get("manifest_sha256") or "")
            ):
                return self._block(
                    "VALIDATING_DIFF",
                    "the workspace changed after diff validation; publication is refused",
                )
        current = self._set_coding(
            lambda item: item.update(
                {"workspace": adopted["workspace"], "workspace_name": adopted["workspace_name"]}
            ),
            note="persisted coding workspace revalidated before resume",
        )
        self.record = current
        return current

    def prepare(self) -> dict[str, Any]:
        current = self._fresh()
        coding = current.get("coding") or {}
        if coding.get("stage") not in (None, "REQUEST_ACCEPTED", "CLAIMED", "WORKSPACE_PREPARED"):
            return current
        if coding.get("workspace") and coding.get("base_commit"):
            if self._agent_started(coding) or os.path.lexists(str(coding.get("workspace"))):
                return self.revalidate_workspace(current)
            self._emit("coding_workspace", "persisted workspace is missing before agent start; recreating it")
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
            publisher.verify_repository()
            source = verify_source_repository(
                self.source_workspace,
                repository=self.repository,
                base_branch=self.base_branch,
                git=self.git,
            )
            requested_base = str(coding.get("base_commit") or source["base_commit"])
            exists = self.git.run(
                ["cat-file", "-e", f"{requested_base}^{{commit}}"],
                cwd=source["source"],
                check=False,
            )
            if exists.returncode != 0:
                raise WorkspacePreparationError(
                    "the persisted base commit no longer exists in the source repository"
                )
            prepared = prepare_workspace(
                workspace_root=self.workspace_root,
                repository=self.repository,
                base_branch=self.base_branch,
                base_commit=requested_base,
                source=source["source"],
                issue_number=int(self.record["issue_number"]),
                task_id=self.task_id,
                identity=self.identity,
                git=self.git,
                allow_existing=True,
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
            "process_group_id": getattr(result, "process_group_id", None),
            "orphans_detected": bool(getattr(result, "orphans_detected", False)),
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
        if agent.get("orphans_detected"):
            return self._block(
                "AGENT_COMPLETED",
                "the coding agent left an orphaned child process group; the result is not publishable",
            )
        if str(agent.get("outcome") or "") != "succeeded":
            return self._block(
                "AGENT_COMPLETED",
                f"the coding agent did not report success (outcome={agent.get('outcome')}); "
                "tests and publication were not started",
            )
        current = self.revalidate_workspace(current)
        coding = current.get("coding") or {}
        if coding.get("blocked"):
            return current
        current = self._ensure_tests(current)
        coding = current.get("coding") or {}
        if coding.get("blocked"):
            return current
        gated = self._require_test_gate(current)
        if gated is not None:
            return gated
        current = self._ensure_validation(current)
        coding = current.get("coding") or {}
        if coding.get("blocked"):
            return current
        gated = self._require_test_gate(current)
        if gated is not None:
            return gated
        current = self._ensure_commit(current, force=force)
        coding = current.get("coding") or {}
        if coding.get("blocked"):
            return current
        gated = self._require_test_gate(current)
        if gated is not None:
            return gated
        current = self._ensure_push(current, force=force)
        coding = current.get("coding") or {}
        publication = coding.get("publication") or {}
        if coding.get("blocked") or not publication.get("branch_pushed"):
            return current
        gated = self._require_test_gate(current)
        if gated is not None:
            return gated
        current = self._ensure_pr(current, force=force)
        return current

    PUBLICATION_STAGES = (
        "VALIDATING_DIFF",
        "COMMIT_PREPARED",
        "BRANCH_PUBLISHED",
        "PR_CREATED",
    )
    TEST_EVIDENCE_STATUSES = {
        "passed",
        "failed",
        "timed_out",
        "interrupted",
        "orphans",
        "error",
        "cancelled",
    }

    def _test_gate_problem(self, coding: Mapping[str, Any]) -> str | None:
        """Return a reason unless tests are explicitly and verifiably passed."""

        tests = coding.get("tests")
        if not isinstance(tests, dict):
            return "trusted test evidence is missing or malformed"
        status = str(tests.get("status") or "")
        if status != "passed":
            return f"trusted tests did not pass (status={status or 'missing'})"
        expected_profile = str((self.project.get("coding") or {}).get("test_profile") or "")
        if expected_profile and str(tests.get("profile") or "") != expected_profile:
            return "trusted test evidence belongs to a different configured test profile"
        commands = tests.get("commands")
        if not isinstance(commands, list) or not commands:
            return "trusted test evidence contains no executed command"
        for item in commands:
            if not isinstance(item, dict):
                return "trusted test evidence contains a malformed command result"
            if str(item.get("status") or "") != "passed":
                return "a trusted test command did not pass"
            try:
                exit_code = int(item.get("exit_code"))
            except (TypeError, ValueError):
                return "a trusted test command has no verified exit code"
            if exit_code != 0:
                return "a trusted test command exited non-zero"
            if item.get("timed_out"):
                return "a trusted test command timed out"
            if item.get("orphans_detected"):
                return "a trusted test command left an orphaned process group"
        if tests.get("failed_command"):
            return "trusted test evidence records a failed command"
        return None

    def _record_test_failure(
        self,
        current: dict[str, Any],
        *,
        reason: str,
        stored: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist test evidence and the blocked state in one ledger write."""

        def extra(coding: dict[str, Any]) -> None:
            if stored is not None:
                coding["tests"] = dict(stored)
            coding.setdefault("milestones", {})["TESTS_PASSED"] = False

        return self._block("TESTING", reason, extra=extra)

    def _require_test_gate(self, current: dict[str, Any]) -> dict[str, Any] | None:
        """Fail closed before validation, commit, push, or PR publication."""

        coding = current.get("coding") or {}
        if coding.get("blocked"):
            return current
        problem = self._test_gate_problem(coding)
        if problem:
            return self._record_test_failure(
                current,
                reason=f"{problem}; publication is refused",
            )
        return None

    def _ensure_tests(self, current: dict[str, Any]) -> dict[str, Any]:
        coding = current.get("coding") or {}
        tests = coding.get("tests")
        if tests is not None and not isinstance(tests, dict):
            return self._record_test_failure(
                current, reason="trusted test evidence is malformed; publication is refused"
            )
        status = str((tests or {}).get("status") or "") if isinstance(tests, dict) else ""
        if status == "passed":
            problem = self._test_gate_problem(coding)
            if problem:
                return self._record_test_failure(
                    current, reason=f"{problem}; publication is refused"
                )
            return current
        has_evidence = bool(
            isinstance(tests, dict)
            and (
                status in self.TEST_EVIDENCE_STATUSES
                or tests.get("commands")
                or status not in ("", "not_run")
            )
        )
        stage = str(coding.get("stage") or "")
        if has_evidence:
            return self._record_test_failure(
                current,
                reason=(
                    f"persisted trusted test evidence is not a pass (status={status or 'missing'}); "
                    "publication is refused and the agent/tests are not re-run"
                ),
            )
        if stage in self.PUBLICATION_STAGES:
            return self._record_test_failure(
                current,
                reason="trusted test evidence is missing at a publication stage; publication is refused",
            )
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
            return self._record_test_failure(
                current, reason=f"trusted test execution could not start: {exc}"
            )
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
                    "process_group_id": item.get("process_group_id"),
                    "orphans_detected": bool(item.get("orphans_detected")),
                }
                for item in evidence.get("commands") or []
            ],
        }
        if evidence.get("status") != "passed":
            return self._record_test_failure(
                current,
                reason=(
                    f"trusted tests did not pass (status={evidence.get('status')}); "
                    "no commit, push, or Draft PR was created"
                ),
                stored=stored,
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
        gated = self._require_test_gate(current)
        if gated is not None:
            return gated
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
            "manifest": evidence.get("manifest"),
            "manifest_sha256": evidence.get("manifest_sha256"),
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
            milestone="DIFF_VALIDATED",
        )
        self._emit("coding_validation", "diff validation passed")
        return updated

    def _ensure_commit(self, current: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
        gated = self._require_test_gate(current)
        if gated is not None:
            return gated
        current = self.revalidate_workspace(current)
        if (current.get("coding") or {}).get("blocked"):
            return current
        coding = current.get("coding") or {}
        publication = coding.get("publication") or {}
        if publication.get("commit"):
            return current
        if not self._publication_due(current, force):
            self._emit("coding_waiting", "publication commit retry is not due yet")
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
            return self._publication_retry(
                current,
                stage="COMMIT_PREPARED",
                error=f"preparing the publication commit failed: {exc}",
                force=force,
            )
        updated = self._stage(
            "COMMIT_PREPARED",
            note=f"commit prepared: {committed['commit']}",
            updates={
                "coding": {
                    "publication": {
                        "branch": branch,
                        "commit": committed["commit"],
                        "staging": committed.get("staging"),
                        "manifest_sha256": committed.get("manifest_sha256"),
                        "status": "COMMITTED",
                        "draft": True,
                    }
                }
            },
            milestone="COMMIT_CREATED",
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
        gated = self._require_test_gate(current)
        if gated is not None:
            return gated
        current = self.revalidate_remote_publication(current, force=force)
        if (current.get("coding") or {}).get("blocked"):
            return current
        coding = current.get("coding") or {}
        publication = coding.get("publication") or {}
        if publication.get("branch_pushed"):
            return current
        if not self._publication_due(current, force):
            self._emit("coding_waiting", "branch publication retry is not due yet")
            return current
        publisher = self._adopt_publisher()
        staging = publication.get("staging")
        staging_ok = False
        if staging and os.path.isdir(str(staging)):
            try:
                publisher.adopt_staging(str(staging))
                staging_ok = True
            except (PublicationError, OSError) as exc:
                self.log(f"persisted publication staging is not reusable: {exc}")
        branch = str(publication.get("branch") or "")
        commit = str(publication.get("commit") or "")
        if not staging_ok:
            try:
                remote_sha = publisher.remote_branch_sha(branch)
            except (PublicationError, OSError) as exc:
                return self._block(
                    "BRANCH_PUBLISHED",
                    f"publication staging is missing and the remote branch could not be reconciled: {exc}",
                )
            if remote_sha == commit:
                updated = self._stage(
                    "BRANCH_PUBLISHED",
                    note="remote branch already contains the verified commit after staging loss",
                    updates={
                        "coding": {
                            "publication": {
                                "branch": branch,
                                "commit": commit,
                                "branch_pushed": True,
                                "status": "BRANCH_PUBLISHED",
                                "last_error": None,
                                "next_attempt_at": None,
                                "staging_recovered_by_remote_sha": True,
                            }
                        }
                    },
                    milestone="BRANCH_PUBLISHED",
                )
                self._emit("coding_branch", "remote branch reconciled after publication staging loss")
                return updated
            if remote_sha:
                return self._block(
                    "BRANCH_PUBLISHED",
                    "the publication staging is unusable and the remote branch contains a different commit; "
                    "refusing to overwrite the published branch",
                )
            self._emit(
                "coding_commit",
                "publication staging was lost before the branch was pushed; rebuilding the verified commit",
            )

            def clear_commit(item: dict[str, Any]) -> None:
                item.setdefault("publication", {}).update(
                    {"commit": None, "staging": None, "status": "PENDING"}
                )

            current = self._set_coding(
                clear_commit, note="rebuilding lost publication staging before branch publication"
            )
            current = self._ensure_commit(current, force=True)
            coding = current.get("coding") or {}
            if coding.get("blocked"):
                return current
            publication = coding.get("publication") or {}
            staging = publication.get("staging")
            commit = str(publication.get("commit") or "")
            if not publication.get("commit") or not staging:
                return self._block(
                    "BRANCH_PUBLISHED", "the publication commit could not be rebuilt after staging loss"
                )
            publisher.adopt_staging(str(staging))
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
            milestone="BRANCH_PUBLISHED",
        )
        self._emit("coding_branch", "task branch published")
        return updated

    def _ensure_pr(self, current: dict[str, Any], *, force: bool) -> dict[str, Any]:
        gated = self._require_test_gate(current)
        if gated is not None:
            return gated
        current = self.revalidate_remote_publication(current, force=force)
        if (current.get("coding") or {}).get("blocked"):
            return current
        coding = current.get("coding") or {}
        publication = coding.get("publication") or {}
        publisher = self._adopt_publisher()
        if publication.get("pr_url"):
            if not (coding.get("milestones") or {}).get("PR_PUBLISHED"):
                current = self._stage(
                    "PR_CREATED",
                    note="reconciled Draft Pull Request publication milestone from existing PR evidence",
                    milestones=["PR_CREATED", "PR_PUBLISHED"],
                )
                coding = current.get("coding") or {}
                publication = coding.get("publication") or {}
            staging = publication.get("staging")
            if staging and os.path.isdir(str(staging)):
                try:
                    publisher.adopt_staging(str(staging))
                    publisher.cleanup(preserve=False)
                except (PublicationError, OSError) as exc:
                    self.log(f"publication staging cleanup is still pending: {exc}")
            return current
        if not publication.get("branch_pushed"):
            return self._block("PR_CREATED", "the publication branch was not verified as pushed")
        if not self._publication_due(current, force):
            self._emit("coding_waiting", "Draft PR creation retry is not due yet")
            return current
        staging = publication.get("staging")
        if staging and os.path.isdir(staging):
            try:
                publisher.adopt_staging(str(staging))
            except (PublicationError, OSError) as exc:
                self.log(f"persisted publication staging is not reusable for PR creation: {exc}")
        branch = str(publication.get("branch") or "")
        commit = str(publication.get("commit") or "")
        project_name = str((current.get("project") or {}).get("name") or "project")
        title = build_pr_title(
            self.publication.get("pr_title_prefix"),
            str(current.get("issue_title") or f"issue #{self.record['issue_number']}"),
        )
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
        generated_body_sha = TrustedPublisher.body_sha256(body)
        remote_body_sha = str(result.get("body_sha256") or generated_body_sha)
        created = bool(result.get("created"))
        external_edit = bool(
            not created
            and (not result.get("body_sha256") or remote_body_sha != generated_body_sha)
        )
        history = list(publication.get("pr_body_sha256_history") or [])
        if external_edit:
            history = []
        else:
            generated_sha = generated_body_sha
            if generated_sha not in history:
                history.append(generated_sha)
        generated_sha = generated_body_sha if not external_edit else None
        provenance = "external" if external_edit else "generated"
        skip_reason = None
        skipped_at = None
        if external_edit:
            skip_reason = (
                "the Draft PR body was modified outside Agent Handoff; the verified report link "
                "was not written, and the existing body was preserved"
            )
            skipped_at = utcnow_iso()
        updated = self._stage(
            "PR_CREATED",
            note=f"Draft Pull Request recorded: {result.get('pr_url')}",
            updates={
                "coding": {
                    "publication": {
                        "pr_number": result.get("pr_number"),
                        "pr_url": result.get("pr_url"),
                        "pr_created": created,
                        "draft": bool(result.get("draft", True)),
                        "last_error": None,
                        "next_attempt_at": None,
                        "status": "PR_CREATED",
                        "pr_body_sha256": remote_body_sha,
                        "pr_body_generated_sha256": generated_sha,
                        "pr_body_sha256_history": history[-10:],
                        "pr_body_external_edit": external_edit,
                        "pr_body_provenance": provenance,
                        "report_link_skip_reason": skip_reason,
                        "report_link_skipped_at": skipped_at,
                        "report_link_last_error": None if external_edit else publication.get("report_link_last_error"),
                        "report_link_next_attempt_at": None if external_edit else publication.get("report_link_next_attempt_at"),
                    }
                }
            },
            milestones=["PR_CREATED", "PR_PUBLISHED"],
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
        ok = cleanup_workspace(
            self.workspace_root,
            workspace,
            identity=self.identity,
            expected_name=self._expected_workspace_name(),
        )
        if ok:
            self._set_coding(
                lambda item: item.update({"workspace": None, "cleaned_up": True}),
                note="disposable coding workspace removed after verified capture",
            )
        return ok
