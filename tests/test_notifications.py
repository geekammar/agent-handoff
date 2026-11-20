from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "lib") not in sys.path:
    sys.path.insert(0, str(ROOT / "lib"))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import Harness  # noqa: E402
from test_dispatch import GH_REPO, DispatchHarness, request_body  # noqa: E402

from agent_handoff.dispatch.ledger import Ledger, new_dispatch_record  # noqa: E402
from agent_handoff.notify.config import validate_notification_config  # noqa: E402
from agent_handoff.notify.messages import (  # noqa: E402
    DELIVERY_COMPLETED,
    DELIVERY_DELAYED,
    NEEDS_ATTENTION,
    build_telegram_message,
)
from agent_handoff.notify.service import NotificationService  # noqa: E402
from agent_handoff.notify.store import NotificationStore  # noqa: E402
from agent_handoff.notify.transport import TelegramTransport  # noqa: E402

TOKEN = "123456789:AAFakeTokenForTests_1234567890"
CHAT_ID = "-1001234567890"


class FakeTelegramServer:
    def __init__(self, token: str = TOKEN):
        self.token = token
        self.expected_token: str | None = token
        self.mode = "ok"
        self.retry_after = 1
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeTelegramHandler)
        self.httpd.fake = self
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def response(self, token: str, payload: dict | None, index: int) -> tuple[int, dict]:
        if self.expected_token and token != self.expected_token:
            return 401, {"ok": False, "error_code": 401, "description": "Unauthorized"}
        if self.mode == "ok":
            return 200, {"ok": True, "result": {"message_id": 1000 + index}}
        if self.mode == "fail_once":
            if index == 1:
                return 503, {"ok": False, "error_code": 503, "description": "Service Unavailable"}
            return 200, {"ok": True, "result": {"message_id": 1000 + index}}
        if self.mode == "chat_not_found":
            return 400, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
        if self.mode == "ok_false":
            return 200, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
        if self.mode == "unavailable":
            return 503, {"ok": False, "error_code": 503, "description": "Service Unavailable"}
        if self.mode == "rate_limited":
            return 429, {
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests: retry later",
                "parameters": {"retry_after": self.retry_after},
            }
        if self.mode == "slow":
            time.sleep(1.5)
            return 200, {"ok": True, "result": {"message_id": 1000 + index}}
        return 500, {"ok": False, "error_code": 500, "description": "Unknown fake mode"}


class _FakeTelegramHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        fake: FakeTelegramServer = self.server.fake  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            payload = None
        token = ""
        if self.path.startswith("/bot"):
            token = self.path[len("/bot") :].split("/", 1)[0]
        with fake._lock:
            fake.requests.append({"path": self.path, "payload": payload})
            index = len(fake.requests)
        status, response = fake.response(token, payload, index)
        body = json.dumps(response, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        return


class NotificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.h = DispatchHarness(self.root)
        self.server = FakeTelegramServer()
        self.server.start()
        self.env = {
            "AGENT_HANDOFF_TELEGRAM_TOKEN": TOKEN,
            "AGENT_HANDOFF_TELEGRAM_API_BASE": self.server.base_url,
        }

    def tearDown(self) -> None:
        self.server.stop()
        self.tmp.cleanup()

    def enable_notifications(self, **overrides) -> None:
        telegram = {
            "enabled": True,
            "chat_id": CHAT_ID,
            "token_env": "AGENT_HANDOFF_TELEGRAM_TOKEN",
            "timeout_seconds": 2,
            "retry_seconds": 5,
            "max_attempts": 3,
            "delayed_after_attempts": 3,
        }
        telegram.update(overrides)
        self.h.config["notifications"] = {"telegram": telegram}
        self.h.write_config()

    def dispatch(self, *args: str, extra_env: dict[str, str] | None = None, check: bool = True) -> dict:
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        return self.h.dispatch(*args, extra_env=env, check=check)

    def run_cli(self, args: list[str], extra_env: dict[str, str] | None = None, check: bool = True):
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        return self.h.run(args, check=check, extra_env=env)

    def jrun_cli(self, args: list[str], extra_env: dict[str, str] | None = None, check: bool = True) -> dict:
        return json.loads(self.run_cli(args, extra_env=extra_env, check=check).stdout)

    def notifications(self) -> list[dict]:
        return NotificationStore(self.h.config).all()

    def notification(self, kind: str) -> dict:
        matches = [item for item in self.notifications() if item.get("kind") == kind]
        self.assertTrue(matches, self.notifications())
        return matches[0]

    def test_completion_message_contains_verified_url_prompt_and_is_idempotent(self) -> None:
        self.enable_notifications()
        self.h.add_issue(number=1, body=request_body(), title="Polish <tag> & emoji — مهمة")
        first = self.dispatch("--force")
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["uploaded"], 1)
        self.assertEqual(first["published"], 1)
        record = self.h.record(1)
        report_url = record["upload"]["report_url"]
        receipt_url = record["upload"]["receipt_url"]
        self.assertTrue(report_url)
        self.assertTrue(receipt_url)
        self.assertEqual(len(self.server.requests), 1)
        payload = self.server.requests[0]["payload"]
        text = payload["text"]
        self.assertEqual(payload["chat_id"], CHAT_ID)
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertTrue(payload["disable_web_page_preview"])
        self.assertIn(report_url, text)
        self.assertIn(record["issue_url"], text)
        self.assertIn("&lt;tag&gt;", text)
        self.assertIn("&amp;", text)
        self.assertIn("مهمة", text)
        self.assertIn("PROMPT FOR CHATGPT:", text)
        self.assertIn(report_url, text.split("PROMPT FOR CHATGPT:", 1)[1])
        self.assertIn("اقرأ تقرير التنفيذ", text)
        self.assertNotIn("Read the project README", text)
        buttons = json.dumps(payload["reply_markup"], ensure_ascii=False)
        self.assertIn("View Report", buttons)
        self.assertIn("View GitHub Issue", buttons)
        self.assertIn(report_url, buttons)
        notification = self.notification(DELIVERY_COMPLETED)
        self.assertEqual(notification["state"], "SENT")
        self.assertTrue(notification["telegram_message_id"])
        self.assertTrue(notification["sent_at"])

        status = self.jrun_cli(["dispatch", "status", "--json"])
        self.assertEqual(status["records"][0]["health"], "delivered")
        self.assertEqual(status["records"][0]["notifications"][0]["state"], "SENT")
        self.assertEqual(status["notifications"]["counts"]["SENT"], 1)

        again = self.dispatch("--force")
        self.assertTrue(again["ok"], again)
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(len(self.h.opencode_calls()), 1)
        self.assertEqual(len(self.h.comments(1)), 1)

    def test_no_completion_before_verified_upload_and_publication(self) -> None:
        self.enable_notifications(delayed_after_attempts=3)
        self.h.add_issue(number=1, body=request_body())
        upload_marker = self.root / "notify-upload-fail"
        upload_marker.write_text("1", encoding="utf-8")
        upload_failure = self.dispatch(extra_env={"FAKE_FAIL_ONCE_FILE": str(upload_marker)}, check=False)
        self.assertEqual(upload_failure["uploaded"], 0)
        self.assertEqual(self.h.record(1)["status"], "UPLOAD_PENDING")
        self.assertEqual(self.server.requests, [])
        self.assertEqual(self.notifications(), [])

        self.dispatch("--force")
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(self.notification(DELIVERY_COMPLETED)["state"], "SENT")

        self.h.add_issue(number=2, body=request_body(), title="Second task")
        comment_marker = self.root / "notify-comment-fail"
        comment_marker.write_text("1", encoding="utf-8")
        publication_failure = self.dispatch(
            extra_env={"FAKE_GH_FAIL_COMMENT_FILE": str(comment_marker)},
            check=False,
        )
        self.assertEqual(publication_failure["published"], 0)
        self.assertEqual(self.h.record(2)["status"], "UPLOAD_VERIFIED")
        self.assertEqual(len(self.server.requests), 1)
        self.dispatch("--force")
        self.assertEqual(len(self.server.requests), 2)
        self.assertEqual(len(self.h.comments(2)), 1)
        self.assertEqual(len(self.h.opencode_calls()), 2)

    def test_delayed_notifications_are_single_and_do_not_claim_success(self) -> None:
        self.enable_notifications(delayed_after_attempts=1)
        self.h.add_issue(number=1, body=request_body())
        upload_marker = self.root / "notify-delay-upload"
        upload_marker.write_text("1", encoding="utf-8")
        self.dispatch(extra_env={"FAKE_FAIL_ONCE_FILE": str(upload_marker)}, check=False)
        self.assertEqual(len(self.server.requests), 1)
        delayed = self.notification(DELIVERY_DELAYED)
        self.assertEqual(delayed["state"], "SENT")
        text = self.server.requests[0]["payload"]["text"]
        self.assertIn("تسليم التقرير متأخر", text)
        self.assertIn("upload pending", text)
        self.assertNotIn("PROMPT FOR CHATGPT:", text)
        self.assertNotIn("تم تسليم تقرير المهمة", text)
        self.assertNotIn("Verified report:", text)

        self.dispatch("--force")
        self.assertEqual(len(self.server.requests), 2)
        self.assertIn("PROMPT FOR CHATGPT:", self.server.requests[1]["payload"]["text"])
        self.dispatch("--force")
        self.assertEqual(len(self.server.requests), 2)

    def test_publication_delay_notification_then_completion(self) -> None:
        self.enable_notifications(delayed_after_attempts=1)
        self.h.add_issue(number=1, body=request_body())
        marker = self.root / "notify-publication-delay"
        marker.write_text("1", encoding="utf-8")
        first = self.dispatch(extra_env={"FAKE_GH_FAIL_COMMENT_FILE": str(marker)}, check=False)
        self.assertEqual(first["published"], 0)
        delayed = self.notification(DELIVERY_DELAYED)
        self.assertEqual(delayed["state"], "SENT")
        self.assertEqual(delayed["payload"]["stage"], "publication")
        self.assertEqual(len(self.server.requests), 1)
        self.assertIn("publication pending", self.server.requests[0]["payload"]["text"])
        self.dispatch("--force")
        self.assertEqual(len(self.server.requests), 2)
        self.assertEqual(self.h.record(1)["status"], "ISSUE_UPDATED")
        self.assertEqual(self.notification(DELIVERY_COMPLETED)["state"], "SENT")

    def test_attention_notification_for_exhausted_publication_then_manual_delivery(self) -> None:
        self.enable_notifications()
        self.h.config["dispatch"]["delivery"]["max_attempts"] = 1
        self.h.write_config()
        self.h.add_issue(number=1, body=request_body())
        marker = self.root / "notify-exhaust"
        marker.write_text("1", encoding="utf-8")
        failed = self.dispatch(extra_env={"FAKE_GH_FAIL_COMMENT_FILE": str(marker)}, check=False)
        self.assertEqual(failed["needs_attention"], 1)
        record = self.h.record(1)
        self.assertEqual(record["status"], "UPLOAD_VERIFIED")
        self.assertEqual(record["publication"]["status"], "EXHAUSTED")
        self.assertEqual(len(self.server.requests), 1)
        text = self.server.requests[0]["payload"]["text"]
        self.assertIn("مهمة تحتاج إلى مراجعة", text)
        self.assertIn("Verified report:", text)
        self.assertIn(record["upload"]["report_url"], text)
        self.assertNotIn("PROMPT FOR CHATGPT:", text)
        attention = self.notification(NEEDS_ATTENTION)
        self.assertEqual(attention["state"], "SENT")

        delivered = self.jrun_cli(
            [
                "dispatch",
                "deliver",
                "--task",
                record["capture"]["task_id"],
                "--run",
                record["capture"]["run_id"],
                "--issue",
                "1",
                "--repository",
                GH_REPO,
                "--json",
            ]
        )
        self.assertTrue(delivered["ok"], delivered)
        self.assertFalse(delivered["opencode_executed"])
        self.assertEqual(delivered["notification"]["state"], "SENT")
        self.assertEqual(len(self.server.requests), 2)
        self.assertEqual(self.notification(DELIVERY_COMPLETED)["state"], "SENT")
        self.assertEqual(len(self.h.opencode_calls()), 1)
        self.assertEqual(self.h.record(1)["status"], "ISSUE_UPDATED")

    def test_attention_notification_for_uncertain_execution_has_no_fake_report(self) -> None:
        self.enable_notifications()
        issue = self.h.add_issue(number=1, body=request_body())
        ledger = Ledger(self.h.config)
        record = new_dispatch_record(
            repository=GH_REPO,
            issue=issue,
            project={
                "name": "Alpha",
                "core_key": "alpha",
                "core_name": "Alpha",
                "workspace": str(self.h.project_path.resolve()),
                "drive_folder_id": "alpha_folder_0001",
            },
            agent="opencode",
            model="provider/model-fast",
            request={"version": "1", "instructions": "stored request"},
        )
        ledger.create(record)
        ledger.set_status(
            GH_REPO,
            1,
            "CLAIMED",
            note="simulated crash after claim",
            updates={"execution": {"status": "CLAIMED"}},
        )
        result = self.dispatch("--force", check=False)
        self.assertEqual(result["needs_attention"], 1)
        self.assertEqual(self.h.opencode_calls(), [])
        self.assertEqual(len(self.server.requests), 1)
        text = self.server.requests[0]["payload"]["text"]
        self.assertIn("Verified report: not available yet", text)
        self.assertNotIn("PROMPT FOR CHATGPT:", text)
        self.assertEqual(self.notification(NEEDS_ATTENTION)["state"], "SENT")
        self.assertEqual(self.h.record(1)["status"], "NEEDS_ATTENTION")

    def test_invalid_chat_id_has_bounded_retries_and_safe_manual_retry(self) -> None:
        self.server.mode = "chat_not_found"
        self.enable_notifications(max_attempts=2)
        self.h.add_issue(number=1, body=request_body())
        first = self.dispatch()
        self.assertTrue(first["ok"], first)
        record = self.h.record(1)
        self.assertEqual(record["status"], "ISSUE_UPDATED")
        self.assertTrue(record["upload"]["verified"])
        self.assertEqual(len(self.h.comments(1)), 1)
        self.assertEqual(len(self.h.opencode_calls()), 1)
        notification = self.notification(DELIVERY_COMPLETED)
        self.assertEqual(notification["state"], "RETRY_WAIT")
        self.assertEqual(notification["attempts"], 1)
        self.assertIn("chat not found", notification["last_error"])
        self.assertTrue(notification["next_attempt_at"])

        status = self.jrun_cli(["dispatch", "status", "--json"])
        self.assertEqual(status["records"][0]["health"], "delivered")
        self.assertEqual(status["records"][0]["notifications"][0]["state"], "RETRY_WAIT")
        self.assertEqual(status["notifications"]["counts"]["RETRY_WAIT"], 1)

        store = NotificationStore(self.h.config)
        store.update(
            notification["notification_id"],
            lambda item: item.update({"next_attempt_at": "2020-01-01T00:00:00Z"}),
        )
        second = self.dispatch()
        self.assertTrue(second["ok"], second)
        notification = self.notification(DELIVERY_COMPLETED)
        self.assertEqual(notification["state"], "NEEDS_ATTENTION")
        self.assertEqual(notification["attempts"], 2)
        self.assertEqual(self.h.record(1)["status"], "ISSUE_UPDATED")
        self.assertEqual(len(self.h.comments(1)), 1)
        self.assertEqual(len(self.h.opencode_calls()), 1)

        self.server.mode = "ok"
        retried = self.jrun_cli(["dispatch", "notify", "--retry", "--json"])
        self.assertTrue(retried["ok"], retried)
        self.assertEqual(retried["sent"], 1)
        notification = self.notification(DELIVERY_COMPLETED)
        self.assertEqual(notification["state"], "SENT")
        self.assertEqual(self.h.record(1)["status"], "ISSUE_UPDATED")
        self.assertEqual(len(self.h.comments(1)), 1)

    def test_telegram_outage_is_independent_and_rate_limit_retry_after(self) -> None:
        self.server.mode = "rate_limited"
        self.server.retry_after = 7
        self.enable_notifications(max_attempts=5)
        self.h.add_issue(number=1, body=request_body())
        first = self.dispatch()
        self.assertTrue(first["ok"], first)
        self.assertEqual(self.h.record(1)["status"], "ISSUE_UPDATED")
        notification = self.notification(DELIVERY_COMPLETED)
        self.assertEqual(notification["state"], "RETRY_WAIT")
        next_at = datetime.fromisoformat(notification["next_attempt_at"].replace("Z", "+00:00"))
        delay = (next_at - datetime.now(next_at.tzinfo)).total_seconds()
        self.assertGreaterEqual(delay, 5)
        self.assertLessEqual(delay, 15)
        self.assertEqual(len(self.h.opencode_calls()), 1)
        self.assertEqual(len(self.h.comments(1)), 1)

        self.server.mode = "ok"
        retried = self.jrun_cli(["dispatch", "notify", "--retry", "--json"])
        self.assertTrue(retried["ok"], retried)
        self.assertEqual(self.notification(DELIVERY_COMPLETED)["state"], "SENT")
        self.assertEqual(self.h.record(1)["status"], "ISSUE_UPDATED")
        self.assertEqual(len(self.h.opencode_calls()), 1)
        self.assertEqual(len(self.h.comments(1)), 1)

    def test_manual_retry_attempts_each_notification_once(self) -> None:
        self.h.add_issue(number=1, body=request_body(), title="Retry A")
        self.h.add_issue(number=2, body=request_body(), title="Retry B")
        self.dispatch("--force")
        self.enable_notifications()
        with mock.patch.dict(os.environ, {"AGENT_HANDOFF_TELEGRAM_TOKEN": TOKEN}, clear=False):
            service = NotificationService(self.h.config)
            for number in (1, 2):
                queued = service.enqueue_completed(self.h.record(number))
                self.assertIsNotNone(queued)
        self.assertEqual(len(self.notifications()), 2)
        self.server.mode = "fail_once"
        result = self.jrun_cli(["dispatch", "notify", "--retry", "--json"], check=False)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["retried"], 2)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(len(self.server.requests), 2)
        texts = [item["payload"]["text"] for item in self.server.requests]
        self.assertTrue(any("Retry A" in text for text in texts), texts)
        self.assertTrue(any("Retry B" in text for text in texts), texts)
        states = sorted(item["state"] for item in self.notifications())
        self.assertEqual(states, ["RETRY_WAIT", "SENT"])

    def test_pending_outbox_survives_restart(self) -> None:
        self.h.add_issue(number=1, body=request_body())
        self.dispatch("--force")
        record = self.h.record(1)
        self.enable_notifications()
        with mock.patch.dict(os.environ, {"AGENT_HANDOFF_TELEGRAM_TOKEN": TOKEN}, clear=False):
            queued = NotificationService(self.h.config).enqueue_completed(record)
        self.assertIsNotNone(queued)
        self.assertEqual(queued["state"], "PENDING")
        self.assertEqual(self.server.requests, [])

        retried = self.jrun_cli(["dispatch", "notify", "--retry", "--json"])
        self.assertTrue(retried["ok"], retried)
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(self.notification(DELIVERY_COMPLETED)["state"], "SENT")
        self.assertEqual(self.h.record(1)["status"], "ISSUE_UPDATED")

    def test_stale_sending_is_flagged_as_ambiguous_instead_of_resent(self) -> None:
        self.h.add_issue(number=1, body=request_body())
        self.dispatch("--force")
        record = self.h.record(1)
        self.enable_notifications()
        with mock.patch.dict(os.environ, {"AGENT_HANDOFF_TELEGRAM_TOKEN": TOKEN}, clear=False):
            queued = NotificationService(self.h.config).enqueue_completed(record)
        self.assertIsNotNone(queued)
        store = NotificationStore(self.h.config)
        notification_id = queued["notification_id"]
        store.update(
            notification_id,
            lambda item: item.update(
                {"state": "SENDING", "sending_at": "2020-01-01T00:00:00Z", "sent_at": None}
            ),
        )
        self.dispatch("--force", check=False)
        self.assertEqual(self.server.requests, [])
        stale = store.get(notification_id)
        self.assertEqual(stale["state"], "NEEDS_ATTENTION")
        self.assertIn("may or may not have accepted", stale["last_error"])
        self.assertEqual(self.h.record(1)["status"], "ISSUE_UPDATED")

    def test_completion_intent_is_recovered_after_restart_before_enqueue(self) -> None:
        self.enable_notifications()
        self.jrun_cli(["dispatch", "status", "--json"])
        self.h.config["notifications"]["telegram"]["enabled"] = False
        self.h.write_config()
        self.h.add_issue(number=1, body=request_body())
        self.dispatch("--force")
        self.assertEqual(self.server.requests, [])
        self.assertEqual(self.notifications(), [])
        self.assertEqual(self.h.record(1)["status"], "ISSUE_UPDATED")

        self.h.config["notifications"]["telegram"]["enabled"] = True
        self.h.write_config()
        self.dispatch("--force")
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(self.notification(DELIVERY_COMPLETED)["state"], "SENT")
        self.assertEqual(len(self.h.opencode_calls()), 1)
        self.assertEqual(len(self.h.comments(1)), 1)
        self.assertEqual(self.h.record(1)["status"], "ISSUE_UPDATED")

    def test_notifications_disabled_and_historical_delivery_is_not_replayed(self) -> None:
        self.h.add_issue(number=1, body=request_body())
        first = self.dispatch("--force")
        self.assertTrue(first["ok"], first)
        self.assertEqual(self.server.requests, [])
        self.assertEqual(self.notifications(), [])
        record = self.h.record(1)
        self.assertEqual(record["status"], "ISSUE_UPDATED")

        self.enable_notifications()
        self.dispatch("--force")
        self.assertEqual(self.server.requests, [])
        self.assertEqual(self.notifications(), [])

        status = self.jrun_cli(["dispatch", "status", "--json"])
        self.assertEqual(status["notifications"]["enabled"], True)
        self.assertEqual(status["records"][0]["health"], "delivered")
        self.assertEqual(status["records"][0]["notifications"], [])
        self.assertEqual(self.h.record(1)["status"], "ISSUE_UPDATED")

    def test_configuration_validation_and_token_redaction(self) -> None:
        disabled = {"notifications": {"telegram": {"enabled": False}}}
        resolved, errors, _warnings = validate_notification_config(disabled)
        self.assertEqual(errors, [])
        self.assertFalse(resolved["enabled"])
        self.assertIsNone(resolved["token"])

        with mock.patch.dict(os.environ, {"AGENT_HANDOFF_TELEGRAM_TOKEN": ""}, clear=False):
            _resolved, errors, _warnings = validate_notification_config(
                {"notifications": {"telegram": {"enabled": True, "chat_id": CHAT_ID}}}
            )
        self.assertTrue(any("AGENT_HANDOFF_TELEGRAM_TOKEN" in item for item in errors))
        self.assertFalse(any(TOKEN in item for item in errors))

        with mock.patch.dict(os.environ, {"AGENT_HANDOFF_TELEGRAM_TOKEN": TOKEN}, clear=False):
            _resolved, errors, _warnings = validate_notification_config(
                {"notifications": {"telegram": {"enabled": True, "chat_id": ""}}}
            )
        self.assertTrue(any("chat_id is required" in item for item in errors))

        with mock.patch.dict(os.environ, {"AGENT_HANDOFF_TELEGRAM_TOKEN": "not-a-token"}, clear=False):
            _resolved, errors, _warnings = validate_notification_config(
                {"notifications": {"telegram": {"enabled": True, "chat_id": CHAT_ID}}}
            )
        self.assertTrue(any("does not look like a Telegram bot token" in item for item in errors))
        self.assertFalse(any("not-a-token" in item for item in errors))

        _resolved, errors, _warnings = validate_notification_config(
            {"notifications": {"telegram": {"enabled": False, "chat_id": "not a chat"}}}
        )
        self.assertTrue(any("chat_id" in item for item in errors))

        self.h.config["notifications"] = {"telegram": {"enabled": True, "chat_id": CHAT_ID}}
        self.h.write_config()
        output = self.run_cli(
            ["config", "validate", "--json"],
            extra_env={"AGENT_HANDOFF_TELEGRAM_TOKEN": ""},
            check=False,
        )
        data = json.loads(output.stdout)
        self.assertFalse(data["ok"])
        self.assertFalse(data["notifications"]["token_present"])
        self.assertNotIn(TOKEN, output.stdout + output.stderr)

    def test_arabic_formatting_escaping_and_length_limits(self) -> None:
        long_title = "مهمة عربية <script>alert('x')</script> & emoji 🚀 " + ("very-long-" * 40)
        payload = {
            "project": "Alpha & Beta",
            "task_title": long_title,
            "report_url": "https://drive.google.com/file/d/fakefile123/view",
            "issue_url": "https://github.test/dev/private-dispatch/issues/9",
            "outcome": "succeeded",
        }
        message = build_telegram_message(DELIVERY_COMPLETED, payload)
        text = message["text"]
        self.assertLessEqual(len(text), 4000)
        self.assertIn("https://drive.google.com/file/d/fakefile123/view", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertIn("&amp;", text)
        self.assertIn("🚀", text)
        self.assertIn("…", text)
        self.assertIn("تم الانتهاء من مهمة", text)
        self.assertEqual(
            text.split("PROMPT FOR CHATGPT:", 1)[1].count(
                "https://drive.google.com/file/d/fakefile123/view"
            ),
            1,
        )

        attention = build_telegram_message(
            NEEDS_ATTENTION,
            {
                "project": "Alpha",
                "task_title": "Needs review",
                "status": "NEEDS_ATTENTION",
                "reason": "capture failed",
                "next_action": "inspect",
                "issue_url": "https://github.test/dev/private-dispatch/issues/9",
            },
        )["text"]
        self.assertIn("Verified report: not available yet", attention)
        self.assertNotIn("PROMPT FOR CHATGPT:", attention)

        delayed = build_telegram_message(
            DELIVERY_DELAYED,
            {
                "project": "Alpha",
                "task_title": "Retry task",
                "stage": "upload",
                "reason": "temporary outage",
                "attempts": 3,
                "issue_url": "https://github.test/dev/private-dispatch/issues/9",
            },
        )["text"]
        self.assertIn("upload pending", delayed)
        self.assertIn("تسليم التقرير متأخر", delayed)
        self.assertNotIn("Verified report:", delayed)

    def test_notification_reason_redacts_local_paths_and_secrets(self) -> None:
        self.enable_notifications()
        with mock.patch.dict(os.environ, {"AGENT_HANDOFF_TELEGRAM_TOKEN": TOKEN}, clear=False):
            service = NotificationService(self.h.config)
        cleaned = service._clean(
            f"failure at {self.h.project_path}/secret.txt TOKEN=SUPERSECRETVALUE123"
        )
        self.assertIn("<local>", cleaned)
        self.assertNotIn(str(self.h.project_path), cleaned)
        self.assertNotIn("SUPERSECRETVALUE123", cleaned)

    def test_transport_redacts_token_and_handles_telegram_errors(self) -> None:
        transport = TelegramTransport(TOKEN, timeout_seconds=2, api_base=self.server.base_url)
        self.server.mode = "ok_false"
        result = transport.send_message(CHAT_ID, "test")
        self.assertFalse(result["ok"])
        self.assertIn("chat not found", result["error"])
        self.assertNotIn(TOKEN, json.dumps(result, ensure_ascii=False))

        self.server.mode = "unavailable"
        result = transport.send_message(CHAT_ID, "test")
        self.assertFalse(result["ok"])
        self.assertTrue(result["retryable"])
        self.assertNotIn(TOKEN, json.dumps(result, ensure_ascii=False))

        self.server.mode = "ok"
        result = transport.send_message(CHAT_ID, "test")
        self.assertTrue(result["ok"])
        self.assertTrue(result["message_id"])

        token = "123456789:AAFakeTokenForTests_1234567890"
        with mock.patch("urllib.request.urlopen", side_effect=OSError(f"failure involving {token}")):
            failed = TelegramTransport(token, timeout_seconds=1).send_message(CHAT_ID, "test")
        self.assertFalse(failed["ok"])
        self.assertNotIn(token, json.dumps(failed, ensure_ascii=False))

    def test_notification_retry_when_disabled_is_safe(self) -> None:
        output = self.run_cli(["dispatch", "notify", "--retry", "--json"], check=False)
        self.assertEqual(output.returncode, 1)
        data = json.loads(output.stdout)
        self.assertFalse(data["ok"])
        self.assertFalse(data["enabled"])
        self.assertEqual(self.server.requests, [])
