from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin" / "agent-handoff"
FAKE_RCLONE = ROOT / "tests" / "fake-rclone"

PARENT_ID = "parent_folder_0001"
ROOT_ID = "root_folder_0001"
PROJECT_ID = "alpha_folder_0001"
ROOT_NAME = "03 — Test Reports"
PROJECT_NAME = "Alpha"


class Harness:
    def __init__(self, root: Path):
        self.root = root
        self.home = root / "home"
        self.workspace = root / "workspace"
        self.drive = root / "fake-drive"
        self.fake_bin = root / "fake-bin"
        self.rclone_config = root / "rclone.conf"
        self.bin = BIN
        self.project_path = self.workspace / "alpha"
        self.home.mkdir(parents=True)
        self.project_path.mkdir(parents=True)
        self.fake_bin.mkdir(parents=True)
        (self.home / "config").mkdir(parents=True)
        shutil.copy2(FAKE_RCLONE, self.fake_bin / "rclone")
        os.chmod(self.fake_bin / "rclone", 0o755)
        self.rclone_config.write_text("", encoding="utf-8")
        os.chmod(self.rclone_config, 0o600)
        self.config = self._config()
        self.write_config()
        self.seed_drive()

    def _config(self) -> dict[str, Any]:
        return {
            "version": 1,
            "drive": {
                "remote": "gdrive",
                "parent_folder_id": PARENT_ID,
                "parent_folder_name": "Test Parent",
                "root_folder_id": ROOT_ID,
                "root_folder_name": ROOT_NAME,
            },
            "storage": {
                "data": str(self.home / "data"),
                "queue": str(self.home / "queue"),
                "state": str(self.home / "state"),
                "locks": str(self.home / "locks"),
                "logs": str(self.home / "logs"),
                "tmp": str(self.home / "tmp"),
            },
            "projects": {
                "alpha": {
                    "name": PROJECT_NAME,
                    "drive_folder_id": PROJECT_ID,
                    "paths": [str(self.project_path)],
                    "path_prefixes": [],
                    "aliases": ["alpha"],
                    "auto_registered": False,
                }
            },
            "workspace_roots": [str(self.workspace)],
            "auto_register_projects": False,
            "upload": {
                "enabled": True,
                "auto_upload": False,
                "verify": True,
                "timeout_seconds": 60,
                "hook_timeout_seconds": 25,
                "max_attempts": 0,
                "verify_interval_minutes": 60,
            },
            "integrations": {"opencode": True, "agy": True},
            "security": {"redact_uploads": True, "max_prompt_bytes": 200000, "max_summary_bytes": 50000},
            "index": {"include_pending_local": True, "max_open_issues": 20},
        }

    def write_config(self) -> None:
        path = self.home / "config" / "config.json"
        path.write_text(json.dumps(self.config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)

    def seed_drive(self) -> None:
        for folder in (PARENT_ID, ROOT_ID, PROJECT_ID):
            (self.drive / folder).mkdir(parents=True, exist_ok=True)
        (self.drive / PARENT_ID / ".children.json").write_text(
            json.dumps({ROOT_NAME: ROOT_ID}, ensure_ascii=False), encoding="utf-8"
        )
        (self.drive / ROOT_ID / ".children.json").write_text(
            json.dumps({PROJECT_NAME: PROJECT_ID}, ensure_ascii=False), encoding="utf-8"
        )

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        value = {
            "HOME": str(self.home),
            "AGENT_HANDOFF_HOME": str(self.home),
            "RCLONE_CONFIG": str(self.rclone_config),
            "FAKE_DRIVE_ROOT": str(self.drive),
            "PATH": f"{self.fake_bin}:{os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')}",
        }
        if extra:
            value.update(extra)
        return value

    def run(
        self,
        args: list[str],
        check: bool = True,
        extra_env: dict[str, str] | None = None,
        input_text: str | None = None,
        timeout: float = 120.0,
    ) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [sys.executable, str(self.bin), *args],
            cwd=str(self.root),
            env=self.env(extra_env),
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if check and result.returncode != 0:
            raise RuntimeError(
                f"command failed ({result.returncode}): {args}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
            )
        return result

    def jrun(self, args: list[str], extra_env: dict[str, str] | None = None, check: bool = True) -> dict[str, Any]:
        return json.loads(self.run(args, check=check, extra_env=extra_env).stdout)

    def create_run(self, agent: str, title: str, prompt: str, body: str) -> dict[str, Any]:
        prompt_file = self.root / f"prompt-{title.replace(' ', '-').lower()}.txt"
        prompt_file.write_text(prompt + "\n", encoding="utf-8")
        task = self.jrun(["init", "--project", PROJECT_NAME, "--agent", agent, "--title", title, "--prompt-file", str(prompt_file), "--json"])
        run_info = self.jrun(["prepare-run", "--task-id", task["task_id"], "--json"])
        report = Path(run_info["run_dir"]) / "REPORT.md"
        report.write_text(f"# {title}\n\n## Executive Summary\n\n{body}\n", encoding="utf-8")
        return {"task": task, "run": run_info, "report": str(report)}

    def complete(self, item: dict[str, Any], outcome: str = "succeeded", tests: str = "not_verified", extra: list[str] | None = None) -> dict[str, Any]:
        args = [
            "complete",
            "--task-id",
            item["task"]["task_id"],
            "--run-id",
            item["run"]["run_id"],
            "--report-file",
            item["report"],
            "--outcome",
            outcome,
            "--tests",
            tests,
            "--json",
        ]
        if extra:
            args.extend(extra)
        return self.jrun(args)

    def task_dir(self, task_id: str) -> Path:
        matches = list((self.home / "data" / "projects" / PROJECT_NAME / "TASKS").glob(f"{task_id}-*"))
        if not matches:
            raise FileNotFoundError(task_id)
        return matches[0]

    def read_complete(self, task_id: str, run_id: str) -> dict[str, Any]:
        path = self.task_dir(task_id) / "RUNS" / run_id / "COMPLETE.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def remote(self, *parts: str) -> Path:
        return self.drive.joinpath(PROJECT_ID, *parts)

    def queue_items(self) -> list[Path]:
        return sorted((self.home / "queue").glob("*.json"))

    def rows(self) -> list[dict[str, Any]]:
        return self.jrun(["list", "--json"])["rows"]
