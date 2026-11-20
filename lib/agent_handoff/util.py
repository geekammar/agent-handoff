from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import signal
import stat
import subprocess
import tempfile
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().isoformat(timespec="seconds").replace("+00:00", "Z")


def utc_from_timestamp(value: float | int | None) -> str:
    if value is None:
        return utcnow_iso()
    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_id(prefix: str = "") -> str:
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    token = secrets.token_hex(4)
    return f"{prefix}{stamp}-{token}" if prefix else f"{stamp}-{token}"


def slugify(value: str, max_length: int = 48) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = value.encode("ascii", "ignore").decode("ascii")
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    if not value:
        value = "task"
    return value[:max_length].rstrip("-") or "task"


def safe_title(value: str, max_length: int = 160) -> str:
    value = re.sub(r"[\r\n\t]+", " ", value or "").strip()
    value = re.sub(r"\s{2,}", " ", value)
    if len(value) > max_length:
        value = value[: max_length - 1].rstrip() + "…"
    return value or "Untitled task"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def sha256_file(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def md5_file(path: Path | str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_dir(path: Path | str, mode: int = 0o700) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p, mode)
    except OSError:
        pass
    return p


def atomic_write_text(path: Path | str, text: str, mode: int = 0o600) -> Path:
    target = Path(path)
    ensure_dir(target.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return target


def atomic_write_json(path: Path | str, data: Any, mode: int = 0o600) -> Path:
    return atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n", mode=mode)


def read_json(path: Path | str, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def read_text(path: Path | str, default: str = "") -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return default


def read_text_limited(path: Path | str, limit: int = 200_000) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return ""


class FileLock:
    def __init__(self, path: Path | str, blocking: bool = True, timeout: float = 60.0):
        self.path = Path(path)
        self.blocking = blocking
        self.timeout = timeout
        self.handle = None

    def acquire(self) -> bool:
        ensure_dir(self.path.parent)
        self.handle = open(self.path, "a+", encoding="utf-8")
        os.chmod(self.path, 0o600)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if not self.blocking:
                    self.handle.close()
                    self.handle = None
                    return False
                if time.monotonic() >= deadline:
                    self.handle.close()
                    self.handle = None
                    raise TimeoutError(f"timeout waiting for lock {self.path}")
                time.sleep(0.05)

    def release(self) -> None:
        if self.handle is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def run_command(
    args: list[str],
    timeout: float = 120.0,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    try:
        return subprocess.run(
            args,
            cwd=cwd,
            env=merged_env,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            args=args,
            returncode=124,
            stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
            stderr=f"timeout after {timeout}s",
        )
    except OSError as exc:
        return subprocess.CompletedProcess(args=args, returncode=127, stdout="", stderr=str(exc))


SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("private-key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.S)),
    ("aws-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("bearer", re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{20,}")),
    ("url-credentials", re.compile(r"(?i)\b(https?://)([^/\s:@]+):([^/\s@]+)@")),
    ("db-credentials", re.compile(r"(?i)\b(postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://([^/\s:@]+):([^/\s@]+)@")),
    (
        "secret-chain",
        re.compile(
            r"(?im)^([ \t]*(?:[A-Za-z0-9_.-]+[ \t]*[:=][ \t]*)+)(['\"]?)([^\s'\"]{6,})\2(?=\s|$|[,;])"
        ),
    ),
    (
        "secret-assignment",
        re.compile(
            r"(?im)^(\s*[A-Za-z0-9_.-]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API[_-]?KEY|PRIVATE[_-]?KEY|CLIENT[_-]?SECRET)[A-Za-z0-9_.-]*\s*[:=]\s*)(?:['\"]([^'\"]{6,})['\"]|([^\s'\"]{6,}))"
        ),
    ),
    (
        "json-secret",
        re.compile(
            r"(?i)(\"(?:access_token|refresh_token|client_secret|client_id|api_key|apikey|password|token)\"\s*:\s*\")([^\"]{8,})(\")"
        ),
    ),
    ("rclone-token-line", re.compile(r"(?im)^(token\s*=\s*).+$")),
    (
        "secret-inline",
        re.compile(
            r"(?i)\b(client[_-]?secret|api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|password|passwd|secret)\b(\s*[:=]\s*)(?:['\"]([^'\"]{6,})['\"]|([A-Za-z0-9_./+=\-]{6,}))"
        ),
    ),
    (
        "post-redacted-assignment",
        re.compile(r"(?i)(\[REDACTED\]\s*[:=]\s*)(?:['\"]([^'\"]{6,})['\"]|([A-Za-z0-9_./+=\-]{6,}))"),
    ),
]


def redact_text(value: str) -> tuple[str, int]:
    if not value:
        return value, 0
    count = 0
    result = value

    for name, pattern in SECRET_PATTERNS:
        def replace(match: re.Match, name: str = name) -> str:
            nonlocal count
            count += 1
            if name == "json-secret":
                return f"{match.group(1)}[REDACTED]{match.group(3)}"
            if name == "secret-chain":
                prefix = match.group(1)
                if not re.search(
                    r"(?i)(SECRET|TOKEN|PASSWORD|PASSWD|API[_-]?KEY|PRIVATE[_-]?KEY|CLIENT[_-]?SECRET)",
                    prefix,
                ):
                    count -= 1
                    return match.group(0)
                quote = match.group(2) or ""
                return f"{prefix}{quote}[REDACTED]{quote}"
            if name == "secret-assignment":
                value = match.group(2) or match.group(3) or ""
                tail = match.string[match.end() :]
                key_like = re.fullmatch(
                    r"(?i)[A-Za-z0-9_.-]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API[_-]?KEY|PRIVATE[_-]?KEY|CLIENT[_-]?SECRET)[A-Za-z0-9_.-]*",
                    value,
                )
                if key_like and tail.lstrip().startswith(("=", ":")):
                    count -= 1
                    return match.group(0)
                return f"{match.group(1)}[REDACTED]"
            if name == "rclone-token-line":
                return f"{match.group(1)}[REDACTED]"
            if name == "bearer":
                return f"{match.group(1)}[REDACTED]"
            if name == "url-credentials":
                return f"{match.group(1)}[REDACTED]:[REDACTED]@"
            if name == "db-credentials":
                return f"{match.group(1)}://[REDACTED]:[REDACTED]@"
            if name == "secret-inline":
                value = match.group(3) or match.group(4) or ""
                tail = match.string[match.end() :]
                key_like = re.fullmatch(
                    r"(?i)[A-Za-z0-9_.-]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API[_-]?KEY|PRIVATE[_-]?KEY|CLIENT[_-]?SECRET)[A-Za-z0-9_.-]*",
                    value,
                )
                if key_like and tail.lstrip().startswith(("=", ":")):
                    count -= 1
                    return match.group(0)
                return f"{match.group(1)}{match.group(2)}[REDACTED]"
            if name == "post-redacted-assignment":
                return f"{match.group(1)}[REDACTED]"
            return "[REDACTED]"

        result = pattern.sub(replace, result)
    return result, count


def sanitize_error(value: str, limit: int = 1200) -> str:
    text, _ = redact_text(value or "")
    text = text.strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def is_within(path: Path | str, root: Path | str) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def file_size(path: Path | str) -> int:
    try:
        return os.stat(path).st_size
    except OSError:
        return -1


def chmod_private(path: Path | str) -> None:
    with contextlib.suppress(OSError):
        mode = os.stat(path).st_mode
        if stat.S_ISDIR(mode):
            os.chmod(path, 0o700)
        else:
            os.chmod(path, 0o600)


def json_dumps(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False)


def truncate(value: str, limit: int = 4000) -> str:
    value = value or ""
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "\n\n[truncated]"


def strip_wrappers(value: str) -> str:
    value = value or ""
    value = re.sub(r"^\s*<USER_REQUEST>\s*", "", value, flags=re.I)
    value = re.sub(r"\n?<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", "", value, flags=re.S | re.I)
    value = re.sub(r"\n?<USER_SETTINGS_CHANGE>.*?</USER_SETTINGS_CHANGE>", "", value, flags=re.S | re.I)
    value = re.sub(r"\s*</USER_REQUEST>\s*$", "", value, flags=re.I)
    value = re.sub(r"\s*</USER_REQUEST>", "", value, flags=re.I)
    return value.strip()


def first_line(value: str, limit: int = 120) -> str:
    line = (value or "").strip().splitlines()[0] if (value or "").strip() else ""
    return safe_title(line, max_length=limit)


@contextlib.contextmanager
def temporary_umask(mask: int = 0o077) -> Iterator[None]:
    old = os.umask(mask)
    try:
        yield
    finally:
        os.umask(old)


@contextlib.contextmanager
def graceful_sigterm(exit_code: int = 143) -> Iterator[None]:
    """Turn SIGTERM into orderly unwinding so child processes are cleaned up."""

    if not hasattr(signal, "SIGTERM") or threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGTERM)

    def stop(signum: int, frame: Any) -> None:
        raise SystemExit(exit_code)

    signal.signal(signal.SIGTERM, stop)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)
