from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from agent_handoff.adapters.agy import agy_outcome, parse_agy_transcript
from agent_handoff.config import DEFAULT_CONFIG, deep_merge, project_by_name, project_by_path
from agent_handoff.report import github_commit_url, ensure_sections
from agent_handoff.util import redact_text, slugify, strip_wrappers


class UtilTests(unittest.TestCase):
    def test_slugify(self) -> None:
        self.assertEqual(slugify("Fix login redirect!"), "fix-login-redirect")
        self.assertEqual(slugify("   "), "task")

    def test_redaction(self) -> None:
        text = "API_KEY = supersecretvalue123\npostgres://user:pass@example.test/db\nBearer abcdefghijklmnopqrstuvwxyz"
        redacted, count = redact_text(text)
        self.assertGreater(count, 0)
        self.assertNotIn("supersecretvalue123", redacted)
        self.assertNotIn("pass@example.test", redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_redaction_handles_key_prefix(self) -> None:
        redacted, count = redact_text("Secret: API_KEY = supersecretvalue123")
        self.assertGreater(count, 0)
        self.assertNotIn("supersecretvalue123", redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_strip_wrappers(self) -> None:
        value = "<USER_REQUEST>\nhello\n</USER_REQUEST>\n<ADDITIONAL_METADATA>x</ADDITIONAL_METADATA>"
        self.assertEqual(strip_wrappers(value), "hello")

    def test_report_sections(self) -> None:
        text = ensure_sections("# Report\n\n## Executive Summary\n\nDone.\n")
        for title in ("Task ID and Run ID", "Tests Passed", "Evidence links"):
            self.assertIn(f"## {title}", text)
        self.assertIn("Not provided.", text)

    def test_github_commit_url(self) -> None:
        self.assertEqual(
            github_commit_url("git@github.com:owner/repo.git", "abc123"),
            "https://github.com/owner/repo/commit/abc123",
        )
        self.assertIsNone(github_commit_url("https://example.test/repo", "abc123"))


class ConfigTests(unittest.TestCase):
    def test_defaults_have_no_user_data(self) -> None:
        self.assertEqual(DEFAULT_CONFIG["projects"], {})
        self.assertEqual(DEFAULT_CONFIG["drive"]["parent_folder_id"], "")
        self.assertEqual(DEFAULT_CONFIG["drive"]["root_folder_id"], "")

    def test_project_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "alpha"
            root.mkdir()
            config = deep_merge(
                DEFAULT_CONFIG,
                {
                    "projects": {
                        "alpha": {
                            "name": "Alpha",
                            "drive_folder_id": "alpha_folder_0001",
                            "paths": [str(root)],
                            "aliases": ["a"],
                        }
                    }
                },
            )
            self.assertEqual(project_by_name(config, "a")["name"], "Alpha")
            self.assertEqual(project_by_path(config, str(root / "sub"))["name"], "Alpha")
            self.assertIsNone(project_by_name(config, "missing"))


class AgyAdapterTests(unittest.TestCase):
    def test_outcome_mapping(self) -> None:
        self.assertEqual(agy_outcome("NO_TOOL_CALL", "", True), "succeeded")
        self.assertEqual(agy_outcome("NO_TOOL_CALL", "", False), "blocked")
        self.assertEqual(agy_outcome("USER_CANCELED", "", True), "cancelled")
        self.assertEqual(agy_outcome("ERROR", "boom", True), "failed")
        self.assertEqual(agy_outcome("MAX_TURNS", "", True), "blocked")

    def test_transcript_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transcript.jsonl"
            path.write_text(
                "\n".join(
                    [
                        '{"type":"USER_INPUT","source":"USER_EXPLICIT","content":"<USER_REQUEST>hello</USER_REQUEST>"}',
                        '{"type":"PLANNER_RESPONSE","status":"DONE","content":"world"}',
                        '{"type":"USER_INPUT","source":"SYSTEM","content":"ignored"}',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            parsed = parse_agy_transcript(str(path))
            self.assertEqual(parsed["users"][0]["content"], "hello")
            self.assertEqual(parsed["assistants"][0]["content"], "world")
            self.assertEqual(len(parsed["users"]), 1)


if __name__ == "__main__":
    unittest.main()
