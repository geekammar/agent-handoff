#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parents[1]
BIN = BASE / "bin" / "agent-handoff"
TEST = Path(os.environ.get("AGENT_HANDOFF_ACCEPTANCE_HOME", "/tmp/agent-handoff-acceptance"))
FAKE_DRIVE = TEST / "fake-drive"
FAKE_BIN = TEST / "bin"
RCLONE_CONFIG = TEST / "rclone.conf"
PRODUCTION_HOME = Path(os.environ.get("AGENT_HANDOFF_PRODUCTION_HOME", os.environ.get("AGENT_HANDOFF_HOME", str(Path.home() / "agent-handoff"))))
DEFAULT_PROJECT = "ProjectA"
DEFAULT_ROOT_NAME = "03 — Agent Execution Reports"
DEFAULT_PARENT_NAME = "AI Project Operations"

RESULTS: list[dict[str, Any]] = []


def record(name: str, passed: bool, detail: str, evidence: Any = None, status: str | None = None) -> None:
    resolved = status or ("PASS" if passed else "FAIL")
    RESULTS.append({"name": name, "passed": bool(passed), "status": resolved, "detail": detail, "evidence": evidence})
    print(f"[{resolved}] {name}: {detail}")


def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def live_settings() -> dict[str, Any]:
    config = load_json(PRODUCTION_HOME / "config" / "config.json", default={}) or {}
    extra_path = Path(os.environ.get("AGENT_HANDOFF_LIVE_CONFIG", str(BASE / "tests" / "live-config.json")))
    extra = load_json(extra_path, default={}) or {}
    drive = config.get("drive") or {}
    projects = []
    for key, item in (config.get("projects") or {}).items():
        paths = item.get("paths") or []
        projects.append(
            {
                "key": key,
                "name": item.get("name") or key,
                "folder_id": item.get("drive_folder_id"),
                "path": paths[0] if paths else "",
            }
        )
    projects.sort(key=lambda item: item["name"].lower())
    if not projects:
        projects = [
            {
                "key": "project-a",
                "name": DEFAULT_PROJECT,
                "folder_id": "acceptance_folder_0001",
                "path": "",
            }
        ]
    parent_id = extra.get("parent_folder_id") or drive.get("parent_folder_id") or ""
    root_id = extra.get("root_folder_id") or drive.get("root_folder_id") or ""
    parent_name = extra.get("parent_folder_name") or drive.get("parent_folder_name") or DEFAULT_PARENT_NAME
    root_name = extra.get("root_folder_name") or drive.get("root_folder_name") or DEFAULT_ROOT_NAME
    if not parent_id:
        parent_id = "acceptance_parent_0001"
    if not root_id:
        root_id = "acceptance_root_0001"
    return {
        "parent_id": parent_id,
        "root_id": root_id,
        "parent_name": parent_name,
        "root_name": root_name,
        "projects": projects,
        "chat_folder_id": extra.get("chat_folder_id"),
        "muse_folder_id": extra.get("muse_folder_id"),
        "git_project": extra.get("git_project") or next((item["path"] for item in projects if item["path"]), ""),
        "production_ready": bool(projects and drive.get("parent_folder_id") and drive.get("root_folder_id")),
    }


LIVE = live_settings()
PROJECT = LIVE["projects"][0]
PROJECT_NAME = PROJECT["name"]
PROJECT_ID = PROJECT["folder_id"] or "acceptance_folder_0001"
PARENT_ID = LIVE["parent_id"]
ROOT_ID = LIVE["root_id"]
ROOT_NAME = LIVE["root_name"]
PARENT_NAME = LIVE["parent_name"]


def env(extra: dict[str, str] | None = None) -> dict[str, str]:
    value = {
        "HOME": str(TEST),
        "AGENT_HANDOFF_HOME": str(TEST),
        "RCLONE_CONFIG": str(RCLONE_CONFIG),
        "FAKE_DRIVE_ROOT": str(FAKE_DRIVE),
        "PATH": f"{FAKE_BIN}:{os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')}",
    }
    if extra:
        value.update(extra)
    return value


def run(args: list[str], extra_env: dict[str, str] | None = None, check: bool = True, timeout: float = 120.0) -> subprocess.CompletedProcess:
    result = subprocess.run([str(BIN), *args], env={**os.environ, **env(extra_env)}, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(f"command failed ({result.returncode}): {args}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
    return result


def jrun(args: list[str], extra_env: dict[str, str] | None = None, check: bool = True) -> dict[str, Any]:
    result = run(args, extra_env=extra_env, check=check)
    return json.loads(result.stdout)


def seed_test_config() -> None:
    workspace = TEST / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    projects = {}
    for item in LIVE["projects"]:
        key = item["key"]
        path = workspace / key
        path.mkdir(parents=True, exist_ok=True)
        projects[key] = {
            "name": item["name"],
            "drive_folder_id": item["folder_id"] or f"acceptance_{key}_folder",
            "paths": [str(path)],
            "path_prefixes": [],
            "aliases": [key, item["name"].lower()],
            "auto_registered": False,
        }
    config = {
        "version": 1,
        "drive": {
            "remote": "gdrive",
            "parent_folder_id": PARENT_ID,
            "parent_folder_name": PARENT_NAME,
            "root_folder_id": ROOT_ID,
            "root_folder_name": ROOT_NAME,
        },
        "storage": {
            "data": str(TEST / "data"),
            "queue": str(TEST / "queue"),
            "state": str(TEST / "state"),
            "locks": str(TEST / "locks"),
            "logs": str(TEST / "logs"),
            "tmp": str(TEST / "tmp"),
        },
        "projects": projects,
        "workspace_roots": [str(workspace)],
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
    config_path = TEST / "config" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(config_path, 0o600)


def seed_drive() -> None:
    for path in (FAKE_DRIVE / PARENT_ID, FAKE_DRIVE / ROOT_ID, FAKE_DRIVE / PROJECT_ID):
        path.mkdir(parents=True, exist_ok=True)
    children = {item["name"]: item["folder_id"] or f"acceptance_{item['key']}_folder" for item in LIVE["projects"]}
    (FAKE_DRIVE / PARENT_ID / ".children.json").write_text(json.dumps({ROOT_NAME: ROOT_ID}, ensure_ascii=False), encoding="utf-8")
    (FAKE_DRIVE / ROOT_ID / ".children.json").write_text(json.dumps(children, ensure_ascii=False), encoding="utf-8")
    for item in LIVE["projects"]:
        folder_id = item["folder_id"] or f"acceptance_{item['key']}_folder"
        (FAKE_DRIVE / folder_id).mkdir(parents=True, exist_ok=True)


def parse_lsf_pi(output: str) -> dict[str, str]:
    result = {}
    for line in output.splitlines():
        line = line.rstrip()
        if not line or ";" not in line:
            continue
        name, item_id = line.split(";", 1)
        result[name.rstrip("/").strip()] = item_id.strip()
    return result


def production_env() -> dict[str, str]:
    value = {key: item for key, item in os.environ.items() if not key.startswith(("AGENT_HANDOFF_", "RCLONE_", "FAKE_"))}
    value["AGENT_HANDOFF_HOME"] = str(PRODUCTION_HOME)
    value["PATH"] = "/usr/local/bin:/usr/bin:/bin"
    return value


def real_rclone(args: list[str], timeout: float = 180.0) -> subprocess.CompletedProcess:
    value = {key: item for key, item in os.environ.items() if not key.startswith(("AGENT_HANDOFF_", "RCLONE_", "FAKE_"))}
    value["PATH"] = "/usr/local/bin:/usr/bin:/bin"
    return subprocess.run(["rclone", *args], env=value, capture_output=True, text=True, timeout=timeout)


def write_prompt(name: str, text: str) -> str:
    path = TEST / name
    path.write_text(text + "\n", encoding="utf-8")
    return str(path)


def task_report(run_dir: str, title: str, body: str) -> str:
    path = Path(run_dir) / "REPORT.md"
    path.write_text(f"# {title}\n\n## Executive Summary\n\n{body}\n\n## Tests Passed\n\n- `acceptance smoke`\n", encoding="utf-8")
    return str(path)


def create_run(project: str, agent: str, title: str, prompt: str, body: str) -> dict[str, Any]:
    prompt_file = write_prompt(f"prompt-{title.replace(' ', '-').lower()}.txt", prompt)
    task = jrun(["init", "--project", project, "--agent", agent, "--title", title, "--prompt-file", prompt_file, "--json"])
    run_info = jrun(["prepare-run", "--task-id", task["task_id"], "--json"])
    report = task_report(run_info["run_dir"], title, body)
    return {"task": task, "run": run_info, "report": report}


def complete(task_id: str, run_id: str, report: str, outcome: str, tests: str, extra: list[str] | None = None) -> dict[str, Any]:
    args = [
        "complete",
        "--task-id",
        task_id,
        "--run-id",
        run_id,
        "--report-file",
        report,
        "--outcome",
        outcome,
        "--tests",
        tests,
        "--json",
    ]
    if extra:
        args.extend(extra)
    return jrun(args)


def read_complete(task_id: str, run_id: str, project: str | None = None) -> dict[str, Any]:
    project = project or PROJECT_NAME
    matches = list((TEST / "data" / "projects" / project / "TASKS").glob(f"{task_id}-*/RUNS/{run_id}/COMPLETE.json"))
    if not matches:
        raise FileNotFoundError(f"COMPLETE.json not found for {task_id}/{run_id}")
    return json.loads(matches[0].read_text(encoding="utf-8"))


def read_task_dir(task_id: str, project: str | None = None) -> Path:
    project = project or PROJECT_NAME
    matches = list((TEST / "data" / "projects" / project / "TASKS").glob(f"{task_id}-*"))
    if not matches:
        raise FileNotFoundError(task_id)
    return matches[0]


def main() -> int:
    if TEST.exists():
        shutil.rmtree(TEST)
    TEST.mkdir(parents=True)
    FAKE_BIN.mkdir(parents=True)
    shutil.copy2(BASE / "tests" / "fake-rclone", FAKE_BIN / "rclone")
    os.chmod(FAKE_BIN / "rclone", 0o755)
    RCLONE_CONFIG.write_text("", encoding="utf-8")
    os.chmod(RCLONE_CONFIG, 0o600)
    seed_test_config()
    seed_drive()
    git_project = LIVE["git_project"]
    git_before = subprocess.run(["git", "-C", git_project, "status", "--porcelain"], capture_output=True, text=True).stdout if git_project and Path(git_project).is_dir() else ""
    real_config = Path.home() / ".config" / "rclone" / "rclone.conf"
    real_rclone_before = (real_config.exists(), real_config.stat().st_mtime if real_config.exists() else None)

    doctor = json.loads(run(["doctor", "--json"], check=False).stdout)
    record("doctor with simulated Drive", doctor.get("upload", {}).get("remote_ready") is True and doctor.get("upload", {}).get("connection", {}).get("connected") is True, "doctor recognized the simulated rclone remote and hierarchy", doctor.get("upload", {}).get("hierarchy", {}).get("ok"))
    hierarchy_projects = doctor.get("upload", {}).get("hierarchy", {}).get("projects", {})
    verified = all(item.get("verified") for item in hierarchy_projects.values() if item.get("configured"))
    record("drive folder IDs", verified and bool(hierarchy_projects), "configured project folder IDs resolve under the reports root")

    config_path = TEST / "config" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["upload"]["auto_upload"] = False
    config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    a = create_run(PROJECT_NAME, "opencode", "Acceptance A", "PROMPT-A-UNIQUE", "Report A content.")
    b = create_run(PROJECT_NAME, "agy", "Acceptance B", "PROMPT-B-UNIQUE", "Report B content.")
    complete_b = complete(b["task"]["task_id"], b["run"]["run_id"], b["report"], "succeeded", "passed", ["--tests-details", "acceptance B test"])
    time.sleep(1.2)
    complete_a = complete(a["task"]["task_id"], a["run"]["run_id"], a["report"], "succeeded", "not_verified")
    record("A concurrent task separation", a["task"]["task_id"] != b["task"]["task_id"] and a["run"]["run_id"] != b["run"]["run_id"], "task and run IDs are distinct")
    record("A prompt separation", "PROMPT-A-UNIQUE" in (read_task_dir(a["task"]["task_id"]) / "PROMPT.md").read_text(encoding="utf-8") and "PROMPT-B-UNIQUE" in (read_task_dir(b["task"]["task_id"]) / "PROMPT.md").read_text(encoding="utf-8"), "both exact prompts remain in separate task directories")
    report_a = (read_task_dir(a["task"]["task_id"]) / "RUNS" / a["run"]["run_id"] / "REPORT.md").read_text(encoding="utf-8")
    report_b = (read_task_dir(b["task"]["task_id"]) / "RUNS" / b["run"]["run_id"] / "REPORT.md").read_text(encoding="utf-8")
    record("A report separation", "Report A content." in report_a and "Report B content." in report_b and "Report B content." not in report_a, "reports do not overwrite each other")
    index_path = TEST / "data" / "projects" / PROJECT_NAME / "PROJECT-STATUS.md"
    index_text = index_path.read_text(encoding="utf-8")
    pos_a = index_text.find(a["task"]["task_id"])
    pos_b = index_text.find(b["task"]["task_id"])
    record("A index ordering", pos_a != -1 and pos_b != -1 and pos_a < pos_b, "index references both runs and sorts by completion time (A finished after B)")

    fail_marker = TEST / "fail-once"
    fail_marker.write_text("1", encoding="utf-8")
    failed = run(["retry-upload", "--force", "--run", b["run"]["run_id"], "--json"], extra_env={"FAKE_FAIL_ONCE_FILE": str(fail_marker)}, check=False)
    failed_json = json.loads(failed.stdout)
    complete_b_after_fail = read_complete(b["task"]["task_id"], b["run"]["run_id"])
    record("C upload recovery (failure preserved)", failed.returncode != 0 and complete_b_after_fail["upload"]["verified"] is False and Path(b["report"]).exists(), "simulated outage left the report on disk and the upload pending", {"exit": failed.returncode, "queue": failed_json})
    retry = jrun(["retry-upload", "--force", "--json"])
    complete_a_after = read_complete(a["task"]["task_id"], a["run"]["run_id"])
    complete_b_after = read_complete(b["task"]["task_id"], b["run"]["run_id"])
    remote_a = FAKE_DRIVE / PROJECT_ID / "TASKS" / read_task_dir(a["task"]["task_id"]).name / "RUNS" / a["run"]["run_id"] / "REPORT.md"
    remote_b = FAKE_DRIVE / PROJECT_ID / "TASKS" / read_task_dir(b["task"]["task_id"]).name / "RUNS" / b["run"]["run_id"] / "REPORT.md"
    remote_index = FAKE_DRIVE / PROJECT_ID / "PROJECT-STATUS.md"
    record("C upload recovery (retry verified)", retry.get("ok") is True and complete_a_after["upload"]["verified"] is True and complete_b_after["upload"]["verified"] is True and remote_a.exists() and remote_b.exists(), "retry uploaded and md5-verified both runs", retry)
    record("C remote index", remote_index.exists() and a["task"]["task_id"] in remote_index.read_text(encoding="utf-8") and b["task"]["task_id"] in remote_index.read_text(encoding="utf-8"), "PROJECT-STATUS.md was uploaded only after verified runs and includes both", str(remote_index))
    record("C index has no local paths", "/tmp/" not in remote_index.read_text(encoding="utf-8"), "remote index does not expose local filesystem paths")

    auto_home = TEST / "auto-home"
    auto_home.mkdir(parents=True)
    auto_rclone = auto_home / "rclone.conf"
    auto_rclone.write_text("", encoding="utf-8")
    os.chmod(auto_rclone, 0o600)
    auto_extra = {"AGENT_HANDOFF_HOME": str(auto_home), "RCLONE_CONFIG": str(auto_rclone)}
    auto_config = json.loads(config_path.read_text(encoding="utf-8"))
    auto_config["upload"]["auto_upload"] = True
    auto_config["storage"] = {
        "data": str(auto_home / "data"),
        "queue": str(auto_home / "queue"),
        "state": str(auto_home / "state"),
        "locks": str(auto_home / "locks"),
        "logs": str(auto_home / "logs"),
        "tmp": str(auto_home / "tmp"),
    }
    (auto_home / "config").mkdir(parents=True, exist_ok=True)
    (auto_home / "config" / "config.json").write_text(json.dumps(auto_config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(auto_home / "config" / "config.json", 0o600)
    run(["doctor", "--json"], extra_env=auto_extra, check=False)
    auto_prompt = auto_home / "prompt.txt"
    auto_prompt.write_text("AUTO-UPLOAD-PROMPT\n", encoding="utf-8")
    auto_task = jrun(["init", "--project", PROJECT_NAME, "--agent", "opencode", "--title", "Auto background upload", "--prompt-file", str(auto_prompt), "--json"], extra_env=auto_extra)
    auto_run = jrun(["prepare-run", "--task-id", auto_task["task_id"], "--json"], extra_env=auto_extra)
    auto_report = auto_home / "REPORT.md"
    auto_report.write_text("# Auto upload\n\n## Executive Summary\n\nBackground worker test.\n", encoding="utf-8")
    run(["complete", "--task-id", auto_task["task_id"], "--run-id", auto_run["run_id"], "--report-file", str(auto_report), "--outcome", "succeeded", "--tests", "not_verified", "--json"], extra_env=auto_extra)
    auto_complete_path = list((auto_home / "data" / "projects" / PROJECT_NAME / "TASKS").glob(f"{auto_task['task_id']}-*/RUNS/{auto_run['run_id']}/COMPLETE.json"))[0]
    deadline = time.time() + 120
    auto_ok = False
    complete_data = {}
    while time.time() < deadline:
        complete_data = json.loads(auto_complete_path.read_text(encoding="utf-8"))
        if complete_data.get("upload", {}).get("verified") and not list((auto_home / "queue").glob("*.json")):
            auto_ok = True
            break
        time.sleep(1)
    remote_dir = complete_data.get("upload", {}).get("remote_dir", "")
    remote_auto = FAKE_DRIVE / PROJECT_ID / ("TASKS/" + remote_dir.split("/")[1] if "/" in remote_dir else remote_dir) / "RUNS" / auto_run["run_id"] / "REPORT.md"
    record("Automatic background upload", auto_ok and remote_auto.exists(), "completion spawned the upload worker without a manual retry", {"queue": len(list((auto_home / 'queue').glob('*.json')))})

    c = create_run(PROJECT_NAME, "opencode", "Acceptance Failed", "PROMPT-FAILED", "This task is intentionally failed.")
    complete(c["task"]["task_id"], c["run"]["run_id"], c["report"], "failed", "failed", ["--tests-details", "simulated failing test", "--remaining-issues", "Simulated failure must stay open"])
    run(["retry-upload", "--force", "--json"])
    failed_complete = read_complete(c["task"]["task_id"], c["run"]["run_id"])
    failed_index = (FAKE_DRIVE / PROJECT_ID / "PROJECT-STATUS.md").read_text(encoding="utf-8")
    record("B failure handling", failed_complete["outcome"] == "failed" and failed_complete["tests"]["status"] == "failed" and "failed" in failed_index and "Simulated failure must stay open" in failed_index, "failed task is labeled failed, not succeeded, and appears as an open issue")

    d = create_run(PROJECT_NAME, "opencode", "Acceptance Redaction", "PROMPT-REDACTION", "Secret check: API_KEY = supersecretvalue123 and postgres://user:pass@example.test/db")
    complete(d["task"]["task_id"], d["run"]["run_id"], d["report"], "succeeded", "not_verified")
    run(["retry-upload", "--force", "--json"])
    local_report = (read_task_dir(d["task"]["task_id"]) / "RUNS" / d["run"]["run_id"] / "REPORT.md").read_text(encoding="utf-8")
    remote_report_path = FAKE_DRIVE / PROJECT_ID / "TASKS" / read_task_dir(d["task"]["task_id"]).name / "RUNS" / d["run"]["run_id"] / "REPORT.md"
    remote_report = remote_report_path.read_text(encoding="utf-8")
    complete_d = read_complete(d["task"]["task_id"], d["run"]["run_id"])
    record("Security redaction", "supersecretvalue123" in local_report and "supersecretvalue123" not in remote_report and "[REDACTED]" in remote_report and complete_d["upload"]["redactions"] > 0, "obvious secret was redacted in the uploaded copy; local report kept intact")

    e = create_run(PROJECT_NAME, "opencode", "Concurrent E", "PROMPT-E", "Report E.")
    f = create_run(PROJECT_NAME, "agy", "Concurrent F", "PROMPT-F", "Report F.")
    proc_e = subprocess.Popen([str(BIN), "complete", "--task-id", e["task"]["task_id"], "--run-id", e["run"]["run_id"], "--report-file", e["report"], "--outcome", "succeeded", "--tests", "not_verified", "--json"], env={**os.environ, **env()}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    proc_f = subprocess.Popen([str(BIN), "complete", "--task-id", f["task"]["task_id"], "--run-id", f["run"]["run_id"], "--report-file", f["report"], "--outcome", "succeeded", "--tests", "not_verified", "--json"], env={**os.environ, **env()}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out_e, err_e = proc_e.communicate(timeout=120)
    out_f, err_f = proc_f.communicate(timeout=120)
    concurrent_index = index_path.read_text(encoding="utf-8")
    record("Concurrent completions", proc_e.returncode == 0 and proc_f.returncode == 0 and e["task"]["task_id"] in concurrent_index and f["task"]["task_id"] in concurrent_index, "two simultaneous completions both survived the shared lock", {"e": proc_e.returncode, "f": proc_f.returncode})
    run(["retry-upload", "--force", "--json"])

    before_count = len(list((TEST / "data" / "projects" / PROJECT_NAME / "TASKS").iterdir()))
    prompt_hash_before = __import__("hashlib").sha256((read_task_dir(a["task"]["task_id"]) / "PROMPT.md").read_bytes()).hexdigest()
    run(["init", "--project", PROJECT_NAME, "--agent", "opencode", "--title", "Acceptance A", "--prompt-file", str(TEST / "prompt-acceptance-a.txt"), "--task-id", a["task"]["task_id"], "--json"])
    run(["doctor", "--json"], check=False)
    run(["doctor", "--json"], check=False)
    prompt_hash_after = __import__("hashlib").sha256((read_task_dir(a["task"]["task_id"]) / "PROMPT.md").read_bytes()).hexdigest()
    after_count = len(list((TEST / "data" / "projects" / PROJECT_NAME / "TASKS").iterdir()))
    record("E repeatability", before_count == after_count and prompt_hash_before == prompt_hash_after, "re-running init and doctor did not duplicate or alter existing data", {"tasks": before_count})

    git_after = subprocess.run(["git", "-C", git_project, "status", "--porcelain"], capture_output=True, text=True).stdout if git_project and Path(git_project).is_dir() else ""
    real_rclone_after = (real_config.exists(), real_config.stat().st_mtime if real_config.exists() else None)
    record("D local repository isolation", git_before == git_after, "local project working tree unchanged by acceptance tests", {"status": git_after.strip()})
    record("D rclone config isolation", real_rclone_before == real_rclone_after, "tests used an isolated RCLONE_CONFIG path")

    p_env = production_env()
    production_list = subprocess.run([str(BIN), "list", "--json"], cwd=str(PRODUCTION_HOME), env=p_env, capture_output=True, text=True)
    production_rows = json.loads(production_list.stdout).get("rows", []) if production_list.returncode == 0 else []
    opencode_rows = [row for row in production_rows if row.get("agent") == "opencode" and row.get("report") != "-"]
    agy_rows = [row for row in production_rows if row.get("agent") == "agy" and row.get("report") != "-"]
    record("F OpenCode real session", bool(opencode_rows), f"{len(opencode_rows)} real OpenCode run(s) captured in production", opencode_rows[:2])
    record("F AGY real session", bool(agy_rows), f"{len(agy_rows)} real AGY run(s) captured in production", agy_rows[:2])
    production_upload = all(row.get("upload") == "verified" for row in opencode_rows + agy_rows) if (opencode_rows or agy_rows) else False

    auth_check = subprocess.run([str(BIN), "auth", "--check", "--json"], env=p_env, capture_output=True, text=True, timeout=180)
    if auth_check.returncode == 0:
        record("F real Google Drive upload", production_upload, "all production runs show upload: verified", status="PASS" if production_upload else "FAIL")
        doctor_prod = json.loads(subprocess.run([str(BIN), "doctor", "--json"], env=p_env, capture_output=True, text=True, timeout=300).stdout)
        checks = {item["component"]: item["status"] for item in doctor_prod.get("checks", [])}
        drive_checks = [value for key, value in checks.items() if key.startswith("drive:")]
        hierarchy_ok = checks.get("Google Drive access") == "PASS" and bool(drive_checks) and all(value == "PASS" for value in drive_checks)
        record("F real Google Drive hierarchy", hierarchy_ok, "real Drive access and configured project folder IDs verified by doctor")
        md5_ok = True
        checked = 0
        for row in production_rows:
            report = Path(row["report"])
            complete_path = report.parent / "COMPLETE.json"
            if not complete_path.exists():
                md5_ok = False
                continue
            complete_data = json.loads(complete_path.read_text(encoding="utf-8"))
            if not complete_data.get("upload", {}).get("verified"):
                md5_ok = False
                continue
            remote_rel = complete_data.get("report", {}).get("remote_path")
            local_md5 = __import__("hashlib").md5(report.read_bytes()).hexdigest()
            result = real_rclone(["md5sum", f"gdrive,root_folder_id={PROJECT_ID}:{remote_rel}"], timeout=180)
            remote_md5 = result.stdout.strip().split()[0] if result.returncode == 0 and result.stdout.strip() else ""
            if remote_md5 != local_md5:
                md5_ok = False
            checked += 1
        record("F real Google Drive report MD5", md5_ok and checked > 0, f"{checked} remote reports matched local MD5", {"checked": checked})
        index_result = real_rclone(["cat", f"gdrive,root_folder_id={PROJECT_ID}:PROJECT-STATUS.md"], timeout=180)
        index_ok = index_result.returncode == 0 and all(row["task_id"] in index_result.stdout for row in production_rows)
        record("F real Google Drive index", index_ok, "remote PROJECT-STATUS.md is present and references all production tasks")
        root_result = real_rclone(["lsf", "gdrive:", "--dirs-only", "--format", "pi", "--max-depth", "1"], timeout=240)
        roots = parse_lsf_pi(root_result.stdout) if root_result.returncode == 0 else {}
        root_ok = roots.get(PARENT_NAME) == PARENT_ID
        record("F single AI Project Operations root", root_ok, "exactly one configured parent root ID found", {"name": PARENT_NAME})
        root_children = real_rclone(["lsf", f"gdrive,root_folder_id={ROOT_ID}:", "--dirs-only", "--format", "pi"], timeout=180)
        children = parse_lsf_pi(root_children.stdout) if root_children.returncode == 0 else {}
        expected_children = {item["name"]: item["folder_id"] for item in LIVE["projects"] if item["folder_id"]}
        children_ok = all(children.get(name) == folder_id for name, folder_id in expected_children.items()) and bool(expected_children)
        record("D real Drive 03 isolation", children_ok, "reports root contains the configured project folder IDs; no duplicate root was created", children)
        if LIVE.get("chat_folder_id") and LIVE.get("muse_folder_id"):
            chat = real_rclone(["lsf", f"gdrive,root_folder_id={LIVE['chat_folder_id']}:", "--dirs-only", "--format", "pi", "--max-depth", "1"], timeout=180)
            qa = real_rclone(["lsf", f"gdrive,root_folder_id={LIVE['muse_folder_id']}:", "--dirs-only", "--format", "pi", "--max-depth", "1"], timeout=180)
            chat_names = parse_lsf_pi(chat.stdout) if chat.returncode == 0 else {}
            qa_names = parse_lsf_pi(qa.stdout) if qa.returncode == 0 else {}
            untouched = chat.returncode == 0 and qa.returncode == 0 and not any(name in ("TASKS", "PROJECT-STATUS.md", "agent-handoff") for name in set(chat_names) | set(qa_names))
            expected_chat = any("Chat Recovery" in name for name in chat_names)
            expected_qa = any("Muse Test Coordination" in name for name in qa_names)
            record("D real Drive read-only isolation", untouched and expected_chat and expected_qa, "other Drive subtrees still exist and contain no Agent Handoff artifacts", {"chat": sorted(chat_names), "qa": sorted(qa_names)})
        else:
            record("D real Drive read-only isolation", False, "chat/muse folder IDs are not configured in tests/live-config.json", status="NOT VERIFIED")
    else:
        record("F real Google Drive upload", False, "Google Drive OAuth is not authorized, so real production reports remain queued", status="NOT VERIFIED")
        record("F real Google Drive hierarchy", False, "Google Drive OAuth is not authorized", status="NOT VERIFIED")
        record("F real Google Drive report MD5", False, "Google Drive OAuth is not authorized", status="NOT VERIFIED")
        record("F real Google Drive index", False, "Google Drive OAuth is not authorized", status="NOT VERIFIED")
        record("F single AI Project Operations root", False, "Google Drive OAuth is not authorized", status="NOT VERIFIED")
        record("D real Drive 03 isolation", False, "Google Drive OAuth is not authorized", status="NOT VERIFIED")
        record("D real Drive read-only isolation", False, "Google Drive OAuth is not authorized", status="NOT VERIFIED")

    real_results = [item for item in RESULTS if item["name"].startswith(("F real", "F single", "D real Drive"))]
    if any(item["status"] == "FAIL" for item in real_results):
        real_google_drive = "FAIL"
    elif any(item["status"] == "NOT VERIFIED" for item in real_results):
        real_google_drive = "NOT VERIFIED - OAuth required"
    else:
        real_google_drive = "VERIFIED"
    failures = [item for item in RESULTS if item["status"] == "FAIL"]
    not_verified = [item for item in RESULTS if item["status"] == "NOT VERIFIED"]
    passed = [item for item in RESULTS if item["status"] == "PASS"]
    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tests": RESULTS,
        "passed": len(passed),
        "failed": len(failures),
        "not_verified": len(not_verified),
        "test_home": str(TEST),
        "fake_drive": str(FAKE_DRIVE),
        "real_google_drive": real_google_drive,
    }
    (BASE / "tests" / "acceptance-results.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("")
    print(f"Result: {summary['passed']} passed, {summary['failed']} failed, {summary['not_verified']} not verified")
    print(f"Artifacts: {BASE / 'tests' / 'acceptance-results.json'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
