from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
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
from test_dispatch import DispatchHarness, request_body  # noqa: E402

from agent_handoff.dispatch.adapters.base import ExecutionRequest  # noqa: E402
from agent_handoff.dispatch.adapters.opencode import (  # noqa: E402
    READ_ONLY_POLICY,
    WORKSPACE_WRITE_POLICY,
    OpenCodeAdapter,
)
from agent_handoff.dispatch.config import (  # noqa: E402
    _parse_isolation,
    _resolve_project_execution,
    resolve_model,
    validate_dispatch_config,
)
from agent_handoff.dispatch.requests import RequestError, parse_issue_request  # noqa: E402
from agent_handoff.dispatch.security import (  # noqa: E402
    DEFAULT_CHILD_PATH,
    IdentityError,
    IsolationError,
    WorkspaceSecurityError,
    build_isolated_environment,
    check_clean_git_workspace,
    popen_isolation_kwargs,
    resolve_child_identity,
    validate_model_auth,
    validate_workspace_filesystem,
    validate_workspace_path,
)
from agent_handoff.notify.config import validate_notification_config  # noqa: E402

SYNTHETIC_TELEGRAM_TOKEN = "123456789:AAIsolationFakeSecret_1234567890"
SECRET_NAMES = (
    "AGENT_HANDOFF_TELEGRAM_TOKEN",
    "AGENT_HANDOFF_HOME",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_HOST",
    "GITHUB_TOKEN_PATH",
    "RCLONE_CONFIG",
    "RCLONE_CONFIG_PASS",
    "OPENAI_API_KEY",
    "AGENT_HANDOFF_TEST_SECRET",
    "GH_ENTERPRISE_TOKEN",
    "RCLONE_TEST_SECRET",
)
UID = 4242
GID = 4242


def fake_identity(
    *,
    uid: int = UID,
    gid: int = GID,
    home: str = "/srv/agent-handoff/home/agent-handoff-dev",
) -> dict:
    return {
        "user": "agent-handoff-dev",
        "uid": uid,
        "group": "agent-handoff-dev",
        "gid": gid,
        "groups": [],
        "home": home,
        "config_home": f"{home}/.config",
        "data_home": f"{home}/.local/share",
        "cache_home": f"{home}/.cache",
        "tmp_dir": f"{home}/tmp",
        "child_path": DEFAULT_CHILD_PATH,
        "model_auth_path": f"{home}/.local/share/opencode/auth.json",
    }


def fake_account(uid: int = UID, gid: int = 4242) -> SimpleNamespace:
    return SimpleNamespace(pw_uid=uid, pw_gid=gid, pw_name="agent-handoff-dev")


def isolation_config(harness: Harness, *, roots: list[str] | None = None) -> dict:
    config = copy.deepcopy(harness.config)
    config["dispatch"]["execution"]["isolation"] = {
        "enabled": True,
        "environment": "allowlist",
        "identity": {
            "user": "agent-handoff-dev",
            "group": "agent-handoff-dev",
            "home": "/srv/agent-handoff/home/agent-handoff-dev",
        },
        "workspace_roots": roots or [str(harness.workspace)],
        "child_path": DEFAULT_CHILD_PATH,
    }
    return config


def identity_patches(*, uid: int = UID, gid: int = 4242, groups: list[int] | None = None):
    group_records = {
        4242: SimpleNamespace(gr_gid=4242, gr_name="agent-handoff-dev"),
        0: SimpleNamespace(gr_gid=0, gr_name="root"),
    }
    return (
        mock.patch("agent_handoff.dispatch.security.pwd.getpwnam", return_value=fake_account(uid, gid)),
        mock.patch(
            "agent_handoff.dispatch.security.grp.getgrnam",
            return_value=SimpleNamespace(gr_gid=4242, gr_name="agent-handoff-dev"),
        ),
        mock.patch(
            "agent_handoff.dispatch.security.grp.getgrgid",
            side_effect=lambda value: group_records.get(value, SimpleNamespace(gr_gid=value, gr_name=f"g{value}")),
        ),
        mock.patch(
            "agent_handoff.dispatch.security.os.getgrouplist",
            return_value=groups if groups is not None else [4242],
        ),
    )


class C1ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.h = DispatchHarness(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_t01_legacy_config_is_unchanged_and_still_valid(self) -> None:
        before = copy.deepcopy(self.h.config)
        resolved, errors, warnings = validate_dispatch_config(self.h.config)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])
        self.assertEqual(self.h.config, before)
        self.assertFalse(resolved["execution"]["isolation"]["enabled"])
        self.assertEqual(resolved["projects"]["Alpha"]["execution"]["permission_profile"], "read_only")
        self.assertFalse(resolved["projects"]["Alpha"]["execution"]["allow_write"])

    def test_t02_t03_rabet_and_yakfi_inherit_read_only(self) -> None:
        config = copy.deepcopy(self.h.config)
        for name, folder in (("Rabet", "rabet_folder_0001"), ("Yakfi", "yakfi_folder_0001")):
            path = self.h.workspace / name.lower()
            path.mkdir(parents=True, exist_ok=True)
            config["projects"][name.lower()] = {
                "name": name,
                "drive_folder_id": folder,
                "paths": [str(path)],
                "path_prefixes": [],
                "aliases": [name.lower()],
                "auto_registered": False,
            }
            config["dispatch"]["projects"][name] = {"workspace": str(path)}
        resolved, errors, _warnings = validate_dispatch_config(config)
        self.assertEqual(errors, [])
        for name in ("Rabet", "Yakfi"):
            policy = resolved["projects"][name]["execution"]
            self.assertEqual(policy["permission_profile"], "read_only")
            self.assertFalse(policy["allow_write"])
        read_only_request = ExecutionRequest(
            model="provider/model-fast",
            workspace=str(self.h.project_path),
            title="Read only",
            instructions="Read only.",
            permission_profile="read_only",
            allow_write=False,
            timeout_seconds=30,
        )
        adapter = OpenCodeAdapter({"permission_profile": "read_only", "allow_write": False})
        self.assertEqual(adapter._command(read_only_request)[4], "plan")

    def test_t04_valid_project_specific_write_policy_executes_per_project(self) -> None:
        self.h.config["dispatch"]["projects"]["Alpha"]["execution"] = {
            "permission_profile": "workspace_write",
            "allow_write": True,
        }
        self.h.write_config()
        self.h.add_issue(number=1, body=request_body(project="Alpha"))
        self.h.add_issue(number=2, body=request_body(project="Beta"))
        result = self.h.dispatch()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["executed"], 2)
        calls = self.h.opencode_calls()
        self.assertEqual(len(calls), 2)
        by_cwd = {call["cwd"]: call for call in calls}
        alpha = by_cwd[str(self.h.project_path.resolve())]
        beta = by_cwd[str(self.h.beta_path.resolve())]
        self.assertEqual(alpha["agent"], "build")
        self.assertEqual(json.loads(alpha["config_content"]), WORKSPACE_WRITE_POLICY)
        self.assertEqual(beta["agent"], "plan")
        self.assertEqual(json.loads(beta["config_content"]), READ_ONLY_POLICY)
        self.assertEqual(self.h.record(1)["project"]["execution"]["permission_profile"], "workspace_write")
        self.assertEqual(self.h.record(2)["project"]["execution"]["permission_profile"], "read_only")

    def test_t05_invalid_project_profile_is_rejected(self) -> None:
        errors: list[str] = []
        policy = _resolve_project_execution(
            {"permission_profile": "root"},
            {"permission_profile": "read_only", "allow_write": False},
            "dispatch project 'Alpha' execution",
            errors,
        )
        self.assertEqual(policy["permission_profile"], "read_only")
        self.assertTrue(any("permission_profile" in item for item in errors))

    def test_t06_write_without_explicit_project_permission_is_rejected(self) -> None:
        errors: list[str] = []
        policy = _resolve_project_execution(
            {"permission_profile": "workspace_write"},
            {"permission_profile": "workspace_write", "allow_write": True},
            "dispatch project 'Alpha' execution",
            errors,
        )
        self.assertFalse(policy["allow_write"])
        self.assertTrue(any("explicit allow_write=true" in item for item in errors))

        errors = []
        policy = _resolve_project_execution(
            {"allow_write": True},
            {"permission_profile": "read_only", "allow_write": False},
            "dispatch project 'Alpha' execution",
            errors,
        )
        self.assertFalse(policy["allow_write"])
        self.assertTrue(any("allow_write" in item for item in errors))

    def test_t07_issue_text_cannot_override_execution_policy(self) -> None:
        body = "\n".join(
            [
                "<!-- agent-handoff-request -->",
                "handoff-version: 1",
                "project: Alpha",
                "agent: opencode",
                "model: fast",
                "session: new",
                "permission_profile: workspace_write",
                "<!-- /agent-handoff-request -->",
                "",
                "Attempt to escalate.",
            ]
        )
        with self.assertRaises(RequestError):
            parse_issue_request(body)
        for field in ("uid", "gid", "allow_write", "workspace", "isolation"):
            bad = body.replace("permission_profile: workspace_write", f"{field}: value")
            with self.assertRaises(RequestError):
                parse_issue_request(bad)

    def test_t08_missing_dedicated_user_fails_closed(self) -> None:
        config = isolation_config(self.h)
        with mock.patch(
            "agent_handoff.dispatch.security.pwd.getpwnam", side_effect=KeyError("agent-handoff-dev")
        ):
            _resolved, errors, _warnings = validate_dispatch_config(config, check_paths=False)
        self.assertTrue(any("does not exist" in item for item in errors), errors)

    def test_t09_root_identity_is_rejected(self) -> None:
        config = isolation_config(self.h)
        config["dispatch"]["execution"]["isolation"]["identity"]["user"] = "root"
        config["dispatch"]["execution"]["isolation"]["identity"]["group"] = "root"
        _resolved, errors, _warnings = validate_dispatch_config(config, check_paths=False)
        self.assertTrue(any("must not be root" in item for item in errors), errors)

    def test_t11_privileged_supplementary_groups_are_rejected(self) -> None:
        config = isolation_config(self.h)
        patches = identity_patches(groups=[4242, 0])
        with patches[0], patches[1], patches[2], patches[3]:
            _resolved, errors, _warnings = validate_dispatch_config(config, check_paths=False)
        self.assertTrue(any("privileged group" in item for item in errors), errors)

    def test_isolation_config_is_valid_with_synthetic_identity(self) -> None:
        config = isolation_config(self.h)
        patches = identity_patches()
        with patches[0], patches[1], patches[2], patches[3]:
            resolved, errors, _warnings = validate_dispatch_config(config, check_paths=False)
        self.assertEqual(errors, [])
        isolation = resolved["execution"]["isolation"]
        self.assertTrue(isolation["enabled"])
        self.assertEqual(isolation["identity"]["uid"], UID)
        self.assertEqual(isolation["identity"]["model_auth_path"], "/srv/agent-handoff/home/agent-handoff-dev/.local/share/opencode/auth.json")
        self.assertEqual(resolved["projects"]["Alpha"]["execution"]["permission_profile"], "read_only")

    def test_t27_model_alias_resolution_is_unchanged(self) -> None:
        model, error = resolve_model(
            {"models": {"fast": "provider/model-fast"}, "default_model": "fast"}, "fast"
        )
        self.assertIsNone(error)
        self.assertEqual(model, "provider/model-fast")

    def test_t26_validation_does_not_mutate_config_or_environment(self) -> None:
        before = copy.deepcopy(self.h.config)
        with mock.patch.dict(os.environ, {"GH_TOKEN": "synthetic-parent-only"}, clear=False):
            validate_dispatch_config(self.h.config)
            self.assertEqual(os.environ["GH_TOKEN"], "synthetic-parent-only")
        self.assertEqual(self.h.config, before)

    def test_t30_runtime_config_without_coding_configuration_stays_compatible(self) -> None:
        minimal = {
            "projects": {
                "alpha": {
                    "name": "Alpha",
                    "drive_folder_id": "alpha_folder_0001",
                    "paths": [str(self.h.project_path)],
                    "aliases": ["alpha"],
                }
            },
            "dispatch": {
                "enabled": True,
                "repository": "dev/private-dispatch",
                "allowed_users": ["dev"],
                "models": {"fast": "provider/model-fast"},
                "projects": {"Alpha": {"workspace": str(self.h.project_path)}},
            },
        }
        resolved, errors, _warnings = validate_dispatch_config(minimal)
        self.assertEqual(errors, [])
        self.assertNotIn("isolation", minimal["dispatch"]["execution"] if "execution" in minimal["dispatch"] else {})
        self.assertFalse(resolved["execution"]["isolation"]["enabled"])


class C1IdentityAndEnvironmentTests(unittest.TestCase):
    def test_t10_wrong_workspace_uid_and_gid_are_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "work"
            workspace.mkdir()
            identity = fake_identity(uid=os.getuid() + 1, gid=os.getgid())
            with self.assertRaises(WorkspaceSecurityError) as owner_error:
                validate_workspace_filesystem(
                    str(workspace), identity, require_write=False, require_clean=False
                )
            self.assertIn("owned by", str(owner_error.exception))

            identity = fake_identity(uid=os.getuid(), gid=os.getgid() + 1)
            with self.assertRaises(WorkspaceSecurityError) as group_error:
                validate_workspace_filesystem(
                    str(workspace), identity, require_write=False, require_clean=False
                )
            self.assertIn("group", str(group_error.exception))

    def test_privilege_drop_kwargs_use_uid_gid_and_no_supplementary_groups(self) -> None:
        identity = fake_identity()
        with mock.patch("agent_handoff.dispatch.security.os.geteuid", return_value=0):
            kwargs = popen_isolation_kwargs(identity)
        self.assertEqual(kwargs["user"], UID)
        self.assertEqual(kwargs["group"], GID)
        self.assertEqual(kwargs["extra_groups"], [])
        self.assertEqual(kwargs["umask"], 0o077)
        with mock.patch("agent_handoff.dispatch.security.os.geteuid", return_value=1000):
            with self.assertRaises(IsolationError):
                popen_isolation_kwargs(identity)
        with self.assertRaises(IsolationError):
            popen_isolation_kwargs(fake_identity(uid=0))
        self.assertEqual(popen_isolation_kwargs(None), {})

    def test_t12_t13_t14_parent_secrets_are_not_inherited_and_parent_access_stays(self) -> None:
        identity = fake_identity()
        parent = {name: f"synthetic-{index}" for index, name in enumerate(SECRET_NAMES)}
        parent.update(
            {
                "PATH": "/root/.local/bin:/usr/bin",
                "LANG": "C.UTF-8",
                "AGENT_HANDOFF_TELEGRAM_TOKEN": SYNTHETIC_TELEGRAM_TOKEN,
            }
        )
        policy = READ_ONLY_POLICY
        env = build_isolated_environment(identity, workspace="/srv/work/repo", policy=policy, parent_env=parent)
        for name in SECRET_NAMES:
            self.assertNotIn(name, env)
        self.assertNotIn("PATH", {key for key in env if env[key].startswith("/root")})
        self.assertEqual(env["PATH"], DEFAULT_CHILD_PATH)
        self.assertEqual(env["LANG"], "C.UTF-8")
        self.assertEqual(env["PWD"], "/srv/work/repo")
        self.assertEqual(json.loads(env["OPENCODE_CONFIG_CONTENT"]), READ_ONLY_POLICY)

        notification_config = {
            "notifications": {
                "telegram": {
                    "enabled": True,
                    "chat_id": "-1001234567890",
                    "token_env": "AGENT_HANDOFF_TELEGRAM_TOKEN",
                }
            }
        }
        with mock.patch.dict(os.environ, {name: parent[name] for name in SECRET_NAMES}, clear=False):
            resolved, errors, _warnings = validate_notification_config(notification_config)
            self.assertEqual(errors, [])
            self.assertEqual(resolved["token"], SYNTHETIC_TELEGRAM_TOKEN)
            self.assertEqual(os.environ["GH_TOKEN"], parent["GH_TOKEN"])
            build_isolated_environment(identity, workspace="/srv/work/repo", policy=policy)
            self.assertEqual(os.environ["RCLONE_CONFIG"], parent["RCLONE_CONFIG"])
            self.assertEqual(os.environ["AGENT_HANDOFF_TELEGRAM_TOKEN"], SYNTHETIC_TELEGRAM_TOKEN)

    def test_t15_t16_child_home_xdg_and_model_auth_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "child-home"
            for directory in (
                home,
                home / ".config",
                home / ".local" / "share",
                home / ".cache",
                home / "tmp",
                home / ".local" / "share" / "opencode",
            ):
                directory.mkdir(parents=True, exist_ok=True)
            identity = fake_identity(uid=os.getuid(), gid=os.getgid(), home=str(home))
            auth = Path(identity["model_auth_path"])
            with self.assertRaises(IsolationError):
                validate_model_auth(identity)
            auth.write_text('{"provider": {"type": "synthetic"}}\n', encoding="utf-8")
            os.chmod(auth, 0o600)
            validate_model_auth(identity)
            env = build_isolated_environment(identity, workspace=str(home), policy=READ_ONLY_POLICY)
            self.assertEqual(env["HOME"], str(home))
            self.assertEqual(env["XDG_CONFIG_HOME"], str(home / ".config"))
            self.assertEqual(env["XDG_DATA_HOME"], str(home / ".local" / "share"))
            self.assertEqual(env["XDG_CACHE_HOME"], str(home / ".cache"))
            self.assertEqual(env["TMPDIR"], str(home / "tmp"))
            self.assertEqual(env["USER"], "agent-handoff-dev")
            self.assertEqual(env["LOGNAME"], "agent-handoff-dev")
            self.assertNotIn("/root", env["HOME"])
            self.assertNotIn("/root", env["XDG_CONFIG_HOME"])
            self.assertNotIn("/root", env["XDG_DATA_HOME"])
            self.assertNotIn("/root", env["XDG_CACHE_HOME"])

            link = Path(tmp) / "auth-link"
            link.symlink_to(auth)
            identity["model_auth_path"] = str(link)
            with self.assertRaises(IsolationError):
                validate_model_auth(identity)


class C1WorkspaceSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.repo = self.work / "repo"
        self.repo.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_t17_canonical_workspace_validation(self) -> None:
        with self.assertRaises(WorkspaceSecurityError):
            validate_workspace_path(
                f"{self.work}/a/../repo",
                workspace_roots=[str(self.work)],
                project_names=["Alpha"],
                denied_projects=[],
                check_paths=False,
            )
        with self.assertRaises(WorkspaceSecurityError):
            validate_workspace_path(
                f"{self.work}//repo",
                workspace_roots=[str(self.work)],
                project_names=["Alpha"],
                denied_projects=[],
                check_paths=False,
            )
        self.assertEqual(
            validate_workspace_path(
                str(self.repo),
                workspace_roots=[str(self.work)],
                project_names=["Alpha"],
                denied_projects=[],
                check_paths=False,
            ),
            str(self.repo),
        )

    def test_t18_symlink_escape_and_inside_symlink_are_rejected(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / ".git").mkdir()
        escape = self.work / "escape"
        escape.symlink_to(outside)
        with self.assertRaises(WorkspaceSecurityError):
            validate_workspace_path(
                str(escape),
                workspace_roots=[str(self.work)],
                project_names=["Alpha"],
                denied_projects=[],
                check_paths=False,
            )
        inside = self.work / "inside"
        inside.mkdir()
        (inside / ".git").mkdir()
        link = self.work / "inside-link"
        link.symlink_to(inside)
        with self.assertRaises(WorkspaceSecurityError):
            validate_workspace_path(
                str(link),
                workspace_roots=[str(self.work)],
                project_names=["Alpha"],
                denied_projects=[],
                check_paths=True,
            )

    def test_t19_dirty_tree_is_refused_without_resetting_work(self) -> None:
        if not shutil_which("git"):
            self.skipTest("git is not available")
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.invalid")
        self._git("config", "user.name", "Test")
        (self.repo / "tracked.txt").write_text("clean\n", encoding="utf-8")
        self._git("add", "tracked.txt")
        self._git("commit", "-q", "-m", "initial")
        check_clean_git_workspace(str(self.repo))
        dirty = self.repo / "local-work.txt"
        dirty.write_text("do not delete\n", encoding="utf-8")
        with self.assertRaises(WorkspaceSecurityError) as error:
            check_clean_git_workspace(str(self.repo))
        self.assertIn("uncommitted or untracked", str(error.exception))
        self.assertEqual(dirty.read_text(encoding="utf-8"), "do not delete\n")

    def test_t20_protected_and_denied_paths_are_rejected(self) -> None:
        with self.assertRaises(WorkspaceSecurityError):
            validate_workspace_path(
                "/etc",
                workspace_roots=["/"],
                project_names=["Alpha"],
                denied_projects=[],
                check_paths=False,
            )
        with self.assertRaises(WorkspaceSecurityError):
            validate_workspace_path(
                "/root/agent-handoff/workspace",
                workspace_roots=["/srv"],
                project_names=["Alpha"],
                denied_projects=[],
                check_paths=False,
            )
        with self.assertRaises(WorkspaceSecurityError):
            validate_workspace_path(
                str(self.repo),
                workspace_roots=[str(self.work)],
                project_names=["Rabet"],
                denied_projects=["Rabet", "Yakfi"],
                check_paths=False,
            )
        denied_component = self.work / "rabet" / "repo"
        denied_component.mkdir(parents=True)
        with self.assertRaises(WorkspaceSecurityError):
            validate_workspace_path(
                str(denied_component),
                workspace_roots=[str(self.work)],
                project_names=["Alpha"],
                denied_projects=["Rabet", "Yakfi"],
                check_paths=False,
            )

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=str(self.repo),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )


def shutil_which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


class C1OsPrivilegeTests(unittest.TestCase):
    def test_real_python_child_drops_uid_gid_and_cannot_read_root_secret_when_available(self) -> None:
        import pwd

        if os.geteuid() != 0:
            self.skipTest("requires root to prove the uid/gid drop")
        try:
            nobody = pwd.getpwnam("nobody")
        except KeyError:
            self.skipTest("no unprivileged nobody account available")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            os.chmod(root, 0o755)
            secret_dir = root / "root-secret"
            secret_dir.mkdir()
            secret = secret_dir / "token.txt"
            secret.write_text("ROOT-ONLY-SYNTHETIC-SECRET\n", encoding="utf-8")
            os.chmod(secret, 0o600)
            os.chmod(secret_dir, 0o700)
            identity = {
                "uid": int(nobody.pw_uid),
                "gid": int(nobody.pw_gid),
            }
            kwargs = popen_isolation_kwargs(identity)
            script = (
                "import os,sys;"
                "print(os.getuid(), os.getgid(), os.getgroups());"
                "print(open(sys.argv[1], encoding='utf-8').read())"
            )
            result = subprocess.run(
                [sys.executable, "-c", script, str(secret)],
                cwd=str(root),
                env={"PATH": DEFAULT_CHILD_PATH},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
                **kwargs,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(f"{int(nobody.pw_uid)} {int(nobody.pw_gid)} []", result.stdout)
            self.assertIn("Permission denied", result.stderr)
            self.assertNotIn("ROOT-ONLY-SYNTHETIC-SECRET", result.stdout)

    def test_real_isolated_adapter_uses_identity_and_allowlist_when_available(self) -> None:
        import pwd
        import shutil

        if os.geteuid() != 0:
            self.skipTest("requires root to prove the isolated child identity")
        if not shutil.which("git"):
            self.skipTest("git is not available")
        try:
            nobody = pwd.getpwnam("nobody")
        except KeyError:
            self.skipTest("no unprivileged nobody account available")

        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as events_tmp:
            root = Path(tmp)
            os.chmod(root, 0o755)
            home = root / "home"
            workspace = root / "work" / "repo"
            fake_bin = root / "fake-bin"
            log_dir = root / "fake-log"
            for directory in (
                home,
                home / ".config",
                home / ".local" / "share",
                home / ".local" / "share" / "opencode",
                home / ".cache",
                home / "tmp",
                workspace,
                fake_bin,
                log_dir,
            ):
                directory.mkdir(parents=True, exist_ok=True)
            auth = home / ".local" / "share" / "opencode" / "auth.json"
            auth.write_text('{"synthetic": {"type": "test"}}\n', encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@example.invalid",
                    "-c",
                    "user.name=Test",
                    "commit",
                    "--allow-empty",
                    "-q",
                    "-m",
                    "initial",
                ],
                cwd=workspace,
                check=True,
            )
            fake_source = (ROOT / "tests" / "fake-opencode").read_text(encoding="utf-8")
            fake_source = fake_source.replace('"/tmp/fake-opencode"', json.dumps(str(log_dir)))
            fake_source = fake_source.replace(
                '"pid": os.getpid(),',
                '"pid": os.getpid(),\n            "uid": os.getuid(),\n'
                '            "gid": os.getgid(),\n            "groups": os.getgroups(),\n'
                '            "HOME": os.environ.get("HOME"),\n'
                '            "XDG_DATA_HOME": os.environ.get("XDG_DATA_HOME"),\n'
                '            "XDG_CONFIG_HOME": os.environ.get("XDG_CONFIG_HOME"),\n'
                '            "GH_TOKEN_present": "GH_TOKEN" in os.environ,\n'
                '            "TELEGRAM_present": "AGENT_HANDOFF_TELEGRAM_TOKEN" in os.environ,',
            )
            fake = fake_bin / "opencode"
            fake.write_text(fake_source, encoding="utf-8")
            os.chmod(fake, 0o755)
            for directory in (
                home,
                home / ".config",
                home / ".local",
                home / ".local" / "share",
                home / ".local" / "share" / "opencode",
                home / ".cache",
                home / "tmp",
                workspace,
            ):
                os.chown(directory, nobody.pw_uid, nobody.pw_gid)
            os.chown(auth, nobody.pw_uid, nobody.pw_gid)
            os.chmod(auth, 0o600)
            os.chmod(home, 0o700)
            os.chmod(home / "tmp", 0o700)
            for directory in (
                home / ".config",
                home / ".local" / "share",
                home / ".cache",
                home / ".local" / "share" / "opencode",
            ):
                os.chmod(directory, 0o755)
            os.chmod(log_dir, 0o777)
            identity = resolve_child_identity({"user": "nobody", "home": str(home)}, check_paths=True)
            with mock.patch.dict(
                os.environ,
                {
                    "AGENT_HANDOFF_OPENCODE_BIN": str(fake),
                    "GH_TOKEN": "synthetic-root-github-token",
                    "AGENT_HANDOFF_TELEGRAM_TOKEN": SYNTHETIC_TELEGRAM_TOKEN,
                },
                clear=False,
            ):
                adapter = OpenCodeAdapter(
                    {"permission_profile": "read_only", "allow_write": False, "timeout_seconds": 20},
                    identity=identity,
                )
                result = adapter.run(
                    ExecutionRequest(
                        model="provider/model-fast",
                        workspace=str(workspace),
                        title="OS proof",
                        instructions="proof",
                        permission_profile="read_only",
                        allow_write=False,
                        timeout_seconds=20,
                    ),
                    events_path=str(Path(events_tmp) / "events.jsonl"),
                    on_session=lambda _session: None,
                )
            self.assertEqual(result.outcome, "succeeded", result.error)
            calls = [
                json.loads(line)
                for line in (log_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            call = calls[-1]
            self.assertEqual(call["uid"], int(nobody.pw_uid))
            self.assertEqual(call["gid"], int(nobody.pw_gid))
            self.assertEqual(call["groups"], [])
            self.assertEqual(call["HOME"], str(home))
            self.assertEqual(call["XDG_DATA_HOME"], str(home / ".local" / "share"))
            self.assertEqual(call["XDG_CONFIG_HOME"], str(home / ".config"))
            self.assertFalse(call["GH_TOKEN_present"])
            self.assertFalse(call["TELEGRAM_present"])
            self.assertEqual(call["agent"], "plan")


class C1ProcessLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.h = DispatchHarness(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _request(self, timeout: float):
        return ExecutionRequest(
            model="provider/model-fast",
            workspace=str(self.h.project_path.resolve()),
            title="C1 lifecycle",
            instructions="Lifecycle marker.",
            permission_profile="read_only",
            allow_write=False,
            timeout_seconds=timeout,
        )

    def test_t21_t23_t24_timeout_progress_and_no_orphan_process(self) -> None:
        messages: list[tuple[str, str]] = []
        env = self.h.env({"FAKE_OPENCODE_DELAY": "10"})
        with mock.patch.dict(os.environ, env, clear=False):
            adapter = OpenCodeAdapter(
                {"timeout_seconds": 1, "permission_profile": "read_only", "allow_write": False}
            )
            result = adapter.run(
                self._request(1),
                events_path=str(self.root / "timeout-events.jsonl"),
                on_session=lambda _session: None,
                on_progress=lambda stage, message: messages.append((stage, message)),
                heartbeat_seconds=0.1,
            )
        self.assertTrue(result.timed_out)
        self.assertEqual(result.outcome, "blocked")
        heartbeat = " ".join(message for stage, message in messages if stage == "executing")
        self.assertIn("bounded timeout", heartbeat)
        self.assertIn("no ETA", heartbeat)
        pid = self.h.opencode_calls()[-1]["pid"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(int(pid), 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail(f"OpenCode child process {pid} survived the execution timeout")


if __name__ == "__main__":
    unittest.main()
