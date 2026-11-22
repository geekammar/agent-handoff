from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "lib") not in sys.path:
    sys.path.insert(0, str(ROOT / "lib"))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from support import Harness  # noqa: E402
from test_dispatch import DispatchHarness, GH_REPO, request_body  # noqa: E402

from agent_handoff.dispatch.adapters.base import ExecutionResult  # noqa: E402
from agent_handoff.dispatch.coding.changes import (  # noqa: E402
    ChangeEntry,
    SafeGit,
    parse_status,
    scan_bytes_for_secrets,
    validate_changes,
)
from agent_handoff.dispatch.coding.config import (  # noqa: E402
    parse_coding_config,
    prefix_valid,
    validate_branch_name,
)
from agent_handoff.dispatch.coding.pipeline import CodingOrchestrator  # noqa: E402
from agent_handoff.dispatch.coding.milestones import milestone_state  # noqa: E402
from agent_handoff.dispatch.coding.publication import (  # noqa: E402
    PublicationError,
    TrustedPublisher,
    build_branch,
    build_pr_body,
    build_pr_title,
    pr_marker,
)
from agent_handoff.dispatch.coding.report import coding_outcome, coding_report_sections  # noqa: E402
from agent_handoff.dispatch.coding.testrunner import (  # noqa: E402
    TestExecutionError,
    build_test_environment,
    run_command_bounded,
    run_test_profile,
)
from agent_handoff.dispatch.coding.workspace import (  # noqa: E402
    WorkspacePreparationError,
    _inspect_existing_workspace,
    cleanup_workspace,
    prepare_workspace,
)
from agent_handoff.dispatch.config import validate_dispatch_config  # noqa: E402
from agent_handoff.dispatch.ledger import Ledger, new_coding_state, new_dispatch_record, safe_record_summary  # noqa: E402
from agent_handoff.dispatch.status import format_status_view, record_status_view  # noqa: E402
from agent_handoff.dispatch.requests import (  # noqa: E402
    RequestError,
    parse_issue_request,
    resolve_request,
)
from agent_handoff.dispatch.runner import DispatchRunner  # noqa: E402
from agent_handoff.dispatch.security import (  # noqa: E402
    DEFAULT_CHILD_PATH,
    IsolationError,
    build_isolated_environment,
    popen_isolation_kwargs,
)
from agent_handoff.notify.messages import DELIVERY_COMPLETED, build_telegram_message  # noqa: E402
from agent_handoff.notify.service import NotificationService  # noqa: E402
from agent_handoff.util import utcnow_iso  # noqa: E402

UID = 4242
GID = 4242
PROJECT_REPO = "dev/coding-repo"


def fake_identity() -> dict:
    home = "/srv/agent-handoff/home/agent-handoff-dev"
    return {
        "user": "agent-handoff-dev",
        "uid": UID,
        "group": "agent-handoff-dev",
        "gid": GID,
        "groups": [],
        "home": home,
        "config_home": f"{home}/.config",
        "data_home": f"{home}/.local/share",
        "cache_home": f"{home}/.cache",
        "tmp_dir": f"{home}/tmp",
        "child_path": DEFAULT_CHILD_PATH,
        "model_auth_path": f"{home}/.local/share/opencode/auth.json",
    }


def git(args: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
        env={
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        },
    )


def make_repository(root: Path, *, origin: str = f"https://github.com/{PROJECT_REPO}.git") -> dict:
    source = root / "source"
    source.mkdir(parents=True)
    git(["init", "-q", "-b", "main"], source)
    (source / "README.md").write_text("# Demo\n", encoding="utf-8")
    (source / "src").mkdir()
    (source / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
    (source / "tests").mkdir()
    (source / "tests" / "test_app.py").write_text("def test_value():\n    assert True\n", encoding="utf-8")
    git(["add", "."], source)
    git(["commit", "-q", "-m", "initial"], source)
    git(["remote", "add", "origin", origin], source)
    base = git(["rev-parse", "HEAD"], source).stdout.strip()
    bare = root / "remote.git"
    git(["clone", "-q", "--bare", str(source), str(bare)], root)
    return {"source": source, "bare": bare, "base": base}


def coding_global(**overrides) -> dict:
    value = {
        "enabled": True,
        "staging_root": None,
        "limits": {
            "test_timeout_seconds": 10,
            "max_files_changed": 10,
            "max_diff_bytes": 200000,
            "max_file_bytes": 50000,
            "max_output_bytes": 10000,
            "max_workspace_bytes": 100000000,
            "max_coding_tasks": 1,
            "max_publication_attempts": 2,
        },
        "publication": {"draft": True, "branch_prefix": "agent-handoff/", "max_attempts": 2},
        "test_profiles": {
            "safe": {"commands": [["python3", "-c", "print('ok')"]], "timeout_seconds": 10}
        },
    }
    value.update(overrides)
    return value


def project_coding(**overrides) -> dict:
    value = {
        "enabled": True,
        "repository": PROJECT_REPO,
        "base_branch": "main",
        "allowed_model_aliases": ["fast"],
        "test_profile": "safe",
        "cleanup_on_success": False,
        "protected_paths": [".github/workflows/**", "config/config.json"],
        "allowed_paths": [],
    }
    value.update(overrides)
    return value


def resolved_project(source: Path, work_root: Path, *, coding: dict | None = None) -> dict:
    return {
        "name": "Alpha",
        "core_name": "Alpha",
        "core_key": "alpha",
        "workspace": str(source),
        "drive_folder_id": "alpha_folder_0001",
        "execution": {
            "permission_profile": "read_only",
            "allow_write": False,
            "inherited": False,
            "isolation": {"enabled": True, "inherited": False},
        },
        "coding": coding or {},
    }


def parse_enabled(root: Path, source: Path, work_root: Path, **global_overrides) -> tuple[dict, list[str], dict]:
    errors: list[str] = []
    warnings: list[str] = []
    project = resolved_project(source, work_root)
    raw_global = coding_global(**global_overrides)
    raw_global["staging_root"] = str(root / "publication")
    raw_projects = {
        "Alpha": {"coding": project_coding(workspace_root=str(work_root / "tasks"), source_workspace=str(source))}
    }
    parsed = parse_coding_config(
        raw_global,
        raw_projects,
        {"Alpha": project},
        {"fast": "provider/model-fast", "review": "provider/model-review"},
        {"enabled": True, "identity": fake_identity(), "workspace_roots": [str(work_root)]},
        workspace_roots=[str(work_root)],
        base_dir=str(root / "agent-handoff"),
        check_paths=False,
        errors=errors,
        warnings=warnings,
    )
    return parsed, errors, project


class FakeStream:
    def __init__(self, data: bytes = b"") -> None:
        self._data = io.BytesIO(data)

    def read(self, size: int = -1) -> bytes:
        return self._data.read(size)

    def close(self) -> None:
        pass


class FakeProcess:
    def __init__(self, *, exit_code: int = 0, timeout_once: bool = False, always_timeout: bool = False, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.pid = 999999
        self.returncode = None
        self._exit_code = exit_code
        self._timeout_once = timeout_once
        self._always_timeout = always_timeout
        self.stdout = FakeStream(stdout)
        self.stderr = FakeStream(stderr)
        self.terminated = False

    def wait(self, timeout: float | None = None) -> int:
        if self._always_timeout and not self.terminated:
            raise subprocess.TimeoutExpired("fake", timeout or 1)
        if self._timeout_once:
            self._timeout_once = False
            raise subprocess.TimeoutExpired("fake", timeout or 1)
        self.returncode = self._exit_code
        return self._exit_code

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.terminated = True
        self.returncode = -9


class TestCodingConfigAndRequests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.repo = make_repository(self.root)
        (self.root / "publication").mkdir()
        (self.root / "tasks").mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_t02_coding_is_disabled_by_default(self) -> None:
        project = resolved_project(self.repo["source"], self.work)
        errors: list[str] = []
        parsed = parse_coding_config(
            {}, {}, {"Alpha": project}, {}, {"enabled": False, "identity": None, "workspace_roots": []},
            workspace_roots=[], base_dir=str(self.root), check_paths=False, errors=errors, warnings=[]
        )
        self.assertFalse(parsed["enabled"])
        self.assertFalse(project["coding"]["enabled"])
        with self.assertRaises(RequestError):
            resolve_request(
                parse_issue_request(
                    request_body().replace("session: new", "session: new\ntask: coding")
                ),
                {
                    "repository": GH_REPO,
                    "models": {"fast": "provider/model-fast"},
                    "default_model": "fast",
                    "execution": {"max_prompt_bytes": 50000},
                    "projects": {"Alpha": project},
                    "agent": "opencode",
                },
            )

    def test_t04_issue_cannot_override_trusted_coding_policy(self) -> None:
        body = request_body().replace("session: new", "session: new\ntask: coding\ncommand: rm -rf /")
        with self.assertRaises(RequestError) as ctx:
            parse_issue_request(body)
        self.assertIn("unknown request metadata field", str(ctx.exception))
        body = request_body().replace("session: new", "session: new\ntask: coding\ntest-profile: evil")
        with self.assertRaises(RequestError):
            parse_issue_request(body)
        for field_name in ("test_profile", "commands", "workspace", "uid", "timeout"):
            with self.subTest(field=field_name):
                body = request_body().replace("session: new", f"session: new\ntask: coding\n{field_name}: bad")
                with self.assertRaises(RequestError):
                    parse_issue_request(body)
        body = request_body().replace("session: new", "session: new\ntask: unknown")
        with self.assertRaises(RequestError) as ctx:
            parse_issue_request(body)
        self.assertIn("unknown task type", str(ctx.exception))

    def test_t05_valid_coding_request_requires_allowed_model_alias(self) -> None:
        _, errors, project = parse_enabled(self.root, self.repo["source"], self.work)
        self.assertEqual(errors, [])
        dispatch = {
            "repository": GH_REPO,
            "models": {"fast": "provider/model-fast", "review": "provider/model-review"},
            "default_model": "fast",
            "execution": {"max_prompt_bytes": 50000},
            "projects": {"Alpha": project},
            "agent": "opencode",
        }
        body = request_body().replace("session: new", "session: new\ntask: coding")
        resolved = resolve_request(parse_issue_request(body), dispatch)
        self.assertEqual(resolved.request.task, "coding")
        self.assertEqual(resolved.model, "provider/model-fast")
        bad = request_body(model="review").replace("session: new", "session: new\ntask: coding")
        with self.assertRaises(RequestError) as ctx:
            resolve_request(parse_issue_request(bad), dispatch)
        self.assertIn("allowed model alias", str(ctx.exception))

    def test_t06_coding_requires_non_root_isolation(self) -> None:
        project = resolved_project(self.repo["source"], self.work)
        errors: list[str] = []
        raw_global = coding_global()
        raw_global["staging_root"] = str(self.root / "publication")
        (self.root / "tasks").mkdir(exist_ok=True)
        parse_coding_config(
            raw_global,
            {"Alpha": {"coding": project_coding(workspace_root=str(self.root / "tasks"))}},
            {"Alpha": project},
            {"fast": "provider/model-fast"},
            {"enabled": False, "identity": None, "workspace_roots": []},
            workspace_roots=[],
            base_dir=str(self.root),
            check_paths=False,
            errors=errors,
            warnings=[],
        )
        self.assertTrue(any("requires effective isolation" in item for item in errors), errors)

    def test_t05_full_config_validation_accepts_explicit_isolated_coding_policy(self) -> None:
        h = DispatchHarness(self.root / "config-harness")
        config = json.loads(json.dumps(h.config))
        coding = project_coding(workspace_root=str(h.workspace / "coding-tasks"), source_workspace=str(h.project_path))
        config["projects"]["alpha"]["paths"] = [str(h.project_path)]
        config["dispatch"]["projects"]["Alpha"]["execution"] = {
            "permission_profile": "workspace_write",
            "allow_write": True,
            "isolation": {"enabled": True},
        }
        config["dispatch"]["projects"]["Alpha"]["coding"] = coding
        config["dispatch"]["execution"]["isolation"] = {
            "enabled": False,
            "environment": "allowlist",
            "identity": {
                "user": "agent-handoff-dev",
                "group": "agent-handoff-dev",
                "home": "/srv/agent-handoff/home/agent-handoff-dev",
            },
            "workspace_roots": [str(h.workspace)],
            "child_path": DEFAULT_CHILD_PATH,
        }
        config["dispatch"]["coding"] = {
            **coding_global(),
            "staging_root": str(self.root / "config-staging"),
            "publication": {"draft": True, "branch_prefix": "agent-handoff/", "max_attempts": 2},
        }
        account = SimpleNamespace(pw_uid=UID, pw_gid=GID, pw_name="agent-handoff-dev")
        group = SimpleNamespace(gr_gid=GID, gr_name="agent-handoff-dev")
        with mock.patch("agent_handoff.dispatch.security.pwd.getpwnam", return_value=account), mock.patch(
            "agent_handoff.dispatch.security.grp.getgrnam", return_value=group
        ), mock.patch("agent_handoff.dispatch.security.grp.getgrgid", return_value=group), mock.patch(
            "agent_handoff.dispatch.security.os.getgrouplist", return_value=[GID]
        ):
            resolved, errors, _warnings = validate_dispatch_config(config, check_paths=False)
        self.assertEqual(errors, [])
        project = resolved["projects"]["Alpha"]
        self.assertTrue(project["coding"]["enabled"])
        self.assertEqual(project["coding"]["repository"], PROJECT_REPO)
        self.assertEqual(project["coding"]["test_profile"], "safe")
        self.assertEqual(project["coding"]["allowed_model_aliases"], ["fast"])
        self.assertTrue(project["execution"]["isolation"]["enabled"])
        self.assertTrue(resolved["coding"]["enabled"])

    def test_r14_agent_failure_blocks_publication(self) -> None:
        _, errors, project = parse_enabled(self.root, self.repo["source"], self.work)
        self.assertEqual(errors, [])
        h = DispatchHarness(self.root / "agent-failure-harness")
        ledger = Ledger(h.config)
        record, _ = ledger.create(
            new_dispatch_record(
                repository=GH_REPO,
                issue={"number": 77, "title": "fail", "html_url": "https://github.test/issues/77"},
                project=project,
                agent="opencode",
                model="provider/model-fast",
                request={"task": "coding", "instructions": "change"},
                task_type="coding",
                coding={**new_coding_state(project), "capture": {"task_id": "t-fail", "run_id": "r-fail"}},
            )
        )
        ledger.update(GH_REPO, 77, lambda item: item["capture"].update({"task_id": "t-fail", "run_id": "r-fail"}))
        record = ledger.get(GH_REPO, 77)
        orchestrator = CodingOrchestrator(
            config=h.config,
            dispatch={"coding": {"enabled": True}, "github": {"command": "gh"}},
            ledger=ledger,
            record=record,
            project=project,
            identity=fake_identity(),
            adapter=SimpleNamespace(),
            git=SafeGit(identity=None, allow_file_protocol=True),
        )
        orchestrator.mark_agent_running(str(self.root / "events.jsonl"))
        orchestrator.mark_agent_completed(
            ExecutionResult(
                session_id="ses_failed",
                final_text="",
                termination_reason="error",
                exit_code=1,
                timed_out=False,
                outcome="failed",
                events_path=str(self.root / "events.jsonl"),
                event_count=1,
                error="provider error",
                started_at=utcnow_iso(),
                finished_at=utcnow_iso(),
            )
        )
        updated = orchestrator.continue_after_agent()
        self.assertEqual(updated["status"], "EXECUTION_COMPLETED")
        self.assertTrue(updated["coding"]["blocked"])
        self.assertEqual(updated["coding"]["blocked_stage"], "AGENT_COMPLETED")
        self.assertFalse(updated["coding"]["milestones"]["TESTS_PASSED"])

    def test_t01_legacy_request_contract_unchanged(self) -> None:
        request = parse_issue_request(request_body())
        self.assertEqual(request.task, "read_only")
        explicit = parse_issue_request(request_body().replace("session: new", "session: new\ntask: read_only"))
        self.assertEqual(explicit.task, "read_only")

    def test_t19_t20_protected_and_allowlisted_paths(self) -> None:
        workspace = self.work / "task"
        shutil.copytree(self.repo["source"], workspace)
        (workspace / ".github" / "workflows").mkdir(parents=True)
        (workspace / ".github" / "workflows" / "ci.yml").write_text("name: ci\n", encoding="utf-8")
        (workspace / "README.md").write_text("# Changed\n", encoding="utf-8")
        evidence = validate_changes(
            str(workspace),
            git=SafeGit(identity=None, allow_file_protocol=True),
            limits={"max_files_changed": 10, "max_file_bytes": 50000, "max_diff_bytes": 200000, "max_workspace_bytes": 1000000},
            protected_paths=[".github/workflows/**"],
            allowed_paths=["README.md"],
        )
        self.assertEqual(evidence["status"], "blocked")
        blocked = " ".join(evidence["blocked"])
        self.assertIn("protected path", blocked)
        self.assertIn("allowlist", blocked)

    def test_t21_secret_scan_blocks_and_redacts(self) -> None:
        workspace = self.work / "secret-task"
        shutil.copytree(self.repo["source"], workspace)
        fake_key = "AKIA" + "A" * 16
        (workspace / "src" / "app.py").write_text(f'KEY = "{fake_key}"\n', encoding="utf-8")
        evidence = validate_changes(
            str(workspace),
            git=SafeGit(identity=None, allow_file_protocol=True),
            limits={"max_files_changed": 10, "max_file_bytes": 50000, "max_diff_bytes": 200000, "max_workspace_bytes": 1000000},
            protected_paths=[],
        )
        self.assertEqual(evidence["status"], "blocked")
        self.assertIn("aws-access-key", evidence["secret_findings"][0])
        self.assertNotIn(fake_key, json.dumps(evidence))
        self.assertEqual(scan_bytes_for_secrets(b"-----BEGIN PRIVATE KEY-----"), ["private-key"])

    def test_t22_t23_branch_policy_rejects_collision_and_default_branch(self) -> None:
        branch = build_branch("agent-handoff/", "Alpha", 4, "t-abc123")
        self.assertTrue(branch.startswith("agent-handoff/alpha/issue-4-"))
        with self.assertRaises(Exception):
            validate_branch_name("main")
        self.assertFalse(prefix_valid("../agent-handoff/"))
        publisher = TrustedPublisher(
            repository=PROJECT_REPO,
            base_branch="main",
            branch_prefix="agent-handoff/",
            staging_root=str(self.root / "publication"),
            token_provider=lambda: "fake-token",
        )
        with self.assertRaises(PublicationError):
            publisher.push(branch="main", commit="0" * 40)
        with self.assertRaises(PublicationError):
            publisher.push(branch="untrusted/branch", commit="0" * 40)


class TestWorkspaceAndDiff(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.repo = make_repository(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _prepare(self, issue: int = 7, task_id: str = "t-abc123", source: Path | None = None):
        with mock.patch("agent_handoff.dispatch.coding.workspace.os.geteuid", return_value=0), mock.patch(
            "agent_handoff.dispatch.coding.workspace.os.chown"
        ):
            return prepare_workspace(
                workspace_root=str(self.work),
                repository=PROJECT_REPO,
                base_branch="main",
                base_commit=self.repo["base"],
                source=str(source or self.repo["source"]),
                issue_number=issue,
                task_id=task_id,
                identity=fake_identity(),
                git=SafeGit(identity=None, allow_file_protocol=True),
            )

    def test_t08_t11_t12_clean_unique_workspaces_at_verified_base(self) -> None:
        first = self._prepare(issue=1, task_id="t-one")
        second = self._prepare(issue=2, task_id="t-two")
        self.assertNotEqual(first["workspace"], second["workspace"])
        self.assertEqual(first["base_commit"], self.repo["base"])
        self.assertTrue((Path(first["workspace"]) / ".git").is_dir())
        status = git(["status", "--porcelain"], Path(first["workspace"])).stdout.strip()
        self.assertEqual(status, "")
        (Path(first["workspace"]) / "README.md").write_text("# Task one only\n", encoding="utf-8")
        self.assertEqual(git(["status", "--porcelain"], Path(second["workspace"])).stdout.strip(), "")
        with self.assertRaises(WorkspacePreparationError):
            self._prepare(issue=1, task_id="t-one")
        (Path(first["workspace"]) / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        evidence = validate_changes(
            str(Path(second["workspace"])),
            git=SafeGit(identity=None, allow_file_protocol=True),
            limits={"max_files_changed": 10, "max_file_bytes": 50000, "max_diff_bytes": 200000, "max_workspace_bytes": 1000000},
            protected_paths=[],
        )
        self.assertEqual(evidence["status"], "blocked")
        self.assertTrue(any("no changes" in item for item in evidence["blocked"]))

    def test_t10_workspace_symlink_is_rejected(self) -> None:
        source = self.root / "symlink-source"
        source.mkdir()
        git(["init", "-q", "-b", "main"], source)
        (source / "target.txt").write_text("safe\n", encoding="utf-8")
        os.symlink("target.txt", source / "link.txt")
        git(["add", "."], source)
        git(["commit", "-q", "-m", "symlink"], source)
        git(["remote", "add", "origin", f"https://github.com/{PROJECT_REPO}.git"], source)
        base = git(["rev-parse", "HEAD"], source).stdout.strip()
        with mock.patch("agent_handoff.dispatch.coding.workspace.os.geteuid", return_value=0), mock.patch(
            "agent_handoff.dispatch.coding.workspace.os.chown"
        ):
            with self.assertRaises(WorkspacePreparationError):
                prepare_workspace(
                    workspace_root=str(self.work),
                    repository=PROJECT_REPO,
                    base_branch="main",
                    base_commit=base,
                    source=str(source),
                    issue_number=3,
                    task_id="t-symlink",
                    identity=fake_identity(),
                    git=SafeGit(identity=None, allow_file_protocol=True),
                )

    def _artifact_fixture(self, *, issue: int = 31, task_id: str = "t-artifact") -> tuple[TrustedPublisher, dict, Path]:
        prepared = self._prepare(issue=issue, task_id=task_id)
        workspace = Path(prepared["workspace"])
        (workspace / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")
        (workspace / "src" / "feature.py").write_text("FEATURE = True\n", encoding="utf-8")
        evidence = validate_changes(
            str(workspace),
            git=SafeGit(identity=None, allow_file_protocol=True),
            limits={"max_files_changed": 10, "max_file_bytes": 50000, "max_diff_bytes": 200000, "max_workspace_bytes": 1000000},
            protected_paths=[],
        )
        self.assertEqual(evidence["status"], "passed", evidence)
        staging = self.root / f"publication-{task_id}"
        staging.mkdir(exist_ok=True)
        os.chmod(staging, 0o700)
        publisher = TrustedPublisher(
            repository=PROJECT_REPO,
            base_branch="main",
            branch_prefix="agent-handoff/",
            staging_root=str(staging),
            token_provider=lambda: "fake-token",
            remote_url=f"file://{self.repo['bare']}",
            git=SafeGit(identity=None, allow_file_protocol=True),
            max_file_bytes=50000,
        )
        with mock.patch.object(publisher, "verify_repository", return_value={"repository": PROJECT_REPO}):
            publisher.prepare(base_commit=self.repo["base"], task_id=task_id, run_id=f"r-{task_id}")
        return publisher, evidence, workspace

    def _commit_fixture(self, publisher: TrustedPublisher, evidence: dict, workspace: Path, *, task_id: str = "t-artifact") -> dict:
        return publisher.stage_and_commit(
            validation=evidence,
            task_workspace=str(workspace),
            project="Alpha",
            title="Artifact integrity",
            issue_number=31,
            task_id=task_id,
            run_id=f"r-{task_id}",
            base_commit=self.repo["base"],
        )

    def test_t01_content_changed_after_validation_is_rejected(self) -> None:
        publisher, evidence, workspace = self._artifact_fixture(task_id="t-t01")
        (workspace / "src" / "app.py").write_text("value = 9\n", encoding="utf-8")
        with self.assertRaises(PublicationError) as ctx:
            self._commit_fixture(publisher, evidence, workspace, task_id="t-t01")
        self.assertIn("artifact changed", str(ctx.exception))

    def test_t02_size_changed_after_validation_is_rejected(self) -> None:
        publisher, evidence, workspace = self._artifact_fixture(task_id="t-t02")
        with (workspace / "src" / "app.py").open("a", encoding="utf-8") as handle:
            handle.write("# extra\n")
        with self.assertRaises(PublicationError) as ctx:
            self._commit_fixture(publisher, evidence, workspace, task_id="t-t02")
        self.assertIn("artifact changed", str(ctx.exception))

    def test_t03_mode_changed_after_validation_is_rejected(self) -> None:
        publisher, evidence, workspace = self._artifact_fixture(task_id="t-t03")
        os.chmod(workspace / "src" / "app.py", 0o755)
        with self.assertRaises(PublicationError) as ctx:
            self._commit_fixture(publisher, evidence, workspace, task_id="t-t03")
        self.assertIn("artifact changed", str(ctx.exception))

    def test_t04_symlink_substitution_during_publication_is_rejected(self) -> None:
        publisher, evidence, workspace = self._artifact_fixture(task_id="t-t04")
        target = workspace / "src" / "app.py"
        target.unlink()
        os.symlink("feature.py", target)
        with self.assertRaises(PublicationError):
            self._commit_fixture(publisher, evidence, workspace, task_id="t-t04")

    def test_t05_manifest_digest_mismatch_is_rejected(self) -> None:
        publisher, evidence, workspace = self._artifact_fixture(task_id="t-t05")
        tampered = json.loads(json.dumps(evidence))
        tampered["manifest"]["files"][1]["sha256"] = "0" * 64
        import hashlib

        canonical = json.dumps(tampered["manifest"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        tampered["manifest_sha256"] = hashlib.sha256(canonical).hexdigest()
        with self.assertRaises(PublicationError):
            self._commit_fixture(publisher, tampered, workspace, task_id="t-t05")

    def test_t06_committed_tree_matches_manifest(self) -> None:
        publisher, evidence, workspace = self._artifact_fixture(task_id="t-t06")
        committed = self._commit_fixture(publisher, evidence, workspace, task_id="t-t06")
        staging = Path(committed["staging"])
        for item in evidence["manifest"]["files"]:
            data = subprocess.run(
                ["git", "cat-file", "-p", f"HEAD:{item['path']}"],
                cwd=staging,
                capture_output=True,
                check=True,
            ).stdout
            self.assertEqual(__import__("hashlib").sha256(data).hexdigest(), item["sha256"])
            tree = subprocess.run(
                ["git", "ls-tree", "HEAD", "--", item["path"]], cwd=staging, capture_output=True, text=True, check=True
            ).stdout
            self.assertIn("100755" if item["mode"] == "0755" else "100644", tree)

    def test_t07_unexpected_staged_file_is_refused(self) -> None:
        publisher, evidence, workspace = self._artifact_fixture(task_id="t-t07")
        staging = Path(publisher._staging)
        (staging / "evil.txt").write_text("unexpected\n", encoding="utf-8")
        subprocess.run(["git", "add", "evil.txt"], cwd=staging, check=True)
        with self.assertRaises(PublicationError) as ctx:
            self._commit_fixture(publisher, evidence, workspace, task_id="t-t07")
        self.assertIn("staged publication paths", str(ctx.exception))

    def test_t08_parent_git_hooks_cannot_execute(self) -> None:
        publisher, evidence, workspace = self._artifact_fixture(task_id="t-t08")
        marker = self.root / "hook-ran"
        hook = Path(publisher._staging) / ".git" / "hooks" / "pre-commit"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
        os.chmod(hook, 0o755)
        self._commit_fixture(publisher, evidence, workspace, task_id="t-t08")
        self.assertFalse(marker.exists())

    def test_t09_git_filters_cannot_execute_as_parent(self) -> None:
        publisher, evidence, workspace = self._artifact_fixture(task_id="t-t09")
        marker = self.root / "filter-ran"
        script = self.root / "evil-filter.sh"
        script.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n", encoding="utf-8")
        os.chmod(script, 0o755)
        config = workspace / ".git" / "config"
        with config.open("a", encoding="utf-8") as handle:
            handle.write(f"\n[filter \"evil\"]\n\tclean = {script}\n\tsmudge = {script}\n")
        (workspace / ".gitattributes").write_text("*.py filter=evil\n", encoding="utf-8")
        self._commit_fixture(publisher, evidence, workspace, task_id="t-t09")
        self.assertFalse(marker.exists())

    def test_t10_untrusted_fsmonitor_configuration_cannot_execute(self) -> None:
        prepared = self._prepare(issue=32, task_id="t-t10")
        workspace = Path(prepared["workspace"])
        marker = self.root / "fsmonitor-ran"
        script = self.root / "evil-fsmonitor.sh"
        script.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
        os.chmod(script, 0o755)
        config = workspace / ".git" / "config"
        with config.open("a", encoding="utf-8") as handle:
            handle.write(f"\n[core]\n\tfsmonitor = {script}\n")
        (workspace / "src" / "app.py").write_text("value = 7\n", encoding="utf-8")
        evidence = validate_changes(
            str(workspace),
            git=SafeGit(identity=None, allow_file_protocol=True),
            limits={"max_files_changed": 10, "max_file_bytes": 50000, "max_diff_bytes": 200000, "max_workspace_bytes": 1000000},
            protected_paths=[],
        )
        self.assertEqual(evidence["status"], "passed", evidence)
        self.assertFalse(marker.exists())

    def test_s5_git_diff_driver_cannot_execute_as_parent(self) -> None:
        prepared = self._prepare(issue=33, task_id="t-diff-driver")
        workspace = Path(prepared["workspace"])
        marker = self.root / "diff-driver-ran"
        script = self.root / "evil-diff.sh"
        script.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n", encoding="utf-8")
        os.chmod(script, 0o755)
        config = workspace / ".git" / "config"
        with config.open("a", encoding="utf-8") as handle:
            handle.write(f"\n[diff \"evil\"]\n\tcommand = {script}\n")
        (workspace / ".gitattributes").write_text("*.py diff=evil\n", encoding="utf-8")
        (workspace / "src" / "app.py").write_text("value = 8\n", encoding="utf-8")
        evidence = validate_changes(
            str(workspace),
            git=SafeGit(identity=None, allow_file_protocol=True),
            limits={"max_files_changed": 10, "max_file_bytes": 50000, "max_diff_bytes": 200000, "max_workspace_bytes": 1000000},
            protected_paths=[],
        )
        self.assertEqual(evidence["status"], "blocked")
        self.assertFalse(marker.exists(), "repository diff drivers must not run as the parent")

    def test_t12_no_force_push_and_file_protocol_is_restricted(self) -> None:
        safe = SafeGit(identity=None)
        command = safe.command(["push", "origin", "abc:refs/heads/task"])
        self.assertNotIn("--force", command)
        self.assertNotIn("protocol.file.allow=always", command)
        self.assertIn("core.hooksPath=/dev/null", command)
        self.assertIn("credential.helper=", command)
        allowed = SafeGit(identity=None, allow_file_protocol=True).command(["clone", "src", "dst"])
        self.assertIn("protocol.file.allow=always", allowed)

    def test_recovery_adopts_only_clean_verified_workspace(self) -> None:
        prepared = self._prepare(issue=41, task_id="t-adopt")
        current_identity = {**fake_identity(), "uid": os.getuid(), "gid": os.getgid()}
        with mock.patch("agent_handoff.dispatch.coding.workspace.os.geteuid", return_value=0):
            adopted = prepare_workspace(
                workspace_root=str(self.work),
                repository=PROJECT_REPO,
                base_branch="main",
                base_commit=self.repo["base"],
                source=str(self.repo["source"]),
                issue_number=41,
                task_id="t-adopt",
                identity=current_identity,
                git=SafeGit(identity=None, allow_file_protocol=True),
                allow_existing=True,
            )
        self.assertTrue(adopted.get("recovered"))
        self.assertEqual(adopted["workspace"], prepared["workspace"])
        (Path(prepared["workspace"]) / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        with mock.patch("agent_handoff.dispatch.coding.workspace.os.geteuid", return_value=0):
            with self.assertRaises(WorkspacePreparationError):
                prepare_workspace(
                    workspace_root=str(self.work),
                    repository=PROJECT_REPO,
                    base_branch="main",
                    base_commit=self.repo["base"],
                    source=str(self.repo["source"]),
                    issue_number=41,
                    task_id="t-adopt",
                    identity=current_identity,
                    git=SafeGit(identity=None, allow_file_protocol=True),
                    allow_existing=True,
                )

    def test_t13_unpersisted_commit_is_safely_recreated_before_publication(self) -> None:
        publisher, evidence, workspace = self._artifact_fixture(task_id="t-t13")
        first = self._commit_fixture(publisher, evidence, workspace, task_id="t-t13")
        with mock.patch.object(publisher, "verify_repository", return_value={"repository": PROJECT_REPO}):
            publisher.prepare(base_commit=self.repo["base"], task_id="t-t13", run_id="r-t-t13")
        second = self._commit_fixture(publisher, evidence, workspace, task_id="t-t13")
        self.assertRegex(second["commit"], r"^[0-9a-f]{40,64}$")
        self.assertTrue((Path(second["staging"]) / ".git").is_dir())
        branches = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname)", "refs/heads"],
            cwd=self.repo["bare"], capture_output=True, text=True, check=True
        ).stdout.strip().splitlines()
        self.assertEqual([item for item in branches if item.startswith("refs/heads/agent-handoff/")], [])
        self.assertRegex(second["commit"], r"^[0-9a-f]{40,64}$")
        self.assertRegex(first["commit"], r"^[0-9a-f]{40,64}$")

    def test_t13_t24_diff_and_commit_from_validated_files(self) -> None:
        prepared = self._prepare(issue=9, task_id="t-change")
        workspace = Path(prepared["workspace"])
        (workspace / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")
        (workspace / "src" / "new.py").write_text("NEW = True\n", encoding="utf-8")
        evidence = validate_changes(
            str(workspace),
            git=SafeGit(identity=None, allow_file_protocol=True),
            limits={"max_files_changed": 10, "max_file_bytes": 50000, "max_diff_bytes": 200000, "max_workspace_bytes": 1000000},
            protected_paths=[".github/workflows/**"],
        )
        self.assertEqual(evidence["status"], "passed", evidence)
        self.assertEqual(evidence["changed_count"], 2)
        staging = self.root / "publication"
        staging.mkdir(exist_ok=True)
        os.chmod(staging, 0o700)
        publisher = TrustedPublisher(
            repository=PROJECT_REPO,
            base_branch="main",
            branch_prefix="agent-handoff/",
            staging_root=str(staging),
            token_provider=lambda: "fake-token",
            remote_url=f"file://{self.repo['bare']}",
            git=SafeGit(identity=None, allow_file_protocol=True),
        )
        with mock.patch.object(publisher, "verify_repository", return_value={"repository": PROJECT_REPO}):
            publisher.prepare(base_commit=self.repo["base"], task_id="t-change", run_id="r-change")
        committed = publisher.stage_and_commit(
            validation=evidence,
            task_workspace=str(workspace),
            project="Alpha",
            title="C2 change",
            issue_number=9,
            task_id="t-change",
            run_id="r-change",
            base_commit=self.repo["base"],
        )
        self.assertRegex(committed["commit"], r"^[0-9a-f]{40,64}$")
        files = git(["show", "--name-only", "--pretty=format:", committed["commit"]], Path(committed["staging"])).stdout
        self.assertIn("src/app.py", files)
        self.assertIn("src/new.py", files)
        self.assertNotIn("README.md", files)
        branch = build_branch("agent-handoff/", "Alpha", 9, "t-change")
        pushed = publisher.push(branch=branch, commit=committed["commit"])
        self.assertTrue(pushed["pushed"])
        remote_sha = git(["rev-parse", f"refs/heads/{branch}"], self.repo["bare"]).stdout.strip()
        self.assertEqual(remote_sha, committed["commit"])


class TestTestRunnerSecurity(unittest.TestCase):
    def _run(self, process: FakeProcess, *, identity: dict | None = None) -> dict:
        calls: list[dict] = []

        def spawn(argv, **kwargs):
            calls.append({"argv": argv, "kwargs": kwargs})
            return process

        result = run_command_bounded(
            ["python3", "-c", "print('ok')"],
            workspace="/tmp",
            environment={"PATH": "/usr/bin:/bin"},
            identity=identity or fake_identity(),
            timeout_seconds=5,
            max_output_bytes=1000,
            spawn=spawn,
        )
        result["_calls"] = calls
        return result

    def test_t07_test_environment_has_no_parent_secrets(self) -> None:
        parent = {
            "AGENT_HANDOFF_TELEGRAM_TOKEN": "secret",
            "GH_TOKEN": "secret",
            "RCLONE_CONFIG": "/secret",
            "LANG": "C.UTF-8",
        }
        env = build_test_environment(fake_identity(), "/tmp/work", {}, parent)
        self.assertNotIn("AGENT_HANDOFF_TELEGRAM_TOKEN", env)
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn("RCLONE_CONFIG", env)
        self.assertEqual(env["LANG"], "C.UTF-8")
        with self.assertRaises(TestExecutionError):
            build_test_environment(fake_identity(), "/tmp/work", {"GH_TOKEN": "leak"})

    def test_t14_trusted_profile_runs_with_argv_and_no_shell(self) -> None:
        process = FakeProcess(exit_code=0, stdout=b"ok\n")
        with mock.patch("agent_handoff.dispatch.coding.testrunner.os.geteuid", return_value=0), mock.patch(
            "agent_handoff.dispatch.coding.testrunner.popen_isolation_kwargs",
            return_value={"user": UID, "group": GID, "extra_groups": [], "umask": 0o077},
        ):
            result = run_test_profile(
                {"name": "safe", "commands": [["python3", "-c", "print('ok')"]], "timeout_seconds": 5, "env": {}},
                workspace="/tmp",
                identity=fake_identity(),
                limits={"test_timeout_seconds": 5, "max_output_bytes": 1000},
                spawn=lambda argv, **kwargs: process,
            )
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["commands"][0]["argv"], ["python3", "-c", "print('ok')"])

    def test_t16_test_timeout_and_t17_failure(self) -> None:
        timeout_process = FakeProcess(exit_code=-15, always_timeout=True)
        with mock.patch("agent_handoff.dispatch.coding.testrunner.os.geteuid", return_value=0), mock.patch(
            "agent_handoff.dispatch.coding.testrunner.popen_isolation_kwargs", return_value={}
        ), mock.patch("agent_handoff.dispatch.coding.testrunner.os.killpg", side_effect=OSError):
            timed = run_command_bounded(
                ["slow"],
                workspace="/tmp",
                environment={},
                identity=fake_identity(),
                timeout_seconds=1,
                max_output_bytes=100,
                spawn=lambda argv, **kwargs: timeout_process,
            )
        self.assertEqual(timed["status"], "timed_out")
        failed = self._run(FakeProcess(exit_code=2, stderr=b"boom\n"))
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["exit_code"], 2)
        self.assertIn("boom", failed["stderr"])

    def test_t18_root_test_identity_is_refused(self) -> None:
        with self.assertRaises(TestExecutionError):
            run_command_bounded(
                ["true"],
                workspace="/tmp",
                environment={},
                identity={**fake_identity(), "uid": 0, "gid": 0},
                timeout_seconds=1,
                max_output_bytes=100,
                spawn=lambda argv, **kwargs: FakeProcess(),
            )
        with self.assertRaises(IsolationError):
            popen_isolation_kwargs({"uid": 0, "gid": 0})


class TestProcessSafety(unittest.TestCase):
    def test_t19_sigterm_unwinding_kills_test_process_tree(self) -> None:
        class SystemExitProcess(FakeProcess):
            def __init__(self) -> None:
                super().__init__(exit_code=0)
                self.calls = 0

            def wait(self, timeout: float | None = None) -> int:
                self.calls += 1
                if self.calls == 1:
                    raise SystemExit(143)
                self.returncode = -9
                return -9

        process = SystemExitProcess()
        with mock.patch("agent_handoff.dispatch.coding.testrunner.os.killpg", side_effect=OSError):
            with self.assertRaises(SystemExit):
                run_command_bounded(
                    ["slow"],
                    workspace="/tmp",
                    environment={},
                    identity=fake_identity(),
                    timeout_seconds=10,
                    max_output_bytes=100,
                    spawn=lambda argv, **kwargs: process,
                )
        self.assertTrue(process.terminated)

    def test_t21_orphan_process_group_blocks_test_success(self) -> None:
        process = FakeProcess(exit_code=0)
        with mock.patch("agent_handoff.dispatch.coding.testrunner.process_group_alive", return_value=True), mock.patch(
            "agent_handoff.dispatch.coding.testrunner.terminate_process_group", return_value=True
        ):
            result = run_command_bounded(
                ["fast"],
                workspace="/tmp",
                environment={},
                identity=fake_identity(),
                timeout_seconds=10,
                max_output_bytes=100,
                spawn=lambda argv, **kwargs: process,
            )
        self.assertEqual(result["status"], "orphans")
        self.assertTrue(result["orphans_detected"])

    def test_t22_output_is_bounded_and_resource_limits_are_set(self) -> None:
        process = FakeProcess(exit_code=0, stdout=b"x" * 5000)
        result = run_command_bounded(
            ["chatty"],
            workspace="/tmp",
            environment={},
            identity=fake_identity(),
            timeout_seconds=10,
            max_output_bytes=100,
            spawn=lambda argv, **kwargs: process,
        )
        self.assertLessEqual(len(result["stdout"]), 100)
        self.assertTrue(result["output_truncated"])
        import resource
        from agent_handoff.dispatch.coding.testrunner import _preexec_limits

        with mock.patch.object(resource, "setrlimit") as setrlimit:
            _preexec_limits(10, memory_bytes=123456789, processes=77, file_bytes=987654321)()
        limits = {call.args[0] for call in setrlimit.call_args_list}
        for name in ("RLIMIT_CPU", "RLIMIT_NOFILE", "RLIMIT_AS", "RLIMIT_NPROC", "RLIMIT_FSIZE"):
            if hasattr(resource, name):
                self.assertIn(getattr(resource, name), limits)


class TestPublicationAndReporting(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.repo = make_repository(self.root)
        self.gh_dir = self.root / "fake-gh"
        self.gh_dir.mkdir()
        shutil.copy2(ROOT / "tests" / "fake-gh", self.gh_dir / "gh")
        os.chmod(self.gh_dir / "gh", 0o755)
        (self.gh_dir / "repo.json").write_text(
            json.dumps({"full_name": PROJECT_REPO, "private": True, "has_issues": True}), encoding="utf-8"
        )
        (self.gh_dir / "user.json").write_text(json.dumps({"login": "dev"}), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _publisher(self, remote: Path | None = None) -> TrustedPublisher:
        staging = self.root / "publication"
        staging.mkdir(exist_ok=True)
        os.chmod(staging, 0o700)
        return TrustedPublisher(
            repository=PROJECT_REPO,
            base_branch="main",
            branch_prefix="agent-handoff/",
            staging_root=str(staging),
            gh_command=str(self.gh_dir / "gh"),
            token_provider=lambda: "fake-token",
            remote_url=f"file://{remote or self.repo['bare']}",
            git=SafeGit(identity=None, allow_file_protocol=True),
        )

    def _commit_change(self, publisher: TrustedPublisher) -> tuple[str, dict]:
        workspace = self.work / "task"
        shutil.copytree(self.repo["source"], workspace)
        (workspace / "src" / "app.py").write_text("value = 3\n", encoding="utf-8")
        evidence = validate_changes(
            str(workspace),
            git=SafeGit(identity=None, allow_file_protocol=True),
            limits={"max_files_changed": 10, "max_file_bytes": 50000, "max_diff_bytes": 200000, "max_workspace_bytes": 1000000},
            protected_paths=[],
        )
        with mock.patch.dict(os.environ, {"FAKE_GH_DIR": str(self.gh_dir)}, clear=False):
            publisher.prepare(base_commit=self.repo["base"], task_id="t-pr", run_id="r-pr")
        committed = publisher.stage_and_commit(
            validation=evidence,
            task_workspace=str(workspace),
            project="Alpha",
            title="Draft PR test",
            issue_number=11,
            task_id="t-pr",
            run_id="r-pr",
            base_commit=self.repo["base"],
        )
        return committed["commit"], evidence

    def test_t25_t26_draft_pr_creation_is_idempotent(self) -> None:
        publisher = self._publisher()
        commit, evidence = self._commit_change(publisher)
        branch = build_branch("agent-handoff/", "Alpha", 11, "t-pr")
        body = build_pr_body(
            task_id="t-pr",
            run_id="r-pr",
            title="Draft PR test",
            issue_url="https://github.test/dev/inbox/issues/11",
            base_commit=self.repo["base"],
            branch=branch,
            validation=evidence,
            tests={"status": "passed", "profile": "safe", "commands": []},
        )
        self.assertIn(pr_marker("t-pr", "r-pr"), body)
        os.environ["FAKE_GH_DIR"] = str(self.gh_dir)
        try:
            first = publisher.create_or_reuse_pr(
                branch=branch, commit=commit, task_id="t-pr", run_id="r-pr", title="Draft", body=body
            )
            second = publisher.create_or_reuse_pr(
                branch=branch, commit=commit, task_id="t-pr", run_id="r-pr", title="Draft", body=body
            )
        finally:
            os.environ.pop("FAKE_GH_DIR", None)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        pulls = json.loads((self.gh_dir / "pulls.json").read_text(encoding="utf-8"))
        self.assertEqual(len(pulls), 1)
        self.assertTrue(pulls[0]["draft"])
        self.assertEqual(pulls[0]["base"]["ref"], "main")
        self.assertIn("No auto-merge", pulls[0]["body"])

    def test_t28_lost_pr_response_is_recovered_without_duplicate(self) -> None:
        publisher = self._publisher()
        commit, evidence = self._commit_change(publisher)
        branch = build_branch("agent-handoff/", "Alpha", 11, "t-pr")
        body = build_pr_body(
            task_id="t-pr", run_id="r-pr", title="Draft", issue_url=None,
            base_commit=self.repo["base"], branch=branch, validation=evidence,
            tests={"status": "passed", "profile": "safe", "commands": []},
        )
        marker = self.root / "lost-pr-once"
        marker.write_text("1", encoding="utf-8")
        os.environ["FAKE_GH_DIR"] = str(self.gh_dir)
        os.environ["FAKE_GH_LOST_PR_RESPONSE_FILE"] = str(marker)
        try:
            result = publisher.create_or_reuse_pr(
                branch=branch, commit=commit, task_id="t-pr", run_id="r-pr", title="Draft", body=body
            )
        finally:
            os.environ.pop("FAKE_GH_DIR", None)
            os.environ.pop("FAKE_GH_LOST_PR_RESPONSE_FILE", None)
        self.assertTrue(result.get("recovered"))
        pulls = json.loads((self.gh_dir / "pulls.json").read_text(encoding="utf-8"))
        self.assertEqual(len(pulls), 1)

    def test_t22_existing_branch_with_different_commit_is_refused(self) -> None:
        publisher = self._publisher()
        commit, _ = self._commit_change(publisher)
        branch = build_branch("agent-handoff/", "Alpha", 11, "t-pr")
        with mock.patch.object(publisher, "_remote_branch_sha", return_value="f" * 40):
            with self.assertRaises(PublicationError) as ctx:
                publisher.push(branch=branch, commit=commit)
        self.assertIn("different commit", str(ctx.exception))
        self.assertIn("refusing to force-push", str(ctx.exception))

    def test_t27_lost_push_response_recovery(self) -> None:
        publisher = self._publisher()
        commit, _ = self._commit_change(publisher)
        branch = build_branch("agent-handoff/", "Alpha", 11, "t-pr")
        remote_calls = {"value": 0}

        def remote_sha(branch_name: str, token: str) -> str | None:
            remote_calls["value"] += 1
            return None if remote_calls["value"] == 1 else commit

        with mock.patch.object(publisher, "_remote_branch_sha", side_effect=remote_sha):
            with mock.patch.object(
                publisher.git, "run", side_effect=[SimpleNamespace(returncode=1, stdout="", stderr="timeout")]
            ):
                result = publisher.push(branch=branch, commit=commit)
        self.assertTrue(result.get("recovered"))

    def test_t31_t32_report_comment_and_notification_include_pr(self) -> None:
        record = {
            "task_type": "coding",
            "repository": GH_REPO,
            "issue_number": 5,
            "issue_url": "https://github.test/issues/5",
            "project": {"name": "Alpha"},
            "agent": "opencode",
            "model": "provider/model-fast",
            "session": {"id": "ses_test"},
            "execution": {"outcome": "succeeded"},
            "capture": {"task_id": "t-test", "run_id": "r-test"},
            "upload": {
                "verified": True,
                "verified_at": "2026-01-01T00:00:00Z",
                "report_url": "https://drive.google.com/file/d/report123/view",
                "receipt_url": "https://drive.google.com/file/d/receipt123/view",
            },
            "coding": {
                "stage": "ISSUE_UPDATED",
                "base_commit": self.repo["base"],
                "blocked": False,
                "tests": {"status": "passed"},
                "publication": {
                    "branch": "agent-handoff/alpha/issue-5-t-test",
                    "commit": "a" * 40,
                    "pr_url": "https://github.test/dev/coding-repo/pull/7",
                },
                "milestones": {
                    "AGENT_COMPLETED": True,
                    "TESTS_PASSED": True,
                    "PR_PUBLISHED": True,
                    "REPORT_DELIVERED": True,
                },
            },
        }
        from agent_handoff.dispatch.delivery import build_issue_comment

        comment = build_issue_comment(record)
        self.assertIn("https://github.test/dev/coding-repo/pull/7", comment)
        self.assertIn("MD5/read-back", comment)
        message = build_telegram_message(
            "DELIVERY_COMPLETED",
            {
                "project": "Alpha",
                "task_title": "C2 task",
                "report_url": record["upload"]["report_url"],
                "issue_url": record["issue_url"],
                "outcome": "succeeded",
                "coding_outcome": "draft_pr_published",
                "tests_status": "passed",
                "pr_url": record["coding"]["publication"]["pr_url"],
            },
        )
        self.assertIn(record["coding"]["publication"]["pr_url"], message["text"])
        self.assertIn("CHATGPT", message["text"])

    def test_report_sections_include_coding_evidence(self) -> None:
        record = {
            "task_type": "coding",
            "request": {"instructions": "Add a feature"},
            "coding": {
                "stage": "TESTING",
                "repository": PROJECT_REPO,
                "base_branch": "main",
                "base_commit": self.repo["base"],
                "test_profile": "safe",
                "limits": {"max_files_changed": 10, "max_diff_bytes": 100},
                "tests": {
                    "status": "passed",
                    "profile": "safe",
                    "commands": [
                        {"argv": ["python3", "-m", "unittest"], "status": "passed", "exit_code": 0, "duration_seconds": 0.1}
                    ],
                },
                "diff": {
                    "status": "passed",
                    "changed_count": 1,
                    "files": [{"path": "src/app.py", "status": "modified", "size": 10}],
                    "blocked": [],
                },
                "publication": {"pr_url": "https://github.test/pull/1", "commit": "b" * 40, "branch": "agent-handoff/x"},
                "agent": {"status": "completed", "outcome": "succeeded"},
                "milestones": {"AGENT_COMPLETED": True, "TESTS_PASSED": True, "PR_PUBLISHED": True},
            },
        }
        text = coding_report_sections(record)
        self.assertIn("Coding task contract", text)
        self.assertIn("Test execution evidence", text)
        self.assertIn("Diff validation evidence", text)
        self.assertIn("Security restrictions and limits", text)
        self.assertIn("Unverified claims", text)
        self.assertEqual(coding_outcome(record), "draft_pr_published")


class TestDeliveryMilestoneReporting(unittest.TestCase):
    def _record(self, **coding_overrides) -> dict:
        coding = {
            "stage": "PR_CREATED",
            "base_commit": "a" * 40,
            "blocked": False,
            "tests": {"status": "passed"},
            "publication": {
                "branch": "agent-handoff/alpha/issue-5-t-test",
                "commit": "b" * 40,
                "pr_url": "https://github.test/dev/coding-repo/pull/7",
            },
            "milestones": {
                "AGENT_COMPLETED": True,
                "TESTS_PASSED": True,
                "DIFF_VALIDATED": True,
                "COMMIT_CREATED": True,
                "BRANCH_PUBLISHED": True,
                "PR_CREATED": True,
                "PR_PUBLISHED": True,
                "REPORT_VERIFIED": False,
                "ISSUE_DELIVERED": False,
                "REPORT_DELIVERED": False,
                "TELEGRAM_SENT": False,
            },
        }
        coding.update(coding_overrides)
        return {
            "task_type": "coding",
            "repository": GH_REPO,
            "issue_number": 5,
            "issue_url": "https://github.test/issues/5",
            "project": {"name": "Alpha"},
            "agent": "opencode",
            "model": "provider/model-fast",
            "session": {"id": "ses_test"},
            "execution": {"outcome": "succeeded"},
            "capture": {"task_id": "t-test", "run_id": "r-test"},
            "upload": {
                "verified": True,
                "verified_at": "2026-01-01T00:00:00Z",
                "report_url": "https://drive.google.com/file/d/report123/view",
                "receipt_url": "https://drive.google.com/file/d/receipt123/view",
            },
            "publication": {"status": "PENDING"},
            "coding": coding,
        }

    def test_t10_t11_pr_title_has_one_canonical_space_and_old_prefixes_work(self) -> None:
        from agent_handoff.dispatch.coding.config import _parse_publication

        errors: list[str] = []
        parsed = _parse_publication(
            {"pr_title_prefix": "Agent Handoff:"},
            {"pr_title_prefix": "Agent Handoff:"},
            "publication",
            errors,
        )
        self.assertEqual(parsed["pr_title_prefix"], "Agent Handoff:")
        self.assertEqual(build_pr_title(parsed["pr_title_prefix"], "C2 live acceptance"), "Agent Handoff: C2 live acceptance")
        self.assertEqual(build_pr_title("Agent Handoff: ", "C2 live acceptance"), "Agent Handoff: C2 live acceptance")
        self.assertEqual(build_pr_title("Agent Handoff:", "  C2 live acceptance  "), "Agent Handoff: C2 live acceptance")
        self.assertEqual(build_pr_title("[Bot]  ", "Task"), "[Bot] Task")
        self.assertEqual(build_pr_title(None, "Task"), "Agent Handoff: Task")
        self.assertEqual(build_pr_title("Agent Handoff:", ""), "Agent Handoff:")

    def test_t01_t02_t13_comment_shows_verified_pending_and_never_claims_own_ack(self) -> None:
        from agent_handoff.dispatch.delivery import build_issue_comment

        record = self._record()
        comment = build_issue_comment(record)
        self.assertIn("REPORT_VERIFIED=`true`", comment)
        self.assertIn("ISSUE_DELIVERED=`pending` (this comment)", comment)
        self.assertIn("TELEGRAM_SENT=`pending` (attempted after this comment)", comment)
        self.assertNotIn("REPORT_DELIVERED=", comment)
        self.assertNotIn("verified report delivered", comment)
        self.assertIn("attempted after this Issue comment", comment)
        self.assertIn("exactly-once Telegram delivery is not claimed", comment)

        record["coding"]["milestones"]["ISSUE_DELIVERED"] = True
        record["coding"]["milestones"]["REPORT_DELIVERED"] = True
        record["coding"]["milestones"]["TELEGRAM_SENT"] = True
        record["coding"]["stage"] = "NOTIFICATION_SENT"
        delivered = build_issue_comment(record)
        self.assertIn("ISSUE_DELIVERED=`true`", delivered)
        self.assertIn("TELEGRAM_SENT=`true`", delivered)

    def test_t09_historical_records_are_read_without_rewriting(self) -> None:
        record = self._record()
        record["coding"]["stage"] = "ISSUE_UPDATED"
        record["coding"]["milestones"] = {"REPORT_DELIVERED": True}
        before = json.loads(json.dumps(record))
        from agent_handoff.dispatch.delivery import build_issue_comment

        self.assertEqual(milestone_state(record["coding"], "ISSUE_DELIVERED"), "true")
        self.assertEqual(milestone_state(record["coding"], "REPORT_DELIVERED"), "true")
        self.assertEqual(milestone_state(record["coding"], "TELEGRAM_SENT"), "pending")
        summary = safe_record_summary(record)
        self.assertTrue(summary["coding"]["milestones"]["issue_delivered"])
        self.assertTrue(summary["coding"]["milestones"]["report_delivered"])
        self.assertFalse(summary["coding"]["milestones"]["telegram_sent"])
        comment = build_issue_comment(record)
        self.assertIn("ISSUE_DELIVERED=`true`", comment)
        self.assertEqual(record, before)

    def test_t12_blocked_coding_report_is_truthful(self) -> None:
        record = self._record()
        record["coding"].update(
            {
                "stage": "TESTING",
                "blocked": True,
                "blocked_stage": "TESTING",
                "blocked_reason": "trusted tests failed",
                "tests": {"status": "failed"},
                "publication": {"branch": None, "commit": None, "pr_url": None},
                "milestones": {
                    "AGENT_COMPLETED": True,
                    "TESTS_PASSED": False,
                    "DIFF_VALIDATED": False,
                    "COMMIT_CREATED": False,
                    "BRANCH_PUBLISHED": False,
                    "PR_CREATED": False,
                    "PR_PUBLISHED": False,
                    "REPORT_VERIFIED": False,
                    "ISSUE_DELIVERED": False,
                    "REPORT_DELIVERED": False,
                    "TELEGRAM_SENT": False,
                },
            }
        )
        from agent_handoff.dispatch.delivery import build_issue_comment

        comment = build_issue_comment(record)
        self.assertIn("Coding outcome: `blocked`", comment)
        self.assertIn("TESTS_PASSED=`false`", comment)
        self.assertIn("PR_PUBLISHED=`not_run`", comment)
        self.assertIn("REPORT_VERIFIED=`true`", comment)
        self.assertIn("ISSUE_DELIVERED=`pending` (this comment)", comment)
        self.assertIn("TELEGRAM_SENT=`pending` (attempted after this comment)", comment)
        self.assertNotIn("draft_pr_published", comment)
        report = coding_report_sections(record)
        self.assertIn("Coding outcome: `blocked:TESTING`", report)
        self.assertIn("Trusted tests passed: `false`", report)
        self.assertIn("Draft PR created: `not_run`", report)
        self.assertIn("Report verified: `pending`", report)
        self.assertIn("Issue delivered: `pending`", report)
        self.assertIn("Telegram sent: `pending`", report)
        self.assertIn("Blocked: `True`", report)

    def test_t14_read_only_delivery_comment_is_unchanged(self) -> None:
        from agent_handoff.dispatch.delivery import build_issue_comment

        record = {
            "task_type": "read_only",
            "repository": GH_REPO,
            "issue_number": 6,
            "project": {"name": "Alpha"},
            "agent": "opencode",
            "model": "provider/model-fast",
            "session": {"id": "ses_read"},
            "execution": {"outcome": "succeeded"},
            "capture": {"task_id": "t-read", "run_id": "r-read"},
            "upload": {
                "verified": True,
                "verified_at": "2026-01-01T00:00:00Z",
                "report_url": "https://drive.google.com/file/d/read123/view",
            },
        }
        comment = build_issue_comment(record)
        self.assertIn("verified report available", comment)
        self.assertIn("read123", comment)
        self.assertNotIn("Coding milestones", comment)
        self.assertNotIn("ISSUE_DELIVERED", comment)

    def test_t13_manual_delivery_helpers_advance_stage_and_milestones(self) -> None:
        from agent_handoff.dispatch.delivery import apply_issue_delivered, apply_upload_verified

        record = self._record()
        record["coding"]["stage"] = "PR_CREATED"
        apply_upload_verified(record)
        self.assertEqual(record["coding"]["stage"], "UPLOAD_VERIFIED")
        self.assertTrue(record["coding"]["milestones"]["REPORT_VERIFIED"])
        apply_issue_delivered(record)
        self.assertEqual(record["coding"]["stage"], "ISSUE_UPDATED")
        self.assertTrue(record["coding"]["milestones"]["ISSUE_DELIVERED"])
        self.assertTrue(record["coding"]["milestones"]["REPORT_DELIVERED"])

    def test_t15_milestone_json_outputs_remain_valid(self) -> None:
        record = self._record()
        record["coding"]["stage"] = "UPLOAD_VERIFIED"
        record["status"] = "UPLOAD_VERIFIED"
        summary = safe_record_summary(record)
        view = record_status_view(record)
        for value in (summary, view):
            text = json.dumps(value)
            self.assertIn("milestones", text)
            self.assertIsInstance(value["coding"]["milestones"], dict)


class TestTelegramMilestones(unittest.TestCase):
    TOKEN = "123456789:AAFakeCodingMilestoneToken_1234567890"

    class FakeTransport:
        def __init__(self) -> None:
            self.result: dict = {"ok": True, "message_id": 6}
            self.calls: list[dict] = []

        def send_message(self, chat_id: str, **message) -> dict:
            self.calls.append({"chat_id": chat_id, **message})
            return dict(self.result)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.h = DispatchHarness(self.root)
        self.h.config["notifications"] = {
            "telegram": {
                "enabled": True,
                "chat_id": "-1001234567890",
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
            {"AGENT_HANDOFF_TELEGRAM_TOKEN": self.TOKEN, "AGENT_HANDOFF_HOME": str(self.h.home)},
            clear=False,
        )
        self.env_patch.start()
        self.ledger = Ledger(self.h.config)
        project = {"name": "Alpha", "core_name": "Alpha", "core_key": "alpha", "coding": {"enabled": True}}
        record = new_dispatch_record(
            repository=GH_REPO,
            issue={"number": 7, "html_url": "https://github.test/issues/7", "title": "Coding telegram"},
            project=project,
            agent="opencode",
            model="provider/model-fast",
            request={"instructions": "test"},
            task_type="coding",
            coding=new_coding_state(project),
        )
        record["status"] = "ISSUE_UPDATED"
        record["capture"].update({"status": "CAPTURED", "task_id": "t-test", "run_id": "r-test"})
        record["upload"] = {
            "status": "VERIFIED",
            "verified": True,
            "verified_at": "2026-01-01T00:00:00Z",
            "report_url": "https://drive.google.com/file/d/report123/view",
            "receipt_url": "https://drive.google.com/file/d/receipt123/view",
        }
        record["publication"] = {
            "status": "PUBLISHED",
            "published_at": "2026-01-01T00:00:10Z",
            "comment_url": "https://github.test/issues/7#issuecomment-1",
            "attempts": 1,
            "last_error": None,
            "next_attempt_at": None,
        }
        coding = record["coding"]
        coding["stage"] = "ISSUE_UPDATED"
        coding["milestones"].update(
            {
                "REPORT_VERIFIED": True,
                "ISSUE_DELIVERED": True,
                "REPORT_DELIVERED": True,
            }
        )
        self.ledger.create(record)
        self.service = NotificationService(self.h.config)
        self.transport = self.FakeTransport()
        self.service.transport = self.transport

    def tearDown(self) -> None:
        self.env_patch.stop()
        self.tmp.cleanup()

    def _current(self) -> dict:
        return self.ledger.get(GH_REPO, 7) or {}

    def _notification(self) -> dict:
        items = [item for item in self.service.store.all() if item.get("kind") == DELIVERY_COMPLETED]
        self.assertTrue(items)
        return items[0]

    def test_t05_t06_telegram_queued_is_not_sent_until_api_acknowledges(self) -> None:
        queued = self.service.enqueue_completed(self._current())
        self.assertIsNotNone(queued)
        self.assertFalse(self._current()["coding"]["milestones"]["TELEGRAM_SENT"])
        self.transport.result = {"ok": False, "error": "Telegram unavailable", "retry_after": None}
        failed = self.service.drain(force=True)
        self.assertEqual(failed["failed"], 1)
        self.assertEqual(self._notification()["state"], "RETRY_WAIT")
        self.assertFalse(self._current()["coding"]["milestones"]["TELEGRAM_SENT"])

        self.transport.result = {"ok": True, "message_id": 6}
        retried = self.service.retry(task_id="t-test", run_id="r-test")
        self.assertEqual(retried["sent"], 1, retried)
        current = self._current()
        self.assertTrue(current["coding"]["milestones"]["TELEGRAM_SENT"])
        self.assertEqual(current["coding"]["telegram_message_id"], 6)
        self.assertEqual(current["coding"]["stage"], "NOTIFICATION_SENT")
        self.assertEqual(self._notification()["state"], "SENT")
        self.assertEqual(len(self.transport.calls), 2)

    def test_t07_t08_ambiguous_send_requires_acknowledgement_before_true_milestone(self) -> None:
        self.service.enqueue_completed(self._current())
        claimed = self.service.store.claim_next(force=True)
        self.assertIsNotNone(claimed)
        notification_id = str(claimed["notification_id"])
        self.service.store.update(
            notification_id,
            lambda item: item.update({"sending_at": "2020-01-01T00:00:00Z"}),
        )
        recovered = self.service.store.recover_stale_sending(stale_after_seconds=30)
        self.assertEqual(len(recovered), 1)
        self.assertTrue(recovered[0]["ambiguous"])
        self.assertEqual(self._notification()["state"], "NEEDS_ATTENTION")
        self.assertFalse(self._current()["coding"]["milestones"]["TELEGRAM_SENT"])

        generic = self.service.retry()
        self.assertEqual(generic["skipped_ambiguous"], 1)
        self.assertEqual(self.transport.calls, [])
        self.assertFalse(self._current()["coding"]["milestones"]["TELEGRAM_SENT"])

        self.transport.result = {"ok": True, "message_id": 6}
        accepted = self.service.retry(notification_id=notification_id, acknowledge_duplicate=True)
        self.assertEqual(accepted["sent"], 1, accepted)
        self.assertTrue(self._current()["coding"]["milestones"]["TELEGRAM_SENT"])
        self.assertEqual(self._current()["coding"]["telegram_message_id"], 6)
        self.assertEqual(len(self.transport.calls), 1)

    def test_s7_sent_notification_milestone_is_reconciled_without_resend(self) -> None:
        self.service.enqueue_completed(self._current())
        self.transport.result = {"ok": True, "message_id": 11}
        self.service.drain(force=True)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertTrue(self._current()["coding"]["milestones"]["TELEGRAM_SENT"])
        self.ledger.update(
            GH_REPO,
            7,
            lambda item: item["coding"].update(
                {"stage": "ISSUE_UPDATED", "milestones": {**item["coding"]["milestones"], "TELEGRAM_SENT": False}}
            ),
            note="simulate crash between Telegram API send and milestone write",
        )
        self.service.reconcile([self._current()])
        restored = self._current()
        self.assertTrue(restored["coding"]["milestones"]["TELEGRAM_SENT"])
        self.assertEqual(restored["coding"]["telegram_message_id"], 11)
        self.assertEqual(len(self.transport.calls), 1, "reconciliation must not resend")

    def test_t09_old_records_without_telegram_key_accept_new_acknowledgement(self) -> None:
        record = self._current()
        record["coding"]["milestones"] = {"REPORT_DELIVERED": True, "ISSUE_DELIVERED": True}
        self.ledger.update(GH_REPO, 7, lambda item: item["coding"].update({"milestones": record["coding"]["milestones"]}))
        self.service.enqueue_completed(self._current())
        self.transport.result = {"ok": True, "message_id": 9}
        self.service.drain(force=True)
        current = self._current()
        self.assertTrue(current["coding"]["milestones"]["TELEGRAM_SENT"])
        self.assertEqual(current["coding"]["telegram_message_id"], 9)


class CodingIntegrationTests(unittest.TestCase):
    """In-process C2 pipeline with synthetic credentials and a local trusted remote."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.h = DispatchHarness(self.root)
        self.repo = make_repository(self.root)
        self.work = self.root / "coding-work"
        self.work.mkdir()
        os.chmod(self.work, 0o700)
        self.staging = self.root / "coding-staging"
        self.staging.mkdir()
        os.chmod(self.staging, 0o700)
        self.identity = {**fake_identity(), "uid": os.getuid(), "gid": os.getgid()}
        self.dispatch_cfg = self._dispatch_config()
        self.ledger = Ledger(self.h.config)
        self.h.gh_dir.joinpath("repo.json").write_text(
            json.dumps({"full_name": PROJECT_REPO, "private": True, "has_issues": True}), encoding="utf-8"
        )
        self.issue = self.h.add_issue(number=21, body=request_body().replace("session: new", "session: new\ntask: coding"))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _dispatch_config(self) -> dict:
        coding = {
            "enabled": True,
            "repository": PROJECT_REPO,
            "base_branch": "main",
            "workspace_root": str(self.work),
            "source_workspace": str(self.repo["source"]),
            "test_profile": "safe",
            "test_definition": {"name": "safe", "commands": [["python3", "-c", "print('ok')"]], "timeout_seconds": 10, "env": {}},
            "allowed_model_aliases": ["fast"],
            "protected_paths": [".github/workflows/**"],
            "allowed_paths": [],
            "limits": {
                "test_timeout_seconds": 10,
                "max_files_changed": 10,
                "max_diff_bytes": 200000,
                "max_file_bytes": 50000,
                "max_output_bytes": 10000,
                "max_workspace_bytes": 100000000,
                "max_coding_tasks": 1,
                "max_publication_attempts": 2,
            },
            "publication": {"draft": True, "branch_prefix": "agent-handoff/", "max_attempts": 2},
            "cleanup_on_success": False,
            "secret_scan": True,
            "staging_root": str(self.staging),
        }
        isolation = {
            "enabled": True,
            "identity": self.identity,
            "workspace_roots": [str(self.work)],
            "denied_projects": ["Rabet", "Yakfi"],
        }
        return {
            **self.h.config["dispatch"],
            "enabled": True,
            "repository": GH_REPO,
            "projects": {
                "Alpha": {
                    "name": "Alpha",
                    "core_name": "Alpha",
                    "core_key": "alpha",
                    "workspace": str(self.repo["source"]),
                    "drive_folder_id": "alpha_folder_0001",
                    "execution": {
                        "permission_profile": "read_only",
                        "allow_write": False,
                        "isolation": {"enabled": True, "inherited": False},
                    },
                    "coding": coding,
                }
            },
            "execution": {
                "timeout_seconds": 30,
                "max_prompt_bytes": 50000,
                "permission_profile": "read_only",
                "allow_write": False,
                "isolation": isolation,
            },
            "coding": {
                "enabled": True,
                "staging_root": str(self.staging),
                "limits": coding["limits"],
                "publication": coding["publication"],
                "test_profiles": {"safe": coding["test_definition"]},
                "protected_paths": coding["protected_paths"],
                "secret_scan": True,
            },
            "delivery": {"retry_seconds": 5, "max_attempts": 0},
            "github": {"command": "gh", "require_private": True},
        }

    def _prepare_workspace(self, **kwargs):
        target = Path(kwargs["workspace_root"]) / f"issue-{kwargs['issue_number']}-{kwargs['task_id']}"
        if target.exists():
            if kwargs.get("allow_existing"):
                sha = subprocess.run(
                    ["git", "rev-parse", "HEAD"], cwd=target, capture_output=True, text=True
                ).stdout.strip()
                if sha != kwargs["base_commit"]:
                    listing = sorted(item.name for item in target.iterdir()) if target.exists() else ["<missing>"]
                    raise WorkspacePreparationError(
                        f"wrong base {sha!r} != {kwargs['base_commit']} cwd={target} listing={listing}"
                    )
                status = subprocess.run(
                    ["git", "status", "--porcelain=v1", "--untracked-files=all"],
                    cwd=target,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                if status and kwargs.get("require_clean", True):
                    raise WorkspacePreparationError("dirty")
                return {
                    "workspace": str(target),
                    "workspace_name": target.name,
                    "base_commit": sha,
                    "base_branch": kwargs["base_branch"],
                    "repository": kwargs["repository"],
                    "recovered": True,
                    "modified": bool(status),
                }
            raise WorkspacePreparationError("exists")
        target.mkdir()
        subprocess.run(
            ["git", "clone", "-q", "--no-local", "--branch", kwargs["base_branch"], kwargs["source"], str(target)],
            check=True,
            capture_output=True,
        )
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=target, capture_output=True, text=True).stdout.strip()
        if sha != kwargs["base_commit"]:
            raise WorkspacePreparationError("wrong base")
        os.chmod(target, 0o700)
        if os.geteuid() == 0:
            os.chown(target, kwargs["identity"]["uid"], kwargs["identity"]["gid"])
            os.chown(target / ".git", kwargs["identity"]["uid"], kwargs["identity"]["gid"])
        return {
            "workspace": str(target),
            "workspace_name": target.name,
            "base_commit": sha,
            "base_branch": kwargs["base_branch"],
            "repository": kwargs["repository"],
        }

    def _fake_orchestrator(self, runner, record, project, execution, adapter):
        publisher = TrustedPublisher(
            repository=PROJECT_REPO,
            base_branch="main",
            branch_prefix="agent-handoff/",
            staging_root=str(self.staging),
            gh_command=str(self.h.fake_bin / "gh"),
            token_provider=lambda: "fake-token",
            remote_url=f"file://{self.repo['bare']}",
            git=SafeGit(identity=None, allow_file_protocol=True),
            max_file_bytes=50000,
        )
        return CodingOrchestrator(
            config=runner.config,
            dispatch=runner.dispatch,
            ledger=runner.ledger,
            record=record,
            project=project,
            identity=self.identity,
            adapter=adapter,
            progress=lambda stage, message: runner._progress(stage, message, record),
            log=runner._log,
            git=SafeGit(identity=None, allow_file_protocol=True),
            publisher=publisher,
            spawn=lambda argv, **kwargs: FakeProcess(exit_code=0, stdout=b"ok\n"),
        )

    def _adapter(self, runner, execution):
        class Adapter:
            def run(self, request, *, events_path, on_session, on_progress=None, heartbeat_seconds=None):
                Path(events_path).parent.mkdir(parents=True, exist_ok=True)
                Path(events_path).write_text(
                    json.dumps({"type": "text", "sessionID": "ses_coding", "part": {"messageID": "m1", "text": "implemented"}})
                    + "\n"
                    + json.dumps({"type": "step_finish", "sessionID": "ses_coding", "part": {"messageID": "m1", "reason": "stop"}})
                    + "\n",
                    encoding="utf-8",
                )
                on_session("ses_coding")
                path = Path(request.workspace) / "src" / "app.py"
                path.write_text("value = 42\n", encoding="utf-8")
                (Path(request.workspace) / "src" / "feature.py").write_text("FEATURE = True\n", encoding="utf-8")
                return ExecutionResult(
                    session_id="ses_coding",
                    final_text="implemented",
                    termination_reason="stop",
                    exit_code=0,
                    timed_out=False,
                    outcome="succeeded",
                    events_path=events_path,
                    event_count=2,
                    error=None,
                    started_at=utcnow_iso(),
                    finished_at=utcnow_iso(),
                )

            def recover(self, events_path):
                return None

        return Adapter()

    def _runner(self) -> DispatchRunner:
        runner = DispatchRunner(self.h.config, self.dispatch_cfg, progress=None)
        runner.summary = {
            "ok": True,
            "busy": False,
            "executed": 0,
            "rejected": 0,
            "needs_attention": 0,
            "uploaded": 0,
            "published": 0,
            "errors": [],
            "warnings": [],
        }
        return runner

    def _run(self, extra_env: dict[str, str] | None = None) -> dict:
        runner = self._runner()
        self.last_runner = runner
        env = self.h.env(extra_env)
        with mock.patch.dict(os.environ, env, clear=False), mock.patch(
            "agent_handoff.upload.load_config", return_value=self.h.config
        ), mock.patch(
            "agent_handoff.dispatch.coding.pipeline.validate_coding_root", return_value=str(self.work)
        ), mock.patch(
            "agent_handoff.dispatch.coding.pipeline.validate_model_auth", return_value=None
        ), mock.patch(
            "agent_handoff.dispatch.coding.pipeline.prepare_workspace", side_effect=self._prepare_workspace
        ), mock.patch(
            "agent_handoff.dispatch.coding.pipeline.verify_source_repository",
            return_value={
                "source": str(self.repo["source"]),
                "repository": PROJECT_REPO,
                "base_branch": "main",
                "base_commit": self.repo["base"],
            },
        ), mock.patch(
            "agent_handoff.dispatch.coding.pipeline.run_test_profile",
            return_value={
                "status": "passed",
                "profile": "safe",
                "timeout_seconds": 10,
                "duration_seconds": 0.1,
                "failed_command": None,
                "commands": [
                    {
                        "argv": ["python3", "-c", "print('ok')"],
                        "status": "passed",
                        "exit_code": 0,
                        "timed_out": False,
                        "duration_seconds": 0.1,
                        "stdout": "ok\n",
                        "stderr": "",
                        "output_truncated": False,
                    }
                ],
            },
        ), mock.patch.object(DispatchRunner, "_coding_orchestrator", autospec=True, side_effect=self._fake_orchestrator), mock.patch.object(
            DispatchRunner, "_coding_adapter", autospec=True, side_effect=self._adapter
        ), mock.patch.object(
            DispatchRunner, "_cleanup_coding_after_capture", autospec=True
        ):
            runner._process_issue(self.issue, force=True)
            return runner.summary

    def test_t03_unauthorized_coding_author_is_rejected_without_execution(self) -> None:
        self.issue = self.h.add_issue(
            number=22,
            body=request_body().replace("session: new", "session: new\ntask: coding"),
            author="mallory",
        )
        runner = self._runner()
        with mock.patch.object(DispatchRunner, "_coding_adapter", side_effect=AssertionError("agent must not run")):
            runner._process_issue(self.issue, force=True)
        record = self.ledger.get(GH_REPO, 22)
        self.assertIsNotNone(record)
        self.assertEqual(record["status"], "REJECTED")
        self.assertEqual(self.h.opencode_calls(), [])
        self.assertEqual(self.h.comments(22), [])

    def test_t29_restart_after_publication_does_not_rerun_agent_or_duplicate_pr(self) -> None:
        first = self._run()
        self.assertEqual(first["executed"], 1)
        self.assertEqual(len(json.loads((self.h.gh_dir / "pulls.json").read_text(encoding="utf-8"))), 1)
        runner = self.last_runner
        runner.summary = {
            "ok": True, "busy": False, "executed": 0, "rejected": 0, "needs_attention": 0,
            "uploaded": 0, "published": 0, "errors": [], "warnings": [],
        }

        def simulate_crash(record: dict) -> None:
            record["status"] = "EXECUTION_COMPLETED"
            record["capture"]["status"] = "PENDING"
            record["upload"].update({"status": "PENDING", "verified": False, "report_url": None, "receipt_url": None, "attempts": 0, "next_attempt_at": None})
            record["publication"].update({"status": "PENDING", "comment_url": None, "attempts": 0, "next_attempt_at": None})
            record["coding"]["milestones"]["REPORT_DELIVERED"] = False

        self.ledger.update(GH_REPO, 21, simulate_crash, note="simulated restart before capture")
        env = self.h.env()
        with mock.patch.dict(os.environ, env, clear=False), mock.patch(
            "agent_handoff.upload.load_config", return_value=self.h.config
        ), mock.patch.object(DispatchRunner, "_coding_adapter", side_effect=AssertionError("agent must not run")):
            runner._advance_all(force=True)
        self.assertEqual(runner.summary["executed"], 0)
        record = self.ledger.get(GH_REPO, 21)
        self.assertEqual(record["status"], "ISSUE_UPDATED")
        self.assertEqual(len(json.loads((self.h.gh_dir / "pulls.json").read_text(encoding="utf-8"))), 1)
        self.assertEqual(len(self.h.comments(21)), 1)

    def test_t30_upload_failure_does_not_rerun_agent(self) -> None:
        marker = self.root / "fail-upload-once"
        marker.write_text("1", encoding="utf-8")
        first = self._run({"FAKE_FAIL_ONCE_FILE": str(marker)})
        self.assertEqual(first["executed"], 1)
        self.assertEqual(first["uploaded"], 0)
        record = self.ledger.get(GH_REPO, 21)
        self.assertEqual(record["status"], "UPLOAD_PENDING")
        self.assertTrue(record["coding"]["publication"]["pr_url"])
        runner = self.last_runner
        runner.summary = {
            "ok": True, "busy": False, "executed": 0, "rejected": 0, "needs_attention": 0,
            "uploaded": 0, "published": 0, "errors": [], "warnings": [],
        }
        env = self.h.env()
        with mock.patch.dict(os.environ, env, clear=False), mock.patch(
            "agent_handoff.upload.load_config", return_value=self.h.config
        ), mock.patch.object(DispatchRunner, "_coding_adapter", side_effect=AssertionError("agent must not run")):
            runner._advance_all(force=True)
        self.assertEqual(runner.summary["executed"], 0)
        self.assertEqual(runner.summary["uploaded"], 1)
        self.assertEqual(runner.summary["published"], 1)
        self.assertEqual(len(json.loads((self.h.gh_dir / "pulls.json").read_text(encoding="utf-8"))), 1)
        self.assertEqual(len(self.h.comments(21)), 1)

    def test_t01_t03_t04_lost_comment_ack_is_reconciled_without_duplicate(self) -> None:
        marker = self.root / "lost-comment-ack"
        marker.write_text("1", encoding="utf-8")
        first = self._run({"FAKE_GH_LOST_COMMENT_RESPONSE_FILE": str(marker)})
        self.assertEqual(first["executed"], 1)
        self.assertEqual(first["uploaded"], 1)
        self.assertEqual(first["published"], 0)
        self.assertFalse(first["ok"])
        record = self.ledger.get(GH_REPO, 21)
        self.assertEqual(record["status"], "UPLOAD_VERIFIED")
        self.assertTrue(record["upload"]["verified"])
        self.assertTrue(record["upload"]["report_url"])
        self.assertEqual(record["publication"]["status"], "FAILED")
        self.assertTrue(record["coding"]["milestones"]["REPORT_VERIFIED"])
        self.assertFalse(record["coding"]["milestones"]["ISSUE_DELIVERED"])
        comments = self.h.comments(21)
        self.assertEqual(len(comments), 1, "the simulated API created the comment before the response was lost")

        runner = self.last_runner
        runner.summary = {
            "ok": True, "busy": False, "executed": 0, "rejected": 0, "needs_attention": 0,
            "uploaded": 0, "published": 0, "errors": [], "warnings": [],
        }
        env = self.h.env()
        with mock.patch.dict(os.environ, env, clear=False), mock.patch(
            "agent_handoff.upload.load_config", return_value=self.h.config
        ), mock.patch.object(DispatchRunner, "_coding_adapter", side_effect=AssertionError("agent must not run")):
            runner._advance_all(force=True)
        reconciled = self.ledger.get(GH_REPO, 21)
        self.assertEqual(reconciled["status"], "ISSUE_UPDATED")
        self.assertEqual(reconciled["publication"]["status"], "PUBLISHED")
        self.assertTrue(reconciled["publication"]["comment_url"])
        self.assertTrue(reconciled["coding"]["milestones"]["REPORT_VERIFIED"])
        self.assertTrue(reconciled["coding"]["milestones"]["ISSUE_DELIVERED"])
        self.assertTrue(reconciled["coding"]["milestones"]["REPORT_DELIVERED"])
        self.assertEqual(len(self.h.comments(21)), 1, "reconciliation must not post a duplicate comment")
        self.assertEqual(runner.summary["executed"], 0)

    def test_t05_t13_t24_t25_t31_t33_end_to_end_coding_pipeline(self) -> None:
        unrelated = self.h.home / "queue" / "q-historical-legacy.json"
        unrelated.parent.mkdir(parents=True, exist_ok=True)
        unrelated.write_text(
            json.dumps(
                {
                    "id": "q-historical-legacy",
                    "kind": "run",
                    "project": "Alpha",
                    "project_key": "alpha",
                    "task_id": "t-historical",
                    "run_id": "r-historical",
                    "files": [],
                    "attempts": 0,
                }
            ),
            encoding="utf-8",
        )
        summary = self._run()
        self.assertTrue(summary["ok"], summary)
        self.assertEqual(summary["executed"], 1)
        self.assertEqual(summary["uploaded"], 1)
        self.assertEqual(summary["published"], 1)
        record = self.ledger.get(GH_REPO, 21)
        self.assertIsNotNone(record)
        self.assertEqual(record["task_type"], "coding")
        self.assertEqual(record["status"], "ISSUE_UPDATED")
        coding = record["coding"]
        self.assertFalse(coding["blocked"])
        self.assertTrue(coding["milestones"]["AGENT_COMPLETED"])
        self.assertTrue(coding["milestones"]["TESTS_PASSED"])
        self.assertTrue(coding["milestones"]["PR_PUBLISHED"])
        self.assertTrue(coding["milestones"]["REPORT_DELIVERED"])
        self.assertEqual(coding["base_commit"], self.repo["base"])
        self.assertTrue(coding["publication"]["branch"].startswith("agent-handoff/"))
        self.assertTrue(coding["publication"]["pr_url"])
        self.assertEqual(record["upload"]["report_url"].split("/")[3], "file")
        branch = coding["publication"]["branch"]
        remote_sha = subprocess.run(
            ["git", "rev-parse", f"refs/heads/{branch}"], cwd=self.repo["bare"], capture_output=True, text=True
        ).stdout.strip()
        self.assertEqual(remote_sha, coding["publication"]["commit"])
        main_sha = subprocess.run(
            ["git", "rev-parse", "refs/heads/main"], cwd=self.repo["bare"], capture_output=True, text=True
        ).stdout.strip()
        self.assertEqual(main_sha, self.repo["base"])
        pulls = json.loads((self.h.gh_dir / "pulls.json").read_text(encoding="utf-8"))
        self.assertEqual(len(pulls), 1)
        self.assertTrue(pulls[0]["draft"])
        self.assertIn(record["upload"]["report_url"], pulls[0]["body"])
        comments = self.h.comments(21)
        self.assertEqual(len(comments), 1)
        self.assertIn(coding["publication"]["pr_url"], comments[0]["body"])
        self.assertIn(record["upload"]["report_url"], comments[0]["body"])
        task_dir = self.h.task_dir_for("Alpha", record["capture"]["task_id"])
        run_dir = task_dir / "RUNS" / record["capture"]["run_id"]
        report = (run_dir / "REPORT.md").read_text(encoding="utf-8")
        for expected in ("Test execution evidence", "Diff validation evidence", "Draft PR", "Security restrictions and limits"):
            self.assertIn(expected, report)
        complete = json.loads((run_dir / "COMPLETE.json").read_text(encoding="utf-8"))
        self.assertTrue(complete["upload"]["verified"])
        self.assertEqual(complete["coding"]["pr_url"], coding["publication"]["pr_url"])
        self.assertTrue(complete["coding"]["tests"]["status"], "passed")
        self.assertTrue((run_dir / "UPLOAD-RECEIPT.json").exists())
        view = record_status_view(record)
        self.assertEqual(view["health"], "delivered")
        self.assertEqual(view["coding_stage"], "ISSUE_UPDATED")
        self.assertEqual(view["pr_url"], coding["publication"]["pr_url"])
        self.assertIn("Draft PR", "\n".join(format_status_view(view)))
        self.assertTrue(unrelated.exists(), "C2 must not process an unrelated historical queue item")
        second = self._run()
        self.assertEqual(second["executed"], 0)
        self.assertEqual(second["uploaded"], 0)
        self.assertEqual(second["published"], 0)
        self.assertEqual(len(self.h.comments(21)), 1)
        self.assertEqual(len(json.loads((self.h.gh_dir / "pulls.json").read_text(encoding="utf-8"))), 1)

    def test_t16_missing_staging_recovers_branch_by_remote_sha(self) -> None:
        first = self._run()
        self.assertEqual(first["executed"], 1)
        record = self.ledger.get(GH_REPO, 21)
        branch = record["coding"]["publication"]["branch"]
        commit = record["coding"]["publication"]["commit"]
        runner = self.last_runner
        runner.summary = {
            "ok": True, "busy": False, "executed": 0, "rejected": 0, "needs_attention": 0,
            "uploaded": 0, "published": 0, "errors": [], "warnings": [],
        }

        def simulate_staging_loss(item: dict) -> None:
            item["status"] = "EXECUTION_COMPLETED"
            item["capture"]["status"] = "PENDING"
            item["upload"].update({"status": "PENDING", "verified": False, "report_url": None, "receipt_url": None, "attempts": 0, "next_attempt_at": None})
            item["publication"].update({"status": "PENDING", "comment_url": None, "attempts": 0, "next_attempt_at": None})
            pub = item["coding"]["publication"]
            pub.update({
                "branch": branch,
                "commit": commit,
                "branch_pushed": False,
                "pr_url": None,
                "pr_number": None,
                "staging": None,
                "status": "COMMITTED",
                "report_link_updated": False,
                "attempts": 0,
                "next_attempt_at": None,
            })
            item["coding"]["stage"] = "PR_CREATED"
            item["coding"]["milestones"]["REPORT_DELIVERED"] = False

        self.ledger.update(GH_REPO, 21, simulate_staging_loss, note="simulated missing publication staging")
        env = self.h.env()
        with mock.patch.dict(os.environ, env, clear=False), mock.patch(
            "agent_handoff.upload.load_config", return_value=self.h.config
        ), mock.patch.object(
            DispatchRunner, "_coding_orchestrator", autospec=True, side_effect=self._fake_orchestrator
        ), mock.patch.object(DispatchRunner, "_coding_adapter", autospec=True, side_effect=self._adapter):
            runner._advance_all(force=True)
        self.assertEqual(runner.summary["executed"], 0)
        recovered = self.ledger.get(GH_REPO, 21)
        self.assertTrue(recovered["coding"]["publication"].get("staging_recovered_by_remote_sha"))
        self.assertEqual(recovered["status"], "ISSUE_UPDATED")
        self.assertEqual(len(json.loads((self.h.gh_dir / "pulls.json").read_text(encoding="utf-8"))), 1)
        self.assertEqual(len(self.h.comments(21)), 1)

    def test_t17_pr_body_report_link_retries_after_upload(self) -> None:
        first = self._run()
        self.assertEqual(first["executed"], 1)
        record = self.ledger.get(GH_REPO, 21)
        report_url = record["upload"]["report_url"]
        branch = record["coding"]["publication"]["branch"]
        commit = record["coding"]["publication"]["commit"]
        pulls_path = self.h.gh_dir / "pulls.json"
        pulls = json.loads(pulls_path.read_text(encoding="utf-8"))
        pulls[0]["body"] = pulls[0]["body"].replace(report_url, "")
        pulls_path.write_text(json.dumps(pulls, indent=2), encoding="utf-8")

        def clear_link(item: dict) -> None:
            item["coding"]["publication"].update(
                {
                    "branch": branch,
                    "commit": commit,
                    "report_link_updated": False,
                    "report_link_next_attempt_at": None,
                    "report_link_attempts": 0,
                    "report_link_last_error": None,
                }
            )

        self.ledger.update(GH_REPO, 21, clear_link, note="simulate PR body update loss")
        runner = self.last_runner
        runner.summary = {
            "ok": True, "busy": False, "executed": 0, "rejected": 0, "needs_attention": 0,
            "uploaded": 0, "published": 0, "errors": [], "warnings": [],
        }
        marker = self.root / "fail-pr-patch-once"
        marker.write_text("1", encoding="utf-8")
        env = self.h.env({"FAKE_GH_FAIL_PATCH_FILE": str(marker)})
        with mock.patch.dict(os.environ, env, clear=False):
            runner._advance_all(force=True)
        failed = self.ledger.get(GH_REPO, 21)
        self.assertFalse(failed["coding"]["publication"]["report_link_updated"])
        self.assertTrue(failed["coding"]["publication"]["report_link_last_error"])
        pulls = json.loads(pulls_path.read_text(encoding="utf-8"))
        self.assertNotIn(report_url, pulls[0]["body"])
        self.assertFalse(marker.exists())

        def make_due(item: dict) -> None:
            item["coding"]["publication"]["report_link_next_attempt_at"] = "2000-01-01T00:00:00Z"

        self.ledger.update(GH_REPO, 21, make_due, note="make PR body retry due")
        env = self.h.env()
        with mock.patch.dict(os.environ, env, clear=False):
            runner._advance_all(force=True)
        recovered = self.ledger.get(GH_REPO, 21)
        self.assertTrue(recovered["coding"]["publication"]["report_link_updated"])
        pulls = json.loads(pulls_path.read_text(encoding="utf-8"))
        self.assertIn(report_url, pulls[0]["body"])
        self.assertEqual(len(pulls), 1)
        self.assertEqual(len(self.h.comments(21)), 1)

    def test_t17_test_failure_blocks_publication_but_delivers_report(self) -> None:
        runner = self._runner()
        env = self.h.env()
        with mock.patch.dict(os.environ, env, clear=False), mock.patch(
            "agent_handoff.upload.load_config", return_value=self.h.config
        ), mock.patch(
            "agent_handoff.dispatch.coding.pipeline.validate_coding_root", return_value=str(self.work)
        ), mock.patch(
            "agent_handoff.dispatch.coding.pipeline.validate_model_auth", return_value=None
        ), mock.patch(
            "agent_handoff.dispatch.coding.pipeline.prepare_workspace", side_effect=self._prepare_workspace
        ), mock.patch(
            "agent_handoff.dispatch.coding.pipeline.verify_source_repository",
            return_value={
                "source": str(self.repo["source"]),
                "repository": PROJECT_REPO,
                "base_branch": "main",
                "base_commit": self.repo["base"],
            },
        ), mock.patch(
            "agent_handoff.dispatch.coding.pipeline.run_test_profile",
            return_value={
                "status": "failed",
                "profile": "safe",
                "timeout_seconds": 10,
                "duration_seconds": 0.1,
                "failed_command": ["python3", "-m", "unittest"],
                "commands": [
                    {
                        "argv": ["python3", "-m", "unittest"],
                        "status": "failed",
                        "exit_code": 1,
                        "timed_out": False,
                        "duration_seconds": 0.1,
                        "stdout": "",
                        "stderr": "FAILED",
                        "output_truncated": False,
                    }
                ],
            },
        ), mock.patch.object(DispatchRunner, "_coding_orchestrator", autospec=True, side_effect=self._fake_orchestrator), mock.patch.object(
            DispatchRunner, "_coding_adapter", autospec=True, side_effect=self._adapter
        ), mock.patch.object(DispatchRunner, "_cleanup_coding_after_capture", autospec=True):
            runner._process_issue(self.issue, force=True)
        record = self.ledger.get(GH_REPO, 21)
        self.assertEqual(record["status"], "ISSUE_UPDATED")
        coding = record["coding"]
        self.assertTrue(coding["blocked"])
        self.assertEqual(coding["blocked_stage"], "TESTING")
        self.assertFalse(coding["publication"]["pr_url"])
        self.assertFalse(coding["milestones"]["TESTS_PASSED"])
        view = record_status_view(record)
        self.assertEqual(view["health"], "attention")
        self.assertEqual(view["phase"], "coding_blocked")
        self.assertEqual(len(json.loads((self.h.gh_dir / "pulls.json").read_text(encoding="utf-8"))) if (self.h.gh_dir / "pulls.json").exists() else 0, 0)
        self.assertEqual(len(self.h.comments(21)), 1)
        self.assertIn(record["upload"]["report_url"], self.h.comments(21)[0]["body"])
        report = (self.h.task_dir_for("Alpha", record["capture"]["task_id"]) / "RUNS" / record["capture"]["run_id"] / "REPORT.md").read_text(encoding="utf-8")
        self.assertIn("blocked:TESTING", report)
        self.assertIn("no commit, push, or Draft PR", report)



    def test_s8_lost_staging_before_push_rebuilds_commit_safely(self) -> None:
        first = self._run()
        self.assertEqual(first["executed"], 1)
        record = self.ledger.get(GH_REPO, 21)
        branch = record["coding"]["publication"]["branch"]
        old_commit = record["coding"]["publication"]["commit"]
        git(["update-ref", "-d", f"refs/heads/{branch}"], self.repo["bare"])

        def simulate_pre_push_loss(item: dict) -> None:
            item["status"] = "EXECUTION_COMPLETED"
            item["capture"]["status"] = "PENDING"
            item["upload"].update({"status": "PENDING", "verified": False, "report_url": None, "receipt_url": None, "attempts": 0, "next_attempt_at": None})
            item["publication"].update({"status": "PENDING", "comment_url": None, "attempts": 0, "next_attempt_at": None})
            pub = item["coding"]["publication"]
            pub.update({
                "branch": branch,
                "commit": old_commit,
                "branch_pushed": False,
                "pr_url": None,
                "pr_number": None,
                "staging": None,
                "status": "COMMITTED",
                "report_link_updated": False,
                "attempts": 0,
                "next_attempt_at": None,
            })
            item["coding"]["stage"] = "PR_CREATED"
            item["coding"]["milestones"]["REPORT_DELIVERED"] = False

        self.ledger.update(GH_REPO, 21, simulate_pre_push_loss, note="simulated staging loss before push")
        runner = self.last_runner
        runner.summary = {
            "ok": True, "busy": False, "executed": 0, "rejected": 0, "needs_attention": 0,
            "uploaded": 0, "published": 0, "errors": [], "warnings": [],
        }
        env = self.h.env()
        with mock.patch.dict(os.environ, env, clear=False), mock.patch(
            "agent_handoff.upload.load_config", return_value=self.h.config
        ), mock.patch.object(
            DispatchRunner, "_coding_orchestrator", autospec=True, side_effect=self._fake_orchestrator
        ), mock.patch.object(DispatchRunner, "_coding_adapter", autospec=True, side_effect=self._adapter):
            runner._advance_all(force=True)
        self.assertEqual(runner.summary["executed"], 0)
        recovered = self.ledger.get(GH_REPO, 21)
        self.assertFalse(recovered["coding"].get("blocked"), recovered["coding"].get("blocked_reason"))
        self.assertTrue(recovered["coding"]["publication"]["branch_pushed"])
        self.assertTrue(recovered["coding"]["publication"]["pr_url"])
        remote_sha = git(["rev-parse", f"refs/heads/{branch}"], self.repo["bare"]).stdout.strip()
        self.assertEqual(remote_sha, recovered["coding"]["publication"]["commit"])
        self.assertEqual(len(json.loads((self.h.gh_dir / "pulls.json").read_text(encoding="utf-8"))), 1)
        self.assertEqual(len(self.h.comments(21)), 1)

    def test_s8_interrupted_publication_cleanup_is_retried_without_new_comment(self) -> None:
        with mock.patch.object(TrustedPublisher, "cleanup", return_value=True), mock.patch.object(
            TrustedPublisher, "cleanup_persisted", return_value=False
        ):
            first = self._run()
        self.assertEqual(first["executed"], 1)
        record = self.ledger.get(GH_REPO, 21)
        staging = record["coding"]["publication"].get("staging")
        self.assertTrue(staging and Path(staging).is_dir(), "simulated interrupted cleanup must leave staging")
        runner = self.last_runner
        runner.summary = {
            "ok": True, "busy": False, "executed": 0, "rejected": 0, "needs_attention": 0,
            "uploaded": 0, "published": 0, "errors": [], "warnings": [],
        }
        env = self.h.env()
        with mock.patch.dict(os.environ, env, clear=False), mock.patch(
            "agent_handoff.upload.load_config", return_value=self.h.config
        ), mock.patch.object(DispatchRunner, "_coding_adapter", side_effect=AssertionError("agent must not run")):
            runner._advance_all(force=True)
        self.assertFalse(Path(staging).exists(), "retry must remove interrupted publication staging")
        self.assertEqual(len(self.h.comments(21)), 1)
        self.assertEqual(len(json.loads((self.h.gh_dir / "pulls.json").read_text(encoding="utf-8"))), 1)


class TestPublicationAuthIsolation(unittest.TestCase):
    """S1: parent Git authentication never shares agent-controlled paths."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.repo = make_repository(self.root)
        self.staging = self.root / "publication"
        self.staging.mkdir()
        os.chmod(self.staging, 0o700)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _publisher(self) -> TrustedPublisher:
        return TrustedPublisher(
            repository=PROJECT_REPO,
            base_branch="main",
            branch_prefix="agent-handoff/",
            staging_root=str(self.staging),
            token_provider=lambda: "synthetic-github-token-0123456789",
            remote_url=f"file://{self.repo['bare']}",
            git=SafeGit(identity=None, allow_file_protocol=True),
        )

    def _prepared(self) -> tuple[TrustedPublisher, dict, Path]:
        workspace = self.root / "task"
        shutil.copytree(self.repo["source"], workspace)
        (workspace / "src" / "app.py").write_text("value = 44\n", encoding="utf-8")
        evidence = validate_changes(
            str(workspace),
            git=SafeGit(identity=None, allow_file_protocol=True),
            limits={"max_files_changed": 10, "max_file_bytes": 50000, "max_diff_bytes": 200000, "max_workspace_bytes": 1000000},
            protected_paths=[],
        )
        self.assertEqual(evidence["status"], "passed", evidence)
        publisher = self._publisher()
        with mock.patch.object(publisher, "verify_repository", return_value={"repository": PROJECT_REPO}):
            publisher.prepare(base_commit=self.repo["base"], task_id="t-auth", run_id="r-auth")
        return publisher, evidence, workspace

    def test_s1_askpass_helper_is_private_ephemeral_and_outside_staging(self) -> None:
        publisher = self._publisher()
        staging = Path(publisher._prepare_staging("t-auth", "r-auth"))
        marker = self.root / "hostile-askpass-ran"
        hostile = staging / ".askpass.sh"
        hostile.write_text(f"#!/bin/sh\ntouch {marker}\nprintf '%s\\n' hostile\n", encoding="utf-8")
        os.chmod(hostile, 0o755)
        script_path: Path | None = None
        with publisher._auth_environment("synthetic-github-token-0123456789") as env:
            script_path = Path(env["GIT_ASKPASS"])
            self.assertNotEqual(script_path.parent, staging)
            self.assertNotIn(staging, script_path.parents)
            self.assertTrue(script_path.parent.name.startswith(".auth-"))
            self.assertEqual(stat.S_IMODE(script_path.stat().st_mode), 0o700)
            result = subprocess.run(
                [str(script_path), "Password for 'https://x-access-token@github.com':"],
                capture_output=True,
                text=True,
                env={**os.environ, "AH_PUBLISH_TOKEN": "synthetic-github-token-0123456789"},
                check=True,
            )
            self.assertEqual(result.stdout.strip(), "synthetic-github-token-0123456789")
            self.assertFalse(marker.exists(), "the staging askpass helper must never be executed")
        self.assertIsNotNone(script_path)
        self.assertFalse(script_path.parent.exists(), "the private auth helper must not survive its operation")

    def test_s1_hostile_and_symlinked_staging_askpass_are_ignored(self) -> None:
        publisher, evidence, workspace = self._prepared()
        staging = Path(publisher._staging)
        marker = self.root / "hostile-askpass-ran"
        script = self.root / "evil-askpass.sh"
        script.write_text(f"#!/bin/sh\ntouch {marker}\nprintf '%s\\n' evil\n", encoding="utf-8")
        os.chmod(script, 0o755)
        (staging / ".askpass.sh").write_text(script.read_text(encoding="utf-8"), encoding="utf-8")
        os.chmod(staging / ".askpass.sh", 0o755)
        committed = publisher.stage_and_commit(
            validation=evidence,
            task_workspace=str(workspace),
            project="Alpha",
            title="Auth isolation",
            issue_number=61,
            task_id="t-auth",
            run_id="r-auth",
            base_commit=self.repo["base"],
        )
        branch = build_branch("agent-handoff/", "Alpha", 61, "t-auth")
        publisher.push(branch=branch, commit=committed["commit"])
        self.assertFalse(marker.exists())
        (staging / ".askpass.sh").unlink()
        os.symlink(script, staging / ".askpass.sh")
        publisher2 = self._publisher()
        with mock.patch.object(publisher2, "verify_repository", return_value={"repository": PROJECT_REPO}):
            publisher2.prepare(base_commit=self.repo["base"], task_id="t-auth2", run_id="r-auth2")
        committed2 = publisher2.stage_and_commit(
            validation=evidence,
            task_workspace=str(workspace),
            project="Alpha",
            title="Auth isolation 2",
            issue_number=62,
            task_id="t-auth2",
            run_id="r-auth2",
            base_commit=self.repo["base"],
        )
        branch2 = build_branch("agent-handoff/", "Alpha", 62, "t-auth2")
        publisher2.push(branch=branch2, commit=committed2["commit"])
        self.assertFalse(marker.exists())

    def test_s1_reserved_askpass_manifest_is_rejected(self) -> None:
        publisher, evidence, workspace = self._prepared()
        hostile = workspace / ".askpass.sh"
        hostile.write_text("#!/bin/sh\ntouch /tmp/should-not-run\n", encoding="utf-8")
        tampered = json.loads(json.dumps(evidence))
        digest = __import__("hashlib").sha256(hostile.read_bytes()).hexdigest()
        tampered["files"].append(
            {"path": ".askpass.sh", "status": "added", "size": hostile.stat().st_size, "mode": "0755", "raw_mode": "0755", "sha256": digest}
        )
        tampered["manifest"]["files"].append(
            {"path": ".askpass.sh", "status": "added", "size": hostile.stat().st_size, "mode": "0755", "raw_mode": "0755", "sha256": digest}
        )
        canonical = json.dumps(tampered["manifest"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        tampered["manifest_sha256"] = __import__("hashlib").sha256(canonical).hexdigest()
        with self.assertRaises(PublicationError) as ctx:
            publisher.stage_and_commit(
                validation=tampered,
                task_workspace=str(workspace),
                project="Alpha",
                title="Reserved path",
                issue_number=63,
                task_id="t-auth",
                run_id="r-auth",
                base_commit=self.repo["base"],
            )
        self.assertIn("reserved", str(ctx.exception))

    def test_s1_publication_token_never_reaches_the_opencode_child(self) -> None:
        parent = {
            "AH_PUBLISH_TOKEN": "synthetic-github-token-0123456789",
            "GH_TOKEN": "synthetic-github-token-0123456789",
            "GITHUB_TOKEN": "synthetic-github-token-0123456789",
            "AGENT_HANDOFF_TELEGRAM_TOKEN": "synthetic-telegram-token",
        }
        env = build_isolated_environment(
            fake_identity(),
            workspace="/tmp/work",
            policy={"share": "disabled"},
            parent_env=parent,
        )
        for name in parent:
            self.assertNotIn(name, env)
        self.assertNotIn("synthetic-github-token-0123456789", json.dumps(env))

    def test_s1_push_errors_are_redacted(self) -> None:
        publisher, evidence, workspace = self._prepared()
        token = "synthetic-github-token-0123456789"
        committed = publisher.stage_and_commit(
            validation=evidence,
            task_workspace=str(workspace),
            project="Alpha",
            title="Redaction",
            issue_number=64,
            task_id="t-auth",
            run_id="r-auth",
            base_commit=self.repo["base"],
        )
        branch = build_branch("agent-handoff/", "Alpha", 64, "t-auth")
        with mock.patch.object(publisher, "_remote_branch_sha", return_value=None):
            with mock.patch.object(
                publisher.git,
                "run",
                return_value=SimpleNamespace(returncode=1, stdout="", stderr=f"fatal: {token}"),
            ):
                with self.assertRaises(PublicationError) as ctx:
                    publisher.push(branch=branch, commit=committed["commit"])
        self.assertNotIn(token, str(ctx.exception))
        self.assertIn("[REDACTED]", str(ctx.exception))


class TestCodingTestGate(unittest.TestCase):
    """S2: only explicitly persisted passing tests may reach publication."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = make_repository(self.root)
        self.work = self.root / "work"
        self.work.mkdir()
        os.chmod(self.work, 0o700)
        self.identity = {**fake_identity(), "uid": os.getuid(), "gid": os.getgid()}
        with mock.patch("agent_handoff.dispatch.coding.workspace.os.geteuid", return_value=0), mock.patch(
            "agent_handoff.dispatch.coding.workspace.os.chown"
        ):
            self.workspace = prepare_workspace(
                workspace_root=str(self.work),
                repository=PROJECT_REPO,
                base_branch="main",
                base_commit=self.repo["base"],
                source=str(self.repo["source"]),
                issue_number=71,
                task_id="t-gate",
                identity=self.identity,
                git=SafeGit(identity=None, allow_file_protocol=True),
            )
        self.h = DispatchHarness(self.root / "harness")
        self.ledger = Ledger(self.h.config)
        self.project = {
            "name": "Alpha",
            "workspace": str(self.repo["source"]),
            "coding": {
                "enabled": True,
                "repository": PROJECT_REPO,
                "base_branch": "main",
                "workspace_root": str(self.work),
                "source_workspace": str(self.repo["source"]),
                "limits": {"test_timeout_seconds": 10, "max_files_changed": 10, "max_diff_bytes": 200000, "max_file_bytes": 50000},
                "test_definition": {"name": "safe", "commands": [["true"]], "timeout_seconds": 10, "env": {}},
                "protected_paths": [],
                "allowed_paths": [],
                "publication": {"branch_prefix": "agent-handoff/", "draft": True},
                "cleanup_on_success": False,
            },
        }
        base = new_dispatch_record(
            repository=GH_REPO,
            issue={"number": 71, "title": "gate", "html_url": "https://github.test/issues/71"},
            project=self.project,
            agent="opencode",
            model="provider/model-fast",
            request={"task": "coding", "instructions": "change"},
            task_type="coding",
            coding=new_coding_state(self.project),
        )
        coding = base["coding"]
        coding.update(
            {
                "workspace": self.workspace["workspace"],
                "workspace_name": self.workspace["workspace_name"],
                "base_commit": self.repo["base"],
                "base_branch": "main",
                "repository": PROJECT_REPO,
                "source_workspace": str(self.repo["source"]),
                "agent": {"status": "completed", "outcome": "succeeded", "orphans_detected": False},
                "stage": "TESTING",
            }
        )
        base["status"] = "EXECUTION_COMPLETED"
        base["capture"].update({"status": "PENDING", "task_id": "t-gate", "run_id": "r-gate"})
        self.record, _ = self.ledger.create(base)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _orchestrator(self) -> CodingOrchestrator:
        return CodingOrchestrator(
            config=self.h.config,
            dispatch={"coding": {"enabled": True}, "github": {"command": "gh"}, "execution": {"isolation": {"workspace_roots": [str(self.work)]}}},
            ledger=self.ledger,
            record=self.ledger.get(GH_REPO, 71) or self.record,
            project=self.project,
            identity=self.identity,
            adapter=SimpleNamespace(),
            git=SafeGit(identity=None, allow_file_protocol=True),
        )

    def _set_tests(self, tests: dict, *, stage: str = "COMMIT_PREPARED", blocked: bool = False) -> dict:
        def apply(item: dict) -> None:
            item["coding"]["tests"] = tests
            item["coding"]["stage"] = stage
            item["coding"]["blocked"] = blocked
            item["coding"]["blocked_stage"] = None
            item["coding"]["blocked_reason"] = None

        return self.ledger.update(GH_REPO, 71, apply, note="test fixture")

    def _failure(self, status: str = "failed") -> dict:
        return {
            "status": status,
            "profile": "safe",
            "timeout_seconds": 10,
            "commands": [
                {
                    "argv": ["false"],
                    "status": status,
                    "exit_code": 1 if status == "failed" else None,
                    "timed_out": status == "timed_out",
                    "orphans_detected": status == "orphans",
                    "duration_seconds": 0.1,
                }
            ],
        }

    def _passing(self) -> dict:
        return {
            "status": "passed",
            "profile": "safe",
            "timeout_seconds": 10,
            "commands": [
                {"argv": ["true"], "status": "passed", "exit_code": 0, "timed_out": False, "orphans_detected": False, "duration_seconds": 0.1}
            ],
        }

    def _assert_no_publication(self) -> None:
        record = self.ledger.get(GH_REPO, 71) or {}
        coding = record.get("coding") or {}
        publication = coding.get("publication") or {}
        self.assertFalse(publication.get("commit"))
        self.assertFalse(publication.get("pr_url"))
        self.assertFalse(any((coding.get("milestones") or {}).get(name) for name in ("COMMIT_CREATED", "BRANCH_PUBLISHED", "PR_CREATED", "PR_PUBLISHED")))

    def test_s2_resume_with_failed_tests_and_no_blocked_flag_never_publishes(self) -> None:
        self._set_tests(self._failure("failed"))
        orchestrator = self._orchestrator()
        with mock.patch.object(TrustedPublisher, "stage_and_commit", side_effect=AssertionError("must not publish")), mock.patch.object(
            TrustedPublisher, "push", side_effect=AssertionError("must not publish")
        ):
            updated = orchestrator.continue_after_agent()
        self.assertTrue(updated["coding"]["blocked"])
        self.assertEqual(updated["coding"]["blocked_stage"], "TESTING")
        self.assertEqual(updated["coding"]["tests"]["status"], "failed")
        self._assert_no_publication()

    def test_s2_crash_after_timeout_evidence_blocks_resume(self) -> None:
        self._set_tests(self._failure("timed_out"))
        updated = self._orchestrator().continue_after_agent()
        self.assertTrue(updated["coding"]["blocked"])
        self.assertEqual(updated["coding"]["tests"]["status"], "timed_out")
        self._assert_no_publication()

    def test_s2_resume_with_missing_tests_at_publication_stage_blocks(self) -> None:
        self._set_tests({"status": "not_run", "commands": []}, stage="VALIDATING_DIFF")
        updated = self._orchestrator().continue_after_agent()
        self.assertTrue(updated["coding"]["blocked"])
        self.assertEqual(updated["coding"]["blocked_stage"], "TESTING")
        self._assert_no_publication()

    def test_s2_stale_tests_passed_milestone_does_not_override_failure(self) -> None:
        self._set_tests(self._failure("failed"))
        self.ledger.update(
            GH_REPO,
            71,
            lambda item: item["coding"].setdefault("milestones", {}).update({"TESTS_PASSED": True}),
            note="stale milestone",
        )
        updated = self._orchestrator().continue_after_agent()
        self.assertTrue(updated["coding"]["blocked"])
        self.assertEqual(milestone_state(updated["coding"], "TESTS_PASSED"), "false")
        self._assert_no_publication()

    def test_s2_orphaned_test_process_group_blocks(self) -> None:
        tests = self._failure("failed")
        tests["status"] = "passed"
        tests["commands"][0].update({"status": "passed", "exit_code": 0, "orphans_detected": True})
        self._set_tests(tests)
        updated = self._orchestrator().continue_after_agent()
        self.assertTrue(updated["coding"]["blocked"])
        self.assertIn("orphan", str(updated["coding"]["blocked_reason"]).lower())
        self._assert_no_publication()

    def test_s2_failed_evidence_and_block_are_one_atomic_transition(self) -> None:
        self._set_tests({"status": "not_run", "commands": []}, stage="AGENT_COMPLETED")
        orchestrator = self._orchestrator()
        writes: list[str] = []
        original = self.ledger.update

        def counted(repository, issue_number, mutator, *, note=None):
            writes.append(str(note))
            return original(repository, issue_number, mutator, note=note)

        with mock.patch.object(self.ledger, "update", side_effect=counted):
            updated = orchestrator._record_test_failure(
                self.ledger.get(GH_REPO, 71), reason="failed evidence", stored=self._failure("failed")
            )
        self.assertTrue(updated["coding"]["blocked"])
        self.assertEqual(updated["coding"]["tests"]["status"], "failed")
        failure_writes = [note for note in writes if "coding blocked" in note]
        self.assertEqual(len(failure_writes), 1, writes)

    def test_s2_explicit_pass_continues_without_rerunning_tests(self) -> None:
        self._set_tests(self._passing(), stage="AGENT_COMPLETED")
        orchestrator = self._orchestrator()
        with mock.patch("agent_handoff.dispatch.coding.pipeline.run_test_profile", side_effect=AssertionError("tests must not rerun")):
            updated = orchestrator._ensure_tests(self.ledger.get(GH_REPO, 71))
        self.assertFalse(updated["coding"]["blocked"])
        self.assertEqual(updated["coding"]["tests"]["status"], "passed")


class TestWorkspaceAdoptionSafety(unittest.TestCase):
    """S3: lstat-first workspace adoption and task identity binding."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        os.chmod(self.work, 0o700)
        self.repo = make_repository(self.root)
        self.identity = {**fake_identity(), "uid": os.getuid(), "gid": os.getgid()}
        self.git = SafeGit(identity=None, allow_file_protocol=True)
        with mock.patch("agent_handoff.dispatch.coding.workspace.os.geteuid", return_value=0), mock.patch(
            "agent_handoff.dispatch.coding.workspace.os.chown"
        ):
            self.workspace = prepare_workspace(
                workspace_root=str(self.work),
                repository=PROJECT_REPO,
                base_branch="main",
                base_commit=self.repo["base"],
                source=str(self.repo["source"]),
                issue_number=81,
                task_id="t-one",
                identity=self.identity,
                git=self.git,
            )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _adopt(self, *, issue: int = 81, task_id: str = "t-one", target: str | None = None):
        return prepare_workspace(
            workspace_root=str(self.work),
            repository=PROJECT_REPO,
            base_branch="main",
            base_commit=self.repo["base"],
            source=str(self.repo["source"]),
            issue_number=issue,
            task_id=task_id,
            identity=self.identity,
            git=self.git,
            allow_existing=True,
            require_clean=True,
        )

    def test_s3_adopts_correct_existing_workspace(self) -> None:
        adopted = self._adopt()
        self.assertTrue(adopted["recovered"])
        self.assertEqual(adopted["workspace"], self.workspace["workspace"])

    def test_s3_rejects_symlink_to_another_task(self) -> None:
        link = self.work / "issue-82-t-two"
        os.symlink(self.workspace["workspace"], link)
        with self.assertRaises(WorkspacePreparationError):
            self._adopt(issue=82, task_id="t-two")

    def test_s3_rejects_incorrect_task_basename(self) -> None:
        other = self.work / "issue-81-other"
        shutil.copytree(self.workspace["workspace"], other, symlinks=True)
        with self.assertRaises(WorkspacePreparationError):
            _inspect_existing_workspace(
                target=str(other),
                expected_name="issue-81-t-one",
                workspace_root=str(self.work),
                repository=PROJECT_REPO,
                base_branch="main",
                base_commit=self.repo["base"],
                source=str(self.repo["source"]),
                identity=self.identity,
                git=self.git,
                require_clean=True,
            )

    def test_s3_rejects_escaped_path(self) -> None:
        outside = self.root / "outside"
        shutil.copytree(self.workspace["workspace"], outside, symlinks=True)
        with self.assertRaises(WorkspacePreparationError):
            _inspect_existing_workspace(
                target=str(outside),
                expected_name="issue-81-t-one",
                workspace_root=str(self.work),
                repository=PROJECT_REPO,
                base_branch="main",
                base_commit=self.repo["base"],
                source=str(self.repo["source"]),
                identity=self.identity,
                git=self.git,
                require_clean=True,
            )

    def test_s3_rejects_unsafe_git_permissions(self) -> None:
        os.chmod(Path(self.workspace["workspace"]) / ".git", 0o777)
        with self.assertRaises(WorkspacePreparationError):
            self._adopt()

    def test_s3_rejects_foreign_owner(self) -> None:
        with self.assertRaises(WorkspacePreparationError):
            prepare_workspace(
                workspace_root=str(self.work),
                repository=PROJECT_REPO,
                base_branch="main",
                base_commit=self.repo["base"],
                source=str(self.repo["source"]),
                issue_number=81,
                task_id="t-one",
                identity={**self.identity, "uid": 424242, "gid": 424242},
                git=self.git,
                allow_existing=True,
            )

    def test_s3_rejects_mismatched_base_commit(self) -> None:
        git(["commit", "--allow-empty", "-q", "-m", "advance"], Path(self.workspace["workspace"]))
        with self.assertRaises(WorkspacePreparationError):
            self._adopt()

    def test_s3_rejects_changed_repository_identity(self) -> None:
        git(["remote", "set-url", "origin", "https://github.com/other/repository.git"], Path(self.workspace["workspace"]))
        with self.assertRaises(WorkspacePreparationError):
            self._adopt()

    def test_s3_cleanup_never_removes_another_task(self) -> None:
        other = self.work / "issue-81-other"
        shutil.copytree(self.workspace["workspace"], other, symlinks=True)
        removed = cleanup_workspace(
            str(self.work), str(self.workspace["workspace"]), expected_name="issue-81-other"
        )
        self.assertFalse(removed)
        self.assertTrue(Path(self.workspace["workspace"]).exists())

    def test_s3_resume_adoption_uses_modified_workspace_only_with_manifest(self) -> None:
        workspace = Path(self.workspace["workspace"])
        (workspace / "src" / "app.py").write_text("value = 99\n", encoding="utf-8")
        with self.assertRaises(WorkspacePreparationError):
            self._adopt()
        adopted = prepare_workspace(
            workspace_root=str(self.work),
            repository=PROJECT_REPO,
            base_branch="main",
            base_commit=self.repo["base"],
            source=str(self.repo["source"]),
            issue_number=81,
            task_id="t-one",
            identity=self.identity,
            git=self.git,
            allow_existing=True,
            require_clean=False,
        )
        self.assertTrue(adopted["modified"])


class TestCopyRenameSemantics(unittest.TestCase):
    """S4: copies never delete their source; renames do."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = make_repository(self.root)
        self.workspace = self.root / "task"
        shutil.copytree(self.repo["source"], self.workspace)
        self.git = SafeGit(identity=None, allow_file_protocol=True)
        self.limits = {"max_files_changed": 10, "max_file_bytes": 50000, "max_diff_bytes": 200000, "max_workspace_bytes": 1000000}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _validate(self, entries: list[ChangeEntry], *, protected: list[str] | None = None) -> dict:
        with mock.patch("agent_handoff.dispatch.coding.changes.collect_status", return_value=entries):
            return validate_changes(
                str(self.workspace),
                git=self.git,
                limits=self.limits,
                protected_paths=protected or [],
            )

    def _publisher(self) -> TrustedPublisher:
        staging = self.root / "publication"
        staging.mkdir(exist_ok=True)
        os.chmod(staging, 0o700)
        publisher = TrustedPublisher(
            repository=PROJECT_REPO,
            base_branch="main",
            branch_prefix="agent-handoff/",
            staging_root=str(staging),
            token_provider=lambda: "fake-token",
            remote_url=f"file://{self.repo['bare']}",
            git=SafeGit(identity=None, allow_file_protocol=True),
        )
        with mock.patch.object(publisher, "verify_repository", return_value={"repository": PROJECT_REPO}):
            publisher.prepare(base_commit=self.repo["base"], task_id="t-copy", run_id="r-copy")
        return publisher

    def _commit(self, publisher: TrustedPublisher, evidence: dict) -> dict:
        return publisher.stage_and_commit(
            validation=evidence,
            task_workspace=str(self.workspace),
            project="Alpha",
            title="Copy rename",
            issue_number=91,
            task_id="t-copy",
            run_id="r-copy",
            base_commit=self.repo["base"],
        )

    def test_s4_parse_status_distinguishes_copy_and_rename(self) -> None:
        entries = parse_status("C  src/copy.py\x00src/app.py\x00R  src/renamed.py\x00src/old.py\x00")
        self.assertEqual(entries[0].status, "C ")
        self.assertEqual(entries[0].old_path, "src/app.py")
        self.assertEqual(entries[1].status, "R ")
        self.assertEqual(entries[1].old_path, "src/old.py")

    def test_s4_copy_only_keeps_source_and_commits_copy(self) -> None:
        shutil.copy2(self.workspace / "src" / "app.py", self.workspace / "src" / "copied.py")
        evidence = self._validate([ChangeEntry(path="src/copied.py", status="C ", old_path="src/app.py")])
        self.assertEqual(evidence["status"], "passed", evidence)
        statuses = {item["path"]: item["status"] for item in evidence["files"]}
        self.assertEqual(statuses.get("src/copied.py"), "copied")
        self.assertNotIn("src/app.py", statuses)
        publisher = self._publisher()
        committed = self._commit(publisher, evidence)
        tree = git(["ls-tree", "-r", "--name-only", "HEAD"], Path(committed["staging"])).stdout.split()
        self.assertIn("src/app.py", tree)
        self.assertIn("src/copied.py", tree)
        self.assertEqual(
            (Path(committed["staging"]) / "src" / "app.py").read_text(encoding="utf-8"),
            "value = 1\n",
        )

    def test_s4_rename_only_deletes_source(self) -> None:
        git(["mv", "src/app.py", "src/renamed.py"], self.workspace)
        evidence = self._validate(
            [ChangeEntry(path="src/renamed.py", status="R ", old_path="src/app.py")]
        )
        self.assertEqual(evidence["status"], "passed", evidence)
        statuses = {item["path"]: item["status"] for item in evidence["files"]}
        self.assertEqual(statuses.get("src/renamed.py"), "renamed")
        self.assertEqual(statuses.get("src/app.py"), "deleted")
        publisher = self._publisher()
        committed = self._commit(publisher, evidence)
        tree = git(["ls-tree", "-r", "--name-only", "HEAD"], Path(committed["staging"])).stdout.split()
        self.assertIn("src/renamed.py", tree)
        self.assertNotIn("src/app.py", tree)

    def test_s4_copy_with_source_modification_keeps_both_changes(self) -> None:
        source = self.workspace / "src" / "app.py"
        (self.workspace / "src" / "copied.py").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        source.write_text("value = 123\n", encoding="utf-8")
        evidence = self._validate([ChangeEntry(path="src/copied.py", status="C ", old_path="src/app.py")])
        self.assertEqual(evidence["status"], "passed", evidence)
        statuses = {item["path"]: item["status"] for item in evidence["files"]}
        self.assertEqual(statuses.get("src/copied.py"), "copied")
        self.assertEqual(statuses.get("src/app.py"), "modified")

    def test_s4_rename_with_destination_modification(self) -> None:
        git(["mv", "src/app.py", "src/renamed.py"], self.workspace)
        (self.workspace / "src" / "renamed.py").write_text("value = 456\n", encoding="utf-8")
        evidence = self._validate([ChangeEntry(path="src/renamed.py", status="RM", old_path="src/app.py")])
        self.assertEqual(evidence["status"], "passed", evidence)
        statuses = {item["path"]: item["status"] for item in evidence["files"]}
        self.assertEqual(statuses.get("src/renamed.py"), "renamed")
        self.assertEqual(statuses.get("src/app.py"), "deleted")

    def test_s4_copy_from_and_to_protected_paths_is_blocked(self) -> None:
        (self.workspace / ".ssh").mkdir()
        (self.workspace / ".ssh" / "config").write_text("Host x\n", encoding="utf-8")
        (self.workspace / "src" / "copied.py").write_text("x = 1\n", encoding="utf-8")
        from_protected = self._validate(
            [ChangeEntry(path="src/copied.py", status="C ", old_path=".ssh/config")]
        )
        self.assertEqual(from_protected["status"], "blocked")
        self.assertTrue(any(".ssh/config" in item for item in from_protected["blocked"]))
        tracked_source = self._validate(
            [ChangeEntry(path="src/copied.py", status="C ", old_path="src/app.py")],
            protected=["src/app.py"],
        )
        self.assertEqual(tracked_source["status"], "blocked")
        self.assertTrue(any("copied from" in item for item in tracked_source["blocked"]))

        (self.workspace / "src" / "ordinary.py").write_text("y = 1\n", encoding="utf-8")
        (self.workspace / "credentials").mkdir()
        (self.workspace / "credentials" / "auth").write_text("z = 1\n", encoding="utf-8")
        to_protected = self._validate(
            [ChangeEntry(path="credentials/auth", status="C ", old_path="src/ordinary.py")]
        )
        self.assertEqual(to_protected["status"], "blocked")
        self.assertTrue(any("credentials/auth" in item for item in to_protected["blocked"]))

    def test_s4_committed_tree_matches_manifest_exactly(self) -> None:
        shutil.copy2(self.workspace / "src" / "app.py", self.workspace / "src" / "copied.py")
        evidence = self._validate(
            [
                ChangeEntry(path="src/copied.py", status="C ", old_path="src/app.py"),
                ChangeEntry(path="src/extra.py", status="??"),
            ]
        )
        (self.workspace / "src" / "extra.py").write_text("EXTRA = 1\n", encoding="utf-8")
        evidence = self._validate(
            [
                ChangeEntry(path="src/copied.py", status="C ", old_path="src/app.py"),
                ChangeEntry(path="src/extra.py", status="??"),
            ]
        )
        self.assertEqual(evidence["status"], "passed", evidence)
        publisher = self._publisher()
        committed = self._commit(publisher, evidence)
        tree = set(git(["ls-tree", "-r", "--name-only", "HEAD"], Path(committed["staging"])).stdout.split())
        changed = {item["path"] for item in evidence["manifest"]["files"]}
        self.assertTrue(changed.issubset(tree))
        self.assertEqual(git(["status", "--porcelain"], Path(committed["staging"])).stdout.strip(), "")


class TestProtectedPathCoverage(unittest.TestCase):
    """S5: root-level and nested sensitive paths are always protected."""

    def test_s5_root_and_nested_sensitive_paths(self) -> None:
        from agent_handoff.dispatch.coding.config import is_protected_path

        paths = [
            ".ssh/config",
            "sub/.ssh/config",
            "credentials/auth",
            "sub/credentials/auth",
            ".askpass.sh",
            "sub/.askpass.sh",
            ".env",
            ".env.local",
            "sub/.env",
            "auth.json",
            "sub/auth.json",
            "rclone.conf",
            "sub/rclone.conf",
            ".gitconfig",
            "sub/.gitconfig",
            ".git-credentials",
            ".netrc",
            ".gitattributes",
        ]
        for path in paths:
            self.assertTrue(is_protected_path(path, []), path)
        self.assertFalse(is_protected_path("src/ordinary.py", []))
        self.assertFalse(is_protected_path("docs/environment.md", []))

    def test_s5_validate_changes_blocks_root_and_nested_paths_but_allows_source(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        try:
            root = Path(tmp.name)
            repo = make_repository(root)
            workspace = root / "task"
            shutil.copytree(repo["source"], workspace)
            (workspace / ".ssh").mkdir()
            (workspace / ".ssh" / "config").write_text("Host x\n", encoding="utf-8")
            (workspace / "nested").mkdir()
            (workspace / "nested" / ".ssh").mkdir()
            (workspace / "nested" / ".ssh" / "config").write_text("Host y\n", encoding="utf-8")
            (workspace / "src" / "ordinary.py").write_text("OK = True\n", encoding="utf-8")
            evidence = validate_changes(
                str(workspace),
                git=SafeGit(identity=None, allow_file_protocol=True),
                limits={"max_files_changed": 10, "max_file_bytes": 50000, "max_diff_bytes": 200000, "max_workspace_bytes": 1000000},
                protected_paths=[],
            )
            self.assertEqual(evidence["status"], "blocked")
            blocked = " ".join(evidence["blocked"])
            self.assertIn(".ssh/config", blocked)
            self.assertIn("nested/.ssh/config", blocked)
        finally:
            tmp.cleanup()



class TestDurableMilestoneReconciliation(unittest.TestCase):
    """S7: reconcile milestones from authoritative evidence after a crash."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.h = DispatchHarness(self.root)
        self.ledger = Ledger(self.h.config)
        self.runner = DispatchRunner(
            self.h.config,
            {
                "repository": GH_REPO,
                "allowed_users": ["dev"],
                "github": {"command": "gh", "require_private": True},
                "execution": {},
                "projects": {},
            },
            progress=None,
        )
        self.runner.summary = {
            "ok": True, "busy": False, "executed": 0, "rejected": 0, "needs_attention": 0,
            "uploaded": 0, "published": 0, "errors": [], "warnings": [],
        }
        project = {"name": "Alpha", "core_name": "Alpha", "core_key": "alpha", "coding": {"enabled": True}}
        record = new_dispatch_record(
            repository=GH_REPO,
            issue={"number": 101, "title": "reconcile", "html_url": "https://github.test/issues/101"},
            project=project,
            agent="opencode",
            model="provider/model-fast",
            request={"instructions": "test"},
            task_type="coding",
            coding=new_coding_state(project),
        )
        record["capture"].update({"status": "CAPTURED", "task_id": "t-rec", "run_id": "r-rec"})
        record["coding"]["base_commit"] = "a" * 40
        record["coding"]["publication"].update(
            {"branch": "agent-handoff/alpha/issue-101-t-rec", "commit": "b" * 40, "branch_pushed": True,
             "pr_number": 8, "pr_url": "https://github.test/dev/coding-repo/pull/8"}
        )
        record["coding"]["tests"] = {"status": "passed", "commands": [{"status": "passed", "exit_code": 0}]}
        record["coding"]["diff"] = {"status": "passed"}
        self.record, _ = self.ledger.create(record)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _update(self, mutator) -> dict:
        return self.ledger.update(GH_REPO, 101, mutator, note="simulate crash")

    def test_s7_report_verified_reconciled_from_verified_upload(self) -> None:
        self._update(lambda item: item.update({"status": "UPLOAD_VERIFIED"}))
        self._update(
            lambda item: item["upload"].update(
                {"status": "VERIFIED", "verified": True, "verified_at": "2026-01-01T00:00:00Z",
                 "report_url": "https://drive.google.com/file/d/rec/view"}
            )
        )
        updated = self.runner._reconcile_coding_milestones(self.ledger.get(GH_REPO, 101))
        self.assertTrue(updated["coding"]["milestones"]["REPORT_VERIFIED"])
        self.assertEqual(updated["coding"]["stage"], "UPLOAD_VERIFIED")

    def test_s7_issue_delivered_reconciled_after_primary_status_update(self) -> None:
        self._update(
            lambda item: item.update(
                {
                    "status": "ISSUE_UPDATED",
                    "upload": {"verified": True, "report_url": "https://drive.google.com/file/d/rec/view"},
                    "publication": {"status": "PUBLISHED", "published_at": "2026-01-01T00:00:10Z"},
                }
            )
        )
        updated = self.runner._reconcile_coding_milestones(self.ledger.get(GH_REPO, 101))
        self.assertTrue(updated["coding"]["milestones"]["ISSUE_DELIVERED"])
        self.assertTrue(updated["coding"]["milestones"]["REPORT_DELIVERED"])
        self.assertEqual(updated["coding"]["stage"], "ISSUE_UPDATED")

    def test_s7_pr_milestones_reconciled_without_duplicate_pr(self) -> None:
        updated = self.runner._reconcile_coding_milestones(self.ledger.get(GH_REPO, 101))
        self.assertTrue(updated["coding"]["milestones"]["PR_CREATED"])
        self.assertTrue(updated["coding"]["milestones"]["PR_PUBLISHED"])
        self.assertEqual(updated["coding"]["publication"]["pr_url"], "https://github.test/dev/coding-repo/pull/8")

    def test_s7_failed_tests_never_gain_tests_passed_milestone(self) -> None:
        self._update(
            lambda item: (
                item["coding"].update({"tests": {"status": "failed"}}),
                item["coding"]["milestones"].update({"TESTS_PASSED": True}),
            )
        )
        updated = self.runner._reconcile_coding_milestones(self.ledger.get(GH_REPO, 101))
        self.assertFalse(updated["coding"]["milestones"].get("TESTS_PASSED"))
        self.assertEqual(milestone_state(updated["coding"], "TESTS_PASSED"), "false")


class TestPublisherRecoveryGuards(unittest.TestCase):
    """S8: PR state/SHA checks and corrupt staging refusal."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.staging = self.root / "publication"
        self.staging.mkdir()
        os.chmod(self.staging, 0o700)
        self.publisher = TrustedPublisher(
            repository=PROJECT_REPO,
            base_branch="main",
            branch_prefix="agent-handoff/",
            staging_root=str(self.staging),
            token_provider=lambda: "fake-token",
            git=SafeGit(identity=None, allow_file_protocol=True),
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _pr(self, **overrides) -> dict:
        value = {
            "number": 5,
            "html_url": "https://github.test/dev/coding-repo/pull/5",
            "state": "open",
            "draft": True,
            "head": {"ref": "agent-handoff/alpha/issue-1-t", "sha": "c" * 40, "repo": {"full_name": PROJECT_REPO}},
            "base": {"ref": "main"},
        }
        value.update(overrides)
        return value

    def test_s8_closed_or_moved_pr_is_rejected(self) -> None:
        with self.assertRaises(PublicationError):
            self.publisher._validate_pr(self._pr(state="closed"), "agent-handoff/alpha/issue-1-t", "c" * 40)
        with self.assertRaises(PublicationError):
            self.publisher._validate_pr(self._pr(head={"ref": "agent-handoff/alpha/issue-1-t", "sha": "d" * 40}), "agent-handoff/alpha/issue-1-t", "c" * 40)
        with self.assertRaises(PublicationError):
            self.publisher._validate_pr(self._pr(draft=False), "agent-handoff/alpha/issue-1-t", "c" * 40)

    def test_s8_corrupt_persisted_staging_is_refused(self) -> None:
        corrupt = self.staging / "t-corrupt-r-corrupt"
        corrupt.mkdir()
        with self.assertRaises(PublicationError):
            self.publisher.adopt_staging(str(corrupt))

    def test_s8_symlinked_persisted_staging_is_refused(self) -> None:
        target = self.root / "elsewhere"
        target.mkdir()
        link = self.staging / "t-link-r-link"
        os.symlink(target, link)
        with self.assertRaises(PublicationError):
            self.publisher.adopt_staging(str(link))


class TestDeliveryConcurrency(unittest.TestCase):
    """S9: manual and automatic delivery share a durable Issue lock."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.h = DispatchHarness(self.root)
        self.ledger = Ledger(self.h.config)
        self.runner = DispatchRunner(
            self.h.config,
            {
                "repository": GH_REPO,
                "allowed_users": ["dev"],
                "github": {"command": "gh", "require_private": True},
                "execution": {},
                "projects": {},
            },
            progress=None,
        )
        self.runner.summary = {
            "ok": True, "busy": False, "executed": 0, "rejected": 0, "needs_attention": 0,
            "uploaded": 0, "published": 0, "errors": [], "warnings": [],
        }
        project = {"name": "Alpha", "core_name": "Alpha", "core_key": "alpha"}
        self.record = new_dispatch_record(
            repository=GH_REPO,
            issue={"number": 111, "title": "concurrent", "html_url": "https://github.test/issues/111"},
            project=project,
            agent="opencode",
            model="provider/model-fast",
            request={"instructions": "test"},
        )
        self.record["status"] = "UPLOAD_VERIFIED"
        self.record["capture"].update({"status": "CAPTURED", "task_id": "t-con", "run_id": "r-con"})
        self.record["upload"].update(
            {"status": "VERIFIED", "verified": True, "verified_at": "2026-01-01T00:00:00Z",
             "report_url": "https://drive.google.com/file/d/con/view"}
        )
        self.record, _ = self.ledger.create(self.record)

        class Transport:
            def __init__(self) -> None:
                self.comments: list[dict] = []
                self.lock = threading.Lock()

            def find_comment(self, issue_number, marker):
                with self.lock:
                    for comment in self.comments:
                        if marker in comment["body"]:
                            return comment
                return None

            def comment(self, issue_number, body):
                time.sleep(0.05)
                with self.lock:
                    comment = {
                        "body": body,
                        "html_url": f"https://github.test/issues/{issue_number}#issuecomment-{len(self.comments) + 1}",
                        "created_at": utcnow_iso(),
                    }
                    self.comments.append(comment)
                    return comment

        self.transport = Transport()
        self.runner.transport = self.transport

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_s9_concurrent_delivery_posts_exactly_one_comment(self) -> None:
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def deliver() -> None:
            try:
                barrier.wait(timeout=5)
                self.runner._publish(self.record, force=True)
            except BaseException as exc:  # noqa: BLE001 - collected for the assertion
                errors.append(exc)

        threads = [threading.Thread(target=deliver) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(len(self.transport.comments), 1)
        final = self.ledger.get(GH_REPO, 111)
        self.assertEqual(final["status"], "ISSUE_UPDATED")
        self.assertEqual(final["publication"]["status"], "PUBLISHED")




class TestProductionPreservation(unittest.TestCase):
    def test_t27_t28_historical_ledger_and_json_status_remain_valid(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        try:
            h = DispatchHarness(Path(tmp.name))
            ledger = Ledger(h.config)
            record, _ = ledger.create(
                new_dispatch_record(
                    repository=GH_REPO,
                    issue={"number": 91, "title": "historical", "html_url": "https://github.test/issues/91"},
                    project={"name": "Alpha", "core_name": "Alpha", "workspace": str(h.project_path), "execution": {}},
                    agent="opencode",
                    model="provider/model-fast",
                    request={"instructions": "legacy"},
                )
            )
            record.pop("task_type", None)
            record["status"] = "ISSUE_UPDATED"
            ledger.update(GH_REPO, 91, lambda item: item.pop("task_type", None))
            loaded = ledger.get(GH_REPO, 91)
            self.assertNotIn("task_type", loaded)
            view = record_status_view(loaded)
            self.assertEqual(view["task_type"], "read_only")
            self.assertIsNone(view["coding"])
            json.dumps(view)
        finally:
            tmp.cleanup()

    def test_t38_config_validation_does_not_mutate_files(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        try:
            h = Harness(Path(tmp.name))
            before = (h.home / "config" / "config.json").read_bytes()
            validate_dispatch_config(h.config, check_paths=False)
            after = (h.home / "config" / "config.json").read_bytes()
            self.assertEqual(before, after)
        finally:
            tmp.cleanup()

    def test_t40_rabet_yakfi_legacy_remain_read_only(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        try:
            h = DispatchHarness(Path(tmp.name))
            config = json.loads(json.dumps(h.config))
            config["dispatch"]["coding"] = {"enabled": False}
            for name in ("Rabet", "Yakfi"):
                path = Path(tmp.name) / name.lower()
                path.mkdir()
                config["projects"][name.lower()] = {
                    "name": name,
                    "drive_folder_id": f"{name.lower()}_folder_0001",
                    "paths": [str(path)],
                    "path_prefixes": [],
                    "aliases": [name.lower()],
                    "auto_registered": False,
                }
                config["dispatch"]["projects"][name] = {"workspace": str(path)}
            resolved, errors, _warnings = validate_dispatch_config(config, check_paths=False)
            self.assertEqual(errors, [])
            for name in ("Rabet", "Yakfi"):
                self.assertFalse(resolved["projects"][name]["coding"]["enabled"])
                self.assertNotEqual(
                    resolved["projects"][name]["execution"]["permission_profile"], "workspace_write"
                )
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
