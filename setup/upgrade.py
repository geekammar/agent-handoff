#!/usr/bin/env python3
from __future__ import annotations

import argparse
import filecmp
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SETUP_DIR = Path(__file__).resolve().parent
if str(SETUP_DIR) not in sys.path:
    sys.path.insert(0, str(SETUP_DIR))

from install import capture_external_state, external_paths, restore_external_state  # noqa: E402
from managed import (  # noqa: E402
    MANIFEST_NAME,
    ManifestError,
    load_manifest,
    safe_target,
    source_commit,
    walk_files,
    write_manifest,
)
from provenance import (  # noqa: E402
    PROVENANCE_UNKNOWN,
    PROVENANCE_VERIFIED,
    managed_content_digest,
    trusted_manifest_source,
    trusted_release_source,
)

SOURCE = Path(__file__).resolve().parents[1]
DEFAULT_HOME = Path(os.environ.get("AGENT_HANDOFF_HOME", str(Path.home() / "agent-handoff")))


def log(message: str) -> None:
    print(f"[upgrade] {message}")


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def releases_root(home: Path) -> Path:
    root = home / "releases"
    root.mkdir(parents=True, exist_ok=True)
    return root


def unique_release(target: Path) -> Path:
    if not target.exists():
        return target
    counter = 2
    while True:
        candidate = target.with_name(f"{target.name}-{counter}")
        if not candidate.exists():
            return candidate
        counter += 1


def atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    shutil.copy2(source, temporary)
    os.replace(temporary, target)


def snapshot(home: Path, label: str) -> Path | None:
    present = walk_files(home)
    if not present:
        log("no installed code to snapshot")
        return None
    runtime_manifest = None
    try:
        runtime_manifest = load_manifest(home / MANIFEST_NAME, required=False)
    except ManifestError as exc:
        log(f"warning: ignoring previous ownership manifest: {exc}")
    if runtime_manifest:
        present_paths = {item.as_posix() for item in present}
        files = [Path(item) for item in runtime_manifest["files"] if item in present_paths]
        origin = "installer-manifest"
    else:
        files = present
        origin = "snapshot-inventory"
    if not files:
        log("no installed managed files to snapshot")
        return None
    file_list = [item.as_posix() for item in files]
    try:
        home_digest = managed_content_digest(home, file_list)
    except (OSError, ValueError):
        home_digest = None
    trusted = trusted_manifest_source(runtime_manifest, home, content_sha256=home_digest)
    installed_provenance = trusted.get("provenance") or PROVENANCE_UNKNOWN
    installed_commit = trusted.get("commit") if installed_provenance == PROVENANCE_VERIFIED else None
    installed_repository = trusted.get("repository")
    installed_reason = trusted.get("reason")
    installed_commit_source = None
    if installed_provenance == PROVENANCE_VERIFIED and runtime_manifest:
        installed_commit_source = runtime_manifest.get("source_commit_source")
    target = unique_release(releases_root(home) / f"{timestamp()}-{label}")
    target.mkdir(parents=True, exist_ok=False)
    code = target / "code"
    code.mkdir()
    for relative in files:
        destination = code / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(home / relative, destination)
    try:
        snapshot_digest = managed_content_digest(code, file_list)
    except (OSError, ValueError):
        snapshot_digest = None
    if home_digest is None or snapshot_digest is None or snapshot_digest != home_digest:
        installed_provenance = PROVENANCE_UNKNOWN
        installed_commit = None
        installed_repository = None
        installed_reason = "snapshot_copy_mismatch"
        installed_commit_source = None
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    upgrade_source_commit = source_commit(SOURCE)
    write_manifest(
        target / "manifest.json",
        file_list,
        {
            "created_at": created_at,
            "label": label,
            "source": str(SOURCE),
            "source_commit": upgrade_source_commit,
            "upgrade_source_commit": upgrade_source_commit,
            "installed_source_commit": installed_commit,
            "installed_source_provenance": installed_provenance,
            "installed_source_repository": installed_repository,
            "installed_source_commit_reason": installed_reason,
            "installed_source_commit_source": installed_commit_source,
            "installed_content_sha256": snapshot_digest,
            "home": str(home),
            "origin": origin,
        },
    )
    metadata = {
        "created_at": created_at,
        "label": label,
        "source": str(SOURCE),
        "source_commit": upgrade_source_commit,
        "upgrade_source_commit": upgrade_source_commit,
        "installed_source_commit": installed_commit,
        "installed_source_provenance": installed_provenance,
        "installed_source_repository": installed_repository,
        "installed_source_commit_reason": installed_reason,
        "installed_source_commit_source": installed_commit_source,
        "installed_content_sha256": snapshot_digest,
        "home": str(home),
        "items": sorted({item.parts[0] for item in files}),
        "files": len(files),
        "manifest": "manifest.json",
    }
    (target / "release.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log(f"snapshot: {target}")
    return target


def restore(home: Path, release: Path) -> None:
    code = release / "code"
    if not code.is_dir():
        raise SystemExit(f"invalid release snapshot: {release}")
    try:
        release_manifest = load_manifest(release / "manifest.json", required=True)
    except ManifestError as exc:
        raise SystemExit(
            f"cannot safely restore {release.name}: {exc}. "
            "Legacy snapshots without an ownership manifest are not restored because newer managed files "
            "cannot be distinguished from unmanaged files. Create a new snapshot with a version that "
            "records ownership before rolling back."
        )
    release_files = set(release_manifest["files"])
    required = {"setup/install.py", "bin/agent-handoff"}
    missing = sorted(required - release_files)
    if missing:
        raise SystemExit(
            f"invalid release snapshot {release.name}: missing required managed file(s): {', '.join(missing)}"
        )
    try:
        current_manifest = load_manifest(home / MANIFEST_NAME, required=False)
    except ManifestError as exc:
        raise SystemExit(f"cannot safely restore {release.name}: invalid current ownership manifest: {exc}")
    if current_manifest is None:
        raise SystemExit(
            f"cannot safely restore {release.name}: the current installation has no ownership manifest, "
            "so newer managed files cannot be identified safely."
        )
    current_files = set(current_manifest["files"])
    for relative in sorted(release_files):
        source = code / relative
        if source.is_symlink() or not source.is_file():
            raise SystemExit(f"invalid release snapshot {release.name}: missing regular file {relative}")
        if safe_target(home, relative) is None:
            raise SystemExit(f"refusing to restore unsafe managed path: {relative}")
    newer = sorted(current_files - release_files)
    for relative in newer:
        if safe_target(home, relative) is None:
            raise SystemExit(f"refusing to remove unsafe managed path: {relative}")
    removed = 0
    for relative in newer:
        target = safe_target(home, relative)
        assert target is not None
        if target.is_symlink() or (target.exists() and target.is_file()):
            target.unlink()
            removed += 1
            log(f"removed newer managed file: {relative}")
        elif target.exists():
            log(f"warning: not removing non-file managed path: {relative}")
    restored = 0
    for relative in sorted(release_files):
        source = code / relative
        target = safe_target(home, relative)
        assert target is not None
        if target.exists() and target.is_file() and not target.is_symlink() and filecmp.cmp(source, target, shallow=False):
            continue
        atomic_copy(source, target)
        restored += 1
    restored_files = sorted(release_files)
    try:
        restored_digest = managed_content_digest(home, restored_files)
    except (OSError, ValueError):
        restored_digest = None
    trusted = trusted_release_source(
        release_manifest,
        home,
        restored_files,
        content_sha256=restored_digest,
    )
    installed_provenance = trusted.get("provenance") or PROVENANCE_UNKNOWN
    write_manifest(
        home / MANIFEST_NAME,
        restored_files,
        {
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "state": "restored",
            "release": release.name,
            "source_commit": trusted.get("commit") if installed_provenance == PROVENANCE_VERIFIED else None,
            "source_provenance": installed_provenance,
            "source_repository": trusted.get("repository") if installed_provenance == PROVENANCE_VERIFIED else None,
            "source_commit_reason": trusted.get("reason"),
            "source_commit_source": "restore",
            "content_sha256": restored_digest,
        },
    )
    log(f"restored code from: {release} ({restored} file(s) restored, {removed} newer file(s) removed)")


def install(home: Path, extra: list[str] | None = None, use_source: bool = True) -> int:
    if use_source:
        installer = SOURCE / "setup" / "install.py"
        if not installer.exists():
            installer = home / "setup" / "install.py"
    else:
        installer = home / "setup" / "install.py"
        if not installer.exists():
            log(f"missing restored installer: {installer}")
            return 1
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
        log(
            f"{path.name}  installed={metadata.get('installed_source_commit') or 'unknown'} "
            f"upgrade_source={metadata.get('source_commit') or 'unknown'}"
        )
    return 0


def latest_release(home: Path, skip: Path | None = None) -> Path | None:
    candidates = [path for path in releases_root(home).iterdir() if path.is_dir() and path != skip and (path / "code").is_dir()]
    return max(candidates, key=lambda path: path.name) if candidates else None


def integration_state(extra: list[str] | None) -> dict[Path, tuple[str, bytes | str | None, int | None]]:
    values = extra or []
    return capture_external_state(
        external_paths("--no-opencode" not in values, "--no-agy" not in values)
    )


def upgrade(home: Path, no_snapshot: bool, extra: list[str] | None = None) -> int:
    external_state = integration_state(extra)
    previous = None if no_snapshot else snapshot(home, "pre-upgrade")
    log(f"installing from: {SOURCE}")
    code = install(home, extra, use_source=True)
    if code != 0:
        log(f"install failed with exit {code}")
        restore_external_state(external_state)
        if previous:
            log("automatic rollback started")
            try:
                restore(home, previous)
            except SystemExit as exc:
                log(f"automatic rollback refused: {exc}")
                return code
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
    external_state = integration_state(extra)
    previous = snapshot(home, "pre-rollback")
    restore(home, target)
    code = install(home, extra, use_source=False)
    if code != 0:
        log(f"rollback install failed with exit {code}")
        restore_external_state(external_state)
        if previous:
            log("restoring the pre-rollback state")
            try:
                restore(home, previous)
            except SystemExit as exc:
                log(f"rollback recovery refused: {exc}")
                return code
            recovery = install(home, extra, use_source=False)
            if recovery != 0:
                log(f"rollback recovery install failed with exit {recovery}")
            else:
                log("pre-rollback state restored")
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
