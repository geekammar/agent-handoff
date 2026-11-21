from __future__ import annotations

import math
import os
import signal
import subprocess
import threading
import time
from typing import Any, Mapping

from ...util import sanitize_error, utcnow_iso
from ..security import DEFAULT_CHILD_PATH, popen_isolation_kwargs
from .config import SECRET_ENV_MARKERS
from .process import process_group_alive, terminate_process_group

DEFAULT_TEST_TIMEOUT_SECONDS = 900
MAX_CAPTURE_SECONDS = 5.0


class TestExecutionError(RuntimeError):
    pass


def build_test_environment(
    identity: Mapping[str, Any],
    workspace: str,
    profile_env: Mapping[str, str] | None = None,
    parent_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a minimal test environment without privileged inherited credentials."""

    parent = parent_env or {}
    env = {
        "PATH": str(identity.get("child_path") or DEFAULT_CHILD_PATH),
        "HOME": str(identity.get("home") or "/"),
        "USER": str(identity.get("user") or "unknown"),
        "LOGNAME": str(identity.get("user") or "unknown"),
        "XDG_CONFIG_HOME": str(identity.get("config_home") or ""),
        "XDG_DATA_HOME": str(identity.get("data_home") or ""),
        "XDG_CACHE_HOME": str(identity.get("cache_home") or ""),
        "TMPDIR": str(identity.get("tmp_dir") or "/tmp"),
        "PWD": workspace,
        "PYTHONHASHSEED": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }
    for name in ("LANG", "LC_ALL", "LC_CTYPE", "TZ"):
        value = parent.get(name)
        if isinstance(value, str) and value and "\x00" not in value and "\n" not in value and len(value) <= 200:
            env[name] = value
    for key, value in (profile_env or {}).items():
        name = str(key)
        upper = name.upper()
        if name in {"PATH", "HOME", "PWD", "USER", "LOGNAME"} or name.startswith(
            ("AGENT_HANDOFF_", "GH_", "GITHUB_", "RCLONE_", "OPENCODE_")
        ):
            raise TestExecutionError(f"trusted test profile tries to override a protected variable: {name}")
        if any(marker in upper for marker in SECRET_ENV_MARKERS):
            raise TestExecutionError(f"trusted test profile contains a credential-like variable: {name}")
        env[name] = str(value)
    return env


def _reader(stream: Any, limit: int, sink: dict[str, Any]) -> None:
    chunks: list[bytes] = []
    kept = 0
    truncated = False
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            if kept < limit:
                take = chunk[: limit - kept]
                chunks.append(take)
                kept += len(take)
                if len(take) < len(chunk):
                    truncated = True
            else:
                truncated = True
    finally:
        try:
            stream.close()
        except OSError:
            pass
    sink["data"] = b"".join(chunks)
    sink["truncated"] = truncated


def _text(data: bytes, limit: int) -> str:
    return data.decode("utf-8", errors="replace")[:limit]


def _preexec_limits(
    timeout_seconds: int,
    *,
    memory_bytes: int = 2_000_000_000,
    processes: int = 128,
    file_bytes: int = 256_000_000,
) -> Any:
    def apply() -> None:
        try:
            import resource

            cpu = max(1, int(math.ceil(timeout_seconds)) + 5)
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 5))
            resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
            if hasattr(resource, "RLIMIT_AS") and memory_bytes > 0:
                resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
            if hasattr(resource, "RLIMIT_NPROC") and processes > 0:
                resource.setrlimit(resource.RLIMIT_NPROC, (processes, processes))
            if hasattr(resource, "RLIMIT_FSIZE") and file_bytes > 0:
                resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes))
        except Exception:
            pass

    return apply


def run_command_bounded(
    argv: list[str],
    *,
    workspace: str,
    environment: Mapping[str, str],
    identity: Mapping[str, Any],
    timeout_seconds: float,
    max_output_bytes: int,
    memory_bytes: int = 2_000_000_000,
    processes: int = 128,
    file_bytes: int = 256_000_000,
    spawn: Any = None,
) -> dict[str, Any]:
    if not argv or not all(isinstance(item, str) and item for item in argv):
        raise TestExecutionError("test command must be a non-empty argument array")
    uid = int(identity.get("uid", -1))
    gid = int(identity.get("gid", -1))
    if uid <= 0 or gid <= 0:
        raise TestExecutionError("test execution requires a dedicated non-root Unix identity")
    if os.geteuid() == 0 and spawn is None:
        kwargs = popen_isolation_kwargs(identity)
    elif spawn is None:
        raise TestExecutionError("test execution requires the dispatcher to run as root and drop to the dedicated identity")
    else:
        kwargs = {}

    started = time.monotonic()
    spawn_callable = spawn or subprocess.Popen
    extra: dict[str, Any] = {}
    if spawn is None and os.name == "posix":
        extra["preexec_fn"] = _preexec_limits(
            int(timeout_seconds),
            memory_bytes=memory_bytes,
            processes=processes,
            file_bytes=file_bytes,
        )
    try:
        process = spawn_callable(
            argv,
            cwd=workspace,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            **kwargs,
            **extra,
        )
    except OSError as exc:
        return {
            "argv": list(argv),
            "status": "error",
            "exit_code": None,
            "timed_out": False,
            "duration_seconds": 0.0,
            "stdout": "",
            "stderr": sanitize_error(str(exc), 1000),
            "output_truncated": False,
            "started_at": utcnow_iso(),
            "finished_at": utcnow_iso(),
        }

    out_sink: dict[str, Any] = {"data": b"", "truncated": False}
    err_sink: dict[str, Any] = {"data": b"", "truncated": False}
    threads = [
        threading.Thread(target=_reader, args=(process.stdout, max_output_bytes, out_sink), daemon=True),
        threading.Thread(target=_reader, args=(process.stderr, max_output_bytes, err_sink), daemon=True),
    ]
    for thread in threads:
        thread.start()
    timed_out = False
    exit_code: int | None = None
    deadline = started + max(1.0, float(timeout_seconds))
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except OSError:
                    process.terminate()
                try:
                    exit_code = process.wait(timeout=MAX_CAPTURE_SECONDS)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except OSError:
                        process.kill()
                    try:
                        exit_code = process.wait(timeout=MAX_CAPTURE_SECONDS)
                    except subprocess.TimeoutExpired:
                        exit_code = None
                break
            try:
                exit_code = process.wait(timeout=min(remaining, 1.0))
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                process.kill()
            try:
                process.wait(timeout=MAX_CAPTURE_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        for thread in threads:
            thread.join(timeout=MAX_CAPTURE_SECONDS)

    duration = max(0.0, time.monotonic() - started)
    process_group_id = process.pid
    orphans_detected = process_group_alive(process_group_id)
    if orphans_detected:
        terminated = terminate_process_group(process_group_id)
        if not terminated:
            err_sink["data"] = (err_sink["data"] + b"\norphaned test process group could not be terminated")[:max_output_bytes]
    if timed_out:
        status = "timed_out"
    elif orphans_detected:
        status = "orphans"
    elif exit_code == 0:
        status = "passed"
    else:
        status = "failed"
    return {
        "argv": list(argv),
        "status": status,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_seconds": round(duration, 3),
        "stdout": _text(out_sink["data"], max_output_bytes),
        "stderr": _text(err_sink["data"], max_output_bytes),
        "output_truncated": bool(out_sink["truncated"] or err_sink["truncated"]),
        "process_group_id": process_group_id,
        "orphans_detected": bool(orphans_detected),
        "started_at": utcnow_iso(),
        "finished_at": utcnow_iso(),
    }


def run_test_profile(
    definition: Mapping[str, Any],
    *,
    workspace: str,
    identity: Mapping[str, Any],
    limits: Mapping[str, Any],
    parent_env: Mapping[str, str] | None = None,
    spawn: Any = None,
) -> dict[str, Any]:
    commands = definition.get("commands")
    if not isinstance(commands, list) or not commands:
        raise TestExecutionError("trusted test profile has no commands")
    profile_timeout = int(definition.get("timeout_seconds") or limits.get("test_timeout_seconds", DEFAULT_TEST_TIMEOUT_SECONDS))
    overall_timeout = min(profile_timeout, int(limits.get("test_timeout_seconds", DEFAULT_TEST_TIMEOUT_SECONDS)))
    max_output = int(limits.get("max_output_bytes", 50_000))
    environment = build_test_environment(
        identity,
        workspace,
        profile_env=definition.get("env") or {},
        parent_env=parent_env,
    )
    started = time.monotonic()
    results: list[dict[str, Any]] = []
    failed_command = None
    for command in commands:
        if not isinstance(command, list) or not command:
            raise TestExecutionError("trusted test profile contains an invalid command")
        argv = [str(item) for item in command]
        remaining = overall_timeout - (time.monotonic() - started)
        if remaining <= 0:
            results.append(
                {
                    "argv": argv,
                    "status": "timed_out",
                    "exit_code": None,
                    "timed_out": True,
                    "duration_seconds": 0.0,
                    "stdout": "",
                    "stderr": "trusted test budget exhausted before this command started",
                    "output_truncated": False,
                }
            )
            failed_command = argv
            break
        result = run_command_bounded(
            argv,
            workspace=workspace,
            environment=environment,
            identity=identity,
            timeout_seconds=remaining,
            max_output_bytes=max_output,
            memory_bytes=int(limits.get("max_test_memory_bytes", 2_000_000_000)),
            processes=int(limits.get("max_test_processes", 128)),
            file_bytes=int(limits.get("max_test_file_bytes", 256_000_000)),
            spawn=spawn,
        )
        results.append(result)
        if result["status"] != "passed":
            failed_command = argv
            break
    statuses = {item["status"] for item in results}
    if failed_command is not None:
        if "orphans" in statuses:
            status = "orphans"
        elif "timed_out" in statuses:
            status = "timed_out"
        else:
            status = "failed"
    else:
        status = "passed"
    return {
        "status": status,
        "profile": definition.get("name"),
        "commands": results,
        "failed_command": failed_command,
        "timeout_seconds": overall_timeout,
        "duration_seconds": round(max(0.0, time.monotonic() - started), 3),
        "completed_at": utcnow_iso(),
    }


def test_evidence_for_report(evidence: Mapping[str, Any]) -> dict[str, Any]:
    status = str(evidence.get("status") or "not_run")
    lines = []
    for item in evidence.get("commands") or []:
        argv = " ".join(str(part) for part in item.get("argv") or [])
        lines.append(
            f"- `{argv}` → status `{item.get('status')}` exit `{item.get('exit_code')}` "
            f"duration `{item.get('duration_seconds')}s`"
        )
        stderr = str(item.get("stderr") or "").strip()
        if stderr and item.get("status") != "passed":
            lines.append("  - stderr: " + sanitize_error(stderr, 600).replace("\n", " "))
    if not lines:
        lines.append("No trusted test command was executed.")
    lines.append(f"- Bounded test timeout: `{evidence.get('timeout_seconds')}s`")
    return {"status": status, "details": "\n".join(lines)}
