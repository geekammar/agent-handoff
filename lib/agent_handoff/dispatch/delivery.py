from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Callable

from ..store import Store
from ..upload import Uploader
from ..util import atomic_write_json, sanitize_error, utcnow, utcnow_iso

RECEIPT_NAME = "UPLOAD-RECEIPT.json"


def _notify(callback: Callable[[str, str], None] | None, stage: str, message: str) -> None:
    if callback is None:
        return
    try:
        callback(stage, message)
    except Exception:
        pass


def delivery_marker(run_id: str) -> str:
    return f"<!-- agent-handoff-delivery:{run_id} -->"


def rejection_marker(issue_number: int, body_sha256: str) -> str:
    return f"<!-- agent-handoff-rejected:{issue_number}:{body_sha256[:16]} -->"


def _complete_upload(complete: dict[str, Any]) -> dict[str, Any]:
    upload = complete.get("upload")
    return upload if isinstance(upload, dict) else {}


def _report_remote_path(complete: dict[str, Any]) -> str | None:
    report = complete.get("report") if isinstance(complete.get("report"), dict) else {}
    upload = _complete_upload(complete)
    return report.get("remote_path") or upload.get("report_remote_path")


def build_receipt(
    *,
    project: dict[str, Any],
    task_id: str,
    run_id: str,
    complete: dict[str, Any],
    report_remote_path: str,
    report_file: dict[str, Any],
    remote_dir: str | None = None,
) -> dict[str, Any]:
    upload = _complete_upload(complete)
    files = []
    for item in upload.get("files") or []:
        if not isinstance(item, dict):
            continue
        files.append(
            {
                "remote": item.get("remote"),
                "size": item.get("size"),
                "verification": item.get("verification"),
            }
        )
    remote_dir = remote_dir or upload.get("remote_dir") or f"TASKS/{run_id}"
    return {
        "schema": 1,
        "kind": "agent-handoff-upload-receipt",
        "project": project.get("name"),
        "task_id": task_id,
        "run_id": run_id,
        "upload": {
            "verified": True,
            "verified_at": upload.get("verified_at"),
            "remote_dir": remote_dir,
            "files": files,
            "redactions": upload.get("redactions", 0),
        },
        "report": {
            "remote_path": report_remote_path,
            "file_id": report_file.get("id"),
            "url": report_file.get("url"),
            "size": report_file.get("size"),
        },
        "complete": {"remote_path": f"{remote_dir}/COMPLETE.json"},
        "verification": "rclone upload copy verified by MD5 or read-back; direct Drive file id resolved by rclone lsjson",
        "created_at": utcnow_iso(),
    }


def reconcile_verified_delivery(
    config: dict[str, Any],
    store: Store,
    uploader: Uploader,
    project: dict[str, Any],
    task_id: str,
    run_id: str,
    *,
    force: bool = False,
    on_progress: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """Ensure a run is upload-verified and produce a truthful receipt with a direct URL.

    This never invokes an agent. It only operates on an existing Agent Handoff run.
    """

    try:
        _, task_path, meta, run_path, complete = store.run_record(project, task_id, run_id)
    except Exception as exc:
        return {"ok": False, "error": sanitize_error(str(exc))}

    upload = _complete_upload(complete)
    if not upload.get("verified"):
        _notify(on_progress, "uploading", "uploading report files and verifying each transfer")
        with store.lock():
            store.queue_run(project, task_path, meta, run_path, run_id)
        result = uploader.process_queue(
            force=force,
            only_task=task_id,
            only_run=run_id,
            time_budget=120.0,
            reconcile=False,
            on_progress=on_progress,
        )
        if result.get("busy"):
            return {"ok": False, "error": "another upload worker is already running"}
        try:
            _, task_path, meta, run_path, complete = store.run_record(project, task_id, run_id)
        except Exception as exc:
            return {"ok": False, "error": sanitize_error(str(exc))}
        upload = _complete_upload(complete)
        if not upload.get("verified"):
            errors = result.get("errors") or []
            return {
                "ok": False,
                "error": sanitize_error("; ".join(str(item) for item in errors) or upload.get("last_error") or "upload is not verified"),
                "upload": upload,
            }

    report_remote_path = _report_remote_path(complete)
    if not report_remote_path:
        return {"ok": False, "error": "run has no remote report path"}

    receipt_path = run_path / RECEIPT_NAME
    existing = upload.get("report_url") and upload.get("receipt_url") and upload.get("reconciled_at") and receipt_path.is_file()
    if existing:
        return {
            "ok": True,
            "reused": True,
            "report_url": upload.get("report_url"),
            "receipt_url": upload.get("receipt_url"),
            "report_remote_path": report_remote_path,
            "receipt_remote_path": upload.get("receipt_remote_path"),
            "report_file_id": upload.get("report_file_id"),
            "receipt_file_id": upload.get("receipt_file_id"),
        }

    _notify(on_progress, "verifying", "resolving the verified report Drive file id")
    report_file = uploader.remote_file_id(report_remote_path, project, on_progress=on_progress)
    if not report_file.get("ok"):
        return {"ok": False, "error": report_file.get("error") or "cannot resolve the report Drive file id"}

    receipt = build_receipt(
        project=project,
        task_id=task_id,
        run_id=run_id,
        complete=complete,
        report_remote_path=report_remote_path,
        report_file=report_file,
        remote_dir=f"TASKS/{meta['task_folder']}/RUNS/{run_id}",
    )
    atomic_write_json(receipt_path, receipt)
    remote_dir = f"TASKS/{meta['task_folder']}/RUNS/{run_id}"
    receipt_remote_path = f"{remote_dir}/{RECEIPT_NAME}"
    _notify(on_progress, "receipt", "uploading and verifying UPLOAD-RECEIPT.json")
    receipt_upload = uploader.upload_file(receipt_path, receipt_remote_path, project, on_progress=on_progress)
    if not receipt_upload.get("ok"):
        return {"ok": False, "error": receipt_upload.get("error") or "upload receipt failed"}
    receipt_file = uploader.remote_file_id(receipt_remote_path, project, on_progress=on_progress)
    if not receipt_file.get("ok"):
        return {"ok": False, "error": receipt_file.get("error") or "cannot resolve the receipt Drive file id"}

    updates = {
        "report_remote_path": report_remote_path,
        "report_file_id": report_file.get("id"),
        "report_url": report_file.get("url"),
        "receipt_remote_path": receipt_remote_path,
        "receipt_file_id": receipt_file.get("id"),
        "receipt_url": receipt_file.get("url"),
        "receipt_verified": True,
        "receipt_verified_at": utcnow_iso(),
    }
    with store.lock():
        store.update_complete(run_path, {"upload": updates, "report": {"remote_url": report_file.get("url")}})
    complete_path = run_path / "COMPLETE.json"
    _notify(on_progress, "reconciling", "re-uploading COMPLETE.json with verified upload metadata")
    complete_upload = uploader.upload_file(complete_path, f"{remote_dir}/COMPLETE.json", project, on_progress=on_progress)
    if not complete_upload.get("ok"):
        return {"ok": False, "error": complete_upload.get("error") or "remote COMPLETE.json reconciliation failed"}
    with store.lock():
        store.update_complete(
            run_path,
            {
                "upload": {
                    "reconciled_at": utcnow_iso(),
                    "metadata_reconciliation": "remote COMPLETE.json re-uploaded after verified upload; receipt is authoritative",
                }
            },
        )
    return {
        "ok": True,
        "reused": False,
        "report_url": report_file.get("url"),
        "receipt_url": receipt_file.get("url"),
        "report_remote_path": report_remote_path,
        "receipt_remote_path": receipt_remote_path,
        "report_file_id": report_file.get("id"),
        "receipt_file_id": receipt_file.get("id"),
        "verified_at": upload.get("verified_at"),
    }


def build_issue_comment(record: dict[str, Any]) -> str:
    capture = record.get("capture") or {}
    upload = record.get("upload") or {}
    execution = record.get("execution") or {}
    session = record.get("session") or {}
    report_url = upload.get("report_url")
    receipt_url = upload.get("receipt_url")
    lines = [
        delivery_marker(str(capture.get("run_id") or record.get("dispatch_id"))),
        "**Agent Handoff — verified report delivered.**",
        "",
        f"- Project: `{(record.get('project') or {}).get('name')}`",
        f"- Agent: `{record.get('agent')}`",
        f"- Model: `{record.get('model') or 'not captured'}`",
        f"- Session: `{session.get('id') or 'not captured'}`",
        f"- Task: `{capture.get('task_id')}`",
        f"- Run: `{capture.get('run_id')}`",
        f"- Outcome: `{execution.get('outcome') or 'not recorded'}`",
        f"- Upload: verified at `{upload.get('verified_at')}`",
        f"- Report: {report_url}",
    ]
    if receipt_url:
        lines.append(f"- Upload receipt: {receipt_url}")
    lines.append("")
    lines.append(
        "The link above was produced only after the report upload passed MD5/read-back verification "
        "and a direct Google Drive file id was resolved. This comment is idempotent; do not rerun the agent."
    )
    return "\n".join(lines)


def publication_backoff(attempts: int, retry_seconds: int) -> str:
    delay = min(3600, max(5, retry_seconds) * (2 ** min(max(attempts - 1, 0), 6)))
    return (utcnow() + timedelta(seconds=delay)).isoformat(timespec="seconds").replace("+00:00", "Z")


def upload_backoff(attempts: int, retry_seconds: int = 60) -> str:
    return publication_backoff(attempts, retry_seconds)


def publication_due(record: dict[str, Any], force: bool = False) -> bool:
    if force:
        return True
    next_at = (record.get("publication") or {}).get("next_attempt_at")
    if not next_at:
        return True
    try:
        return datetime.fromisoformat(str(next_at).replace("Z", "+00:00")) <= utcnow()
    except ValueError:
        return True


def diagnose_historical_run(config: dict[str, Any], project: dict[str, Any], task_id: str, run_id: str) -> dict[str, Any]:
    """Read-only inspection of local and remote metadata for one run."""

    store = Store(config)
    uploader = Uploader(config, Store(config))
    try:
        _, task_path, meta, run_path, complete = store.run_record(project, task_id, run_id)
    except Exception as exc:
        return {"ok": False, "error": sanitize_error(str(exc))}
    upload = _complete_upload(complete)
    report_remote_path = _report_remote_path(complete)
    remote_report = uploader.remote_file_id(report_remote_path, project) if report_remote_path else {"ok": False}
    remote_complete = None
    remote_complete_verified = False
    if report_remote_path:
        remote_rel = report_remote_path.rsplit("/", 1)[0] + "/COMPLETE.json"
        remote_complete = uploader.read_remote_text(remote_rel, project)
        if remote_complete.get("ok"):
            try:
                remote_complete_verified = bool((json.loads(remote_complete.get("text") or "{}").get("upload") or {}).get("verified"))
            except ValueError:
                remote_complete_verified = False
    return {
        "ok": bool(upload.get("verified") and (remote_report.get("ok") or remote_complete_verified)),
        "remote_complete_verified": remote_complete_verified,
        "local_upload_verified": bool(upload.get("verified")),
        "local_verified_at": upload.get("verified_at"),
        "local_report_url": upload.get("report_url"),
        "remote_report": remote_report,
        "remote_complete": remote_complete,
        "run_dir": str(run_path),
        "report_remote_path": report_remote_path,
    }
