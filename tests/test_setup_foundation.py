from __future__ import annotations

import contextlib
import copy
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from agent_handoff import guided_setup as gs  # noqa: E402

ROOT_FOLDER_ID = "root_folder_0001"
PARENT_FOLDER_ID = "parent_folder_0001"
PROJECT_FOLDER_ID = "project_folder_0001"


def make_defaults(root: Path) -> dict:
    defaults = copy.deepcopy(gs.DEFAULT_CONFIG)
    defaults["drive"] = {
        "remote": "gdrive",
        "parent_folder_id": "",
        "parent_folder_name": "AI Project Operations",
        "root_folder_id": "",
        "root_folder_name": "03 — Agent Execution Reports",
    }
    defaults["storage"] = {
        "data": str(root / "data"),
        "queue": str(root / "queue"),
        "state": str(root / "state"),
        "locks": str(root / "locks"),
        "logs": str(root / "logs"),
        "tmp": str(root / "tmp"),
    }
    return defaults


def write_config(home: Path, config: dict) -> Path:
    path = home / "config" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def base_config(root: Path, *, root_id: str = ROOT_FOLDER_ID, project: bool = True) -> dict:
    config = make_defaults(root)
    config["drive"]["root_folder_id"] = root_id
    if project:
        workspace = root / "workspace" / "alpha"
        workspace.mkdir(parents=True, exist_ok=True)
        config["projects"] = {
            "alpha": {
                "name": "Alpha",
                "drive_folder_id": PROJECT_FOLDER_ID,
                "paths": [str(workspace)],
                "path_prefixes": [],
                "aliases": ["alpha"],
                "auto_registered": False,
            }
        }
    return config


class FakePlugin:
    def __init__(self, root: Path, *, authenticated: bool = False, secret: str | None = None):
        self.root = root
        self.authenticated = authenticated
        self.secret = secret
        self.bin = root / "fake-bin"
        self.bin.mkdir(parents=True, exist_ok=True)
        (self.bin / "rclone").write_text("", encoding="utf-8")

    def which(self, name: str) -> str | None:
        if name in {"git", "rclone", "gh", "opencode"}:
            return str(self.bin / name)
        return None

    def runner(self, command: list[str], timeout: float) -> tuple[int, str, str]:
        name = Path(command[0]).name
        if name == "git":
            return 0, "git version 9.9.9\n", ""
        if name == "rclone":
            if len(command) > 1 and command[1] == "version":
                return 0, "rclone v9.9.9\n", ""
            if len(command) > 1 and command[1] == "listremotes":
                return 0, "gdrive:\n", ""
            return 0, "", ""
        if name == "gh":
            if len(command) > 1 and command[1] == "--version":
                return 0, "gh version 9.9.9\n", ""
            output = "Authenticated as fake"
            if self.secret:
                output += f" token={self.secret}"
            return (0 if self.authenticated else 1), output, ""
        if name == "opencode":
            return 0, "9.9.9\n", ""
        return 127, "", "not found"


class SetupFoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.defaults = make_defaults(self.home)
        self.plugin = FakePlugin(self.root)
        self.patch_which = mock.patch.object(gs.shutil, "which", side_effect=self.plugin.which)
        self.patch_which.start()
        self.addCleanup(self.patch_which.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def build(self, state: gs.ConfigState | None = None, **kwargs):
        return gs.build_setup_plan(
            state,
            defaults=self.defaults,
            base_dir=self.home,
            home=self.home,
            runner=self.plugin.runner,
            platform_system="Linux",
            version_info=(3, 12, 4),
            **kwargs,
        )

    def state(self, path: Path | None = None) -> gs.ConfigState:
        return gs.read_config_state(path or self.home / "config" / "config.json", defaults=self.defaults)

    def test_clean_plan_is_read_only_and_deterministic(self) -> None:
        first = self.build(self.state())
        second = self.build(self.state())
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        config_path = self.home / "config" / "config.json"
        self.assertFalse(config_path.exists())
        self.assertFalse(gs.journal_path_for(config_path).exists())
        self.assertEqual(first["command"], "setup.plan")
        self.assertEqual(first["status"], "action_required")
        self.assertTrue(first["ok"])
        statuses = {item["id"]: item["status"] for item in first["checks"]}
        self.assertEqual(statuses["config.file"], "missing")
        self.assertEqual(statuses["rclone"], "ready")
        self.assertEqual(statuses["github.auth"], "missing")
        self.assertEqual(statuses["opencode"], "ready")
        self.assertFalse(first["config_plan"]["changes_required"])
        self.assertFalse(first["config_plan"]["would_mutate_config"])

    def test_existing_installation_discovery_and_verified_capture(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / ".agent-handoff-manifest.json").write_text('{"schema": 1, "files": []}\n', encoding="utf-8")
        (self.home / "bin").mkdir()
        (self.home / "bin" / "agent-handoff").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        config = base_config(self.home)
        write_config(self.home, config)
        rclone_config = self.root / "rclone.conf"
        rclone_config.write_text("[gdrive]\ntype = drive\n", encoding="utf-8")
        complete = self.home / "data" / "projects" / "Alpha" / "TASKS" / "t-1" / "RUNS" / "r-1" / "COMPLETE.json"
        complete.parent.mkdir(parents=True)
        complete.write_text(json.dumps({"upload": {"verified": True}}), encoding="utf-8")
        self.plugin.authenticated = True
        with mock.patch.dict(os.environ, {"RCLONE_CONFIG": str(rclone_config)}):
            plan = self.build(self.state())
        self.assertTrue(plan["existing_installation"])
        self.assertEqual(plan["status"], "ready")
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["state"]["projects_with_drive"], 1)
        self.assertTrue(plan["state"]["verified_capture"])
        self.assertEqual(plan["state"]["dispatch_enabled"], False)
        self.assertIn("verify.capture", plan["completed_steps"])
        self.assertEqual(plan["remaining_required_steps"], [])

    def test_missing_dependencies_are_reported_without_blocking_core(self) -> None:
        with mock.patch.object(gs.shutil, "which", return_value=None):
            plan = self.build(self.state())
        statuses = {item["id"]: item["status"] for item in plan["checks"]}
        self.assertEqual(statuses["git"], "missing")
        self.assertEqual(statuses["rclone"], "missing")
        self.assertEqual(statuses["github.cli"], "optional")
        self.assertEqual(statuses["opencode"], "optional")
        self.assertEqual(statuses["drive.authorization"], "missing")
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["status"], "action_required")

    def test_invalid_config_blocks_and_is_never_rewritten(self) -> None:
        path = self.home / "config" / "config.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not valid json\n", encoding="utf-8")
        os.chmod(path, 0o600)
        plan = self.build(self.state())
        self.assertFalse(plan["ok"])
        self.assertEqual(plan["status"], "blocked")
        self.assertIn("config.json", [item["id"] for item in plan["blockers"]])
        self.assertEqual(path.read_text(encoding="utf-8"), "{not valid json\n")
        with self.assertRaises(gs.SetupValidationError):
            gs.apply_setup_patch(
                path,
                patch={"drive.root_folder_id": ROOT_FOLDER_ID},
                defaults=self.defaults,
                approve=True,
                config_lock=contextlib.nullcontext,
            )
        self.assertEqual(path.read_text(encoding="utf-8"), "{not valid json\n")

    def test_apply_preserves_unknown_fields_and_unrelated_projects(self) -> None:
        workspace = self.root / "new-workspace"
        workspace.mkdir()
        config = base_config(self.home, root_id="")
        config["future_field"] = {"keep": True}
        config["projects"]["alpha"]["future_project_field"] = {"keep": True}
        config["dispatch"] = {"enabled": False, "repository": "owner/private"}
        config["notifications"] = {"telegram": {"enabled": False, "token_env": "AGENT_HANDOFF_TELEGRAM_TOKEN"}}
        path = write_config(self.home, config)
        original = path.read_text(encoding="utf-8")
        result = gs.apply_setup_patch(
            path,
            patch={"drive.root_folder_id": ROOT_FOLDER_ID, "workspace_roots": [str(workspace)]},
            defaults=self.defaults,
            approve=True,
            config_lock=contextlib.nullcontext,
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        self.assertTrue(result["backup"])
        self.assertEqual(Path(result["backup"]).read_text(encoding="utf-8"), original)
        updated = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(updated["future_field"], {"keep": True})
        self.assertEqual(updated["projects"]["alpha"]["future_project_field"], {"keep": True})
        self.assertEqual(updated["dispatch"], config["dispatch"])
        self.assertEqual(updated["notifications"], config["notifications"])
        self.assertEqual(updated["drive"]["root_folder_id"], ROOT_FOLDER_ID)
        self.assertIn(str(workspace), updated["workspace_roots"])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertFalse(gs.journal_path_for(path).exists())

    def test_apply_is_idempotent(self) -> None:
        config = base_config(self.home, root_id="")
        path = write_config(self.home, config)
        patch = {"drive.root_folder_id": ROOT_FOLDER_ID}
        first = gs.apply_setup_patch(
            path, patch=patch, defaults=self.defaults, approve=True, config_lock=contextlib.nullcontext
        )
        second = gs.apply_setup_patch(
            path, patch=patch, defaults=self.defaults, approve=True, config_lock=contextlib.nullcontext
        )
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        backups = sorted(path.parent.glob(f"{path.name}{gs.BACKUP_MARKER}*"))
        self.assertEqual(len(backups), 1)

    def test_apply_requires_explicit_approval(self) -> None:
        path = write_config(self.home, base_config(self.home, root_id=""))
        with self.assertRaises(gs.SetupApprovalError):
            gs.apply_setup_patch(
                path,
                patch={"drive.root_folder_id": ROOT_FOLDER_ID},
                defaults=self.defaults,
                approve=False,
                config_lock=contextlib.nullcontext,
            )
        self.assertEqual(json.loads(path.read_text())["drive"]["root_folder_id"], "")

    def test_apply_rejects_forbidden_fields_and_secret_like_keys(self) -> None:
        path = write_config(self.home, base_config(self.home, root_id=""))
        original = path.read_text(encoding="utf-8")
        for patch in (
            {"dispatch": {"enabled": True}},
            {"drive.root_folder_id": ROOT_FOLDER_ID, "notifications.telegram.token": "x"},
            {"drive.root_folder_id": ROOT_FOLDER_ID, "projects": {}},
            {"unsupported_field": "x"},
        ):
            with self.assertRaises(gs.SetupValidationError):
                gs.apply_setup_patch(
                    path, patch=patch, defaults=self.defaults, approve=True, config_lock=contextlib.nullcontext
                )
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_apply_rolls_back_when_post_write_validation_fails(self) -> None:
        path = write_config(self.home, base_config(self.home, root_id=""))
        original = path.read_text(encoding="utf-8")

        def fail(_view):
            raise RuntimeError("injected post-write failure")

        with self.assertRaises(gs.SetupError):
            gs.apply_setup_patch(
                path,
                patch={"drive.root_folder_id": ROOT_FOLDER_ID},
                defaults=self.defaults,
                approve=True,
                post_write_validator=fail,
                config_lock=contextlib.nullcontext,
            )
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        self.assertFalse(gs.journal_path_for(path).exists())
        self.assertTrue(list(path.parent.glob(f"{path.name}{gs.BACKUP_MARKER}*")))

    def test_interrupted_setup_cleanup_is_detected_and_recovered(self) -> None:
        path = write_config(self.home, base_config(self.home, root_id=""))
        before = path.read_text(encoding="utf-8")
        after = json.dumps({**json.loads(before), "drive": {**json.loads(before)["drive"], "root_folder_id": ROOT_FOLDER_ID}}, indent=2) + "\n"
        backup = path.with_name(f"{path.name}{gs.BACKUP_MARKER}test")
        backup.write_text(before, encoding="utf-8")
        journal = gs.journal_path_for(path)
        journal.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "config_path": str(path),
                    "backup": str(backup),
                    "before_sha256": gs.sha256_text(before),
                    "after_sha256": gs.sha256_text(after),
                }
            ),
            encoding="utf-8",
        )
        path.write_text(after, encoding="utf-8")
        inspection = gs.inspect_setup_recovery(path)
        self.assertTrue(inspection["required"])
        self.assertEqual(inspection["status"], "complete_cleanup_required")
        plan = self.build(self.state(path))
        self.assertFalse(plan["ok"])
        self.assertEqual(plan["status"], "blocked")
        with self.assertRaises(gs.SetupApprovalError):
            gs.recover_setup(path, approve=False)
        result = gs.recover_setup(path, approve=True)
        self.assertTrue(result["changed"])
        self.assertFalse(journal.exists())
        self.assertEqual(path.read_text(encoding="utf-8"), after)
        self.assertFalse(gs.inspect_setup_recovery(path)["required"])

    def test_symlink_configuration_is_blocked_and_not_overwritten(self) -> None:
        target = self.root / "target-config.json"
        target.write_text(json.dumps(base_config(self.home, root_id="")), encoding="utf-8")
        link = self.home / "config" / "config.json"
        link.parent.mkdir(parents=True)
        link.symlink_to(target)
        state = self.state(link)
        self.assertTrue(state.is_symlink)
        plan = self.build(state)
        self.assertFalse(plan["ok"])
        self.assertEqual(plan["status"], "blocked")
        self.assertIn("config.file", [item["id"] for item in plan["blockers"]])
        with self.assertRaises(gs.SetupValidationError):
            gs.apply_setup_patch(
                link,
                patch={"drive.root_folder_id": ROOT_FOLDER_ID},
                defaults=self.defaults,
                approve=True,
                config_lock=contextlib.nullcontext,
            )
        self.assertTrue(link.is_symlink())

    def test_ambiguous_interrupted_setup_is_not_modified(self) -> None:
        path = write_config(self.home, base_config(self.home, root_id=""))
        before = path.read_text(encoding="utf-8")
        journal = gs.journal_path_for(path)
        backup = path.with_name(f"{path.name}{gs.BACKUP_MARKER}ambiguous")
        backup.write_text(before, encoding="utf-8")
        journal.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "config_path": str(path),
                    "backup": str(backup),
                    "before_sha256": gs.sha256_text(before),
                    "after_sha256": gs.sha256_text("different\n"),
                }
            ),
            encoding="utf-8",
        )
        path.write_text(before + "\n", encoding="utf-8")
        ambiguous_text = path.read_text(encoding="utf-8")
        inspection = gs.inspect_setup_recovery(path)
        self.assertEqual(inspection["status"], "ambiguous")
        with self.assertRaises(gs.SetupRecoveryError):
            gs.recover_setup(path, approve=True)
        self.assertEqual(path.read_text(encoding="utf-8"), ambiguous_text)
        self.assertTrue(journal.exists())

    def test_plan_output_never_contains_secret_values(self) -> None:
        secret = "supersecretvalue123456"
        self.plugin.secret = secret
        config = base_config(self.home)
        config["notifications"] = {"telegram": {"enabled": False, "token_env": "AGENT_HANDOFF_TELEGRAM_TOKEN"}}
        write_config(self.home, config)
        rclone_config = self.root / "rclone.conf"
        rclone_config.write_text(f"[gdrive]\ntype = drive\ntoken = {secret}\n", encoding="utf-8")
        with mock.patch.dict(
            os.environ,
            {"RCLONE_CONFIG": str(rclone_config), "AGENT_HANDOFF_TELEGRAM_TOKEN": secret},
        ):
            plan = self.build(self.state())
        serialized = json.dumps(plan)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("supersecret", serialized)

    def test_apply_never_enables_services_coding_or_write_isolation(self) -> None:
        config = base_config(self.home, root_id="")
        config["dispatch"] = {
            "enabled": False,
            "transport": "github_issues",
            "repository": "owner/private",
            "allowed_users": ["owner"],
            "models": {"fast": "provider/model"},
            "execution": {"permission_profile": "read_only", "allow_write": False},
        }
        path = write_config(self.home, config)
        before_dispatch = json.loads(path.read_text(encoding="utf-8"))["dispatch"]
        result = gs.apply_setup_patch(
            path,
            patch={"drive.root_folder_id": ROOT_FOLDER_ID},
            defaults=self.defaults,
            approve=True,
            config_lock=contextlib.nullcontext,
        )
        self.assertTrue(result["changed"])
        updated = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(updated["dispatch"], before_dispatch)
        self.assertFalse(updated["dispatch"]["enabled"])
        self.assertFalse(updated["dispatch"]["execution"].get("allow_write"))
        self.assertNotEqual(updated["dispatch"]["execution"].get("permission_profile"), "workspace_write")
        self.assertNotIn("coding", updated["dispatch"])
        self.assertNotIn("isolation", updated["dispatch"]["execution"])
        self.assertFalse((self.home / ".config" / "systemd" / "user" / gs.DISPATCH_UNIT_NAME).exists())
        plan = self.build(self.state(path))
        self.assertIn("dispatch.enabled", plan["config_plan"]["forbidden_activations"])

    def test_dispatch_authorization_failure_blocks_only_dispatch_scope(self) -> None:
        config = base_config(self.home)
        config["dispatch"] = {
            "enabled": True,
            "transport": "github_issues",
            "repository": "owner/private",
            "allowed_users": ["owner"],
            "agent": "opencode",
            "models": {"fast": "provider/model"},
            "default_model": "fast",
            "projects": {"Alpha": {"workspace": str(self.home / "workspace" / "alpha")}},
            "execution": {"timeout_seconds": 1200, "permission_profile": "read_only", "allow_write": False},
        }
        path = write_config(self.home, config)
        self.plugin.authenticated = False
        plan = self.build(self.state(path))
        self.assertTrue(plan["ok"])
        self.assertFalse(plan["all_scopes_ready"])
        self.assertIn("github.auth", [item["id"] for item in plan["blockers"]])
        self.assertEqual(plan["blockers"][0]["scope"], "dispatch")


class SetupCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.bin = ROOT / "bin" / "agent-handoff"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def cli(self, args: list[str], *, expected: int | None = None) -> subprocess.CompletedProcess:
        env = {
            "HOME": str(self.home),
            "AGENT_HANDOFF_HOME": str(self.home),
            "PATH": str(self.root / "empty-bin"),
            "PYTHONPATH": str(LIB),
        }
        result = subprocess.run(
            [sys.executable, str(self.bin), *args],
            cwd=str(self.root),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if expected is not None:
            self.assertEqual(result.returncode, expected, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
        return result

    def test_cli_plan_json_is_deterministic_and_read_only(self) -> None:
        first = self.cli(["setup", "plan", "--json"], expected=0)
        second = self.cli(["setup", "plan", "--json"], expected=0)
        self.assertEqual(first.stdout, second.stdout)
        plan = json.loads(first.stdout)
        self.assertEqual(first.stderr, "")
        self.assertEqual(plan["command"], "setup.plan")
        self.assertFalse((self.home / "config" / "config.json").exists())
        self.assertFalse((self.home / "config" / "config.json").parent.exists())

    def test_cli_apply_requires_approval_then_applies_and_is_idempotent(self) -> None:
        workspace = self.root / "workspace"
        workspace.mkdir()
        base = [
            "setup",
            "apply",
            "--json",
            "--drive-root-folder-id",
            ROOT_FOLDER_ID,
            "--workspace-root",
            str(workspace),
        ]
        preview = self.cli(base, expected=2)
        payload = json.loads(preview.stdout)
        self.assertTrue(payload["approval_required"])
        self.assertFalse(payload["ok"])
        self.assertIn("Approval required", preview.stderr)
        self.assertFalse((self.home / "config" / "config.json").exists())
        applied = self.cli([*base, "--approve"], expected=0)
        result = json.loads(applied.stdout)
        self.assertTrue(result["changed"])
        config_path = self.home / "config" / "config.json"
        self.assertTrue(config_path.exists())
        self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)
        again = self.cli([*base, "--approve"], expected=0)
        self.assertFalse(json.loads(again.stdout)["changed"])
        validate = self.cli(["config", "validate", "--json"], expected=0)
        self.assertTrue(json.loads(validate.stdout)["ok"])

    def test_cli_human_plan_has_first_time_labels(self) -> None:
        result = self.cli(["setup", "plan"], expected=0)
        self.assertIn("Agent Handoff guided setup", result.stdout)
        self.assertIn("Environment checks:", result.stdout)
        self.assertIn("Required next actions:", result.stdout)
        self.assertIn("[MISSING ]", result.stdout)
        self.assertIn("[OPTIONAL]", result.stdout)

    def test_cli_recover_requires_approval_and_finalizes_cleanup(self) -> None:
        config = base_config(self.home, root_id="")
        path = write_config(self.home, config)
        before = path.read_text(encoding="utf-8")
        journal = gs.journal_path_for(path)
        journal.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "config_path": str(path),
                    "backup": None,
                    "before_sha256": gs.sha256_text(before),
                    "after_sha256": gs.sha256_text(before),
                }
            ),
            encoding="utf-8",
        )
        preview = self.cli(["setup", "recover", "--json"], expected=2)
        self.assertTrue(json.loads(preview.stdout)["approval_required"])
        self.assertTrue(journal.exists())
        done = self.cli(["setup", "recover", "--approve", "--json"], expected=0)
        self.assertTrue(json.loads(done.stdout)["changed"])
        self.assertFalse(journal.exists())
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_cli_json_errors_stay_on_stdout(self) -> None:
        path = self.home / "config" / "config.json"
        path.parent.mkdir(parents=True)
        path.write_text("{broken\n", encoding="utf-8")
        result = self.cli(
            ["setup", "apply", "--approve", "--json", "--drive-root-folder-id", ROOT_FOLDER_ID],
            expected=1,
        )
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("not valid JSON", payload["error"])
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
