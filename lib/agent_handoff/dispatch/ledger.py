from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable

from ..config import storage_path
from .coding.milestones import milestone_state
from ..util import FileLock, atomic_write_json, ensure_dir, read_json, utcnow_iso, utcnow_iso_precise

SCHEMA = 1
UNCHANGED = object()
TERMINAL_STATUSES = {"ISSUE_UPDATED", "REJECTED", "NEEDS_ATTENTION"}
ACTIVE_STATUSES = {
    "NEW",
    "CLAIMED",
    "RUNNING",
    "EXECUTION_COMPLETED",
    "REPORT_CAPTURED",
    "UPLOAD_PENDING",
    "UPLOAD_VERIFIED",
}


def record_id(repository: str, issue_number: int) -> str:
    value = f"{repository.strip().lower()}#{int(issue_number)}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


class Ledger:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.root = ensure_dir(storage_path(config, "state") / "dispatch")
        self.records_dir = ensure_dir(self.root / "records")
        self.sessions_dir = ensure_dir(self.root / "sessions")

    def lock(self, blocking: bool = True, timeout: float = 60.0) -> FileLock:
        return FileLock(self.root / "ledger.lock", blocking=blocking, timeout=timeout)

    def publication_lock(
        self, repository: str, issue_number: int, *, timeout: float = 120.0
    ) -> FileLock:
        return FileLock(
            self.root / f"publication-{record_id(repository, issue_number)}.lock",
            blocking=True,
            timeout=timeout,
        )

    def path(self, repository: str, issue_number: int) -> Path:
        return self.records_dir / f"{record_id(repository, issue_number)}.json"

    def get(self, repository: str, issue_number: int) -> dict[str, Any] | None:
        return read_json(self.path(repository, issue_number), default=None)

    def all(self) -> list[dict[str, Any]]:
        records = []
        for path in sorted(self.records_dir.glob("*.json")):
            data = read_json(path, default=None)
            if isinstance(data, dict):
                records.append(data)
        records.sort(key=lambda item: int(item.get("issue_number", 0)))
        return records

    def find_by_run(self, task_id: str, run_id: str) -> dict[str, Any] | None:
        for record in self.all():
            capture = record.get("capture") or {}
            if capture.get("task_id") == task_id and capture.get("run_id") == run_id:
                return record
        return None

    def _write(self, record: dict[str, Any]) -> dict[str, Any]:
        record["schema"] = SCHEMA
        record["updated_at"] = utcnow_iso()
        history = record.setdefault("history", [])
        if len(history) > 80:
            record["history"] = history[-80:]
        atomic_write_json(self.path(record["repository"], int(record["issue_number"])), record)
        return record

    def create(self, record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        repository = str(record["repository"])
        issue_number = int(record["issue_number"])
        with self.lock():
            existing = self.get(repository, issue_number)
            if existing:
                return existing, False
            record = dict(record)
            record["schema"] = SCHEMA
            record.setdefault("history", [])
            record["created_at"] = record.get("created_at") or utcnow_iso()
            self._write(record)
            return record, True

    def update(
        self,
        repository: str,
        issue_number: int,
        mutator: Callable[[dict[str, Any]], Any],
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Apply one mutator under the ledger lock.

        A mutator may return ``UNCHANGED`` to report that the authoritative
        record needs no mutation. That skips the history entry, ``updated_at``,
        and the file write while preserving the existing behavior of mutators
        that return ``None``.
        """

        with self.lock():
            record = self.get(repository, issue_number)
            if not record:
                raise FileNotFoundError(f"dispatch record not found: {repository}#{issue_number}")
            before = record.get("status")
            if mutator(record) is UNCHANGED:
                return record
            if note or record.get("status") != before:
                record.setdefault("history", []).append(
                    {"at": utcnow_iso(), "status": record.get("status"), "note": note or ""}
                )
            return self._write(record)

    def set_status(
        self,
        repository: str,
        issue_number: int,
        status: str,
        *,
        note: str | None = None,
        updates: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        def apply(record: dict[str, Any]) -> None:
            previous = record.get("status")
            record["status"] = status
            if status == "NEEDS_ATTENTION" and previous != status:
                record["attention_since"] = utcnow_iso_precise()
            elif status != "NEEDS_ATTENTION":
                record["attention_since"] = None
            for key, value in (updates or {}).items():
                if isinstance(value, dict) and isinstance(record.get(key), dict):
                    record[key].update(value)
                else:
                    record[key] = value

        return self.update(repository, issue_number, apply, note=note)

    def event_path(self, repository: str, issue_number: int, stamp: str) -> Path:
        return self.sessions_dir / f"{record_id(repository, issue_number)}-{stamp}.jsonl"


def new_coding_state(project: dict[str, Any]) -> dict[str, Any]:
    coding = project.get("coding") or {}
    publication = coding.get("publication") or {}
    now = utcnow_iso()
    return {
        "enabled": bool(coding.get("enabled")),
        "stage": "REQUEST_ACCEPTED",
        "stage_history": [],
        "repository": coding.get("repository"),
        "base_branch": coding.get("base_branch"),
        "base_commit": None,
        "source_workspace": coding.get("source_workspace"),
        "workspace_root": coding.get("workspace_root"),
        "workspace": None,
        "test_profile": coding.get("test_profile"),
        "allowed_model_aliases": list(coding.get("allowed_model_aliases") or []),
        "limits": dict(coding.get("limits") or {}),
        "publication": {
            "branch": None,
            "commit": None,
            "branch_pushed": False,
            "pr_number": None,
            "pr_url": None,
            "attempts": 0,
            "next_attempt_at": None,
            "last_error": None,
            "draft": bool(publication.get("draft", True)),
        },
        "tests": {"status": "not_run", "commands": []},
        "diff": {"status": "not_run", "files": [], "changed_count": 0, "blocked": []},
        "agent": {"status": "not_started", "outcome": None},
        "blocked": False,
        "blocked_stage": None,
        "blocked_reason": None,
        "recovery": {"attempts": 0, "last_error": None},
        "artifacts_preserved": False,
        "milestones": {
            "AGENT_COMPLETED": False,
            "TESTS_PASSED": False,
            "DIFF_VALIDATED": False,
            "COMMIT_CREATED": False,
            "BRANCH_PUBLISHED": False,
            "PR_CREATED": False,
            "PR_PUBLISHED": False,
            "REPORT_VERIFIED": False,
            "ISSUE_DELIVERED": False,
            "REPORT_DELIVERED": False,
            "TELEGRAM_SENT": False,
        },
        "created_at": now,
    }


def new_dispatch_record(
    *,
    repository: str,
    issue: dict[str, Any],
    project: dict[str, Any],
    agent: str,
    model: str,
    request: dict[str, Any],
    task_type: str = "read_only",
    coding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = utcnow_iso()
    record = {
        "schema": SCHEMA,
        "task_type": task_type,
        "dispatch_id": f"d-{record_id(repository, int(issue['number']))}",
        "repository": repository,
        "issue_number": int(issue["number"]),
        "issue_url": issue.get("html_url") or issue.get("url"),
        "issue_title": issue.get("title") or "",
        "request": request,
        "project": {
            "name": project.get("name"),
            "core_key": project.get("core_key"),
            "core_name": project.get("core_name"),
            "workspace": project.get("workspace"),
            "drive_folder_id": project.get("drive_folder_id"),
            "execution": project.get("execution"),
        },
        "agent": agent,
        "model": model,
        "session": {"mode": "new", "id": None, "events_path": None},
        "execution": {
            "status": "NEW",
            "started_at": None,
            "finished_at": None,
            "exit_code": None,
            "timed_out": False,
            "outcome": None,
            "termination_reason": None,
            "last_error": None,
        },
        "capture": {
            "status": "PENDING",
            "task_id": None,
            "run_id": None,
            "report_path": None,
            "captured_at": None,
            "last_error": None,
        },
        "upload": {
            "status": "PENDING",
            "verified": False,
            "verified_at": None,
            "report_remote_path": None,
            "report_url": None,
            "receipt_remote_path": None,
            "receipt_url": None,
            "attempts": 0,
            "last_error": None,
        },
        "publication": {
            "status": "PENDING",
            "attempts": 0,
            "published_at": None,
            "comment_url": None,
            "last_error": None,
            "next_attempt_at": None,
        },
        "status": "NEW",
        "attention_reason": None,
        "created_at": now,
        "updated_at": now,
        "history": [{"at": now, "status": "NEW", "note": "request received"}],
    }
    record["coding"] = coding if task_type == "coding" else None
    return record


def safe_record_summary(record: dict[str, Any]) -> dict[str, Any]:
    capture = record.get("capture") or {}
    upload = record.get("upload") or {}
    publication = record.get("publication") or {}
    execution = record.get("execution") or {}
    session = record.get("session") or {}
    coding = record.get("coding") or {}
    coding_publication = coding.get("publication") or {}
    tests = coding.get("tests") or {}
    diff = coding.get("diff") or {}
    milestones = coding.get("milestones") or {}
    return {
        "dispatch_id": record.get("dispatch_id"),
        "repository": record.get("repository"),
        "issue_number": record.get("issue_number"),
        "issue_url": record.get("issue_url"),
        "status": record.get("status"),
        "task_type": record.get("task_type") or "read_only",
        "project": (record.get("project") or {}).get("name"),
        "agent": record.get("agent"),
        "model": record.get("model"),
        "session_id": session.get("id"),
        "execution": {
            "status": execution.get("status"),
            "outcome": execution.get("outcome"),
            "started_at": execution.get("started_at"),
            "finished_at": execution.get("finished_at"),
            "last_error": execution.get("last_error"),
        },
        "capture": {
            "status": capture.get("status"),
            "task_id": capture.get("task_id"),
            "run_id": capture.get("run_id"),
            "captured_at": capture.get("captured_at"),
            "last_error": capture.get("last_error"),
        },
        "upload": {
            "status": upload.get("status"),
            "verified": upload.get("verified"),
            "verified_at": upload.get("verified_at"),
            "report_url": upload.get("report_url"),
            "receipt_url": upload.get("receipt_url"),
            "attempts": upload.get("attempts", 0),
            "last_error": upload.get("last_error"),
            "next_attempt_at": upload.get("next_attempt_at"),
        },
        "publication": {
            "status": publication.get("status"),
            "attempts": publication.get("attempts", 0),
            "published_at": publication.get("published_at"),
            "comment_url": publication.get("comment_url"),
            "last_error": publication.get("last_error"),
            "next_attempt_at": publication.get("next_attempt_at"),
        },
        "coding": {
            "stage": coding.get("stage"),
            "base_branch": coding.get("base_branch"),
            "base_commit": coding.get("base_commit"),
            "branch": coding_publication.get("branch"),
            "commit": coding_publication.get("commit"),
            "branch_pushed": coding_publication.get("branch_pushed"),
            "pr_number": coding_publication.get("pr_number"),
            "pr_url": coding_publication.get("pr_url"),
            "tests": {
                "status": tests.get("status"),
                "failed_command": tests.get("failed_command"),
            },
            "diff": {
                "status": diff.get("status"),
                "changed_count": diff.get("changed_count"),
                "blocked": list(diff.get("blocked") or []),
            },
            "blocked": bool(coding.get("blocked")),
            "blocked_stage": coding.get("blocked_stage"),
            "blocked_reason": coding.get("blocked_reason"),
            "milestones": {
                "agent_completed": milestone_state(coding, "AGENT_COMPLETED") == "true",
                "tests_passed": milestone_state(coding, "TESTS_PASSED") == "true",
                "diff_validated": milestone_state(coding, "DIFF_VALIDATED") == "true",
                "commit_created": milestone_state(coding, "COMMIT_CREATED") == "true",
                "branch_published": milestone_state(coding, "BRANCH_PUBLISHED") == "true",
                "pr_created": milestone_state(coding, "PR_CREATED") == "true",
                "pr_published": milestone_state(coding, "PR_PUBLISHED") == "true",
                "report_verified": milestone_state(coding, "REPORT_VERIFIED") == "true",
                "issue_delivered": milestone_state(coding, "ISSUE_DELIVERED") == "true",
                "report_delivered": milestone_state(coding, "REPORT_DELIVERED") == "true",
                "telegram_sent": milestone_state(coding, "TELEGRAM_SENT") == "true",
            },
            "publication_attempts": coding_publication.get("attempts", 0),
            "publication_next_attempt_at": coding_publication.get("next_attempt_at"),
            "report_link_updated": bool(coding_publication.get("report_link_updated")),
            "report_link_last_error": coding_publication.get("report_link_last_error"),
            "report_link_next_attempt_at": coding_publication.get("report_link_next_attempt_at"),
            "report_link_skip_reason": (
                coding_publication.get("report_link_skip_reason")
                or coding_publication.get("report_link_skipped_reason")
            ),
            "report_link_skipped_at": coding_publication.get("report_link_skipped_at"),
            "pr_body_provenance": coding_publication.get("pr_body_provenance"),
            "pr_body_external_edit": bool(coding_publication.get("pr_body_external_edit")),
            "remote_state": coding_publication.get("remote_state"),
            "remote_invalid_reason": coding_publication.get("remote_invalid_reason"),
            "remote_verified_at": coding_publication.get("remote_verified_at"),
            "remote_invalidated_at": coding_publication.get("remote_invalidated_at"),
            "historical_branch_published": bool(coding_publication.get("branch_pushed")),
            "historical_pr_created": bool(coding_publication.get("pr_url")),
            "artifact_preserved": coding.get("artifacts_preserved"),
        }
        if record.get("task_type") == "coding"
        else None,
        "attention_reason": record.get("attention_reason"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
    }
