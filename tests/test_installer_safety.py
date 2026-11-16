from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ".agent-handoff-manifest.json"


def make_source(root: Path, version: str = "old", plugin: str = "// old plugin\n") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for relative in ("bin/agent-handoff", "config/config.example.json", "README.md"):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    for script in sorted((ROOT / "setup").glob("*.py")):
        target = root / "setup" / script.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(script, target)
    for relative in (
        "integrations/AGENT-INSTRUCTIONS.md",
        "integrations/opencode/agent-handoff.js",
        "integrations/agy/hooks.example.json",
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    package = root / "lib" / "agent_handoff"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    (root / "integrations" / "opencode" / "agent-handoff.js").write_text(plugin, encoding="utf-8")
    return root


def inject_mid_install_failure(installer: Path) -> None:
    lines = installer.read_text(encoding="utf-8").splitlines(keepends=True)
    updated = []
    injected = False
    for line in lines:
        if "install_agy(home, bin_link)" in line and not line.lstrip().startswith("def "):
            indent = line[: len(line) - len(line.lstrip())]
            updated.append(f'{indent}raise SystemExit("injected mid-install failure")\n')
            injected = True
        updated.append(line)
    if not injected:
        raise AssertionError("could not find the AGY installation call for failure injection")
    installer.write_text("".join(updated), encoding="utf-8")


class InstallerSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "runtime"
        self.user_home = self.root / "user"
        self.user_home.mkdir()
        (self.home / "config").mkdir(parents=True)
        (self.home / "data").mkdir(parents=True)
        self.config = self.home / "config" / "config.json"
        self.config.write_text('{"runtime": "RUNTIME_SENTINEL"}\n', encoding="utf-8")
        os.chmod(self.config, 0o600)
        (self.home / "data" / "keep.txt").write_text("RUNTIME_DATA\n", encoding="utf-8")
        self.link = self.root / "bin" / "agent-handoff"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def env(self) -> dict[str, str]:
        value = os.environ.copy()
        value["HOME"] = str(self.user_home)
        value["PYTHONDONTWRITEBYTECODE"] = "1"
        return value

    def run_script(self, script: Path, *args: str) -> subprocess.CompletedProcess:
        command = [sys.executable, str(script), "--home", str(self.home), "--bin-link", str(self.link)]
        command.extend(args)
        return subprocess.run(command, capture_output=True, text=True, check=False, env=self.env())

    def install(self, source: Path, *args: str) -> subprocess.CompletedProcess:
        return self.run_script(source / "setup" / "install.py", *args)

    def upgrade(self, source: Path, *args: str) -> subprocess.CompletedProcess:
        return self.run_script(source / "setup" / "upgrade.py", *args)

    def assert_runtime_preserved(self) -> None:
        self.assertEqual(self.config.read_text(encoding="utf-8"), '{"runtime": "RUNTIME_SENTINEL"}\n')
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.home / "data" / "keep.txt").read_text(encoding="utf-8"), "RUNTIME_DATA\n")

    def test_non_git_source_never_copies_runtime_config(self) -> None:
        source = make_source(self.root / "archive-source")
        (source / "config" / "config.json").write_text('{"source": "SOURCE_SENTINEL"}\n', encoding="utf-8")
        result = self.install(source, "--no-opencode", "--no-agy")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_runtime_preserved()
        self.assertTrue((self.home / "config" / "config.example.json").is_file())
        self.assertEqual((self.home / "config" / "config.example.json").read_text(), (source / "config" / "config.example.json").read_text())

    def test_git_tracked_runtime_config_is_still_excluded(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git is unavailable")
        source = make_source(self.root / "git-source")
        (source / "config" / "config.json").write_text('{"source": "SOURCE_SENTINEL"}\n', encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=source, check=True, env=self.env())
        subprocess.run(["git", "add", "-A"], cwd=source, check=True, env=self.env())
        result = self.install(source, "--no-opencode", "--no-agy")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_runtime_preserved()
        self.assertTrue((self.home / "config" / "config.example.json").is_file())

    def test_source_symlink_is_not_followed(self) -> None:
        outside = self.root / "outside-secret.txt"
        outside.write_text("OUTSIDE_SENTINEL\n", encoding="utf-8")
        source = make_source(self.root / "symlink-source")
        leak = source / "lib" / "agent_handoff" / "leak.py"
        leak.symlink_to(outside)
        result = self.install(source, "--no-opencode", "--no-agy")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.home / "lib" / "agent_handoff" / "leak.py").exists())

    def test_unsafe_destination_symlink_cannot_escape_home(self) -> None:
        source = make_source(self.root / "destination-source")
        outside = self.root / "outside-destination"
        outside.mkdir()
        unsafe = self.home / "lib"
        unsafe.mkdir()
        (unsafe / "agent_handoff").symlink_to(outside, target_is_directory=True)
        result = self.install(source, "--no-opencode", "--no-agy")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((outside / "__init__.py").exists())
        self.assert_runtime_preserved()

    def test_rollback_removes_newer_owned_files_and_restores_release(self) -> None:
        v1 = make_source(self.root / "v1", version="old", plugin="// plugin-v1\n")
        legacy = v1 / "lib" / "agent_handoff" / "legacy_module.py"
        legacy.write_text("LEGACY = True\n", encoding="utf-8")
        self.assertEqual(self.install(v1, "--no-opencode", "--no-agy").returncode, 0)
        unmanaged = self.home / "lib" / "agent_handoff" / "local_notes.txt"
        unmanaged.write_text("LOCAL_ONLY\n", encoding="utf-8")

        v2 = make_source(self.root / "v2", version="new", plugin="// plugin-v2\n")
        (v2 / "lib" / "agent_handoff" / "new_module.py").write_text("NEW = True\n", encoding="utf-8")
        upgraded = self.upgrade(v2, "--no-opencode", "--no-agy")
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
        self.assertTrue((self.home / "lib" / "agent_handoff" / "new_module.py").exists())
        self.assertFalse((self.home / "lib" / "agent_handoff" / "legacy_module.py").exists())
        self.assertIn('__version__ = "new"', (self.home / "lib" / "agent_handoff" / "__init__.py").read_text())
        self.assert_runtime_preserved()
        unmanaged.write_text("LOCAL_ONLY_CHANGED\n", encoding="utf-8")

        rolled = self.upgrade(v2, "--no-opencode", "--no-agy", "--rollback", "latest")
        self.assertEqual(rolled.returncode, 0, rolled.stderr)
        self.assertFalse((self.home / "lib" / "agent_handoff" / "new_module.py").exists())
        self.assertTrue((self.home / "lib" / "agent_handoff" / "legacy_module.py").exists())
        self.assertIn('__version__ = "old"', (self.home / "lib" / "agent_handoff" / "__init__.py").read_text())
        self.assertEqual(unmanaged.read_text(), "LOCAL_ONLY_CHANGED\n")
        self.assert_runtime_preserved()
        manifest = json.loads((self.home / MANIFEST).read_text(encoding="utf-8"))
        self.assertNotIn("lib/agent_handoff/new_module.py", manifest["files"])

        self.assertEqual(self.upgrade(v2, "--no-opencode", "--no-agy").returncode, 0)
        self.assertTrue((self.home / "lib" / "agent_handoff" / "new_module.py").exists())
        repeated = self.upgrade(v2, "--no-opencode", "--no-agy", "--rollback", "latest")
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertFalse((self.home / "lib" / "agent_handoff" / "new_module.py").exists())
        self.assert_runtime_preserved()

    def test_legacy_snapshot_fails_safely_without_removing_files(self) -> None:
        source = make_source(self.root / "legacy-source", version="current")
        self.assertEqual(self.install(source, "--no-opencode", "--no-agy").returncode, 0)
        before = (self.home / "lib" / "agent_handoff" / "__init__.py").read_text()
        legacy = self.home / "releases" / "20200101T000000Z-legacy"
        (legacy / "code" / "lib" / "agent_handoff").mkdir(parents=True)
        (legacy / "code" / "lib" / "agent_handoff" / "__init__.py").write_text('__version__ = "legacy"\n', encoding="utf-8")
        (legacy / "release.json").write_text('{"label": "legacy"}\n', encoding="utf-8")
        result = self.upgrade(source, "--no-opencode", "--no-agy", "--rollback", "20200101T000000Z-legacy")
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertIn("ownership manifest", combined.lower())
        self.assertEqual((self.home / "lib" / "agent_handoff" / "__init__.py").read_text(), before)
        self.assert_runtime_preserved()

    def test_snapshot_manifest_paths_are_validated_before_changes(self) -> None:
        source = make_source(self.root / "manifest-source", version="current")
        self.assertEqual(self.install(source, "--no-opencode", "--no-agy").returncode, 0)
        before = (self.home / "lib" / "agent_handoff" / "__init__.py").read_text()
        target = self.home / "releases" / "20200101T000001Z-unsafe"
        (target / "code" / "lib" / "agent_handoff").mkdir(parents=True)
        (target / "code" / "lib" / "agent_handoff" / "__init__.py").write_text('__version__ = "unsafe"\n', encoding="utf-8")
        (target / "manifest.json").write_text(
            json.dumps({"schema": 1, "files": ["../escape.txt", "lib/agent_handoff/__init__.py"]}),
            encoding="utf-8",
        )
        result = self.upgrade(source, "--no-opencode", "--no-agy", "--rollback", "20200101T000001Z-unsafe")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.home / "lib" / "agent_handoff" / "__init__.py").read_text(), before)
        self.assertFalse((self.root / "escape.txt").exists())
        self.assert_runtime_preserved()

    def seed_integrations(self) -> None:
        opencode = self.user_home / ".config" / "opencode"
        (opencode / "plugin").mkdir(parents=True)
        self.opencode_config = opencode / "opencode.json"
        self.opencode_config.write_text(
            json.dumps({"theme": "dark", "instructions": ["/opt/keep/AGENTS.md"], "custom": {"keep": True}}),
            encoding="utf-8",
        )
        self.plugin = opencode / "plugin" / "agent-handoff.js"
        self.plugin.write_text("// local plugin\n", encoding="utf-8")
        agy = self.user_home / ".gemini" / "config"
        agy.mkdir(parents=True)
        self.hooks = agy / "hooks.json"
        self.hooks.write_text(
            json.dumps(
                {
                    "unrelated-hook": {"Stop": [{"type": "command", "command": "keep-me"}]},
                    "agent-handoff-probe": {"Stop": []},
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        self.rules = agy / "AGENTS.md"
        self.rules.write_text("# House rules\n\nKeep this instruction.\n", encoding="utf-8")

    def test_integrations_are_idempotent_and_preserve_unrelated_content(self) -> None:
        source = make_source(self.root / "integration-source", version="current", plugin="// installed plugin\n")
        self.seed_integrations()
        first = self.install(source)
        self.assertEqual(first.returncode, 0, first.stderr)
        data = json.loads(self.opencode_config.read_text(encoding="utf-8"))
        self.assertEqual(data["theme"], "dark")
        self.assertTrue(data["custom"]["keep"])
        self.assertIn("/opt/keep/AGENTS.md", data["instructions"])
        self.assertEqual(data["instructions"].count(str(self.home / "integrations" / "AGENT-INSTRUCTIONS.md")), 1)
        self.assertEqual(self.plugin.read_text(encoding="utf-8"), "// installed plugin\n")
        hooks = json.loads(self.hooks.read_text(encoding="utf-8"))
        self.assertEqual(hooks["unrelated-hook"]["Stop"][0]["command"], "keep-me")
        self.assertNotIn("agent-handoff-probe", hooks)
        self.assertEqual(hooks["agent-handoff-report"]["Stop"][0]["command"], f"{self.link} hook agy")
        rules = self.rules.read_text(encoding="utf-8")
        self.assertIn("Keep this instruction.", rules)
        self.assertEqual(rules.count("<!-- AGENT HANDOFF REPORTING v1.0 -->"), 1)
        self.assertEqual(rules.count("<!-- END AGENT HANDOFF REPORTING -->"), 1)
        before = {
            path: path.read_bytes()
            for path in (self.opencode_config, self.plugin, self.hooks, self.rules)
        }
        backups_before = sorted(path.name for path in self.user_home.rglob("*.backup-agent-handoff-*"))
        second = self.install(source)
        self.assertEqual(second.returncode, 0, second.stderr)
        after = {
            path: path.read_bytes()
            for path in (self.opencode_config, self.plugin, self.hooks, self.rules)
        }
        self.assertEqual(before, after)
        self.assertEqual(sorted(path.name for path in self.user_home.rglob("*.backup-agent-handoff-*")), backups_before)
        self.assert_runtime_preserved()

    def test_invalid_opencode_json_is_rejected_before_plugin_mutation(self) -> None:
        source = make_source(self.root / "invalid-opencode-source", version="current", plugin="// installed plugin\n")
        self.seed_integrations()
        self.opencode_config.write_text("{not valid json\n", encoding="utf-8")
        original_plugin = self.plugin.read_text(encoding="utf-8")
        original_rules = self.rules.read_text(encoding="utf-8")
        result = self.install(source)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid opencode.json", result.stdout + result.stderr)
        self.assertEqual(self.plugin.read_text(encoding="utf-8"), original_plugin)
        self.assertEqual(self.rules.read_text(encoding="utf-8"), original_rules)
        self.assert_runtime_preserved()

    def test_invalid_agy_json_is_rejected_before_opencode_mutation(self) -> None:
        source = make_source(self.root / "invalid-agy-source", version="current", plugin="// installed plugin\n")
        self.seed_integrations()
        self.hooks.write_text("{not valid json\n", encoding="utf-8")
        original_plugin = self.plugin.read_text(encoding="utf-8")
        original_config = self.opencode_config.read_text(encoding="utf-8")
        result = self.install(source)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid AGY hooks.json", result.stdout + result.stderr)
        self.assertEqual(self.plugin.read_text(encoding="utf-8"), original_plugin)
        self.assertEqual(self.opencode_config.read_text(encoding="utf-8"), original_config)
        self.assert_runtime_preserved()

    def test_mid_install_failure_restores_managed_integration_files(self) -> None:
        source = make_source(self.root / "transaction-source", version="current", plugin="// installed plugin\n")
        self.seed_integrations()
        original = {
            path: path.read_bytes()
            for path in (self.opencode_config, self.plugin, self.hooks, self.rules)
        }
        spec = importlib.util.spec_from_file_location("agent_handoff_install_under_test", source / "setup" / "install.py")
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        module.HOME = self.user_home
        module.OPENCODE_DIR = self.user_home / ".config" / "opencode"
        module.OPENCODE_PLUGIN = module.OPENCODE_DIR / "plugin" / "agent-handoff.js"
        module.OPENCODE_CONFIG = module.OPENCODE_DIR / "opencode.json"
        module.AGY_CONFIG = self.user_home / ".gemini" / "config"
        module.AGY_HOOKS = module.AGY_CONFIG / "hooks.json"
        module.AGY_RULES = module.AGY_CONFIG / "AGENTS.md"

        def failing_agy(home: Path, bin_link: Path) -> None:
            raise RuntimeError("injected AGY failure")

        module.install_agy = failing_agy
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
            module.main(["--home", str(self.home), "--bin-link", str(self.link)])
        for path, content in original.items():
            self.assertEqual(path.read_bytes(), content, path)
        self.assert_runtime_preserved()

    def test_rollback_reapplies_selected_release_integrations(self) -> None:
        v1 = make_source(self.root / "rollback-integration-v1", version="old", plugin="// plugin-v1\n")
        v2 = make_source(self.root / "rollback-integration-v2", version="new", plugin="// plugin-v2\n")
        self.seed_integrations()
        self.assertEqual(self.install(v1).returncode, 0)
        self.assertEqual(self.plugin.read_text(encoding="utf-8"), "// plugin-v1\n")
        self.assertEqual(self.upgrade(v2).returncode, 0)
        self.assertEqual(self.plugin.read_text(encoding="utf-8"), "// plugin-v2\n")
        result = self.upgrade(v2, "--rollback", "latest")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.plugin.read_text(encoding="utf-8"), "// plugin-v1\n")
        data = json.loads(self.opencode_config.read_text(encoding="utf-8"))
        self.assertEqual(data["theme"], "dark")
        self.assertTrue(data["custom"]["keep"])
        self.assertIn("/opt/keep/AGENTS.md", data["instructions"])
        hooks = json.loads(self.hooks.read_text(encoding="utf-8"))
        self.assertEqual(hooks["unrelated-hook"]["Stop"][0]["command"], "keep-me")
        self.assertIn("Keep this instruction.", self.rules.read_text(encoding="utf-8"))
        self.assert_runtime_preserved()

    def test_upgrade_failure_leaves_no_mixed_code_or_integration_state(self) -> None:
        v1 = make_source(self.root / "failure-v1", version="old", plugin="// plugin-v1\n")
        self.seed_integrations()
        self.assertEqual(self.install(v1).returncode, 0)
        self.assertEqual(self.plugin.read_text(encoding="utf-8"), "// plugin-v1\n")
        v2 = make_source(self.root / "failure-v2", version="new", plugin="// plugin-v2\n")
        (v2 / "lib" / "agent_handoff" / "new_module.py").write_text("NEW = True\n", encoding="utf-8")
        inject_mid_install_failure(v2 / "setup" / "install.py")
        result = self.upgrade(v2)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / "lib" / "agent_handoff" / "new_module.py").exists())
        self.assertIn('__version__ = "old"', (self.home / "lib" / "agent_handoff" / "__init__.py").read_text())
        self.assertEqual(self.plugin.read_text(encoding="utf-8"), "// plugin-v1\n")
        data = json.loads(self.opencode_config.read_text(encoding="utf-8"))
        self.assertEqual(data["theme"], "dark")
        self.assertTrue(data["custom"]["keep"])
        self.assertIn("/opt/keep/AGENTS.md", data["instructions"])
        hooks = json.loads(self.hooks.read_text(encoding="utf-8"))
        self.assertEqual(hooks["unrelated-hook"]["Stop"][0]["command"], "keep-me")
        self.assertEqual(hooks["agent-handoff-report"]["Stop"][0]["command"], f"{self.link} hook agy")
        self.assertIn("Keep this instruction.", self.rules.read_text(encoding="utf-8"))
        self.assert_runtime_preserved()

if __name__ == "__main__":
    unittest.main()
