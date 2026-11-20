from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from ..config import storage_path
from ..util import FileLock, atomic_write_json, atomic_write_text, ensure_dir, read_json, read_text, utcnow, utcnow_iso

SCHEMA = 1


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _due(value: Any, *, force: bool) -> bool:
    if force or not value:
        return True
    moment = _parse(value)
    if moment is None:
        return True
    return moment <= utcnow()


class NotificationStore:
    def __init__(self, config: dict[str, Any]):
        self.root = ensure_dir(storage_path(config, "state") / "dispatch" / "notifications")
        self.records_dir = ensure_dir(self.root / "records")

    def lock(self, blocking: bool = True, timeout: float = 30.0) -> FileLock:
        return FileLock(self.root / "notifications.lock", blocking=blocking, timeout=timeout)

    def path(self, notification_id: str) -> Path:
        return self.records_dir / f"{notification_id}.json"

    def activation_path(self) -> Path:
        return self.root / "activated_at"

    def ensure_activation(self) -> datetime:
        with self.lock():
            moment = _parse(read_text(self.activation_path(), default=""))
            if moment is not None:
                return moment
            now = utcnow()
            atomic_write_text(self.activation_path(), now.isoformat(timespec="microseconds").replace("+00:00", "Z"))
            return now

    def get(self, notification_id: str) -> dict[str, Any] | None:
        return read_json(self.path(notification_id), default=None)

    def all(self) -> list[dict[str, Any]]:
        records = []
        for path in sorted(self.records_dir.glob("*.json")):
            data = read_json(path, default=None)
            if isinstance(data, dict):
                records.append(data)
        records.sort(key=lambda item: str(item.get("created_at") or ""))
        return records

    def create(self, record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        notification_id = str(record["notification_id"])
        with self.lock():
            existing = self.get(notification_id)
            if existing:
                return existing, False
            record = dict(record)
            record["schema"] = SCHEMA
            record.setdefault("history", [])
            record["created_at"] = record.get("created_at") or utcnow_iso()
            record["updated_at"] = record["created_at"]
            atomic_write_json(self.path(notification_id), record)
            return record, True

    def update(
        self,
        notification_id: str,
        mutator: Callable[[dict[str, Any]], None],
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        with self.lock():
            record = self.get(notification_id)
            if not record:
                raise FileNotFoundError(f"notification not found: {notification_id}")
            before = record.get("state")
            mutator(record)
            if note or record.get("state") != before:
                record.setdefault("history", []).append(
                    {
                        "at": utcnow_iso(),
                        "state": record.get("state"),
                        "note": note or "",
                    }
                )
                record["history"] = record["history"][-40:]
            record["updated_at"] = utcnow_iso()
            atomic_write_json(self.path(notification_id), record)
            return record

    def recover_stale_sending(self, *, stale_after_seconds: int) -> list[dict[str, Any]]:
        recovered = []
        now = utcnow()
        with self.lock():
            for record in self.all():
                if record.get("state") != "SENDING":
                    continue
                moment = _parse(record.get("sending_at") or record.get("updated_at"))
                if moment is None or (now - moment).total_seconds() < max(30, stale_after_seconds):
                    continue
                record["state"] = "NEEDS_ATTENTION"
                record["sending_at"] = None
                record["next_attempt_at"] = None
                record["last_error"] = (
                    "notification send was interrupted; Telegram may or may not have accepted the message. "
                    "Review before retrying to avoid a duplicate."
                )
                record["updated_at"] = utcnow_iso()
                record.setdefault("history", []).append(
                    {
                        "at": record["updated_at"],
                        "state": "NEEDS_ATTENTION",
                        "note": "ambiguous send outcome after interruption",
                    }
                )
                atomic_write_json(self.path(record["notification_id"]), record)
                recovered.append(record)
        return recovered

    def claim_next(
        self,
        *,
        force: bool = False,
        allowed_ids: set[str] | None = None,
        excluded_ids: set[str] | None = None,
    ) -> dict[str, Any] | None:
        with self.lock():
            for record in self.all():
                state = record.get("state")
                if state not in ("PENDING", "RETRY_WAIT"):
                    continue
                if allowed_ids is not None and record.get("notification_id") not in allowed_ids:
                    continue
                if excluded_ids and record.get("notification_id") in excluded_ids:
                    continue
                if not _due(record.get("next_attempt_at"), force=force):
                    continue
                record["state"] = "SENDING"
                record["attempts"] = int(record.get("attempts", 0) or 0) + 1
                record["sending_at"] = utcnow_iso()
                record["last_error"] = None
                record["updated_at"] = record["sending_at"]
                record.setdefault("history", []).append(
                    {"at": record["updated_at"], "state": "SENDING", "note": "Telegram send started"}
                )
                record["history"] = record["history"][-40:]
                atomic_write_json(self.path(record["notification_id"]), record)
                return record
        return None

    def mark_sent(self, notification_id: str, message_id: Any) -> dict[str, Any]:
        now = utcnow_iso()

        def apply(record: dict[str, Any]) -> None:
            record["state"] = "SENT"
            record["sent_at"] = now
            record["telegram_message_id"] = message_id
            record["next_attempt_at"] = None
            record["sending_at"] = None
            record["last_error"] = None

        return self.update(notification_id, apply, note="Telegram message sent")

    def mark_failed(
        self,
        notification_id: str,
        *,
        error: str,
        max_attempts: int,
        retry_seconds: int,
        retry_after: int | None = None,
    ) -> dict[str, Any]:
        def apply(record: dict[str, Any]) -> None:
            attempts = int(record.get("attempts", 0) or 0)
            record["last_error"] = error
            record["sending_at"] = None
            if attempts >= max_attempts:
                record["state"] = "NEEDS_ATTENTION"
                record["next_attempt_at"] = None
            else:
                record["state"] = "RETRY_WAIT"
                delay = retry_after if retry_after is not None else min(
                    3600, max(5, retry_seconds) * (2 ** min(max(attempts - 1, 0), 6))
                )
                record["next_attempt_at"] = (
                    (utcnow() + timedelta(seconds=delay)).isoformat(timespec="seconds").replace("+00:00", "Z")
                )

        return self.update(notification_id, apply, note=error[:300])

    def requeue(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        notification_id: str | None = None,
    ) -> list[dict[str, Any]]:
        changed = []
        with self.lock():
            for record in self.all():
                if notification_id and record.get("notification_id") != notification_id:
                    continue
                identity = record.get("identity") or {}
                if task_id and identity.get("task_id") != task_id:
                    continue
                if run_id and identity.get("run_id") != run_id:
                    continue
                if record.get("state") in ("SENT", "SENDING"):
                    continue
                record["state"] = "PENDING"
                record["attempts"] = 0
                record["next_attempt_at"] = None
                record["sending_at"] = None
                record["updated_at"] = utcnow_iso()
                record.setdefault("history", []).append(
                    {"at": record["updated_at"], "state": "PENDING", "note": "operator requested notification retry"}
                )
                record["history"] = record["history"][-40:]
                atomic_write_json(self.path(record["notification_id"]), record)
                changed.append(record)
        return changed

    def filter(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        records = self.all()
        if task_id:
            records = [item for item in records if (item.get("identity") or {}).get("task_id") == task_id]
        if run_id:
            records = [item for item in records if (item.get("identity") or {}).get("run_id") == run_id]
        return records

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.all():
            state = str(record.get("state") or "UNKNOWN")
            counts[state] = counts.get(state, 0) + 1
        return counts
