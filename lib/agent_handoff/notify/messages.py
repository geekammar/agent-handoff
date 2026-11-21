from __future__ import annotations

import html
import re
from typing import Any

DELIVERY_COMPLETED = "DELIVERY_COMPLETED"
NEEDS_ATTENTION = "NEEDS_ATTENTION"
DELIVERY_DELAYED = "DELIVERY_DELAYED"
KINDS = (DELIVERY_COMPLETED, NEEDS_ATTENTION, DELIVERY_DELAYED)

MAX_MESSAGE_CHARS = 4000
URL_RE = re.compile(r"^https?://[^\s<>]+$")


class NotificationMessageError(ValueError):
    pass


def text_value(value: Any, limit: int = 120) -> str:
    text = re.sub(r"[\r\n\t]+", " ", str(value or "")).strip()
    text = re.sub(r"\s{2,}", " ", text)
    if len(text) > limit:
        text = text[: max(1, limit - 1)].rstrip() + "…"
    return text


def safe_url(value: Any) -> str | None:
    url = str(value or "").strip()
    if url and URL_RE.fullmatch(url):
        return url
    return None


def _escape(value: Any, limit: int = 120) -> str:
    return html.escape(text_value(value, limit=limit), quote=False)


def _link(url: str) -> str:
    escaped = html.escape(url, quote=True)
    return f'<a href="{escaped}">{html.escape(url, quote=False)}</a>'


def _button(label: str, url: str | None) -> list[dict[str, str]]:
    if not url:
        return []
    return [{"text": label, "url": url}]


def build_chatgpt_prompt(*, project: str, task_title: str, report_url: str) -> str:
    return (
        f"تم الانتهاء من مهمة {project} — {task_title}.\n"
        "اقرأ تقرير التنفيذ:\n"
        f"{report_url}\n"
        "وراجع الكود والاختبارات، ثم أخبرني بالنتيجة\n"
        "والإصلاحات والمتبقي والخطوة التالية."
    )


def _reply_markup(rows: list[list[dict[str, str]]]) -> dict[str, Any] | None:
    rows = [row for row in rows if row]
    if not rows:
        return None
    return {"inline_keyboard": rows}


def _completed(payload: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    project = _escape(payload.get("project") or "unknown", 80)
    title = _escape(payload.get("task_title") or "Untitled task", 120)
    report_url = safe_url(payload.get("report_url"))
    issue_url = safe_url(payload.get("issue_url"))
    pr_url = safe_url(payload.get("pr_url"))
    outcome = text_value(payload.get("outcome") or "not recorded", 40)
    coding_outcome = text_value(payload.get("coding_outcome") or "", 60)
    tests_status = text_value(payload.get("tests_status") or "", 30)
    if not report_url:
        raise NotificationMessageError("a verified report URL is required for a completion notification")

    healthy = outcome == "succeeded" and coding_outcome in ("", "draft_pr_published") and tests_status in ("", "passed")
    heading = "✅ تم تسليم تقرير المهمة" if healthy else "📄 تم تسليم تقرير المهمة (يلزم مراجعة النتيجة)"
    lines = [
        heading,
        "",
        f"Project: <b>{project}</b>",
        f"Task: <b>{title}</b>",
        "Status: Verified &amp; Delivered",
        f"Execution outcome: <b>{html.escape(outcome, quote=False)}</b>",
    ]
    if coding_outcome:
        lines.append(f"Coding outcome: <b>{html.escape(coding_outcome, quote=False)}</b>")
    if tests_status:
        lines.append(f"Trusted tests: <b>{html.escape(tests_status, quote=False)}</b>")
    lines.extend(["", "Report:", _link(report_url)])
    if pr_url:
        lines.extend(["", "Draft PR (not merged):", _link(pr_url)])
    if issue_url:
        lines.extend(["", "GitHub Issue:", _link(issue_url)])
    prompt = build_chatgpt_prompt(
        project=text_value(payload.get("project") or "unknown", 80),
        task_title=text_value(payload.get("task_title") or "Untitled task", 120),
        report_url=report_url,
    )
    lines.extend(
        [
            "",
            "PROMPT FOR CHATGPT:",
            f"<pre>{html.escape(prompt, quote=False)}</pre>",
        ]
    )
    buttons = [
        [*_button("View Report", report_url)],
        [*_button("View Draft PR", pr_url)],
        [*_button("View GitHub Issue", issue_url)],
    ]
    return "\n".join(lines), _reply_markup(buttons)


def _attention(payload: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    project = _escape(payload.get("project") or "unknown", 80)
    title = _escape(payload.get("task_title") or "Untitled task", 120)
    status = _escape(payload.get("status") or "NEEDS_ATTENTION", 80)
    reason = _escape(payload.get("reason") or "operator review is required", 500)
    report_url = safe_url(payload.get("report_url"))
    issue_url = safe_url(payload.get("issue_url"))
    attempts = payload.get("attempts")
    lines = [
        "⚠️ مهمة تحتاج إلى مراجعة",
        "",
        f"Project: <b>{project}</b>",
        f"Task: <b>{title}</b>",
        f"Status: <b>{status}</b>",
        f"Reason: {reason}",
    ]
    if attempts is not None:
        lines.append(f"Attempts: <b>{int(attempts)}</b>")
    if report_url:
        lines.extend(["", "Verified report:", _link(report_url)])
    else:
        lines.extend(["", "Verified report: not available yet"])
    if issue_url:
        lines.extend(["", "GitHub Issue:", _link(issue_url)])
    next_action = _escape(payload.get("next_action") or "", 500)
    if next_action:
        lines.extend(["", f"Next action: {next_action}"])
    lines.extend(["", "هذا تنبيه حالة، وليس تأكيد نجاح للمهمة."])
    buttons = [[*_button("View GitHub Issue", issue_url)]]
    return "\n".join(lines), _reply_markup(buttons)


def _delayed(payload: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    project = _escape(payload.get("project") or "unknown", 80)
    title = _escape(payload.get("task_title") or "Untitled task", 120)
    stage = text_value(payload.get("stage") or "delivery", 30)
    reason = _escape(payload.get("reason") or "delivery is retrying", 500)
    report_url = safe_url(payload.get("report_url"))
    issue_url = safe_url(payload.get("issue_url"))
    attempts = payload.get("attempts")
    lines = [
        "⏳ تسليم التقرير متأخر",
        "",
        f"Project: <b>{project}</b>",
        f"Task: <b>{title}</b>",
        f"Delivery: <b>{html.escape(stage, quote=False)} pending</b>",
    ]
    if attempts is not None:
        lines.append(f"Attempts: <b>{int(attempts)}</b>")
    lines.append(f"Reason: {reason}")
    if stage == "upload":
        lines.extend(
            [
                "",
                "تم حفظ التقرير محليًا، لكن التسليم الموثّق إلى Drive غير متاح بعد.",
            ]
        )
        closing = (
            "سيُعاد المحاولة تلقائيًا. لن تُنفَّذ المهمة مرة أخرى، "
            "والتقرير المحفوظ محليًا لم يُفقد."
        )
    elif stage == "publication" and report_url:
        lines.extend(
            [
                "",
                "التقرير موثّق ومرفوع إلى Drive، لكن النشر في GitHub لم يكتمل بعد.",
                "",
                "Verified report:",
                _link(report_url),
            ]
        )
        closing = "سيُعاد المحاولة تلقائيًا. لن تُنفَّذ المهمة مرة أخرى، والتقرير الموثّق لم يُفقد."
    else:
        lines.extend(
            [
                "",
                "لا يمكن تأكيد رابط تقرير موثّق بعد، والتسليم لم يكتمل.",
            ]
        )
        closing = "سيُعاد المحاولة تلقائيًا. لن تُنفَّذ المهمة مرة أخرى."
        if report_url:
            lines.extend(["", "Verified report:", _link(report_url)])
    if issue_url:
        lines.extend(["", "GitHub Issue:", _link(issue_url)])
    lines.extend(["", closing])
    buttons = [
        [*_button("View Report", report_url)],
        [*_button("View GitHub Issue", issue_url)],
    ]
    return "\n".join(lines), _reply_markup(buttons)


def build_telegram_message(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    if kind == DELIVERY_COMPLETED:
        text, markup = _completed(payload)
    elif kind == NEEDS_ATTENTION:
        text, markup = _attention(payload)
    elif kind == DELIVERY_DELAYED:
        text, markup = _delayed(payload)
    else:
        raise NotificationMessageError(f"unsupported notification kind: {kind}")
    if len(text) > MAX_MESSAGE_CHARS:
        raise NotificationMessageError("notification text exceeds the Telegram message limit")
    data: dict[str, Any] = {"text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if markup:
        data["reply_markup"] = markup
    return data
