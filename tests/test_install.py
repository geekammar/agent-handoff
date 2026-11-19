from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "setup" / "install.py"
UPGRADE = ROOT / "setup" / "upgrade.py"


class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        (self.home / "config").mkdir()
        (self.home / "data").mkdir()
        self.config = self.home / "config" / "config.json"
        self.config.write_text('{"sentinel": true}\n', encoding="utf-8")
        os.chmod(self.config, 0o600)
        (self.home / "data" / "keep.txt").write_text("runtime\n", encoding="utf-8")
        (self.home / "state" / "dispatch" / "records").mkdir(parents=True)
        (self.home / "state" / "dispatch" / "records" / "sentinel.json").write_text('{"keep": true}\n', encoding="utf-8")
        self.link = self.root / "bin" / "agent-handoff"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def install(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                str(INSTALL),
                "--home",
                str(self.home),
                "--bin-link",
                str(self.link),
                "--no-opencode",
                "--no-agy",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_install_is_idempotent_and_preserves_runtime(self) -> None:
        first = self.install()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue((self.home / "lib" / "agent_handoff" / "adapters" / "agy.py").exists())
        self.assertTrue(self.link.is_symlink())
        self.assertEqual(self.link.resolve(), (self.home / "bin" / "agent-handoff").resolve())
        self.assertEqual(self.config.read_text(), '{"sentinel": true}\n')
        self.assertEqual((self.home / "data" / "keep.txt").read_text(), "runtime\n")
        self.assertEqual((self.home / "state" / "dispatch" / "records" / "sentinel.json").read_text(), '{"keep": true}\n')
        marker = self.home / "lib" / "agent_handoff" / "__init__.py"
        before = marker.stat().st_mtime_ns
        second = self.install()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(marker.stat().st_mtime_ns, before)
        self.assertEqual(self.config.read_text(), '{"sentinel": true}\n')

    def test_upgrade_and_rollback_preserve_runtime_config(self) -> None:
        self.assertEqual(self.install().returncode, 0)
        version_file = self.home / "lib" / "agent_handoff" / "__init__.py"
        version_file.write_text('__version__ = "test-old"\n', encoding="utf-8")
        upgrade = subprocess.run(
            [
                sys.executable,
                str(UPGRADE),
                "--home",
                str(self.home),
                "--bin-link",
                str(self.link),
                "--no-opencode",
                "--no-agy",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(upgrade.returncode, 0, upgrade.stderr)
        self.assertIn('__version__ = "1.1.0"', version_file.read_text())
        self.assertEqual(self.config.read_text(), '{"sentinel": true}\n')
        self.assertEqual((self.home / "data" / "keep.txt").read_text(), "runtime\n")
        self.assertEqual((self.home / "state" / "dispatch" / "records" / "sentinel.json").read_text(), '{"keep": true}\n')
        releases = [path for path in (self.home / "releases").iterdir() if path.is_dir()]
        self.assertTrue(releases)
        version_file.write_text('__version__ = "test-new"\n', encoding="utf-8")
        rollback = subprocess.run(
            [
                sys.executable,
                str(UPGRADE),
                "--home",
                str(self.home),
                "--bin-link",
                str(self.link),
                "--no-opencode",
                "--no-agy",
                "--rollback",
                "latest",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(rollback.returncode, 0, rollback.stderr)
        self.assertIn('__version__ = "test-old"', version_file.read_text())
        self.assertEqual(self.config.read_text(), '{"sentinel": true}\n')
        self.assertEqual((self.home / "data" / "keep.txt").read_text(), "runtime\n")


if __name__ == "__main__":
    unittest.main()
