from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
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
    SafeGit,
    scan_bytes_for_secrets,
    validate_changes,
)
from agent_handoff.dispatch.coding.config import (  # noqa: E402
    parse_coding_config,
    prefix_valid,
    validate_branch_name,
)
from agent_handoff.dispatch.coding.pipeline import CodingOrchestrator  # noqa: E402
from agent_handoff.dispatch.coding.publication import (  # noqa: E402
    PublicationError,
    TrustedPublisher,
    build_branch,
    build_pr_body,
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
    prepare_workspace,
)
from agent_handoff.dispatch.config import validate_dispatch_config  # noqa: E402
from agent_handoff.dispatch.ledger import Ledger, new_coding_state, new_dispatch_record  # noqa: E402
from agent_handoff.dispatch.status import format_status_view, record_status_view  # noqa: E402
from agent_handoff.dispatch.requests import (  # noqa: E402
    RequestError,
    parse_issue_request,
    resolve_request,
)
from agent_handoff.dispatch.runner import DispatchRunner  # noqa: E402
from agent_handoff.dispatch.security import DEFAULT_CHILD_PATH, IsolationError, popen_isolation_kwargs  # noqa: E402
from agent_handoff.notify.messages import build_telegram_message  # noqa: E402
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
            git=SafeGit(identity=None),
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
            git=SafeGit(identity=None),
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
            git=SafeGit(identity=None),
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
                git=SafeGit(identity=None),
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
            git=SafeGit(identity=None),
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
                    git=SafeGit(identity=None),
                )

    def test_t13_t24_diff_and_commit_from_validated_files(self) -> None:
        prepared = self._prepare(issue=9, task_id="t-change")
        workspace = Path(prepared["workspace"])
        (workspace / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")
        (workspace / "src" / "new.py").write_text("NEW = True\n", encoding="utf-8")
        evidence = validate_changes(
            str(workspace),
            git=SafeGit(identity=None),
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
            git=SafeGit(identity=None),
        )
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
            git=SafeGit(identity=None),
        )

    def _commit_change(self, publisher: TrustedPublisher) -> tuple[str, dict]:
        workspace = self.work / "task"
        shutil.copytree(self.repo["source"], workspace)
        (workspace / "src" / "app.py").write_text("value = 3\n", encoding="utf-8")
        evidence = validate_changes(
            str(workspace),
            git=SafeGit(identity=None),
            limits={"max_files_changed": 10, "max_file_bytes": 50000, "max_diff_bytes": 200000, "max_workspace_bytes": 1000000},
            protected_paths=[],
        )
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


class CodingIntegrationTests(unittest.TestCase):
    """In-process C2 pipeline with synthetic credentials and a local trusted remote."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.h = DispatchHarness(self.root)
        self.repo = make_repository(self.root)
        self.work = self.root / "coding-work"
        self.work.mkdir()
        self.staging = self.root / "coding-staging"
        self.staging.mkdir()
        os.chmod(self.staging, 0o700)
        self.identity = fake_identity()
        self.dispatch_cfg = self._dispatch_config()
        self.ledger = Ledger(self.h.config)
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
            git=SafeGit(identity=None),
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
            git=SafeGit(identity=None),
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


class TestProductionPreservation(unittest.TestCase):
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
