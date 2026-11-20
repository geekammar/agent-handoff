from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from ..util import sanitize_error, utcnow
from .ledger import safe_record_summary

DELIVERED = "delivered"
ATTENTION = "attention"
STALLED = "stalled"
RETRYABLE = "retryable"
PENDING = "pending"
ACTIVE = "active"
IGNORED = "ignored"

GROUP_ORDER = (
    (ACTIVE, "Executing / current"),
    (STALLED, "Stalled"),
    (ATTENTION, "Needs attention"),
    (RETRYABLE, "Retryable"),
    (PENDING, "Pending"),
    (DELIVERED, "Delivered"),
    (IGNORED, "Ignored"),
)


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _seconds_since(value: Any, now: datetime) -> int | None:
    moment = _parse(value)
    if moment is None:
        return None
    return max(0, int((now - moment).total_seconds()))


def _pid_alive(pid: Any) -> bool | None:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    try:
        os.kill(value, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None


def _action_for_upload(view: dict[str, Any]) -> tuple[str, str, str]:
    upload = view.get("upload") or {}
    if upload.get("status") == "FAILED":
        retry = upload.get("next_attempt_at")
        action = "Retry upload only: agent-handoff dispatch run --once --force"
        if retry:
            action += f" (automatic retry not before {retry})"
        return RETRYABLE, "retry_upload", action + ". The claimed OpenCode session is never re-executed."
    return PENDING, "pending_upload", "Upload is pending: agent-handoff dispatch run --once (upload only)."


def record_status_view(
    record: dict[str, Any],
    dispatch: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
    notifications: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Extend the stable machine summary with local, actionable health fields."""

    view = safe_record_summary(record)
    now = now or utcnow()
    status = str(record.get("status") or "UNKNOWN")
    execution = record.get("execution") or {}
    upload = record.get("upload") or {}
    publication = record.get("publication") or {}
    capture = record.get("capture") or {}
    timeout_seconds = float(((dispatch or {}).get("execution") or {}).get("timeout_seconds", 1200))
    updated_age = _seconds_since(record.get("updated_at"), now)
    started_age = _seconds_since(execution.get("started_at"), now)
    runner_alive = _pid_alive(execution.get("runner_pid"))

    health = IGNORED
    phase = status.lower()
    stalled = False
    next_action = "Review the durable record; no automatic action is pending."
    retry_at = None

    if status in ("CLAIMED", "RUNNING"):
        stall_age = started_age if status == "RUNNING" else updated_age
        if runner_alive is False or (
            runner_alive is None and stall_age is not None and stall_age > timeout_seconds + 120
        ):
            health = STALLED
            phase = "stalled_execution"
            stalled = True
            next_action = (
                "The recorded dispatcher process is not alive. Run agent-handoff dispatch run --once "
                "to reconcile from persisted session output without re-executing OpenCode."
            )
        elif stall_age is not None and stall_age > timeout_seconds + 120:
            health = STALLED
            phase = "stalled_execution"
            stalled = True
            next_action = (
                f"Execution has exceeded the configured {timeout_seconds:g}s timeout. Inspect the host "
                "and session evidence; do not start a duplicate session or blindly rerun."
            )
        else:
            health = ACTIVE
            phase = "executing" if status == "RUNNING" else "claimed"
            elapsed = started_age if status == "RUNNING" else updated_age
            detail = f" for {elapsed}s" if elapsed is not None else ""
            if status == "RUNNING":
                next_action = (
                    f"OpenCode is executing{detail} under a bounded {timeout_seconds:g}s timeout; "
                    "wait and do not submit a duplicate issue."
                )
            else:
                next_action = (
                    f"The request is claimed{detail} by a live dispatcher process; execution startup is in "
                    "progress. Wait and do not submit a duplicate issue."
                )
    elif status == "EXECUTION_COMPLETED":
        health = PENDING
        phase = "pending_capture"
        next_action = "Capture the persisted session output: agent-handoff dispatch run --once (no re-execution)."
    elif status in ("REPORT_CAPTURED", "UPLOAD_PENDING"):
        health, phase, next_action = _action_for_upload(view)
    elif status == "UPLOAD_VERIFIED":
        if publication.get("status") == "EXHAUSTED":
            health = ATTENTION
            phase = "publication_exhausted"
            next_action = (
                "Publication retries reached dispatch.delivery.max_attempts. Use "
                "agent-handoff dispatch deliver to publish the verified report manually; "
                "the upload is reused and the agent is not re-executed."
            )
        elif publication.get("status") == "FAILED":
            health = RETRYABLE
            phase = "retry_publication"
            retry_at = publication.get("next_attempt_at")
            next_action = "Retry publication only: agent-handoff dispatch run --once --force or dispatch deliver"
            if retry_at:
                next_action += f" (automatic retry not before {retry_at})"
            next_action += ". The verified upload is reused."
        else:
            health = PENDING
            phase = "pending_publication"
            next_action = "Publish the verified report URL: agent-handoff dispatch run --once (publication only)."
    elif status == "ISSUE_UPDATED":
        health = DELIVERED
        phase = "delivered"
        next_action = "None. Execution, verified upload, and idempotent issue publication are complete."
    elif status == "NEEDS_ATTENTION":
        health = ATTENTION
        phase = "needs_attention"
        next_action = (
            "Inspect the host and session evidence. Do not rerun automatically; submit a new issue "
            "if the work must be repeated."
        )
    elif status == "REJECTED":
        health = ATTENTION
        phase = "rejected"
        next_action = "Submit a new, corrected issue. Editing a rejected issue does not execute it."
    elif status == "NEW":
        health = ACTIVE
        phase = "queued"
        next_action = "The next dispatcher poll may claim this request (it has not executed yet)."
    else:
        next_action = "Inspect this record; its state is not recognized by this version."

    for candidate in (publication.get("next_attempt_at"), upload.get("next_attempt_at")):
        if candidate:
            retry_at = candidate
            break

    elapsed_seconds = None
    if execution.get("started_at") and execution.get("finished_at"):
        started = _parse(execution.get("started_at"))
        finished = _parse(execution.get("finished_at"))
        if started and finished:
            elapsed_seconds = max(0, int((finished - started).total_seconds()))
    elif execution.get("started_at"):
        elapsed_seconds = _seconds_since(execution.get("started_at"), now)

    view.update(
        {
            "health": health,
            "phase": phase,
            "stalled": stalled,
            "age_seconds": updated_age,
            "elapsed_seconds": elapsed_seconds,
            "next_action": next_action,
            "next_attempt_at": retry_at,
            "captured": capture.get("status") == "CAPTURED",
            "last_error": sanitize_error(
                upload.get("last_error") or publication.get("last_error") or record.get("attention_reason") or "",
                400,
            )
            or None,
            "notifications": list(notifications or []),
        }
    )
    return view


def status_group(view: dict[str, Any]) -> str:
    health = str(view.get("health") or IGNORED)
    if health in {ACTIVE, STALLED, ATTENTION, RETRYABLE, PENDING, DELIVERED}:
        return health
    return IGNORED


def format_status_view(view: dict[str, Any]) -> list[str]:
    issue = view.get("issue_number")
    status = view.get("status")
    phase = view.get("phase")
    project = view.get("project") or "-"
    model = view.get("model") or "not captured"
    age = view.get("age_seconds")
    age_text = f" · updated {age}s ago" if age is not None else ""
    lines = [
        f"  #{issue}  {status}  ({phase})  project={project}  model={model}{age_text}",
    ]
    if view.get("issue_url"):
        lines.append(f"      issue: {view['issue_url']}")
    if (view.get("upload") or {}).get("verified") and (view.get("upload") or {}).get("report_url"):
        lines.append(f"      report: {view['upload']['report_url']}")
        if (view.get("upload") or {}).get("receipt_url"):
            lines.append(f"      receipt: {view['upload']['receipt_url']}")
    if (view.get("publication") or {}).get("comment_url"):
        lines.append(f"      delivery comment: {view['publication']['comment_url']}")
    if view.get("next_attempt_at"):
        lines.append(f"      next retry: {view['next_attempt_at']}")
    if view.get("last_error"):
        lines.append(f"      last error: {view['last_error']}")
    for notification in view.get("notifications") or []:
        detail = (
            f"      notification: {notification.get('kind')} {notification.get('state')}"
            f" · attempts={notification.get('attempts', 0)}"
        )
        if notification.get("telegram_message_id") is not None:
            detail += f" · telegram_message_id={notification.get('telegram_message_id')}"
        if notification.get("next_attempt_at"):
            detail += f" · next retry {notification.get('next_attempt_at')}"
        lines.append(detail)
        if notification.get("last_error"):
            lines.append(f"        notification error: {notification.get('last_error')}")
        state = notification.get("state")
        if state == "PENDING":
            lines.append("        notification note: the next dispatcher poll will send it; report delivery is unaffected")
        elif state == "RETRY_WAIT":
            lines.append(
                "        notification note: automatic retry is scheduled; use "
                "agent-handoff dispatch notify --retry for an immediate retry. Report delivery is unaffected"
            )
        elif state == "NEEDS_ATTENTION":
            lines.append(
                "        notification note: fix the Telegram cause, then use "
                "agent-handoff dispatch notify --retry. Report delivery is unaffected"
            )
    lines.append(f"      next action: {view.get('next_action')}")
    return lines
