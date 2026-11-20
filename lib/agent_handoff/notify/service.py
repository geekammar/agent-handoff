from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..config import all_projects, storage_path
from ..util import sanitize_error, utcnow_iso
from .config import NotificationConfigError, validate_notification_config
from .messages import (
    DELIVERY_COMPLETED,
    DELIVERY_DELAYED,
    KINDS,
    NEEDS_ATTENTION,
    NotificationMessageError,
    build_telegram_message,
    safe_url,
    text_value,
)
from .store import NotificationStore
from .transport import TelegramTransport

SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b([A-Za-z0-9_.-]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API[_-]?KEY|PRIVATE[_-]?KEY|CLIENT[_-]?SECRET)[A-Za-z0-9_.-]*\s*[:=]\s*)"
    r"(?:['\"]([^'\"]{6,})['\"]|([A-Za-z0-9_./+=\-]{6,}))"
)
RETRY_NEXT_ACTION = (
    "Fix the Telegram token/chat configuration if needed, then run "
    "`agent-handoff dispatch notify --retry`. Report delivery itself is unaffected."
)


def notification_id(project: str, task_id: str, run_id: str, kind: str) -> str:
    value = "|".join([str(project).strip().lower(), str(task_id), str(run_id), str(kind)])
    return "n-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _next_action(record: dict[str, Any]) -> str:
    state = str(record.get("state") or "")
    if state == "PENDING":
        return "The next dispatcher poll will send this notification."
    if state == "SENDING":
        return "A send is in progress. If it was interrupted, it becomes visible for review after the timeout."
    if state == "RETRY_WAIT":
        next_at = record.get("next_attempt_at")
        return (
            f"Automatic retry is scheduled for {next_at}. "
            "Task/report delivery is not affected."
            if next_at
            else "Automatic retry is pending. Task/report delivery is not affected."
        )
    if state == "NEEDS_ATTENTION":
        return RETRY_NEXT_ACTION
    return "No action."


def notification_view(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload") or {}
    identity = record.get("identity") or {}
    return {
        "notification_id": record.get("notification_id"),
        "kind": record.get("kind"),
        "state": record.get("state"),
        "attempts": int(record.get("attempts", 0) or 0),
        "project": payload.get("project") or identity.get("project"),
        "task_id": identity.get("task_id"),
        "run_id": identity.get("run_id"),
        "repository": record.get("repository"),
        "issue_number": record.get("issue_number"),
        "issue_url": payload.get("issue_url") or record.get("issue_url"),
        "report_url": payload.get("report_url"),
        "stage": payload.get("stage"),
        "reason": payload.get("reason"),
        "created_at": record.get("created_at"),
        "sent_at": record.get("sent_at"),
        "telegram_message_id": record.get("telegram_message_id"),
        "next_attempt_at": record.get("next_attempt_at"),
        "last_error": record.get("last_error"),
        "next_action": _next_action(record),
    }


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class NotificationService:
    def __init__(self, config: dict[str, Any], *, log: Callable[[str], None] | None = None):
        self.store: NotificationStore | None = None
        self.store_error: str | None = None
        try:
            self.store = NotificationStore(config)
        except Exception as exc:
            self.store_error = sanitize_error(str(exc))
        self._log = log
        try:
            resolved, errors, _warnings = validate_notification_config(config)
        except NotificationConfigError as exc:
            resolved = {"enabled": False, "token": None}
            errors = [str(exc)]
        self.settings = resolved
        self.config_errors = list(errors)
        if self.store_error:
            self.config_errors.append(f"notification state directory is not usable: {self.store_error}")
        self.configured_enabled = bool(resolved.get("enabled"))
        self.enabled = self.configured_enabled and not self.config_errors
        self.activated_at: datetime | None = None
        if self.enabled and self.store is not None:
            try:
                self.activated_at = self.store.ensure_activation()
            except Exception as exc:
                self.config_errors.append(
                    f"notification activation marker could not be written: {sanitize_error(str(exc))}"
                )
                self.enabled = False
        self.transport = (
            TelegramTransport(str(resolved.get("token")), timeout_seconds=int(resolved.get("timeout_seconds", 10)))
            if self.enabled
            else None
        )
        self._redactions = self._path_redactions(config)

    def _log_safe(self, message: str) -> None:
        if self._log is None:
            return
        try:
            self._log(sanitize_error(message, 500))
        except Exception:
            pass

    def _path_redactions(self, config: dict[str, Any]) -> list[str]:
        values = {str(Path.home())}
        for key in ("data", "queue", "state", "locks", "logs", "tmp"):
            try:
                values.add(str(storage_path(config, key)))
            except Exception:
                pass
        for project in all_projects(config):
            values.update(str(item) for item in project.get("paths") or [])
        dispatch_projects = (config.get("dispatch") or {}).get("projects")
        if isinstance(dispatch_projects, dict):
            for item in dispatch_projects.values():
                if isinstance(item, dict) and item.get("workspace"):
                    values.add(str(item["workspace"]))
        return sorted((item for item in values if len(item) > 1), key=len, reverse=True)

    def _clean(self, value: Any, limit: int = 500) -> str:
        text = sanitize_error(str(value or ""), 4000)
        text = SECRET_ASSIGN_RE.sub(r"\1[REDACTED]", text)
        text = " ".join(text.split())
        for path in self._redactions:
            text = text.replace(path, "<local>")
        if len(text) > limit:
            text = text[: max(1, limit - 1)].rstrip() + "…"
        return text

    def _identity(self, record: dict[str, Any]) -> dict[str, str]:
        project = record.get("project") or {}
        capture = record.get("capture") or {}
        project_key = str(project.get("core_key") or project.get("name") or "unknown")
        task_id = str(capture.get("task_id") or record.get("dispatch_id") or "unassigned")
        run_id = str(capture.get("run_id") or "unassigned")
        return {"project": project_key, "task_id": task_id, "run_id": run_id}

    def _payload(self, record: dict[str, Any], *, reason: str | None = None, **extra: Any) -> dict[str, Any]:
        project = record.get("project") or {}
        upload = record.get("upload") or {}
        publication = record.get("publication") or {}
        execution = record.get("execution") or {}
        issue_number = record.get("issue_number")
        title = text_value(
            self._clean(record.get("issue_title") or f"Agent Handoff issue #{issue_number}", 240),
            120,
        )
        project_name = text_value(
            self._clean(project.get("name") or project.get("core_name") or "unknown", 160),
            80,
        )
        report_url = safe_url(upload.get("report_url")) if upload.get("verified") else None
        payload = {
            "project": project_name,
            "task_title": title,
            "issue_url": record.get("issue_url"),
            "issue_number": issue_number,
            "repository": record.get("repository"),
            "report_url": report_url,
            "receipt_url": safe_url(upload.get("receipt_url")) if upload.get("verified") else None,
            "outcome": execution.get("outcome"),
            "status": record.get("status"),
            "publication_status": publication.get("status"),
            "reason": self._clean(reason, 500) if reason else None,
        }
        payload.update(extra)
        return payload

    def _enqueue(self, kind: str, record: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any] | None:
        if not self.enabled or self.store is None:
            return None
        if kind not in KINDS:
            return None
        identity = self._identity(record)
        notification = notification_id(identity["project"], identity["task_id"], identity["run_id"], kind)
        try:
            build_telegram_message(kind, payload)
        except NotificationMessageError as exc:
            self._log_safe(f"notification {notification} was not queued: {exc}")
            return None
        now = utcnow_iso()
        item = {
            "schema": 1,
            "notification_id": notification,
            "kind": kind,
            "identity": identity,
            "repository": record.get("repository"),
            "issue_number": record.get("issue_number"),
            "issue_url": record.get("issue_url"),
            "dispatch_id": record.get("dispatch_id"),
            "dispatch_status": record.get("status"),
            "payload": payload,
            "state": "PENDING",
            "attempts": 0,
            "created_at": now,
            "updated_at": now,
            "next_attempt_at": None,
            "sent_at": None,
            "telegram_message_id": None,
            "last_error": None,
            "history": [{"at": now, "state": "PENDING", "note": f"{kind} queued after a durable dispatch transition"}],
        }
        try:
            created, is_new = self.store.create(item)
        except Exception as exc:
            self._log_safe(f"notification queue write failed: {exc}")
            return None
        if is_new:
            self._log_safe(f"notification {notification} queued kind={kind}")
        result = dict(created)
        result["_new"] = is_new
        return result

    def enqueue_completed(self, record: dict[str, Any]) -> dict[str, Any] | None:
        upload = record.get("upload") or {}
        publication = record.get("publication") or {}
        if record.get("status") != "ISSUE_UPDATED":
            return None
        if not upload.get("verified") or not safe_url(upload.get("report_url")):
            return None
        if not safe_url(upload.get("receipt_url")):
            return None
        if publication.get("status") != "PUBLISHED" or not publication.get("published_at"):
            return None
        return self._enqueue(DELIVERY_COMPLETED, record, self._payload(record))

    def enqueue_needs_attention(self, record: dict[str, Any], reason: str, next_action: str) -> dict[str, Any] | None:
        payload = self._payload(
            record,
            reason=reason or record.get("attention_reason"),
            next_action=self._clean(next_action, 500),
        )
        return self._enqueue(NEEDS_ATTENTION, record, payload)

    def enqueue_delivery_delayed(
        self,
        record: dict[str, Any],
        *,
        reason: str,
        stage: str,
        attempts: int,
    ) -> dict[str, Any] | None:
        threshold = int(self.settings.get("delayed_after_attempts", 3) or 3)
        if attempts < threshold:
            return None
        payload = self._payload(record, reason=reason, stage=stage, attempts=attempts)
        return self._enqueue(DELIVERY_DELAYED, record, payload)

    def reconcile(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        """Recover notification intents after a crash without replaying history."""

        result = {"queued": 0}
        if not self.enabled or self.store is None or self.activated_at is None:
            return result
        for record in records:
            updated = _parse_time(record.get("updated_at"))
            if updated is None or updated < self.activated_at:
                continue
            status = record.get("status")
            upload = record.get("upload") or {}
            publication = record.get("publication") or {}
            item = None
            if status == "ISSUE_UPDATED":
                item = self.enqueue_completed(record)
            elif status == "NEEDS_ATTENTION":
                item = self.enqueue_needs_attention(
                    record,
                    str(record.get("attention_reason") or "operator review is required"),
                    "Inspect the host and session evidence. The agent will not be rerun automatically.",
                )
            elif status == "UPLOAD_VERIFIED" and publication.get("status") == "EXHAUSTED":
                item = self.enqueue_needs_attention(
                    record,
                    str(publication.get("last_error") or record.get("attention_reason") or "publication retries exhausted"),
                    "Use `agent-handoff dispatch deliver` to publish the verified report manually; "
                    "the upload is reused and the agent is not re-executed.",
                )
            elif status == "UPLOAD_PENDING" and upload.get("status") == "FAILED":
                item = self.enqueue_delivery_delayed(
                    record,
                    reason=str(upload.get("last_error") or "upload is retrying"),
                    stage="upload",
                    attempts=int(upload.get("attempts", 0) or 0),
                )
            elif status == "UPLOAD_VERIFIED" and publication.get("status") == "FAILED":
                item = self.enqueue_delivery_delayed(
                    record,
                    reason=str(publication.get("last_error") or "publication is retrying"),
                    stage="publication",
                    attempts=int(publication.get("attempts", 0) or 0),
                )
            if item and item.get("_new"):
                result["queued"] += 1
        if result["queued"]:
            self._log_safe(f"recovered {result['queued']} durable notification intent(s) after a restart")
        return result

    def drain(
        self,
        *,
        force: bool = False,
        limit: int = 10,
        allowed_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        result = {
            "enabled": self.enabled,
            "configuration_error": "; ".join(self.config_errors) if self.config_errors else None,
            "sent": 0,
            "failed": 0,
            "attempted": 0,
            "errors": [],
        }
        if not self.enabled or self.transport is None or self.store is None:
            result["counts"] = self.store.counts() if self.store is not None else {}
            return result
        stale_after = max(120, int(self.settings.get("timeout_seconds", 10)) * 3)
        try:
            recovered = self.store.recover_stale_sending(stale_after_seconds=stale_after)
        except Exception as exc:
            result["errors"].append(self._clean(f"notification recovery failed: {exc}"))
            recovered = []
        for record in recovered:
            self._log_safe(
                f"notification {record.get('notification_id')} had an ambiguous interrupted send; operator review required"
            )
        attempted_ids: set[str] = set()
        for _index in range(max(1, limit)):
            try:
                claimed = self.store.claim_next(
                    force=force,
                    allowed_ids=allowed_ids,
                    excluded_ids=attempted_ids,
                )
            except Exception as exc:
                result["errors"].append(self._clean(f"notification queue claim failed: {exc}"))
                break
            if not claimed:
                break
            result["attempted"] += 1
            notification = str(claimed.get("notification_id"))
            attempted_ids.add(notification)
            try:
                message = build_telegram_message(str(claimed.get("kind")), claimed.get("payload") or {})
                sent = self.transport.send_message(str(self.settings.get("chat_id")), **message)
            except Exception as exc:
                sent = {
                    "ok": False,
                    "error": f"notification preparation failed: {exc.__class__.__name__}",
                    "retry_after": None,
                }
            if sent.get("ok"):
                try:
                    self.store.mark_sent(notification, sent.get("message_id"))
                    result["sent"] += 1
                except Exception as exc:
                    result["errors"].append(self._clean(f"notification result write failed: {exc}"))
                continue
            error = self._clean(sent.get("error") or "Telegram send failed", 400)
            result["failed"] += 1
            result["errors"].append(f"{notification}: {error}")
            try:
                updated = self.store.mark_failed(
                    notification,
                    error=error,
                    max_attempts=int(self.settings.get("max_attempts", 5) or 5),
                    retry_seconds=int(self.settings.get("retry_seconds", 60) or 60),
                    retry_after=sent.get("retry_after"),
                )
                self._log_safe(
                    f"notification {notification} state={updated.get('state')} error={error}"
                )
            except Exception as exc:
                result["errors"].append(self._clean(f"notification failure write failed: {exc}"))
        result["counts"] = self.store.counts()
        return result

    def retry(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        if not self.enabled or self.store is None:
            return {
                "ok": False,
                "enabled": False,
                "configuration_error": "; ".join(self.config_errors) if self.config_errors else None,
                "error": "Telegram notifications are disabled or the configuration is invalid",
                "retried": 0,
                "counts": self.store.counts() if self.store is not None else {},
            }
        try:
            self.store.recover_stale_sending(
                stale_after_seconds=max(120, int(self.settings.get("timeout_seconds", 10)) * 3)
            )
        except Exception:
            pass
        changed = self.store.requeue(task_id=task_id, run_id=run_id)
        allowed = {str(item.get("notification_id")) for item in changed}
        summary = self.drain(force=True, limit=max(1, len(changed)), allowed_ids=allowed)
        return {
            "ok": summary.get("sent", 0) == len(changed) and not summary.get("errors"),
            "enabled": True,
            "configuration_error": None,
            "retried": len(changed),
            **summary,
        }

    def list_records(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        include_sent: bool = True,
    ) -> list[dict[str, Any]]:
        if self.store is None:
            return []
        records = self.store.filter(task_id=task_id, run_id=run_id)
        if not include_sent:
            records = [item for item in records if item.get("state") != "SENT"]
        return [notification_view(item) for item in records]

    def counts(self) -> dict[str, int]:
        return self.store.counts() if self.store is not None else {}

    def overall_next_action(self) -> str:
        if self.config_errors:
            return "Fix notifications.telegram configuration, then run `agent-handoff config validate`."
        if not self.configured_enabled:
            return "Optional and disabled."
        counts = self.counts()
        if counts.get("NEEDS_ATTENTION"):
            return "Run `agent-handoff dispatch notify --json`, fix the cause, then `agent-handoff dispatch notify --retry`."
        if counts.get("RETRY_WAIT") or counts.get("PENDING") or counts.get("SENDING"):
            return "The dispatcher retries automatically; task/report delivery is unaffected."
        return "None."
