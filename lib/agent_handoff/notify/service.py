from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..config import all_projects, storage_path
from ..dispatch.coding.milestones import PER_STAGE_ORDER, milestone_state
from ..dispatch.ledger import Ledger
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
    if state == "SUPERSEDED":
        return (
            "No action. The underlying task condition changed before this notification was sent; "
            "the record is retained for audit."
        )
    if record.get("ambiguous") and state != "SENT":
        notification = record.get("notification_id")
        return (
            "Review the Telegram chat before doing anything: the interrupted send may already have arrived. "
            "Generic `--retry` will not resend it. If an intentional duplicate is acceptable, run "
            f"`agent-handoff dispatch notify --retry --notification {notification} --acknowledge-duplicate`."
        )
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
        if record.get("unresolved"):
            return (
                "The related dispatch record is unavailable, so this notification was not sent. "
                "Inspect the durable dispatch state, then use `agent-handoff dispatch notify --retry`."
            )
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
        "ambiguous": bool(record.get("ambiguous")),
        "ambiguous_at": record.get("ambiguous_at"),
        "unresolved": bool(record.get("unresolved")),
        "superseded_at": record.get("superseded_at"),
        "superseded_reason": record.get("superseded_reason"),
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
        self.ledger: Ledger | None = None
        self.ledger_error: str | None = None
        try:
            self.ledger = Ledger(config)
        except Exception as exc:
            self.ledger_error = sanitize_error(str(exc))
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
                if isinstance(item, dict):
                    coding = item.get("coding") or {}
                    for key in ("workspace_root", "staging_root", "source_workspace"):
                        if coding.get(key):
                            values.add(str(coding[key]))
        coding_global = (config.get("dispatch") or {}).get("coding") or {}
        if isinstance(coding_global, dict) and coding_global.get("staging_root"):
            values.add(str(coding_global["staging_root"]))
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
        coding = record.get("coding") or {}
        coding_publication = coding.get("publication") or {}
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
            "task_type": record.get("task_type") or "read_only",
            "coding_outcome": (
                "blocked" if coding.get("blocked") else ("draft_pr_published" if coding_publication.get("pr_url") else None)
            )
            if record.get("task_type") == "coding"
            else None,
            "pr_url": safe_url(coding_publication.get("pr_url")),
            "base_commit": coding.get("base_commit"),
            "tests_status": (coding.get("tests") or {}).get("status"),
            "milestones": {
                "agent_completed": milestone_state(coding, "AGENT_COMPLETED") == "true",
                "tests_passed": milestone_state(coding, "TESTS_PASSED") == "true",
                "pr_published": milestone_state(coding, "PR_PUBLISHED") == "true",
                "report_delivered": milestone_state(coding, "REPORT_DELIVERED") == "true",
            }
            if record.get("task_type") == "coding"
            else None,
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

    def _record_telegram_sent(self, notification: dict[str, Any], stored: dict[str, Any]) -> None:
        """Record an acknowledged Telegram API send without affecting delivery."""

        if str(notification.get("kind") or "") != DELIVERY_COMPLETED:
            return
        if self.ledger is None:
            return
        repository = str(notification.get("repository") or "").strip()
        issue_number = notification.get("issue_number")
        if not repository or issue_number is None:
            return
        try:
            number = int(issue_number)
        except (TypeError, ValueError):
            return
        sent_at = str(stored.get("sent_at") or utcnow_iso())
        message_id = stored.get("telegram_message_id")

        def apply(item: dict[str, Any]) -> None:
            coding = item.get("coding")
            if not isinstance(coding, dict):
                return
            coding.setdefault("milestones", {})["TELEGRAM_SENT"] = True
            coding["telegram_sent_at"] = sent_at
            coding["telegram_message_id"] = message_id
            current = str(coding.get("stage") or "")
            if PER_STAGE_ORDER.get("NOTIFICATION_SENT", -1) >= PER_STAGE_ORDER.get(current, -1):
                coding["stage"] = "NOTIFICATION_SENT"

        try:
            self.ledger.update(
                repository,
                number,
                apply,
                note="Telegram API send acknowledged",
            )
        except Exception as exc:
            # The notification is already sent. A ledger update failure must
            # never change notification state or trigger a duplicate send.
            self._log_safe(f"Telegram milestone ledger write failed independently: {exc}")

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

    def _after_activation(self, value: Any) -> bool:
        moment = _parse_time(value)
        if moment is None or self.activated_at is None:
            return False
        return moment >= self.activated_at

    def _history_transition_at(self, record: dict[str, Any], status: str) -> str | None:
        entries = record.get("history") if isinstance(record.get("history"), list) else []
        found = None
        previous = None
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            current = str(entry.get("status") or "")
            if current == status and previous != status and index > 0:
                found = entry.get("at")
            previous = current
        return str(found) if found else None

    def _history_note_at(self, record: dict[str, Any], needle: str, *, status: str | None = None) -> str | None:
        entries = record.get("history") if isinstance(record.get("history"), list) else []
        needle = needle.lower()
        found = None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if status is not None and entry.get("status") != status:
                continue
            if needle in str(entry.get("note") or "").lower():
                found = entry.get("at")
        return str(found) if found else None

    def _ledger_record(self, notification: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        if self.ledger is None:
            return None, self.ledger_error or "the durable dispatch ledger is not available"
        repository = str(notification.get("repository") or "").strip()
        issue_number = notification.get("issue_number")
        if not repository or issue_number is None:
            return None, "the related dispatch record identity is missing"
        try:
            record = self.ledger.get(repository, int(issue_number))
        except (TypeError, ValueError):
            return None, "the related dispatch record identity is invalid"
        except Exception as exc:
            return None, f"the related dispatch record could not be read: {sanitize_error(str(exc), 200)}"
        if not record:
            return None, "the related dispatch record is unavailable"
        return record, None

    def _eligibility(self, notification: dict[str, Any]) -> tuple[str, str | None]:
        record, error = self._ledger_record(notification)
        if record is None:
            return "unresolved", error or "the related dispatch record is unavailable"
        kind = str(notification.get("kind") or "")
        payload = notification.get("payload") or {}
        status = str(record.get("status") or "")
        upload = record.get("upload") or {}
        publication = record.get("publication") or {}
        if kind == DELIVERY_COMPLETED:
            if (
                status == "ISSUE_UPDATED"
                and upload.get("verified")
                and safe_url(upload.get("report_url"))
                and publication.get("status") == "PUBLISHED"
                and _parse_time(publication.get("published_at")) is not None
            ):
                return "valid", None
            return "stale", (
                "the task is no longer in a verified delivered state "
                f"(current status: {status or 'unknown'})"
            )
        if kind == NEEDS_ATTENTION:
            if status == "NEEDS_ATTENTION":
                return "valid", None
            if status == "UPLOAD_VERIFIED" and publication.get("status") == "EXHAUSTED":
                return "valid", None
            return "stale", (
                "the delivery attention condition was resolved before the notification was sent "
                f"(current status: {status or 'unknown'})"
            )
        if kind == DELIVERY_DELAYED:
            stage = str(payload.get("stage") or "")
            if stage == "upload":
                if (
                    status in ("REPORT_CAPTURED", "UPLOAD_PENDING")
                    and upload.get("status") == "FAILED"
                    and not upload.get("verified")
                ):
                    return "valid", None
                return "stale", (
                    "the upload delay condition changed before the notification was sent "
                    f"(current status: {status or 'unknown'})"
                )
            if stage == "publication":
                if (
                    status == "UPLOAD_VERIFIED"
                    and publication.get("status") == "FAILED"
                    and upload.get("verified")
                ):
                    return "valid", None
                return "stale", (
                    "the publication delay condition changed before the notification was sent "
                    f"(current status: {status or 'unknown'})"
                )
            return "stale", "the delayed notification has no recognized delivery stage"
        return "stale", "the notification kind is not recognized by this version"

    def reconcile(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        """Recover notification intents after a crash without replaying history."""

        result = {"queued": 0}
        if not self.enabled or self.store is None or self.activated_at is None:
            return result
        for record in records:
            status = record.get("status")
            upload = record.get("upload") or {}
            publication = record.get("publication") or {}
            item = None
            if status == "ISSUE_UPDATED":
                if not self._after_activation(publication.get("published_at")):
                    continue
                item = self.enqueue_completed(record)
            elif status == "NEEDS_ATTENTION":
                boundary = record.get("attention_since") or self._history_transition_at(record, "NEEDS_ATTENTION")
                if not self._after_activation(boundary):
                    continue
                item = self.enqueue_needs_attention(
                    record,
                    str(record.get("attention_reason") or "operator review is required"),
                    "Inspect the host and session evidence. The agent will not be rerun automatically.",
                )
            elif status == "UPLOAD_VERIFIED" and publication.get("status") == "EXHAUSTED":
                boundary = publication.get("exhausted_at") or self._history_note_at(
                    record, "publication retries exhausted", status="UPLOAD_VERIFIED"
                )
                if not self._after_activation(boundary):
                    continue
                item = self.enqueue_needs_attention(
                    record,
                    str(publication.get("last_error") or record.get("attention_reason") or "publication retries exhausted"),
                    "Use `agent-handoff dispatch deliver` to publish the verified report manually; "
                    "the upload is reused and the agent is not re-executed.",
                )
            elif status == "UPLOAD_PENDING" and upload.get("status") == "FAILED":
                boundary = upload.get("last_failed_at") or self._history_note_at(
                    record, "verified upload pending retry", status="UPLOAD_PENDING"
                )
                if not self._after_activation(boundary):
                    continue
                item = self.enqueue_delivery_delayed(
                    record,
                    reason=str(upload.get("last_error") or "upload is retrying"),
                    stage="upload",
                    attempts=int(upload.get("attempts", 0) or 0),
                )
            elif status == "UPLOAD_VERIFIED" and publication.get("status") == "FAILED":
                boundary = publication.get("last_failed_at") or self._history_note_at(
                    record, "github publication failed; retryable", status="UPLOAD_VERIFIED"
                )
                if not self._after_activation(boundary):
                    continue
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
        self._reconcile_sent_milestones()
        return result

    def _reconcile_sent_milestones(self) -> None:
        """Rebuild TELEGRAM_SENT from SENT notifications after a crash."""

        if self.store is None or self.ledger is None:
            return
        try:
            notifications = self.store.all()
        except Exception:
            return
        for notification in notifications:
            if str(notification.get("state") or "") != "SENT":
                continue
            if str(notification.get("kind") or "") != DELIVERY_COMPLETED:
                continue
            record, _error = self._ledger_record(notification)
            if not record:
                continue
            coding = record.get("coding") or {}
            if (coding.get("milestones") or {}).get("TELEGRAM_SENT"):
                continue
            self._record_telegram_sent(notification, notification)

    def drain(
        self,
        *,
        force: bool = False,
        limit: int = 10,
        allowed_ids: set[str] | None = None,
        allow_ambiguous: bool = False,
    ) -> dict[str, Any]:
        result = {
            "enabled": self.enabled,
            "configuration_error": "; ".join(self.config_errors) if self.config_errors else None,
            "sent": 0,
            "failed": 0,
            "attempted": 0,
            "superseded": 0,
            "preserved": 0,
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
                    allow_ambiguous=allow_ambiguous,
                )
            except Exception as exc:
                result["errors"].append(self._clean(f"notification queue claim failed: {exc}"))
                break
            if not claimed:
                break
            notification = str(claimed.get("notification_id"))
            attempted_ids.add(notification)
            try:
                eligibility, reason = self._eligibility(claimed)
            except Exception as exc:
                eligibility, reason = "unresolved", f"notification eligibility check failed: {exc.__class__.__name__}"
            if eligibility == "stale":
                try:
                    self.store.mark_superseded(notification, reason or "the underlying task condition changed")
                    result["superseded"] += 1
                    self._log_safe(f"notification {notification} superseded without sending: {reason}")
                except Exception as exc:
                    result["errors"].append(self._clean(f"notification supersede write failed: {exc}"))
                continue
            if eligibility == "unresolved":
                reason = reason or "the related dispatch record is unavailable"
                try:
                    self.store.mark_unresolved(notification, reason)
                    result["preserved"] += 1
                    result["errors"].append(f"{notification}: {self._clean(reason, 300)}")
                    self._log_safe(f"notification {notification} preserved without sending: {reason}")
                except Exception as exc:
                    result["errors"].append(self._clean(f"notification preserve write failed: {exc}"))
                continue
            result["attempted"] += 1
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
                    stored = self.store.mark_sent(notification, sent.get("message_id"))
                    self._record_telegram_sent(claimed, stored)
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
        notification_id: str | None = None,
        acknowledge_duplicate: bool = False,
    ) -> dict[str, Any]:
        if not self.enabled or self.store is None:
            return {
                "ok": False,
                "enabled": False,
                "configuration_error": "; ".join(self.config_errors) if self.config_errors else None,
                "error": "Telegram notifications are disabled or the configuration is invalid",
                "retried": 0,
                "skipped_ambiguous": 0,
                "counts": self.store.counts() if self.store is not None else {},
            }
        try:
            self.store.recover_stale_sending(
                stale_after_seconds=max(120, int(self.settings.get("timeout_seconds", 10)) * 3)
            )
        except Exception:
            pass
        skipped_ambiguous = 0
        if notification_id:
            target = self.store.get(notification_id)
            if not target:
                return {
                    "ok": False,
                    "enabled": True,
                    "configuration_error": None,
                    "error": f"notification not found: {notification_id}",
                    "retried": 0,
                    "skipped_ambiguous": 0,
                    "counts": self.store.counts(),
                }
            state = str(target.get("state") or "")
            if state in ("SENT", "SUPERSEDED"):
                return {
                    "ok": False,
                    "enabled": True,
                    "configuration_error": None,
                    "error": f"notification {notification_id} is {state}; it is never retried",
                    "retried": 0,
                    "skipped_ambiguous": 0,
                    "counts": self.store.counts(),
                }
            if state == "SENDING":
                return {
                    "ok": False,
                    "enabled": True,
                    "configuration_error": None,
                    "error": "a send is already in progress for this notification; wait for it to finish or time out",
                    "retried": 0,
                    "skipped_ambiguous": 0,
                    "counts": self.store.counts(),
                }
            if target.get("ambiguous") and not acknowledge_duplicate:
                return {
                    "ok": False,
                    "enabled": True,
                    "configuration_error": None,
                    "error": (
                        "this send has an ambiguous outcome and may already have reached Telegram; "
                        "review the chat, then repeat with --notification and --acknowledge-duplicate "
                        "if an intentional duplicate is acceptable"
                    ),
                    "retried": 0,
                    "skipped_ambiguous": 1,
                    "counts": self.store.counts(),
                }
            changed = self.store.requeue(
                notification_id=notification_id,
                allow_ambiguous=acknowledge_duplicate,
            )
        else:
            if acknowledge_duplicate:
                return {
                    "ok": False,
                    "enabled": True,
                    "configuration_error": None,
                    "error": "--acknowledge-duplicate requires --notification with one notification id",
                    "retried": 0,
                    "skipped_ambiguous": 0,
                    "counts": self.store.counts(),
                }
            skipped_ambiguous = sum(
                1
                for item in self.store.all()
                if item.get("ambiguous")
                and item.get("state") not in ("SENT", "SUPERSEDED", "SENDING")
            )
            changed = self.store.requeue(task_id=task_id, run_id=run_id)
        allowed = {str(item.get("notification_id")) for item in changed}
        summary = self.drain(
            force=True,
            limit=max(1, len(changed)),
            allowed_ids=allowed,
            allow_ambiguous=bool(notification_id and acknowledge_duplicate),
        )
        ok = summary.get("sent", 0) == len(changed) and not summary.get("errors") and not skipped_ambiguous
        next_action = None
        if skipped_ambiguous:
            next_action = (
                "Ambiguous notification(s) were not retried. Review the Telegram chat; if an intentional "
                "duplicate is acceptable, use `agent-handoff dispatch notify --retry "
                "--notification <id> --acknowledge-duplicate`."
            )
        elif not changed:
            next_action = "No retryable notifications matched."
        return {
            "ok": ok,
            "enabled": True,
            "configuration_error": None,
            "retried": len(changed),
            "skipped_ambiguous": skipped_ambiguous,
            "next_action": next_action,
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
        ambiguous = self.store is not None and any(
            item.get("ambiguous") and item.get("state") not in ("SENT", "SUPERSEDED")
            for item in self.store.all()
        )
        if ambiguous:
            return (
                "Review ambiguous Telegram send(s) in the chat. Generic `dispatch notify --retry` "
                "will not resend them; use a targeted `--notification <id> --acknowledge-duplicate` "
                "only if an intentional duplicate is acceptable."
            )
        if counts.get("NEEDS_ATTENTION"):
            return "Run `agent-handoff dispatch notify --json`, fix the cause, then `agent-handoff dispatch notify --retry`."
        if counts.get("RETRY_WAIT") or counts.get("PENDING") or counts.get("SENDING"):
            return "The dispatcher retries automatically; task/report delivery is unaffected."
        return "None."
