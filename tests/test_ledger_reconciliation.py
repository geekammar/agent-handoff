from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "lib") not in sys.path:
    sys.path.insert(0, str(ROOT / "lib"))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_dispatch import GH_REPO, DispatchHarness, request_body  # noqa: E402

from agent_handoff.dispatch.ledger import Ledger, new_coding_state, new_dispatch_record  # noqa: E402
from agent_handoff.notify.messages import DELIVERY_COMPLETED  # noqa: E402
from agent_handoff.notify.service import NotificationService, notification_id  # noqa: E402
from agent_handoff.notify.store import NotificationStore  # noqa: E402

TOKEN = "123456789:AAFakeTokenForLedgerReconciliation_1234567890"
CHAT_ID = "-1001234567890"
REPORT_URL = "https://drive.google.com/file/d/report-legacy/view"
RECEIPT_URL = "https://drive.google.com/file/d/receipt-legacy/view"
PUBLISHED_AT = "2026-01-01T00:00:03Z"
CAPTURED_AT = "2026-01-01T00:00:01Z"
VERIFIED_AT = "2026-01-01T00:00:02Z"
SENT_AT = "2026-01-01T00:00:05Z"
COMMENT_URL = "https://github.test/issues/1#issuecomment-1"


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.result = {"ok": True, "message_id": 501}

    def send_message(self, chat_id: str, **message) -> dict:
        self.calls.append({"chat_id": chat_id, **message})
        return dict(self.result)


class LedgerReconciliationFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.h = DispatchHarness(self.root)
        self.h.config["notifications"] = {
            "telegram": {
                "enabled": True,
                "chat_id": CHAT_ID,
                "token_env": "AGENT_HANDOFF_TELEGRAM_TOKEN",
                "timeout_seconds": 2,
                "retry_seconds": 5,
                "max_attempts": 3,
                "delayed_after_attempts": 3,
            }
        }
        self.h.write_config()
        self.env_patch = mock.patch.dict(
            os.environ,
            {"AGENT_HANDOFF_TELEGRAM_TOKEN": TOKEN},
            clear=False,
        )
        self.env_patch.start()
        self.ledger = Ledger(self.h.config)
        self.store = NotificationStore(self.h.config)
        self.service = NotificationService(self.h.config)
        self.transport = FakeTransport()
        self.service.transport = self.transport

    def tearDown(self) -> None:
        self.env_patch.stop()
        self.tmp.cleanup()

    def _project(self, *, coding_enabled: bool = False) -> dict:
        project = {
            "name": "Alpha",
            "core_name": "Alpha",
            "core_key": "alpha",
            "workspace": str(self.h.project_path),
            "drive_folder_id": "alpha_folder_0001",
        }
        if coding_enabled:
            project["coding"] = {"enabled": True}
        return project

    def _base_record(
        self,
        number: int,
        *,
        task_type: str | None = "read_only",
        coding: str = "auto",
        status: str = "ISSUE_UPDATED",
    ) -> dict:
        project = self._project(coding_enabled=task_type == "coding")
        record = new_dispatch_record(
            repository=GH_REPO,
            issue={
                "number": number,
                "html_url": f"https://github.test/issues/{number}",
                "title": f"reconciliation fixture {number}",
            },
            project=project,
            agent="opencode",
            model="provider/model-fast",
            request={"instructions": "fixture"},
            task_type=task_type or "read_only",
            coding=new_coding_state(project) if task_type == "coding" else None,
        )
        record["status"] = status
        record["capture"].update(
            {
                "status": "CAPTURED",
                "task_id": f"t-{number}",
                "run_id": f"r-{number}",
                "captured_at": CAPTURED_AT,
            }
        )
        record["execution"].update(
            {
                "status": "COMPLETED",
                "started_at": "2026-01-01T00:00:00Z",
                "finished_at": "2026-01-01T00:00:01Z",
                "exit_code": 0,
                "outcome": "succeeded",
            }
        )
        record["upload"] = {
            "status": "VERIFIED",
            "verified": True,
            "verified_at": VERIFIED_AT,
            "report_remote_path": f"reports/{number}/REPORT.md",
            "report_url": REPORT_URL,
            "receipt_remote_path": f"reports/{number}/UPLOAD-RECEIPT.json",
            "receipt_url": RECEIPT_URL,
            "attempts": 1,
            "last_error": None,
        }
        record["publication"] = {
            "status": "PUBLISHED",
            "attempts": 1,
            "published_at": PUBLISHED_AT,
            "comment_url": COMMENT_URL,
            "last_error": None,
            "next_attempt_at": None,
        }
        if task_type == "coding" and isinstance(record.get("coding"), dict):
            record["coding"]["stage"] = "ISSUE_UPDATED"
            record["coding"]["milestones"].update(
                {
                    "AGENT_COMPLETED": True,
                    "TESTS_PASSED": True,
                    "REPORT_VERIFIED": True,
                    "ISSUE_DELIVERED": True,
                    "REPORT_DELIVERED": True,
                }
            )
            record["coding"]["milestones"].pop("TELEGRAM_SENT", None)
            record["coding"]["telegram_sent_at"] = None
            record["coding"]["telegram_message_id"] = None
        if task_type is None:
            record.pop("task_type", None)
        elif task_type != "coding":
            record["task_type"] = task_type
        if coding == "missing":
            record.pop("coding", None)
        elif coding != "auto":
            record["coding"] = coding
        return record

    def _create(self, record: dict) -> dict:
        stored, _ = self.ledger.create(record)
        return stored

    def _sent_notification(
        self,
        record: dict,
        *,
        message_id: int = 501,
        sent_at: str = SENT_AT,
    ) -> dict:
        number = int(record["issue_number"])
        project = record.get("project") or {}
        capture = record.get("capture") or {}
        item = {
            "schema": 1,
            "notification_id": notification_id(
                project.get("core_key") or "alpha",
                str(capture.get("task_id") or f"t-{number}"),
                str(capture.get("run_id") or f"r-{number}"),
                DELIVERY_COMPLETED,
            ),
            "kind": DELIVERY_COMPLETED,
            "identity": {
                "project": project.get("core_key") or "alpha",
                "task_id": capture.get("task_id") or f"t-{number}",
                "run_id": capture.get("run_id") or f"r-{number}",
            },
            "repository": GH_REPO,
            "issue_number": number,
            "issue_url": record.get("issue_url"),
            "dispatch_id": record.get("dispatch_id"),
            "dispatch_status": record.get("status"),
            "payload": {"report_url": REPORT_URL},
            "state": "SENT",
            "attempts": 1,
            "created_at": "2026-01-01T00:00:04Z",
            "updated_at": sent_at,
            "sent_at": sent_at,
            "telegram_message_id": message_id,
            "next_attempt_at": None,
            "last_error": None,
            "history": [],
        }
        stored, _ = self.store.create(item)
        return stored

    def _path(self, number: int) -> Path:
        return self.ledger.path(GH_REPO, number)

    def _hash(self, number: int) -> str:
        return hashlib.sha256(self._path(number).read_bytes()).hexdigest()

    def _get(self, number: int) -> dict:
        return self.ledger.get(GH_REPO, number) or {}

    def _notes(self, record: dict, needle: str) -> list[dict]:
        return [
            entry
            for entry in record.get("history") or []
            if isinstance(entry, dict) and needle.lower() in str(entry.get("note") or "").lower()
        ]

    def _force_updated_at(self, number: int, value: str) -> None:
        path = self._path(number)
        record = json.loads(path.read_text(encoding="utf-8"))
        record["updated_at"] = value
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def _assert_preserved(self, before: dict, after: dict) -> None:
        self.assertEqual(after["upload"]["report_url"], before["upload"]["report_url"])
        self.assertEqual(after["upload"]["receipt_url"], before["upload"]["receipt_url"])
        self.assertEqual(after["upload"]["verified_at"], before["upload"]["verified_at"])
        self.assertEqual(after["publication"]["published_at"], before["publication"]["published_at"])
        self.assertEqual(after["publication"]["comment_url"], before["publication"]["comment_url"])
        self.assertEqual(after["execution"], before["execution"])
        self.assertEqual(after["capture"], before["capture"])
        self.assertEqual(after["request"], before["request"])
        self.assertEqual(after["created_at"], before["created_at"])

    def _assert_legacy_unchanged(self, number: int, before_hash: str, before: dict) -> None:
        after = self._get(number)
        self.assertEqual(self._hash(number), before_hash)
        self.assertEqual(after, before)
        self.assertNotIn("coding", after)
        self.assertNotIn("task_type", after)

    def _assert_no_sends(self) -> None:
        self.assertEqual(self.transport.calls, [])


class LegacyReadOnlyReconciliationTests(LedgerReconciliationFixture):
    def test_t01_legacy_sent_notification_causes_zero_ledger_changes(self) -> None:
        record = self._create(self._base_record(1, task_type=None, coding="missing"))
        self._sent_notification(record)
        before_hash = self._hash(1)
        before = self._get(1)
        self.service.reconcile([before])
        self._assert_legacy_unchanged(1, before_hash, before)
        self._assert_no_sends()

    def test_t02_ten_polls_keep_identical_legacy_ledger_hash(self) -> None:
        record = self._create(self._base_record(2, task_type=None, coding="missing"))
        self._sent_notification(record, message_id=502)
        before_hash = self._hash(2)
        before = self._get(2)
        for _ in range(10):
            self.service.reconcile([self._get(2)])
        self._assert_legacy_unchanged(2, before_hash, before)
        self._assert_no_sends()

    def test_t03_legacy_without_task_type_has_zero_mutations(self) -> None:
        record = self._create(self._base_record(3, task_type=None))
        self._sent_notification(record, message_id=503)
        before_hash = self._hash(3)
        before = self._get(3)
        self.assertNotIn("task_type", before)
        self.assertIsNone(before.get("coding"))
        for _ in range(3):
            self.service.reconcile([self._get(3)])
        self.assertEqual(self._hash(3), before_hash)
        self.assertEqual(self._get(3), before)
        self._assert_no_sends()

    def test_t04_legacy_with_null_coding_has_zero_mutations(self) -> None:
        record = self._create(self._base_record(4, task_type="read_only", coding=None))
        self._sent_notification(record, message_id=504)
        before_hash = self._hash(4)
        before = self._get(4)
        self.assertEqual(before["task_type"], "read_only")
        self.assertIsNone(before.get("coding"))
        for _ in range(3):
            self.service.reconcile([self._get(4)])
        self.assertEqual(self._hash(4), before_hash)
        self.assertEqual(self._get(4), before)
        self._assert_no_sends()

    def test_t05_malformed_coding_metadata_fails_safely(self) -> None:
        malformed = (
            ["not", "a", "coding", "dictionary"],
            {"milestones": ["not", "a", "milestone", "dictionary"]},
            {"milestones": "corrupt"},
        )
        for index, coding in enumerate(malformed, start=10):
            with self.subTest(coding=coding):
                record = self._create(
                    self._base_record(index, task_type="coding", coding=coding)
                )
                self._sent_notification(record, message_id=600 + index)
                before_hash = self._hash(index)
                before = self._get(index)
                for _ in range(3):
                    self.service.reconcile([self._get(index)])
                self.assertEqual(self._hash(index), before_hash)
                self.assertEqual(self._get(index), before)
                self._assert_no_sends()


class CodingMilestoneIdempotencyTests(LedgerReconciliationFixture):
    def test_t06_coding_record_missing_telegram_sent_updates_once(self) -> None:
        record = self._create(self._base_record(6, task_type="coding", coding="auto"))
        notification = self._sent_notification(record, message_id=606, sent_at="2026-01-01T00:00:06Z")
        before = self._get(6)
        before_hash = self._hash(6)
        self.assertFalse(before["coding"]["milestones"].get("TELEGRAM_SENT"))
        self.service.reconcile([before])
        after = self._get(6)
        self.assertTrue(after["coding"]["milestones"]["TELEGRAM_SENT"])
        self.assertEqual(after["coding"]["telegram_sent_at"], notification["sent_at"])
        self.assertEqual(after["coding"]["telegram_message_id"], 606)
        self.assertEqual(after["coding"]["stage"], "NOTIFICATION_SENT")
        self.assertEqual(len(after["history"]), len(before["history"]) + 1)
        self.assertEqual(after["history"][-1]["note"], "Telegram API send acknowledged")
        self.assertEqual(after["history"][:-1], before["history"])
        self._assert_preserved(before, after)
        self.assertNotEqual(self._hash(6), before_hash)
        self._assert_no_sends()

    def test_t07_ten_reconciliations_do_not_append_history(self) -> None:
        record = self._create(self._base_record(7, task_type="coding", coding="auto"))
        self._sent_notification(record, message_id=607)
        self.service.reconcile([self._get(7)])
        after_first = self._get(7)
        first_hash = self._hash(7)
        first_history_len = len(after_first["history"])
        for _ in range(10):
            self.service.reconcile([self._get(7)])
        after = self._get(7)
        self.assertEqual(self._hash(7), first_hash)
        self.assertEqual(after, after_first)
        self.assertEqual(len(after["history"]), first_history_len)
        self.assertEqual(len(self._notes(after, "Telegram API send acknowledged")), 1)
        self._assert_no_sends()

    def test_t08_existing_milestone_keeps_original_timestamp_and_no_writes(self) -> None:
        record = self._base_record(8, task_type="coding", coding="auto")
        record["coding"]["milestones"]["TELEGRAM_SENT"] = True
        record["coding"]["telegram_sent_at"] = "2025-12-31T23:59:59Z"
        record["coding"]["telegram_message_id"] = 808
        record["coding"]["stage"] = "NOTIFICATION_SENT"
        self._create(record)
        self._sent_notification(record, message_id=999, sent_at="2026-01-01T00:00:06Z")
        before_hash = self._hash(8)
        before = self._get(8)
        for _ in range(10):
            self.service.reconcile([self._get(8)])
        after = self._get(8)
        self.assertEqual(self._hash(8), before_hash)
        self.assertEqual(after, before)
        self.assertEqual(after["coding"]["telegram_sent_at"], "2025-12-31T23:59:59Z")
        self.assertEqual(after["coding"]["telegram_message_id"], 808)
        self._assert_no_sends()

    def test_t09_restart_reconciliation_does_not_duplicate_milestone_or_send(self) -> None:
        record = self._create(self._base_record(9, task_type="coding", coding="auto"))
        notification = self._sent_notification(record, message_id=609)
        first_service = NotificationService(self.h.config)
        first_service.reconcile([self._get(9)])
        after_first = self._get(9)
        first_hash = self._hash(9)
        restarted = NotificationService(self.h.config)
        restarted.reconcile([self._get(9)])
        restarted.reconcile([self._get(9)])
        after = self._get(9)
        self.assertEqual(self._hash(9), first_hash)
        self.assertEqual(after, after_first)
        self.assertEqual(after["coding"]["telegram_message_id"], 609)
        self.assertEqual(len(self._notes(after, "Telegram API send acknowledged")), 1)
        self.assertEqual(self.store.get(notification["notification_id"])["state"], "SENT")
        self._assert_no_sends()

    def test_t10_concurrent_reconciliation_records_at_most_one_transition(self) -> None:
        record = self._create(self._base_record(10, task_type="coding", coding="auto"))
        self._sent_notification(record, message_id=610)
        barrier = threading.Barrier(8)
        errors: list[BaseException] = []

        def reconcile_once() -> None:
            try:
                barrier.wait(timeout=10)
                service = NotificationService(self.h.config)
                service.reconcile([self._get(10)])
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _index: reconcile_once(), range(8)))
        self.assertEqual(errors, [])
        after = self._get(10)
        self.assertTrue(after["coding"]["milestones"]["TELEGRAM_SENT"])
        self.assertEqual(len(self._notes(after, "Telegram API send acknowledged")), 1)
        self.assertEqual(after["coding"]["telegram_message_id"], 610)
        self._assert_no_sends()

    def test_t11_previously_sent_message_is_not_resent(self) -> None:
        record = self._create(self._base_record(11, task_type="coding", coding="auto"))
        queued = self.service.enqueue_completed(self._get(11))
        self.assertIsNotNone(queued)
        self.transport.result = {"ok": True, "message_id": 611}
        first = self.service.drain(force=True)
        self.assertEqual(first["sent"], 1)
        self.assertEqual(len(self.transport.calls), 1)
        current = self._get(11)
        self.assertEqual(current["coding"]["telegram_message_id"], 611)
        notification = self.store.get(str(queued["notification_id"]))
        self.assertEqual(notification["state"], "SENT")
        self.assertEqual(notification["telegram_message_id"], 611)
        for _ in range(5):
            self.service.reconcile([self._get(11)])
        self.service.drain(force=True)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self._get(11)["coding"]["telegram_message_id"], 611)
        self.assertEqual(self.store.get(str(queued["notification_id"]))["telegram_message_id"], 611)

    def test_t12_failed_ledger_write_keeps_notification_sent_and_recovers(self) -> None:
        record = self._create(self._base_record(12, task_type="coding", coding="auto"))
        queued = self.service.enqueue_completed(record)
        self.assertIsNotNone(queued)
        self.transport.result = {"ok": True, "message_id": 612}
        import agent_handoff.dispatch.ledger as ledger_module

        real_write = ledger_module.atomic_write_json
        state = {"calls": 0}

        def fail_once(*args, **kwargs):
            state["calls"] += 1
            if state["calls"] == 1:
                raise OSError("simulated ledger persistence failure")
            return real_write(*args, **kwargs)

        with mock.patch.object(ledger_module, "atomic_write_json", side_effect=fail_once):
            first = self.service.drain(force=True)
        self.assertEqual(first["sent"], 1)
        notification = self.store.get(str(queued["notification_id"]))
        self.assertEqual(notification["state"], "SENT")
        self.assertEqual(notification["telegram_message_id"], 612)
        self.assertFalse(self._get(12)["coding"]["milestones"].get("TELEGRAM_SENT"))
        self.assertEqual(state["calls"], 1)
        self.assertEqual(len(self.transport.calls), 1)
        self.service.drain(force=True)
        self.assertEqual(len(self.transport.calls), 1)
        self.service.reconcile([self._get(12)])
        after = self._get(12)
        self.assertTrue(after["coding"]["milestones"]["TELEGRAM_SENT"])
        self.assertEqual(after["coding"]["telegram_message_id"], 612)
        self.assertEqual(self.store.get(str(queued["notification_id"]))["state"], "SENT")
        self.assertEqual(len(self.transport.calls), 1)

    def test_t15_new_legitimate_delivery_transitions_once(self) -> None:
        record = self._create(self._base_record(15, task_type="coding", coding="auto"))
        before = self._get(15)
        queued = self.service.enqueue_completed(before)
        self.assertIsNotNone(queued)
        self.transport.result = {"ok": True, "message_id": 615}
        result = self.service.drain(force=True)
        self.assertEqual(result["sent"], 1, result)
        self.assertEqual(len(self.transport.calls), 1)
        after = self._get(15)
        self.assertTrue(after["coding"]["milestones"]["TELEGRAM_SENT"])
        self.assertEqual(after["coding"]["telegram_message_id"], 615)
        self.assertEqual(after["coding"]["stage"], "NOTIFICATION_SENT")
        self.assertEqual(len(self._notes(after, "Telegram API send acknowledged")), 1)
        self._assert_preserved(before, after)
        self.service.drain(force=True)
        self.assertEqual(len(self.transport.calls), 1)


class DispatcherReconciliationIntegrationTests(LedgerReconciliationFixture):
    def _dispatch(self) -> dict:
        return self.h.dispatch(
            "--force",
            extra_env={"AGENT_HANDOFF_TELEGRAM_TOKEN": TOKEN},
        )

    def test_t13_legacy_delivered_record_posts_no_duplicate_comment(self) -> None:
        record = self._create(self._base_record(13, task_type=None, coding="missing"))
        self._sent_notification(record, message_id=613)
        self.h.add_issue(number=13, body=request_body())
        self.h.set_comments({"13": [{"html_url": COMMENT_URL, "body": "existing delivery"}]})
        before_hash = self._hash(13)
        before = self._get(13)
        for _ in range(3):
            result = self._dispatch()
            self.assertTrue(result["ok"], result)
        self.assertEqual(self._hash(13), before_hash)
        self.assertEqual(self._get(13), before)
        self.assertEqual(len(self.h.comments(13)), 1)
        self.assertEqual(self.h.opencode_calls(), [])
        self._assert_no_sends()

    def test_t14_coding_delivered_record_posts_no_duplicate_comment(self) -> None:
        record = self._base_record(14, task_type="coding", coding="auto")
        record["coding"]["milestones"]["TELEGRAM_SENT"] = True
        record["coding"]["milestones"].pop("AGENT_COMPLETED", None)
        record["coding"]["milestones"].pop("TESTS_PASSED", None)
        record["coding"]["telegram_sent_at"] = SENT_AT
        record["coding"]["telegram_message_id"] = 614
        record["coding"]["stage"] = "NOTIFICATION_SENT"
        self._create(record)
        self._sent_notification(record, message_id=614)
        self.h.set_comments({"14": [{"html_url": COMMENT_URL, "body": "existing delivery"}]})
        before_hash = self._hash(14)
        before = self._get(14)
        for _ in range(3):
            result = self._dispatch()
            self.assertTrue(result["ok"], result)
        self.assertEqual(self._hash(14), before_hash)
        self.assertEqual(self._get(14), before)
        self.assertEqual(len(self.h.comments(14)), 1)
        self.assertEqual(self.h.opencode_calls(), [])

    def test_t16_read_only_dispatch_workflow_is_unchanged(self) -> None:
        self.h.config["notifications"] = {"telegram": {"enabled": False}}
        self.h.write_config()
        self.h.add_issue(number=16, body=request_body())
        result = self._dispatch()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["uploaded"], 1)
        self.assertEqual(result["published"], 1)
        record = self.ledger.get(GH_REPO, 16) or {}
        self.assertEqual(record["status"], "ISSUE_UPDATED")
        self.assertIsNone(record.get("coding"))
        self.assertEqual(len(self.h.comments(16)), 1)
        self.assertEqual(len(self.h.opencode_calls()), 1)

    def test_t17_coding_ledger_notes_and_status_transitions_still_work(self) -> None:
        record = self._create(self._base_record(17, task_type="coding", coding="auto", status="UPLOAD_VERIFIED"))
        self._force_updated_at(17, "2026-01-01T00:00:00Z")
        before = self._get(17)
        updated = self.ledger.set_status(
            GH_REPO,
            17,
            "NEEDS_ATTENTION",
            note="intentional operator transition",
            updates={"attention_reason": "review required"},
        )
        self.assertEqual(updated["status"], "NEEDS_ATTENTION")
        self.assertEqual(updated["attention_reason"], "review required")
        self.assertEqual(updated["history"][-1]["note"], "intentional operator transition")
        self.assertNotEqual(updated["updated_at"], before["updated_at"])
        self.assertTrue(updated["coding"]["milestones"]["REPORT_VERIFIED"])
        self.assertEqual(updated["history"][:-1], before["history"])


class LegacyUpgradeSimulationTests(LedgerReconciliationFixture):
    def test_t19_isolated_c1_to_c2_reconciliation_preserves_terminal_history(self) -> None:
        delivered = self._base_record(21, task_type=None, coding="missing")
        explicit = self._base_record(22, task_type="read_only", coding=None)
        rejected_project = self._project()
        rejected = new_dispatch_record(
            repository=GH_REPO,
            issue={"number": 23, "html_url": "https://github.test/issues/23", "title": "rejected"},
            project=rejected_project,
            agent="opencode",
            model="provider/model-fast",
            request={"instructions": "rejected"},
        )
        rejected["status"] = "REJECTED"
        rejected["attention_reason"] = "not authorized"
        rejected["execution"]["status"] = "NOT_STARTED"
        rejected["capture"]["status"] = "NOT_STARTED"
        rejected["upload"]["status"] = "NOT_STARTED"
        rejected["publication"]["status"] = "NOT_STARTED"
        coding = self._base_record(24, task_type="coding", coding="auto")
        pending = self._base_record(25, task_type="read_only", coding=None, status="UPLOAD_PENDING")
        pending["upload"] = {
            "status": "FAILED",
            "verified": False,
            "verified_at": None,
            "report_url": None,
            "receipt_url": None,
            "attempts": 1,
            "last_error": "upload interrupted",
        }
        pending["publication"] = {
            "status": "PENDING",
            "attempts": 0,
            "published_at": None,
            "comment_url": None,
            "last_error": None,
            "next_attempt_at": None,
        }
        for item in (delivered, explicit, rejected, coding, pending):
            self._create(item)
        self._sent_notification(delivered, message_id=621)
        self._sent_notification(coding, message_id=624)
        unchanged_numbers = (21, 22, 23, 25)
        before_hashes = {number: self._hash(number) for number in unchanged_numbers}
        before_records = {number: self._get(number) for number in unchanged_numbers}
        before_notifications = {
            item["notification_id"]: json.loads(json.dumps(item)) for item in self.store.all()
        }
        coding_before = self._get(24)
        for _ in range(3):
            self.service.reconcile(self.ledger.all())
        restarted = NotificationService(self.h.config)
        restarted.reconcile(self.ledger.all())
        for number in unchanged_numbers:
            self.assertEqual(self._hash(number), before_hashes[number])
            self.assertEqual(self._get(number), before_records[number])
        coding_after = self._get(24)
        self.assertTrue(coding_after["coding"]["milestones"]["TELEGRAM_SENT"])
        self.assertEqual(coding_after["coding"]["telegram_message_id"], 624)
        self.assertEqual(coding_after["coding"]["telegram_sent_at"], SENT_AT)
        self.assertEqual(len(self._notes(coding_after, "Telegram API send acknowledged")), 1)
        self._assert_preserved(coding_before, coding_after)
        after_notifications = {item["notification_id"]: item for item in self.store.all()}
        self.assertEqual(set(after_notifications), set(before_notifications))
        for notification_id_value, before_notification in before_notifications.items():
            self.assertEqual(after_notifications[notification_id_value], before_notification)
        self.assertEqual(self.h.comments(21), [])
        self.assertEqual(self.h.comments(24), [])
        self.assertEqual(self.h.opencode_calls(), [])
        self._assert_no_sends()


if __name__ == "__main__":
    unittest.main()
