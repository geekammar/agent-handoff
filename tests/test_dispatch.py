from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "lib") not in sys.path:
    sys.path.insert(0, str(ROOT / "lib"))

from support import Harness  # noqa: E402

from agent_handoff.dispatch.ledger import Ledger, new_dispatch_record  # noqa: E402

BETA_ID = "beta_folder_0001"
GH_REPO = "dev/private-dispatch"


def request_body(
    *,
    project: str = "Alpha",
    agent: str = "opencode",
    model: str | None = "fast",
    session: str = "new",
    instructions: str = "Read the project README and report its first heading. Do not edit files.",
) -> str:
    lines = [
        "<!-- agent-handoff-request -->",
        "handoff-version: 1",
        f"project: {project}",
        f"agent: {agent}",
    ]
    if model is not None:
        lines.append(f"model: {model}")
    lines.extend(
        [
            f"session: {session}",
            "<!-- /agent-handoff-request -->",
            "",
            instructions,
        ]
    )
    return "\n".join(lines)


class DispatchHarness(Harness):
    def __init__(self, root: Path):
        super().__init__(root)
        self.beta_path = self.workspace / "beta"
        self.beta_path.mkdir(parents=True)
        self.gh_dir = root / "fake-gh"
        self.gh_dir.mkdir(parents=True)
        self.opencode_dir = root / "fake-opencode"
        self.opencode_dir.mkdir(parents=True)
        for source, target in ((ROOT / "tests" / "fake-gh", self.fake_bin / "gh"), (ROOT / "tests" / "fake-opencode", self.fake_bin / "opencode")):
            shutil.copy2(source, target)
            os.chmod(target, 0o755)
        self.set_repo()
        self.set_user("dev")
        self.set_issues([])
        self.set_comments({})
        self.config["projects"]["beta"] = {
            "name": "Beta",
            "drive_folder_id": BETA_ID,
            "paths": [str(self.beta_path)],
            "path_prefixes": [],
            "aliases": ["beta"],
            "auto_registered": False,
        }
        self.drive.joinpath(BETA_ID).mkdir(parents=True, exist_ok=True)
        self.drive.joinpath(self.drive_root_id()).joinpath(".children.json").write_text(
            json.dumps({"Alpha": self.drive_alpha_id(), "Beta": BETA_ID}), encoding="utf-8"
        )
        self.config["dispatch"] = {
            "enabled": True,
            "transport": "github_issues",
            "repository": GH_REPO,
            "allowed_users": ["dev"],
            "agent": "opencode",
            "models": {"fast": "provider/model-fast", "review": "provider/model-review"},
            "default_model": "fast",
            "projects": {
                "Alpha": {"workspace": str(self.project_path)},
                "Beta": {"workspace": str(self.beta_path)},
            },
            "poll_interval_seconds": 5,
            "execution": {
                "timeout_seconds": 30,
                "max_prompt_bytes": 50000,
                "permission_profile": "read_only",
                "allow_write": False,
            },
            "delivery": {"retry_seconds": 5, "max_attempts": 0},
            "github": {"command": "gh", "require_private": True},
        }
        self.write_config()

    def drive_root_id(self) -> str:
        from support import ROOT_ID

        return ROOT_ID

    def drive_alpha_id(self) -> str:
        from support import PROJECT_ID

        return PROJECT_ID

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        value = super().env(extra)
        value.update(
            {
                "FAKE_GH_DIR": str(self.gh_dir),
                "FAKE_OPENCODE_DIR": str(self.opencode_dir),
            }
        )
        return value

    def set_repo(self, *, private: bool = True) -> None:
        self.gh_dir.joinpath("repo.json").write_text(
            json.dumps({"full_name": GH_REPO, "private": private, "has_issues": True}), encoding="utf-8"
        )

    def set_user(self, login: str) -> None:
        self.gh_dir.joinpath("user.json").write_text(json.dumps({"login": login}), encoding="utf-8")

    def set_issues(self, issues: list[dict]) -> None:
        self.gh_dir.joinpath("issues.json").write_text(json.dumps(issues, indent=2), encoding="utf-8")

    def set_comments(self, comments: dict) -> None:
        self.gh_dir.joinpath("comments.json").write_text(json.dumps(comments, indent=2), encoding="utf-8")

    def add_issue(
        self,
        *,
        number: int,
        body: str,
        author: str = "dev",
        title: str = "Dispatch request",
        state: str = "open",
    ) -> dict:
        path = self.gh_dir / "issues.json"
        issues = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        issue = {
            "number": number,
            "title": title,
            "body": body,
            "state": state,
            "user": {"login": author},
            "html_url": f"https://github.test/{GH_REPO}/issues/{number}",
            "url": f"https://api.github.test/repos/{GH_REPO}/issues/{number}",
        }
        issues.append(issue)
        self.set_issues(issues)
        return issue

    def comments(self, number: int) -> list[dict]:
        path = self.gh_dir / "comments.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return data.get(str(number), [])

    def gh_calls(self) -> list[list[str]]:
        path = self.gh_dir / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def opencode_calls(self) -> list[dict]:
        path = self.opencode_dir / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def dispatch(self, *args: str, extra_env: dict[str, str] | None = None, check: bool = True) -> dict:
        return self.jrun(["dispatch", "run", "--once", "--json", *args], extra_env=extra_env, check=check)

    def task_dir_for(self, project: str, task_id: str) -> Path:
        matches = list((self.home / "data" / "projects" / project / "TASKS").glob(f"{task_id}-*"))
        if not matches:
            raise FileNotFoundError(task_id)
        return matches[0]

    def record(self, issue: int) -> dict:
        ledger = Ledger(self.config)
        record = ledger.get(GH_REPO, issue)
        if not record:
            raise AssertionError(f"dispatch record missing for issue #{issue}")
        return record


class DispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.h = DispatchHarness(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_valid_request_executes_once_and_publishes_verified_link(self) -> None:
        self.h.add_issue(number=1, body=request_body())
        first = self.h.dispatch("--force")
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["executed"], 1)
        self.assertEqual(first["uploaded"], 1)
        self.assertEqual(first["published"], 1)
        calls = self.h.opencode_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["cwd"], str(self.h.project_path.resolve()))
        self.assertEqual(calls[0]["PWD"], str(self.h.project_path.resolve()))
        self.assertEqual(calls[0]["model"], "provider/model-fast")
        self.assertEqual(calls[0]["agent"], "plan")
        self.assertEqual(calls[0]["format"], "json")
        record = self.h.record(1)
        self.assertEqual(record["status"], "ISSUE_UPDATED")
        self.assertEqual(record["project"]["name"], "Alpha")
        task_id = record["capture"]["task_id"]
        run_id = record["capture"]["run_id"]
        complete = json.loads(
            (self.h.task_dir_for("Alpha", task_id) / "RUNS" / run_id / "COMPLETE.json").read_text(encoding="utf-8")
        )
        self.assertEqual(complete["agent"]["session_id"], "ses_fake_00000000000000000000000000")
        self.assertEqual(complete["agent"]["model"], "provider/model-fast")
        self.assertTrue(complete["upload"]["verified"])
        self.assertTrue(complete["upload"]["report_url"].startswith("https://drive.google.com/file/d/"))
        receipt = self.h.task_dir_for("Alpha", task_id) / "RUNS" / run_id / "UPLOAD-RECEIPT.json"
        self.assertTrue(receipt.exists())
        remote_complete = json.loads(
            self.h.remote("TASKS", complete["upload"]["remote_dir"].split("/")[1], "RUNS", run_id, "COMPLETE.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(remote_complete["upload"]["verified"])
        self.assertEqual(remote_complete["upload"]["report_url"], complete["upload"]["report_url"])
        comments = self.h.comments(1)
        self.assertEqual(len(comments), 1)
        self.assertIn("Agent Handoff", comments[0]["body"])
        self.assertIn(complete["upload"]["report_url"], comments[0]["body"])
        self.assertIn("verified", comments[0]["body"].lower())

        second = self.h.dispatch("--force")
        self.assertEqual(second["executed"], 0)
        self.assertEqual(len(self.h.opencode_calls()), 1)
        self.assertEqual(len(self.h.comments(1)), 1)

    def test_unauthorized_unknown_project_unknown_model_and_same_session_are_rejected(self) -> None:
        self.h.add_issue(number=1, body=request_body(), author="mallory")
        self.h.add_issue(number=2, body=request_body(project="ProjectZ"))
        self.h.add_issue(number=3, body=request_body(model="missing"))
        self.h.add_issue(number=4, body=request_body(session="same"))
        self.h.add_issue(number=5, body=request_body(agent="agy"))
        self.h.add_issue(
            number=6,
            body=request_body().replace(
                "session: new",
                "session: new\ncommand: rm -rf /",
            ),
        )
        result = self.h.dispatch("--force")
        self.assertEqual(result["executed"], 0)
        self.assertEqual(result["rejected"], 6)
        self.assertEqual(self.h.opencode_calls(), [])
        self.assertEqual(len(self.h.comments(1)), 0)
        for number in (2, 3, 4, 5, 6):
            comments = self.h.comments(number)
            self.assertEqual(len(comments), 1)
            self.assertIn("rejected", comments[0]["body"].lower())
        self.assertIn("SAME SESSION", self.h.comments(4)[0]["body"])
        self.assertIn("unsupported agent", self.h.comments(5)[0]["body"])
        self.assertIn("unknown request metadata field", self.h.comments(6)[0]["body"])
        again = self.h.dispatch("--force")
        self.assertEqual(again["executed"], 0)
        for number in (2, 3, 4, 5, 6):
            self.assertEqual(len(self.h.comments(number)), 1)

    def test_projects_are_isolated_and_config_rejects_cross_project_workspace(self) -> None:
        self.h.add_issue(number=1, body=request_body(project="Alpha", instructions="Alpha only marker"))
        self.h.add_issue(number=2, body=request_body(project="Beta", instructions="Beta only marker"))
        result = self.h.dispatch("--force")
        self.assertEqual(result["executed"], 2)
        calls = self.h.opencode_calls()
        self.assertEqual(calls[0]["cwd"], str(self.h.project_path.resolve()))
        self.assertEqual(calls[0]["PWD"], str(self.h.project_path.resolve()))
        self.assertEqual(calls[1]["cwd"], str(self.h.beta_path.resolve()))
        self.assertEqual(calls[1]["PWD"], str(self.h.beta_path.resolve()))
        alpha = self.h.record(1)
        beta = self.h.record(2)
        self.assertEqual(alpha["project"]["name"], "Alpha")
        self.assertEqual(beta["project"]["name"], "Beta")
        self.assertNotEqual(alpha["capture"]["run_id"], beta["capture"]["run_id"])
        alpha_report = self.h.task_dir_for("Alpha", alpha["capture"]["task_id"])
        beta_report = self.h.task_dir_for("Beta", beta["capture"]["task_id"])
        self.assertTrue((alpha_report / "RUNS" / alpha["capture"]["run_id"] / "REPORT.md").exists())
        self.assertTrue((beta_report / "RUNS" / beta["capture"]["run_id"] / "REPORT.md").exists())
        self.assertFalse((alpha_report / "RUNS" / beta["capture"]["run_id"]).exists())
        self.assertFalse((beta_report / "RUNS" / alpha["capture"]["run_id"]).exists())

        bad = dict(self.h.config)
        bad["dispatch"] = json.loads(json.dumps(bad["dispatch"]))
        bad["dispatch"]["projects"]["Beta"]["workspace"] = str(self.h.project_path)
        self.h.config = bad
        self.h.write_config()
        validation = self.h.jrun(["config", "validate", "--json"], check=False)
        self.assertFalse(validation["ok"])
        self.assertTrue(any("cross-project" in item for item in validation["errors"]))

    def test_upload_failure_retries_without_restarting_opencode(self) -> None:
        self.h.add_issue(number=1, body=request_body())
        marker = self.root / "fail-upload-once"
        marker.write_text("1", encoding="utf-8")
        first = self.h.dispatch(extra_env={"FAKE_FAIL_ONCE_FILE": str(marker)}, check=False)
        self.assertEqual(first["executed"], 1)
        self.assertEqual(first["uploaded"], 0)
        self.assertEqual(len(self.h.opencode_calls()), 1)
        record = self.h.record(1)
        self.assertEqual(record["status"], "UPLOAD_PENDING")
        self.assertEqual(record["capture"]["status"], "CAPTURED")
        self.assertFalse(record["upload"]["verified"])
        second = self.h.dispatch("--force")
        self.assertEqual(second["executed"], 0)
        self.assertEqual(second["uploaded"], 1)
        self.assertEqual(len(self.h.opencode_calls()), 1)
        self.assertTrue(self.h.record(1)["upload"]["verified"])

    def test_github_publication_failure_retries_without_reexecution_or_duplicate(self) -> None:
        self.h.add_issue(number=1, body=request_body())
        marker = self.root / "fail-comment-once"
        marker.write_text("1", encoding="utf-8")
        first = self.h.dispatch(extra_env={"FAKE_GH_FAIL_COMMENT_FILE": str(marker)}, check=False)
        self.assertEqual(first["executed"], 1)
        self.assertEqual(first["published"], 0)
        self.assertEqual(len(self.h.opencode_calls()), 1)
        record = self.h.record(1)
        self.assertEqual(record["status"], "UPLOAD_VERIFIED")
        self.assertEqual(record["publication"]["status"], "FAILED")
        self.assertTrue(record["upload"]["verified"])
        second = self.h.dispatch("--force")
        self.assertEqual(second["executed"], 0)
        self.assertEqual(second["published"], 1)
        self.assertEqual(len(self.h.opencode_calls()), 1)
        self.assertEqual(len(self.h.comments(1)), 1)
        third = self.h.dispatch("--force")
        self.assertEqual(third["executed"], 0)
        self.assertEqual(len(self.h.comments(1)), 1)

    def test_crash_after_claim_requires_attention_without_execution(self) -> None:
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
        ledger.set_status(GH_REPO, 1, "CLAIMED", note="simulated crash after claim", updates={"execution": {"status": "CLAIMED"}})
        result = self.h.dispatch("--force", check=False)
        self.assertEqual(result["executed"], 0)
        self.assertEqual(result["needs_attention"], 1)
        self.assertEqual(self.h.opencode_calls(), [])
        self.assertEqual(self.h.record(1)["status"], "NEEDS_ATTENTION")

    def test_restart_after_verified_upload_publishes_without_execution(self) -> None:
        item = self.h.create_run("opencode", "Resume delivery", "PROMPT-RESUME", "Resume delivery report.")
        self.h.complete(item)
        self.h.jrun(["retry-upload", "--force", "--json"])
        complete = self.h.read_complete(item["task"]["task_id"], item["run"]["run_id"])
        self.assertTrue(complete["upload"]["verified"])
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
            model=complete["agent"]["model"],
            request={"version": "1", "instructions": "stored request"},
        )
        record["capture"].update(
            {
                "status": "CAPTURED",
                "task_id": item["task"]["task_id"],
                "run_id": item["run"]["run_id"],
                "report_path": str(Path(item["report"])),
            }
        )
        record["session"]["id"] = complete["agent"]["session_id"]
        record["upload"].update({"status": "VERIFIED", "verified": True, "verified_at": complete["upload"]["verified_at"]})
        record["status"] = "UPLOAD_VERIFIED"
        ledger.create(record)
        result = self.h.dispatch("--force")
        self.assertEqual(result["executed"], 0)
        self.assertEqual(result["published"], 1)
        self.assertEqual(self.h.opencode_calls(), [])
        self.assertEqual(len(self.h.comments(1)), 1)

    def test_manual_delivery_reconciles_stale_remote_complete_without_execution(self) -> None:
        item = self.h.create_run("opencode", "Historical reconciliation", "PROMPT-HIST", "Historical report.")
        self.h.complete(item)
        self.h.jrun(["retry-upload", "--force", "--json"])
        complete = self.h.read_complete(item["task"]["task_id"], item["run"]["run_id"])
        self.assertTrue(complete["upload"]["verified"])
        task_id = item["task"]["task_id"]
        run_id = item["run"]["run_id"]
        remote_dir = complete["upload"]["remote_dir"].split("/")[1]
        stale = dict(complete)
        stale["upload"] = dict(complete["upload"])
        stale["upload"]["status"] = "pending"
        stale["upload"]["verified"] = False
        stale["upload"]["verified_at"] = None
        self.h.remote("TASKS", remote_dir, "RUNS", run_id, "COMPLETE.json").write_text(
            json.dumps(stale, indent=2), encoding="utf-8"
        )
        self.h.add_issue(number=1, body="historical pilot request without the new metadata block")
        result = self.h.jrun(
            [
                "dispatch",
                "deliver",
                "--task",
                task_id,
                "--run",
                run_id,
                "--project",
                "Alpha",
                "--issue",
                "1",
                "--repository",
                GH_REPO,
                "--json",
            ]
        )
        self.assertTrue(result["ok"])
        self.assertFalse(result["opencode_executed"])
        self.assertTrue(result["report_url"].startswith("https://drive.google.com/file/d/"))
        self.assertTrue(result["receipt_url"].startswith("https://drive.google.com/file/d/"))
        self.assertEqual(self.h.opencode_calls(), [])
        updated = self.h.read_complete(task_id, run_id)
        self.assertEqual(updated["upload"]["report_url"], result["report_url"])
        self.assertTrue(updated["upload"]["receipt_verified"])
        remote_complete = json.loads(self.h.remote("TASKS", remote_dir, "RUNS", run_id, "COMPLETE.json").read_text())
        self.assertTrue(remote_complete["upload"]["verified"])
        self.assertEqual(remote_complete["upload"]["report_url"], result["report_url"])
        remote_receipt = self.h.remote("TASKS", remote_dir, "RUNS", run_id, "UPLOAD-RECEIPT.json")
        self.assertTrue(remote_receipt.exists())
        self.assertEqual(len(self.h.comments(1)), 1)
        again = self.h.jrun(
            [
                "dispatch",
                "deliver",
                "--task",
                task_id,
                "--run",
                run_id,
                "--project",
                "Alpha",
                "--issue",
                "1",
                "--repository",
                GH_REPO,
                "--json",
            ]
        )
        self.assertTrue(again["reused_comment"])
        self.assertEqual(len(self.h.comments(1)), 1)

    def test_write_mode_requires_explicit_permission_pair(self) -> None:
        bad = json.loads(json.dumps(self.h.config))
        bad["dispatch"]["execution"]["permission_profile"] = "workspace_write"
        bad["dispatch"]["execution"]["allow_write"] = False
        self.h.config = bad
        self.h.write_config()
        validation = self.h.jrun(["config", "validate", "--json"], check=False)
        self.assertFalse(validation["ok"])
        self.assertTrue(any("workspace_write" in item for item in validation["errors"]))
        bad["dispatch"]["execution"]["permission_profile"] = "read_only"
        bad["dispatch"]["execution"]["allow_write"] = True
        self.h.config = bad
        self.h.write_config()
        validation = self.h.jrun(["config", "validate", "--json"], check=False)
        self.assertFalse(validation["ok"])
        self.assertTrue(any("allow_write" in item for item in validation["errors"]))

    def test_dispatch_disabled_leaves_core_capture_working(self) -> None:
        self.h.config["dispatch"]["enabled"] = False
        self.h.write_config()
        validation = self.h.jrun(["config", "validate", "--json"])
        self.assertTrue(validation["ok"])
        self.assertFalse(validation["dispatch"]["enabled"])
        blocked = self.h.run(["dispatch", "run", "--once", "--json"], check=False)
        self.assertNotEqual(blocked.returncode, 0)
        item = self.h.create_run("opencode", "Core capture", "PROMPT-CORE", "Core capture report.")
        self.h.complete(item)
        self.assertTrue(self.h.read_complete(item["task"]["task_id"], item["run"]["run_id"])["upload"]["verified"] is False)
        self.h.jrun(["retry-upload", "--force", "--json"])
        self.assertTrue(self.h.read_complete(item["task"]["task_id"], item["run"]["run_id"])["upload"]["verified"])

    def test_example_config_is_sanitized_and_dispatcher_is_off(self) -> None:
        main = json.loads((ROOT / "config" / "config.example.json").read_text(encoding="utf-8"))
        two = json.loads((ROOT / "config" / "config.dispatch.example.json").read_text(encoding="utf-8"))
        self.assertFalse(main["dispatch"]["enabled"])
        self.assertFalse(two["dispatch"]["enabled"])
        for data in (main, two):
            text = json.dumps(data)
            for forbidden in ("geekammar", "Rabet", "Yakfi", "/root/projects", "deepseek/deepseek-flash"):
                self.assertNotIn(forbidden, text)
        self.assertEqual(set(two["dispatch"]["projects"]), {"ProjectA", "ProjectB"})


if __name__ == "__main__":
    unittest.main()
