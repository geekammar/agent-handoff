from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .config import all_projects, load_config, project_by_name
from .store import Store
from .util import (
    ensure_dir,
    is_within,
    md5_file,
    read_json,
    read_text_limited,
    redact_text,
    sanitize_error,
    utcnow,
    utcnow_iso,
)

REMOTE_TIMEOUT = 120.0
HEARTBEAT_SECONDS = 5.0


def _notify(callback: Callable[[str, str], None] | None, stage: str, message: str) -> None:
    if callback is None:
        return
    try:
        callback(stage, message)
    except Exception:
        pass


def _md5_hex(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{32}", (value or "").strip().lower()))


class Uploader:
    def __init__(self, config: dict[str, Any], store: Store):
        self.config = config
        self.store = store
        self.remote_name = (config.get("drive") or {}).get("remote", "gdrive")
        self._listing_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._project_verify_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._mkdir_cache: set[str] = set()

    def rclone(self) -> str | None:
        return shutil.which("rclone")

    def config_file(self) -> Path:
        env = os.environ.get("RCLONE_CONFIG")
        if env:
            return Path(env).expanduser()
        return Path.home() / ".config" / "rclone" / "rclone.conf"

    def rclone_remotes(self) -> tuple[bool, list[str], str | None]:
        binary = self.rclone()
        if not binary:
            return False, [], "rclone is not installed"
        result = subprocess.run([binary, "listremotes"], capture_output=True, text=True, timeout=30, check=False)
        if result.returncode != 0:
            return False, [], sanitize_error(result.stderr or result.stdout)
        remotes = [line.strip().rstrip(":") for line in result.stdout.splitlines() if line.strip()]
        return True, remotes, None

    def remote_ready(self) -> tuple[bool, str | None]:
        if not (self.config.get("upload") or {}).get("enabled", True):
            return False, "upload is disabled in config"
        binary = self.rclone()
        if not binary:
            return False, "rclone is not installed"
        if not self.config_file().exists():
            return False, f"rclone config not found at {self.config_file()}"
        ok, remotes, error = self.rclone_remotes()
        if not ok:
            return False, error
        if self.remote_name not in remotes:
            return False, f"rclone remote '{self.remote_name}' is not configured"
        return True, None

    def remote_spec(self, folder_id: str, rel: str = "") -> str:
        folder_id = (folder_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{10,}", folder_id):
            raise ValueError("invalid Google Drive folder id")
        rel = (rel or "").strip().lstrip("/")
        if rel.startswith("..") or "/../" in rel or rel.endswith("/.."):
            raise ValueError("remote path escapes project folder")
        return f"{self.remote_name},root_folder_id={folder_id}:{rel}"

    def _run(
        self,
        args: list[str],
        timeout: float = REMOTE_TIMEOUT,
        *,
        on_progress: Callable[[str, str], None] | None = None,
        heartbeat_seconds: float | None = None,
        action: str | None = None,
    ) -> subprocess.CompletedProcess:
        binary = self.rclone()
        if not binary:
            return subprocess.CompletedProcess(args=["rclone", *args], returncode=127, stdout="", stderr="rclone is not installed")
        command = [binary, *args]
        if on_progress is not None and heartbeat_seconds and heartbeat_seconds > 0:
            return self._run_with_heartbeat(command, timeout, on_progress, heartbeat_seconds, action or "the rclone operation")
        try:
            return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(args=command, returncode=124, stdout="", stderr=f"timeout after {timeout}s")
        except OSError as exc:
            return subprocess.CompletedProcess(args=command, returncode=127, stdout="", stderr=str(exc))

    def _run_with_heartbeat(
        self,
        command: list[str],
        timeout: float,
        on_progress: Callable[[str, str], None],
        heartbeat_seconds: float,
        action: str,
    ) -> subprocess.CompletedProcess:
        started = time.monotonic()
        deadline = started + timeout
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            return subprocess.CompletedProcess(args=command, returncode=127, stdout="", stderr=str(exc))
        next_beat = started + heartbeat_seconds
        try:
            while True:
                now = time.monotonic()
                remaining = deadline - now
                if remaining <= 0:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except OSError:
                        process.kill()
                    try:
                        process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                    return subprocess.CompletedProcess(args=command, returncode=124, stdout="", stderr=f"timeout after {timeout}s")
                wait = min(remaining, max(0.05, next_beat - now))
                try:
                    stdout, stderr = process.communicate(timeout=wait)
                    return subprocess.CompletedProcess(args=command, returncode=process.returncode, stdout=stdout, stderr=stderr)
                except subprocess.TimeoutExpired:
                    now = time.monotonic()
                    if now >= next_beat:
                        _notify(
                            on_progress,
                            "waiting",
                            f"{action} is still in progress after {now - started:.0f}s "
                            f"(bounded timeout {timeout:g}s; no ETA)",
                        )
                        next_beat = now + heartbeat_seconds
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    process.kill()
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    pass

    def check_connection(self, timeout: float = 90.0) -> dict[str, Any]:
        ready, error = self.remote_ready()
        if not ready:
            return {"ready": False, "connected": False, "error": error}
        root_id = (self.config.get("drive") or {}).get("root_folder_id")
        if root_id:
            listing = self.list_folder_ids(root_id, timeout=timeout)
            connected = bool(listing.get("ok"))
            error = listing.get("error")
        else:
            result = self._run(["lsd", f"{self.remote_name}:", "--max-depth", "1"], timeout=timeout)
            connected = result.returncode == 0
            error = None if connected else sanitize_error(result.stderr or result.stdout)
        return {
            "ready": True,
            "connected": connected,
            "error": None if connected else sanitize_error(error or "not reachable"),
            "remote": self.remote_name,
            "config": str(self.config_file()),
        }

    def list_folder_ids(
        self,
        folder_id: str,
        timeout: float = 90.0,
        on_progress: Callable[[str, str], None] | None = None,
    ) -> dict[str, Any]:
        cached = self._listing_cache.get(folder_id)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        result = self._run(
            ["lsf", self.remote_spec(folder_id), "--dirs-only", "--format", "pi"],
            timeout=timeout,
            on_progress=on_progress,
            heartbeat_seconds=HEARTBEAT_SECONDS,
            action="rclone Drive folder listing",
        )
        if result.returncode != 0:
            return {"ok": False, "error": sanitize_error(result.stderr or result.stdout), "items": {}}
        items: dict[str, str] = {}
        duplicates: dict[str, list[str]] = {}
        for raw in result.stdout.splitlines():
            line = raw.rstrip()
            if not line:
                continue
            if ";" in line:
                name, item_id = line.split(";", 1)
            else:
                name, item_id = line, ""
            name = name.rstrip("/").strip()
            item_id = item_id.strip()
            if not name:
                continue
            if name in items and items[name] != item_id:
                duplicates.setdefault(name, [items[name]]).append(item_id)
            items[name] = item_id
        value = {"ok": True, "items": items, "duplicates": duplicates}
        self._listing_cache[folder_id] = (time.monotonic() + 300, value)
        return value

    def verify_hierarchy(self, timeout: float = 90.0) -> dict[str, Any]:
        drive = self.config.get("drive") or {}
        parent_id = drive.get("parent_folder_id")
        root_id = drive.get("root_folder_id")
        parent_name = drive.get("parent_folder_name")
        root_name = drive.get("root_folder_name")
        if not parent_id or not root_id:
            return {"ok": False, "error": "drive parent/root folder IDs are not configured"}
        parent = self.list_folder_ids(parent_id, timeout=timeout)
        if not parent.get("ok"):
            return {"ok": False, "error": f"cannot list parent folder: {parent.get('error')}", "step": "parent"}
        if parent["items"].get(root_name) != root_id:
            return {
                "ok": False,
                "error": f"root folder '{root_name}' was not found with the configured ID under the parent folder",
                "step": "root",
                "found": parent["items"].get(root_name),
                "expected": root_id,
            }
        root = self.list_folder_ids(root_id, timeout=timeout)
        if not root.get("ok"):
            return {"ok": False, "error": f"cannot list 03 Agent Execution Reports: {root.get('error')}", "step": "root-list"}
        projects = {}
        for project in all_projects(self.config):
            folder_id = project.get("drive_folder_id")
            if not folder_id:
                projects[project["key"]] = {"configured": False, "verified": False}
                continue
            actual = root["items"].get(project["name"])
            projects[project["key"]] = {
                "configured": True,
                "verified": actual == folder_id,
                "expected": folder_id,
                "found": actual,
                "name": project["name"],
            }
        return {"ok": True, "projects": projects, "root_items": root["items"]}

    def verify_project(
        self,
        project: dict[str, Any],
        create: bool = False,
        timeout: float = 90.0,
        on_progress: Callable[[str, str], None] | None = None,
    ) -> dict[str, Any]:
        cache_key = f"{project.get('key')}:{project.get('drive_folder_id')}"
        if not create:
            cached = self._project_verify_cache.get(cache_key)
            if cached and cached[0] > time.monotonic():
                return dict(cached[1])
        drive = self.config.get("drive") or {}
        root_id = drive.get("root_folder_id")
        if not root_id:
            return {"ok": False, "error": "drive root folder id is not configured"}
        folder_id = project.get("drive_folder_id")
        if not folder_id and not create:
            return {"ok": False, "error": f"project '{project['name']}' has no Drive folder id; run: agent-handoff project add --name \"{project['name']}\""}
        listing = self.list_folder_ids(root_id, timeout=timeout, on_progress=on_progress)
        if not listing.get("ok"):
            return {"ok": False, "error": f"cannot list 03 Agent Execution Reports: {listing.get('error')}"}
        items = listing["items"]
        if folder_id:
            if items.get(project["name"]) != folder_id:
                return {
                    "ok": False,
                    "error": f"folder id for '{project['name']}' is not a direct child of 03 Agent Execution Reports (expected {folder_id}, found {items.get(project['name'])})",
                }
            value = {"ok": True, "folder_id": folder_id, "created": False}
            self._project_verify_cache[cache_key] = (time.monotonic() + 600, value)
            return dict(value)
        if not create:
            return {"ok": False, "error": "no folder id"}
        matches = [item_id for name, item_id in items.items() if name == project["name"]]
        if len(matches) > 1:
            return {"ok": False, "error": f"multiple folders named '{project['name']}' exist; provide --folder-id explicitly"}
        if len(matches) == 1:
            folder_id = matches[0]
            if not folder_id:
                return {"ok": False, "error": "rclone did not return the folder id; provide --folder-id explicitly"}
            self._persist_project_folder(project, folder_id)
            return {"ok": True, "folder_id": folder_id, "created": False, "reused": True}
        result = self._run(["mkdir", self.remote_spec(root_id, project["name"])], timeout=timeout)
        if result.returncode != 0:
            return {"ok": False, "error": sanitize_error(result.stderr or result.stdout)}
        self._listing_cache.pop(root_id, None)
        listing = self.list_folder_ids(root_id, timeout=timeout, on_progress=on_progress)
        if not listing.get("ok"):
            return {"ok": False, "error": f"folder created but cannot be listed: {listing.get('error')}"}
        folder_id = listing["items"].get(project["name"])
        if not folder_id:
            return {"ok": False, "error": "folder created but its id could not be determined; provide --folder-id explicitly"}
        self._persist_project_folder(project, folder_id)
        return {"ok": True, "folder_id": folder_id, "created": True}

    def _persist_project_folder(self, project: dict[str, Any], folder_id: str) -> None:
        from .config import set_project_drive_id

        set_project_drive_id(self.config, project["key"], folder_id)
        project["drive_folder_id"] = folder_id
        self.config = load_config()

    def _sanitized_local_copy(self, local: Path, max_bytes: int) -> tuple[Path, int, bool]:
        try:
            raw = local.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return local, 0, False
        if not raw:
            return local, 0, False
        if (self.config.get("security") or {}).get("redact_uploads", True):
            redacted, count = redact_text(raw)
            if count:
                fd, tmp_name = tempfile.mkstemp(prefix="upload-", suffix=local.suffix, dir=str(self.store.tmp_root))
                tmp = Path(tmp_name)
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(redacted)
                return tmp, count, True
        return local, 0, False

    def verify_remote_file(
        self,
        remote_rel: str,
        local: Path,
        project: dict[str, Any],
        timeout: float = 60.0,
        on_progress: Callable[[str, str], None] | None = None,
    ) -> dict[str, Any]:
        remote = self.remote_spec(project["drive_folder_id"], remote_rel)
        local_md5 = md5_file(local)
        result = self._run(
            ["md5sum", remote],
            timeout=timeout,
            on_progress=on_progress,
            heartbeat_seconds=HEARTBEAT_SECONDS,
            action="rclone MD5 verification",
        )
        remote_md5 = ""
        if result.returncode == 0:
            first = (result.stdout or "").strip().splitlines()[0] if (result.stdout or "").strip() else ""
            token = first.split()[0] if first else ""
            if _md5_hex(token):
                remote_md5 = token.lower()
        if remote_md5:
            return {
                "verified": remote_md5 == local_md5,
                "method": "md5",
                "local_md5": local_md5,
                "remote_md5": remote_md5,
                "error": None if remote_md5 == local_md5 else "remote md5 does not match local md5",
            }
        fd, tmp_name = tempfile.mkstemp(prefix="verify-", dir=str(self.store.tmp_root))
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            readback = self._run(
                ["copyto", remote, str(tmp)],
                timeout=timeout,
                on_progress=on_progress,
                heartbeat_seconds=HEARTBEAT_SECONDS,
                action="rclone read-back verification",
            )
            if readback.returncode != 0:
                return {
                    "verified": False,
                    "method": "readback",
                    "error": sanitize_error(readback.stderr or readback.stdout),
                }
            readback_md5 = md5_file(tmp)
            return {
                "verified": readback_md5 == local_md5,
                "method": "readback",
                "local_md5": local_md5,
                "remote_md5": readback_md5,
                "error": None if readback_md5 == local_md5 else "read-back md5 does not match local md5",
            }
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    def upload_file(
        self,
        local_path: str | Path,
        remote_rel: str,
        project: dict[str, Any],
        timeout: float = REMOTE_TIMEOUT,
        on_progress: Callable[[str, str], None] | None = None,
    ) -> dict[str, Any]:
        local = Path(local_path)
        if not local.exists() or not local.is_file():
            return {"ok": False, "error": f"local file missing: {local}", "redactions": 0}
        if not is_within(local, self.store.data_root) and not is_within(local, self.store.tmp_root):
            return {"ok": False, "error": f"refusing to upload a file outside the managed data directory: {local}", "redactions": 0}
        remote_rel = (remote_rel or "").strip().lstrip("/")
        if not remote_rel or remote_rel.startswith("..") or "/../" in remote_rel:
            return {"ok": False, "error": f"invalid remote path: {remote_rel!r}", "redactions": 0}
        _notify(on_progress, "uploading", f"transferring {remote_rel} (bounded timeout {timeout:g}s, no total ETA)")
        parent = str(Path(remote_rel).parent)
        mkdir_key = f"{project['drive_folder_id']}:{parent}"
        if parent and parent != "." and mkdir_key not in self._mkdir_cache:
            mkdir = self._run(
                ["mkdir", self.remote_spec(project["drive_folder_id"], parent)],
                timeout=timeout,
                on_progress=on_progress,
                heartbeat_seconds=HEARTBEAT_SECONDS,
                action="rclone remote folder creation",
            )
            if mkdir.returncode != 0:
                return {"ok": False, "error": sanitize_error(mkdir.stderr or mkdir.stdout), "redactions": 0}
            self._mkdir_cache.add(mkdir_key)
        max_bytes = int((self.config.get("security") or {}).get("max_prompt_bytes", 200000))
        sanitized, redactions, is_temp = self._sanitized_local_copy(local, max_bytes)
        try:
            transfer = self._run(
                ["copyto", str(sanitized), self.remote_spec(project["drive_folder_id"], remote_rel), "--ignore-times", "--stats", "0"],
                timeout=timeout,
                on_progress=on_progress,
                heartbeat_seconds=HEARTBEAT_SECONDS,
                action="rclone upload transfer",
            )
            if transfer.returncode != 0:
                return {"ok": False, "error": sanitize_error(transfer.stderr or transfer.stdout), "redactions": redactions}
            _notify(on_progress, "verifying", f"verifying {remote_rel} with MD5/read-back (bounded timeout {timeout:g}s)")
            verification = self.verify_remote_file(remote_rel, sanitized, project, timeout=timeout, on_progress=on_progress)
            if not verification.get("verified"):
                return {
                    "ok": False,
                    "error": verification.get("error") or "remote verification failed",
                    "redactions": redactions,
                    "verification": verification,
                }
            return {
                "ok": True,
                "remote": remote_rel,
                "redactions": redactions,
                "verification": verification,
                "size": sanitized.stat().st_size,
            }
        finally:
            if is_temp:
                try:
                    sanitized.unlink()
                except OSError:
                    pass

    def read_remote_text(self, remote_rel: str, project: dict[str, Any], timeout: float = 120.0, limit: int = 200000) -> dict[str, Any]:
        remote = self.remote_spec(project["drive_folder_id"], remote_rel)
        fd, tmp_name = tempfile.mkstemp(prefix="remote-read-", dir=str(self.store.tmp_root))
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            result = self._run(["copyto", remote, str(tmp)], timeout=timeout)
            if result.returncode == 124:
                result = self._run(["copyto", remote, str(tmp)], timeout=timeout)
            if result.returncode != 0:
                return {"ok": False, "error": sanitize_error(result.stderr or result.stdout or "remote read failed")}
            text = read_text_limited(tmp, limit)
            return {"ok": True, "text": text, "size": tmp.stat().st_size, "truncated": tmp.stat().st_size > len(text.encode("utf-8", errors="replace"))}
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    def remote_file_id(
        self,
        remote_rel: str,
        project: dict[str, Any],
        timeout: float = 120.0,
        on_progress: Callable[[str, str], None] | None = None,
    ) -> dict[str, Any]:
        remote = self.remote_spec(project["drive_folder_id"], remote_rel)
        result = self._run(
            ["lsjson", "--stat", remote],
            timeout=timeout,
            on_progress=on_progress,
            heartbeat_seconds=HEARTBEAT_SECONDS,
            action="rclone Drive file id resolution",
        )
        if result.returncode == 124:
            result = self._run(
                ["lsjson", "--stat", remote],
                timeout=timeout,
                on_progress=on_progress,
                heartbeat_seconds=HEARTBEAT_SECONDS,
                action="rclone Drive file id resolution",
            )
        if result.returncode != 0:
            return {"ok": False, "error": sanitize_error(result.stderr or result.stdout or "remote file not found")}
        try:
            item = json.loads(result.stdout or "{}")
        except ValueError as exc:
            return {"ok": False, "error": f"rclone returned invalid JSON: {exc}"}
        if not isinstance(item, dict):
            return {"ok": False, "error": "rclone returned an unexpected remote file record"}
        file_id = str(item.get("ID") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{10,}", file_id):
            return {"ok": False, "error": "rclone did not return a Google Drive file id; cannot publish a direct verified link"}
        return {
            "ok": True,
            "id": file_id,
            "url": f"https://drive.google.com/file/d/{file_id}/view",
            "size": item.get("Size"),
            "mime_type": item.get("MimeType"),
            "mod_time": item.get("ModTime"),
        }

    def _log(self, message: str) -> None:
        path = self.store.logs_root / "upload.log"
        try:
            if path.exists() and path.stat().st_size > 2 * 1024 * 1024:
                path.replace(path.with_suffix(".log.1"))
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(f"{utcnow_iso()} {sanitize_error(message, 4000)}\n")
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _queue_failure(self, item: dict[str, Any], error: str, increment: bool = True) -> None:
        data = item["data"]
        attempts = int(data.get("attempts", 0)) + (1 if increment else 0)
        data["attempts"] = attempts
        data["last_error"] = sanitize_error(error)
        delay = min(3600, 60 * (2 ** min(max(attempts - 1, 0), 6)))
        data["next_attempt_at"] = (utcnow() + timedelta(seconds=delay)).isoformat(timespec="seconds").replace("+00:00", "Z")
        from .util import atomic_write_json

        atomic_write_json(item["path"], data)
        self._log(f"queue {data.get('id')} failed attempt {attempts}: {error}")

    def _mark_run_failure(self, project: dict[str, Any], task_id: str, run_id: str, error: str, attempts: int) -> None:
        try:
            with self.store.lock():
                _, task_path, _, run_path, complete = self.store.run_record(project, task_id, run_id)
                try:
                    previous = max(0, int((complete.get("upload") or {}).get("attempts") or 0))
                except (TypeError, ValueError):
                    previous = 0
                self.store.update_complete(
                    run_path,
                    {
                        "upload": {
                            "status": "pending",
                            "verified": False,
                            "attempts": max(previous + 1, attempts),
                            "last_error": sanitize_error(error),
                        }
                    },
                )
        except Exception:
            pass

    def reconcile_queue(self) -> None:
        for project in all_projects(self.config):
            records = self.store.task_records(project)
            latest_verified = None
            for record in records:
                for run in record["runs"]:
                    complete = run["complete"]
                    upload = complete.get("upload") or {}
                    if upload.get("verified"):
                        verified_at = upload.get("verified_at") or ""
                        if latest_verified is None or verified_at > latest_verified:
                            latest_verified = verified_at
                    elif (complete.get("report") or {}).get("available", True):
                        self.store.queue_run(project, record["path"], record["meta"], run["path"], complete.get("run_id"))
            if latest_verified:
                state = read_json(self.store.index_upload_path(), default={}) or {}
                current = state.get(project.get("key") or project["name"].lower()) or {}
                if not current.get("verified") or (current.get("verified_at") or "") < latest_verified:
                    self.store.queue_index(project)

    def process_queue(
        self,
        force: bool = False,
        only_project: str | None = None,
        only_task: str | None = None,
        only_run: str | None = None,
        time_budget: float = 300.0,
        reconcile: bool = True,
        on_progress: Callable[[str, str], None] | None = None,
    ) -> dict[str, Any]:
        lock = self.store.worker_lock()
        if not lock.acquire():
            return {"ok": True, "busy": True, "processed": 0, "failed": 0, "pending": len(self.store.queue_items())}
        started = time.monotonic()
        summary = {"ok": True, "busy": False, "processed": 0, "failed": 0, "skipped": 0, "pending": 0, "errors": []}
        try:
            self.config = load_config()
            if reconcile:
                self.reconcile_queue()
            ready, reason = self.remote_ready()
            attempted: set[str] = set()
            while True:
                if time.monotonic() - started > time_budget:
                    summary["ok"] = False
                    summary["errors"].append("time budget exceeded; remaining items left in queue")
                    break
                items = self.store.queue_items()
                if only_project:
                    items = [i for i in items if (i["data"].get("project_key") == only_project.lower() or i["data"].get("project", "").lower() == only_project.lower())]
                if only_task:
                    items = [i for i in items if i["data"].get("task_id") == only_task]
                if only_run:
                    items = [i for i in items if i["data"].get("run_id") == only_run]
                if not ready:
                    for item in items:
                        self._queue_failure(item, f"Google Drive unavailable: {reason}")
                    summary["ok"] = False
                    summary["errors"].append(reason or "Google Drive unavailable")
                    break
                eligible = []
                for item in items:
                    data = item["data"]
                    item_id = data.get("id") or str(item["path"])
                    if item_id in attempted:
                        continue
                    next_at = data.get("next_attempt_at")
                    if next_at and not force:
                        try:
                            next_dt = datetime.fromisoformat(next_at.replace("Z", "+00:00"))
                            if next_dt > utcnow():
                                continue
                        except ValueError:
                            pass
                    eligible.append(item)
                if not eligible:
                    break
                item = eligible[0]
                data = item["data"]
                attempted.add(data.get("id") or str(item["path"]))
                project = project_by_name(self.config, data.get("project_key") or data.get("project") or "")
                if not project:
                    self._queue_failure(item, f"project is no longer configured: {data.get('project')}")
                    summary["failed"] += 1
                    continue
                verification = self.verify_project(project, on_progress=on_progress)
                if not verification.get("ok"):
                    prior_attempts = int(data.get("attempts", 0))
                    self._queue_failure(item, verification.get("error") or "project folder verification failed")
                    if data.get("kind") == "run":
                        self._mark_run_failure(project, data.get("task_id"), data.get("run_id"), verification.get("error") or "project folder verification failed", prior_attempts + 1)
                    summary["failed"] += 1
                    summary["ok"] = False
                    summary["errors"].append(verification.get("error") or "project folder verification failed")
                    continue
                if data.get("kind") == "run":
                    result = self._process_run(project, item, on_progress=on_progress)
                elif data.get("kind") == "index":
                    result = self._process_index(project, item)
                else:
                    self._queue_failure(item, f"unknown queue item kind: {data.get('kind')}")
                    summary["failed"] += 1
                    continue
                if result.get("ok"):
                    summary["processed"] += 1
                    self.store.remove_queue_item(item["path"])
                else:
                    prior_attempts = int(data.get("attempts", 0))
                    self._queue_failure(item, result.get("error") or "upload failed")
                    if data.get("kind") == "run":
                        self._mark_run_failure(project, data.get("task_id"), data.get("run_id"), result.get("error") or "upload failed", prior_attempts + 1)
                    summary["failed"] += 1
                    summary["ok"] = False
                    summary["errors"].append(result.get("error") or "upload failed")
            summary["pending"] = len(self.store.queue_items())
            return summary
        finally:
            lock.release()

    def _process_run(
        self,
        project: dict[str, Any],
        item: dict[str, Any],
        *,
        on_progress: Callable[[str, str], None] | None = None,
    ) -> dict[str, Any]:
        data = item["data"]
        task_id = data.get("task_id")
        run_id = data.get("run_id")
        if not task_id or not run_id:
            return {"ok": False, "error": "queue item is missing task_id/run_id"}
        try:
            _, task_path, meta, run_path, complete = self.store.run_record(project, task_id, run_id)
        except Exception as exc:
            return {"ok": False, "error": sanitize_error(str(exc))}
        files = data.get("files") or []
        if not files:
            files = [
                {"local": str(task_path / "PROMPT.md"), "remote": f"TASKS/{meta['task_folder']}/PROMPT.md"},
                {"local": str(task_path / "META.json"), "remote": f"TASKS/{meta['task_folder']}/META.json"},
                {"local": str(run_path / "REPORT.md"), "remote": f"TASKS/{meta['task_folder']}/RUNS/{run_id}/REPORT.md"},
                {"local": str(run_path / "COMPLETE.json"), "remote": f"TASKS/{meta['task_folder']}/RUNS/{run_id}/COMPLETE.json"},
            ]
        verified_files = []
        total_redactions = 0
        for file_item in files:
            result = self.upload_file(file_item["local"], file_item["remote"], project, on_progress=on_progress)
            if not result.get("ok"):
                return {"ok": False, "error": result.get("error") or "upload failed"}
            total_redactions += int(result.get("redactions") or 0)
            verified_files.append({"remote": result.get("remote"), "size": result.get("size"), "verification": result.get("verification", {}).get("method")})
        try:
            queue_attempts = max(0, int(data.get("attempts") or 0))
        except (TypeError, ValueError):
            queue_attempts = 0
        try:
            recorded_attempts = max(0, int((complete.get("upload") or {}).get("attempts") or 0))
        except (TypeError, ValueError):
            recorded_attempts = 0
        attempts = max(queue_attempts, recorded_attempts) + 1
        with self.store.lock():
            self.store.update_complete(
                run_path,
                {
                    "upload": {
                        "status": "verified",
                        "verified": True,
                        "verified_at": utcnow_iso(),
                        "attempts": attempts,
                        "last_error": None,
                        "remote_dir": f"TASKS/{meta['task_folder']}/RUNS/{run_id}",
                        "files": verified_files,
                        "redactions": total_redactions,
                    },
                    "report": {"remote_path": f"TASKS/{meta['task_folder']}/RUNS/{run_id}/REPORT.md"},
                },
            )
            self.store.write_local_index(project)
            self.store.queue_index(project)
        self._log(f"run {task_id}/{run_id} uploaded and verified ({len(verified_files)} files, {total_redactions} redactions)")
        return {"ok": True, "redactions": total_redactions, "files": len(verified_files)}

    def _process_index(self, project: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        with self.store.lock():
            text = self.store.generate_index(project, verified_only=True, include_local_paths=False)
        fd, tmp_name = tempfile.mkstemp(prefix="project-status-", suffix=".md", dir=str(self.store.tmp_root))
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            redacted, count = redact_text(text)
            handle.write(redacted)
        tmp = Path(tmp_name)
        try:
            result = self.upload_file(tmp, "PROJECT-STATUS.md", project)
            if not result.get("ok"):
                return {"ok": False, "error": result.get("error") or "index upload failed"}
            from .util import atomic_write_json

            with self.store.lock():
                state = read_json(self.store.index_upload_path(), default={}) or {}
                state[project.get("key") or project["name"].lower()] = {
                    "verified": True,
                    "verified_at": utcnow_iso(),
                    "remote_path": f"{project['name']}/PROJECT-STATUS.md",
                    "redactions": count,
                }
                atomic_write_json(self.store.index_upload_path(), state)
            self._log(f"project index uploaded and verified for {project['name']} ({count} redactions)")
            return {"ok": True, "redactions": count}
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    def doctor(self) -> dict[str, Any]:
        ready, error = self.remote_ready()
        result: dict[str, Any] = {
            "rclone_found": bool(self.rclone()),
            "rclone_config": str(self.config_file()),
            "rclone_config_exists": self.config_file().exists(),
            "remote_name": self.remote_name,
            "remote_ready": ready,
            "remote_error": error,
            "connection": None,
            "hierarchy": None,
            "queue": len(self.store.queue_items()),
        }
        if ready:
            result["connection"] = self.check_connection()
            if result["connection"].get("connected"):
                result["hierarchy"] = self.verify_hierarchy()
        return result
