from __future__ import annotations

from typing import Any, Mapping

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

# Minimum durable stage at which a milestone can be truthfully confirmed.
# A false milestone before its stage is "pending"; a false milestone after the
# blocked stage is "not_run"; a false milestone at or after its stage is
# "false".
MILESTONE_STAGE = {
    "AGENT_COMPLETED": "AGENT_COMPLETED",
    "TESTS_PASSED": "TESTING",
    "DIFF_VALIDATED": "VALIDATING_DIFF",
    "COMMIT_CREATED": "COMMIT_PREPARED",
    "BRANCH_PUBLISHED": "BRANCH_PUBLISHED",
    "PR_CREATED": "PR_CREATED",
    "PR_PUBLISHED": "PR_CREATED",
    "REPORT_VERIFIED": "UPLOAD_VERIFIED",
    "ISSUE_DELIVERED": "ISSUE_UPDATED",
    "REPORT_DELIVERED": "ISSUE_UPDATED",
    "TELEGRAM_SENT": "NOTIFICATION_SENT",
}

# These milestones belong to report delivery and can still be confirmed after
# a coding task is blocked (for example, a failed test still produces a
# verified report and an Issue comment).
DELIVERY_MILESTONES = {"REPORT_VERIFIED", "ISSUE_DELIVERED", "REPORT_DELIVERED", "TELEGRAM_SENT"}


def _confirmed(milestones: Mapping[str, Any], key: str) -> bool:
    if milestones.get(key):
        return True
    if key in ("ISSUE_DELIVERED", "REPORT_DELIVERED"):
        return bool(milestones.get("ISSUE_DELIVERED") or milestones.get("REPORT_DELIVERED"))
    if key in ("PR_CREATED", "PR_PUBLISHED"):
        return bool(milestones.get("PR_CREATED") or milestones.get("PR_PUBLISHED"))
    return False


def milestone_state(coding: Mapping[str, Any] | None, key: str) -> str:
    """Return true, false, pending, or not_run for one coding milestone.

    ``pending`` means the durable stage has not reached the point where the
    milestone can be confirmed. ``not_run`` means the task was blocked before
    that point. Historical records that only have the legacy REPORT_DELIVERED
    flag are treated as Issue-delivered evidence without rewriting the record.
    """

    value = coding or {}
    milestones = value.get("milestones") or {}
    if _confirmed(milestones, key):
        return "true"
    current = str(value.get("stage") or "REQUEST_ACCEPTED")
    required = MILESTONE_STAGE.get(key)
    current_order = PER_STAGE_ORDER.get(current, -1)
    if required and current_order < PER_STAGE_ORDER.get(required, -1):
        if value.get("blocked") and key not in DELIVERY_MILESTONES:
            blocked_stage = str(value.get("blocked_stage") or current)
            if PER_STAGE_ORDER.get(blocked_stage, -1) < PER_STAGE_ORDER.get(required, -1):
                return "not_run"
        return "pending"
    return "false"
