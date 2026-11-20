from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable

from ..config import storage_path
from ..util import FileLock, atomic_write_json, ensure_dir, read_json, utcnow_iso

SCHEMA = 1
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
        mutator: Callable[[dict[str, Any]], None],
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        with self.lock():
            record = self.get(repository, issue_number)
            if not record:
                raise FileNotFoundError(f"dispatch record not found: {repository}#{issue_number}")
            before = record.get("status")
            mutator(record)
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
            record["status"] = status
            for key, value in (updates or {}).items():
                if isinstance(value, dict) and isinstance(record.get(key), dict):
                    record[key].update(value)
                else:
                    record[key] = value

        return self.update(repository, issue_number, apply, note=note)

    def event_path(self, repository: str, issue_number: int, stamp: str) -> Path:
        return self.sessions_dir / f"{record_id(repository, issue_number)}-{stamp}.jsonl"


def new_dispatch_record(
    *,
    repository: str,
    issue: dict[str, Any],
    project: dict[str, Any],
    agent: str,
    model: str,
    request: dict[str, Any],
) -> dict[str, Any]:
    now = utcnow_iso()
    return {
        "schema": SCHEMA,
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


def safe_record_summary(record: dict[str, Any]) -> dict[str, Any]:
    capture = record.get("capture") or {}
    upload = record.get("upload") or {}
    publication = record.get("publication") or {}
    execution = record.get("execution") or {}
    session = record.get("session") or {}
    return {
        "dispatch_id": record.get("dispatch_id"),
        "repository": record.get("repository"),
        "issue_number": record.get("issue_number"),
        "issue_url": record.get("issue_url"),
        "status": record.get("status"),
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
        "attention_reason": record.get("attention_reason"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
    }
