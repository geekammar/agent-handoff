from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from support import BIN, ROOT, Harness


class IntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.h = Harness(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_doctor_and_cli_compatibility(self) -> None:
        version = self.h.run(["--version"]).stdout.strip()
        self.assertEqual(version, "agent-handoff 1.0.0")
        doctor = json.loads(self.h.run(["doctor", "--json"], check=False).stdout)
        self.assertTrue(doctor["upload"]["remote_ready"])
        self.assertTrue(doctor["upload"]["hierarchy"]["projects"]["alpha"]["verified"])
        self.assertTrue(doctor["checks"])

    def test_concurrent_sessions_and_attribution(self) -> None:
        a = self.h.create_run("opencode", "Concurrent A", "PROMPT-A-UNIQUE", "Report A content.")
        b = self.h.create_run("agy", "Concurrent B", "PROMPT-B-UNIQUE", "Report B content.")
        self.h.complete(b, tests="passed", extra=["--tests-details", "B test"])
        time.sleep(1.1)
        self.h.complete(a)
        self.assertNotEqual(a["task"]["task_id"], b["task"]["task_id"])
        self.assertNotEqual(a["run"]["run_id"], b["run"]["run_id"])
        self.assertIn("PROMPT-A-UNIQUE", (self.h.task_dir(a["task"]["task_id"]) / "PROMPT.md").read_text())
        self.assertIn("PROMPT-B-UNIQUE", (self.h.task_dir(b["task"]["task_id"]) / "PROMPT.md").read_text())
        report_a = (self.h.task_dir(a["task"]["task_id"]) / "RUNS" / a["run"]["run_id"] / "REPORT.md").read_text()
        report_b = (self.h.task_dir(b["task"]["task_id"]) / "RUNS" / b["run"]["run_id"] / "REPORT.md").read_text()
        self.assertIn("Report A content.", report_a)
        self.assertIn("Report B content.", report_b)
        self.assertNotIn("Report B content.", report_a)
        index = (self.h.home / "data" / "projects" / "Alpha" / "PROJECT-STATUS.md").read_text()
        self.assertLess(index.find(a["task"]["task_id"]), index.find(b["task"]["task_id"]))
        complete_a = self.h.read_complete(a["task"]["task_id"], a["run"]["run_id"])
        complete_b = self.h.read_complete(b["task"]["task_id"], b["run"]["run_id"])
        self.assertEqual(complete_a["agent"]["name"], "opencode")
        self.assertEqual(complete_b["agent"]["name"], "agy")

        c = self.h.create_run("opencode", "Concurrent C", "PROMPT-C", "Report C.")
        d = self.h.create_run("opencode", "Concurrent D", "PROMPT-D", "Report D.")
        commands = []
        for item in (c, d):
            commands.append(
                [
                    sys.executable,
                    str(BIN),
                    "complete",
                    "--task-id",
                    item["task"]["task_id"],
                    "--run-id",
                    item["run"]["run_id"],
                    "--report-file",
                    item["report"],
                    "--outcome",
                    "succeeded",
                    "--tests",
                    "not_verified",
                    "--json",
                ]
            )
        processes = [
            subprocess.Popen(command, cwd=str(self.h.root), env=self.h.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for command in commands
        ]
        results = [process.communicate(timeout=120) for process in processes]
        self.assertEqual([process.returncode for process in processes], [0, 0], results)
        index = (self.h.home / "data" / "projects" / "Alpha" / "PROJECT-STATUS.md").read_text()
        self.assertIn(c["task"]["task_id"], index)
        self.assertIn(d["task"]["task_id"], index)

    def test_upload_retry_index_and_no_duplicate_upload(self) -> None:
        item = self.h.create_run("opencode", "Upload Retry", "PROMPT-RETRY", "Retry report.")
        self.h.complete(item)
        self.assertEqual(len(self.h.queue_items()), 1)
        marker = self.h.root / "fail-once"
        marker.write_text("1", encoding="utf-8")
        failed = self.h.run(
            ["retry-upload", "--force", "--json"],
            check=False,
            extra_env={"FAKE_FAIL_ONCE_FILE": str(marker)},
        )
        failed_data = json.loads(failed.stdout)
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse(self.h.read_complete(item["task"]["task_id"], item["run"]["run_id"])["upload"]["verified"])
        self.assertTrue(Path(item["report"]).exists())
        self.assertEqual(len(self.h.queue_items()), 1)
        self.assertGreater(json.loads(self.h.queue_items()[0].read_text())["attempts"], 0)
        failed_remote = self.h.read_complete(item["task"]["task_id"], item["run"]["run_id"])["upload"]["remote_dir"].split("/")
        self.assertFalse(self.h.remote("TASKS", failed_remote[1], "RUNS", item["run"]["run_id"], "REPORT.md").exists())

        retry = self.h.jrun(["retry-upload", "--force", "--json"])
        self.assertTrue(retry["ok"])
        complete = self.h.read_complete(item["task"]["task_id"], item["run"]["run_id"])
        self.assertTrue(complete["upload"]["verified"])
        verified_at = complete["upload"]["verified_at"]
        remote_report = self.h.remote(
            "TASKS",
            complete["upload"]["remote_dir"].split("/")[1],
            "RUNS",
            item["run"]["run_id"],
            "REPORT.md",
        )
        self.assertTrue(remote_report.exists())
        remote_index = self.h.remote("PROJECT-STATUS.md")
        self.assertTrue(remote_index.exists())
        self.assertIn(item["task"]["task_id"], remote_index.read_text())
        self.assertNotIn("/tmp/", remote_index.read_text())
        self.assertEqual(len(self.h.queue_items()), 0)

        again = self.h.jrun(["retry-upload", "--force", "--json"])
        self.assertEqual(again["processed"], 0)
        self.assertEqual(len(self.h.queue_items()), 0)
        self.assertEqual(self.h.read_complete(item["task"]["task_id"], item["run"]["run_id"])["upload"]["verified_at"], verified_at)

    def test_security_redaction(self) -> None:
        item = self.h.create_run(
            "opencode",
            "Redaction",
            "PROMPT-SECRET",
            "Secret: API_KEY = supersecretvalue123 and postgres://user:pass@example.test/db",
        )
        self.h.complete(item)
        self.h.jrun(["retry-upload", "--force", "--json"])
        local = (self.h.task_dir(item["task"]["task_id"]) / "RUNS" / item["run"]["run_id"] / "REPORT.md").read_text()
        complete = self.h.read_complete(item["task"]["task_id"], item["run"]["run_id"])
        remote = self.h.remote(
            "TASKS",
            complete["upload"]["remote_dir"].split("/")[1],
            "RUNS",
            item["run"]["run_id"],
            "REPORT.md",
        ).read_text()
        self.assertIn("supersecretvalue123", local)
        self.assertNotIn("supersecretvalue123", remote)
        self.assertIn("[REDACTED]", remote)
        self.assertGreater(complete["upload"]["redactions"], 0)

    def test_agy_hook_capture_and_deduplication(self) -> None:
        transcript = self.h.root / "transcript.jsonl"
        transcript.write_text(
            "\n".join(
                [
                    '{"type":"USER_INPUT","source":"USER_EXPLICIT","content":"<USER_REQUEST>HOOK-PROMPT-UNIQUE</USER_REQUEST>","created_at":"2026-11-16T10:00:00Z"}',
                    '{"type":"PLANNER_RESPONSE","status":"DONE","content":"HOOK-SUMMARY-OK","created_at":"2026-11-16T10:00:05Z"}',
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        payload = {
            "hookEventName": "Stop",
            "conversationId": "conv-acceptance-phase2",
            "cwd": str(self.h.project_path),
            "transcriptPath": str(transcript),
            "terminationReason": "NO_TOOL_CALL",
            "fullyIdle": True,
            "lastUserInput": "HOOK-PROMPT-UNIQUE",
            "error": "",
        }
        result = self.h.run(["hook", "agy"], input_text=json.dumps(payload))
        self.assertEqual(result.stdout.strip(), "{}")
        self.assertEqual(result.returncode, 0)
        rows = self.h.rows()
        agy_rows = [row for row in rows if row["agent"] == "agy"]
        self.assertEqual(len(agy_rows), 1)
        row = agy_rows[0]
        task_path = self.h.task_dir(row["task_id"])
        self.assertIn("HOOK-PROMPT-UNIQUE", (task_path / "PROMPT.md").read_text())
        report = (task_path / "RUNS" / row["run_id"] / "REPORT.md").read_text()
        self.assertIn("HOOK-SUMMARY-OK", report)
        self.assertIn("succeeded", report)
        self.assertEqual(len(self.h.queue_items()), 1)

        duplicate = self.h.run(["hook", "agy"], input_text=json.dumps(payload))
        self.assertEqual(duplicate.stdout.strip(), "{}")
        self.assertEqual(len([item for item in self.h.rows() if item["agent"] == "agy"]), 1)
        self.assertEqual(len(self.h.queue_items()), 1)

    def test_opencode_plugin_static_contract(self) -> None:
        plugin = ROOT / "integrations" / "opencode" / "agent-handoff.js"
        text = plugin.read_text()
        self.assertIn('"chat.message"', text)
        self.assertIn("session.idle", text)
        self.assertIn("auto-complete", text)
        self.assertIn("payload-file", text)
        node = shutil.which("node")
        if node:
            result = subprocess.run([node, "--check", str(plugin)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.skipTest("node is not installed; plugin syntax check skipped")


if __name__ == "__main__":
    unittest.main()
