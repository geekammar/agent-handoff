#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
DEFAULT_HOME = Path(os.environ.get("AGENT_HANDOFF_HOME", "/root/agent-handoff"))
MANAGED = ["bin", "lib", "integrations", "setup", "tests", "docs", "README.md"]
SNAPSHOT_IGNORE = shutil.ignore_patterns(
    "__pycache__",
    "*.pyc",
    ".pytest_cache",
    "acceptance-results.json",
    "live-config.json",
    "*.backup-agent-handoff-*",
)


def log(message: str) -> None:
    print(f"[upgrade] {message}")


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def releases_root(home: Path) -> Path:
    root = home / "releases"
    root.mkdir(parents=True, exist_ok=True)
    return root


def source_commit(source: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def snapshot(home: Path, label: str) -> Path | None:
    existing = [item for item in MANAGED if (home / item).exists()]
    if not existing:
        log("no installed code to snapshot")
        return None
    target = releases_root(home) / f"{timestamp()}-{label}"
    target.mkdir(parents=True, exist_ok=False)
    code = target / "code"
    code.mkdir()
    for item in existing:
        source = home / item
        destination = code / item
        if source.is_dir():
            shutil.copytree(source, destination, ignore=SNAPSHOT_IGNORE)
        else:
            shutil.copy2(source, destination)
    metadata = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "label": label,
        "source": str(SOURCE),
        "source_commit": source_commit(SOURCE),
        "home": str(home),
        "items": existing,
    }
    (target / "release.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log(f"snapshot: {target}")
    return target


def restore(home: Path, release: Path) -> None:
    code = release / "code"
    if not code.is_dir():
        raise SystemExit(f"invalid release snapshot: {release}")
    for item in MANAGED:
        source = code / item
        if not source.exists():
            continue
        destination = home / item
        if source.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            for path in source.rglob("*"):
                if path.is_file():
                    relative = path.relative_to(source)
                    target = destination / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
        else:
            shutil.copy2(source, destination)
    log(f"restored code from: {release}")


def install(home: Path, extra: list[str] | None = None, use_source: bool = True) -> int:
    installer = SOURCE / "setup" / "install.py" if use_source else home / "setup" / "install.py"
    if not installer.exists():
        installer = home / "setup" / "install.py" if use_source else SOURCE / "setup" / "install.py"
    command = [sys.executable, str(installer), "--home", str(home), *(extra or [])]
    result = subprocess.run(command, check=False)
    return result.returncode


def list_releases(home: Path) -> int:
    root = releases_root(home)
    for path in sorted(root.iterdir()):
        if not path.is_dir():
            continue
        metadata = {}
        try:
            metadata = json.loads((path / "release.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        log(f"{path.name}  commit={metadata.get('source_commit') or 'unknown'}")
    return 0


def latest_release(home: Path, skip: Path | None = None) -> Path | None:
    candidates = [path for path in releases_root(home).iterdir() if path.is_dir() and path != skip and (path / "code").is_dir()]
    return max(candidates, key=lambda path: path.name) if candidates else None


def upgrade(home: Path, no_snapshot: bool, extra: list[str] | None = None) -> int:
    previous = None if no_snapshot else snapshot(home, "pre-upgrade")
    log(f"installing from: {SOURCE}")
    code = install(home, extra, use_source=True)
    if code != 0:
        log(f"install failed with exit {code}")
        if previous:
            log("automatic rollback started")
            restore(home, previous)
            restore_code = install(home, extra, use_source=False)
            if restore_code != 0:
                log(f"rollback install failed with exit {restore_code}")
            else:
                log("rollback complete")
        return code
    log("upgrade complete")
    return 0


def rollback(home: Path, release: str | None, extra: list[str] | None = None) -> int:
    if release:
        target = releases_root(home) / release
    else:
        target = latest_release(home)
    if not target or not target.is_dir():
        raise SystemExit("no release snapshot is available for rollback")
    snapshot(home, "pre-rollback")
    restore(home, target)
    code = install(home, extra, use_source=False)
    if code != 0:
        log(f"rollback install failed with exit {code}")
        return code
    log(f"rollback complete: {target.name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Upgrade or roll back Agent Handoff while preserving runtime data.")
    parser.add_argument("--home", default=str(DEFAULT_HOME), help="runtime installation directory")
    parser.add_argument("--rollback", nargs="?", const="latest", help="roll back to the latest or named release")
    parser.add_argument("--list", action="store_true", help="list release snapshots")
    parser.add_argument("--no-snapshot", action="store_true", help="skip the pre-upgrade snapshot")
    parser.add_argument("--no-opencode", action="store_true", help="do not modify OpenCode configuration")
    parser.add_argument("--no-agy", action="store_true", help="do not modify AGY configuration")
    parser.add_argument("--bin-link", default=None, help="PATH symlink location")
    args = parser.parse_args(argv)
    home = Path(args.home).expanduser().resolve()
    extra = []
    if args.no_opencode:
        extra.append("--no-opencode")
    if args.no_agy:
        extra.append("--no-agy")
    if args.bin_link:
        extra.extend(["--bin-link", args.bin_link])
    if args.list:
        return list_releases(home)
    if args.rollback:
        return rollback(home, None if args.rollback == "latest" else args.rollback, extra)
    return upgrade(home, args.no_snapshot, extra)


if __name__ == "__main__":
    raise SystemExit(main())
