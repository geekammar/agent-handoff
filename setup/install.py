#!/usr/bin/env python3
from __future__ import annotations

import argparse
import filecmp
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

SETUP_DIR = Path(__file__).resolve().parent
if str(SETUP_DIR) not in sys.path:
    sys.path.insert(0, str(SETUP_DIR))

from managed import (  # noqa: E402
    MANIFEST_NAME,
    ManifestError,
    load_manifest,
    safe_target,
    source_files,
    write_manifest,
)

SOURCE = Path(__file__).resolve().parents[1]
DEFAULT_HOME = Path(os.environ.get("AGENT_HANDOFF_HOME", str(Path.home() / "agent-handoff")))
DEFAULT_BIN_LINK = Path(os.environ.get("AGENT_HANDOFF_BIN_LINK", str(Path.home() / ".local" / "bin" / "agent-handoff")))
HOME = Path.home()
OPENCODE_DIR = HOME / ".config" / "opencode"
OPENCODE_PLUGIN = OPENCODE_DIR / "plugin" / "agent-handoff.js"
OPENCODE_CONFIG = OPENCODE_DIR / "opencode.json"
AGY_CONFIG = HOME / ".gemini" / "config"
AGY_HOOKS = AGY_CONFIG / "hooks.json"
AGY_RULES = AGY_CONFIG / "AGENTS.md"
MARKER = "<!-- AGENT HANDOFF REPORTING v1.0 -->"
END_MARKER = "<!-- END AGENT HANDOFF REPORTING -->"


def log(message: str) -> None:
    print(f"[install] {message}")


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def backup(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    target = path.with_name(f"{path.name}.backup-agent-handoff-{timestamp()}")
    if path.is_symlink():
        target.symlink_to(os.readlink(path))
    elif path.is_dir():
        shutil.copytree(path, target)
    else:
        shutil.copy2(path, target)
    log(f"backup: {target}")


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    shutil.copy2(source, tmp)
    os.replace(tmp, target)


def atomic_write_bytes(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def sync_code(source: Path, home: Path) -> None:
    if source.resolve() == home.resolve():
        log("source is the installation home; code sync skipped")
        return
    home.mkdir(parents=True, exist_ok=True)
    selected = source_files(source)
    selected_paths = [item.as_posix() for item in selected]
    manifest = home / MANIFEST_NAME
    previous: set[str] = set()
    try:
        previous_manifest = load_manifest(manifest, required=False)
    except ManifestError as exc:
        log(f"warning: ignoring previous ownership manifest: {exc}")
        previous_manifest = None
    if previous_manifest:
        previous = set(previous_manifest["files"])
    write_manifest(manifest, sorted(previous | set(selected_paths)), {"updated_at": timestamp(), "state": "synchronizing"})
    copied = 0
    for relative in selected:
        src = source / relative
        dst = safe_target(home, relative)
        if dst is None:
            raise SystemExit(f"refusing to write unsafe managed path: {relative}")
        if dst.exists() and dst.is_file() and not dst.is_symlink() and filecmp.cmp(src, dst, shallow=False):
            continue
        atomic_copy(src, dst)
        copied += 1
    removed = 0
    for relative in sorted(previous - set(selected_paths)):
        dst = safe_target(home, relative)
        if dst is None:
            log(f"warning: not removing unsafe managed path: {relative}")
            continue
        if dst.is_symlink() or (dst.exists() and dst.is_file()):
            dst.unlink()
            removed += 1
        elif dst.exists():
            log(f"warning: not removing non-file managed path: {relative}")
    write_manifest(manifest, selected_paths, {"updated_at": timestamp(), "state": "installed"})
    log(f"code synchronized: {copied} file(s) updated, {removed} obsolete file(s) removed in {home}")


def install_cli(home: Path, bin_link: Path) -> None:
    target = home / "bin" / "agent-handoff"
    if not target.exists():
        raise SystemExit(f"missing executable: {target}")
    os.chmod(target, 0o755)
    bin_link.parent.mkdir(parents=True, exist_ok=True)
    if bin_link.is_symlink() or bin_link.exists():
        if bin_link.is_symlink() and bin_link.resolve() == target.resolve():
            log(f"PATH link already correct: {bin_link}")
            return
        backup(bin_link)
        bin_link.unlink()
    bin_link.symlink_to(target)
    log(f"PATH link installed: {bin_link} -> {target}")


def install_opencode(home: Path) -> None:
    source = home / "integrations" / "opencode" / "agent-handoff.js"
    if not source.exists():
        raise SystemExit(f"missing OpenCode plugin source: {source}")
    OPENCODE_PLUGIN.parent.mkdir(parents=True, exist_ok=True)
    if not OPENCODE_PLUGIN.exists() or not filecmp.cmp(source, OPENCODE_PLUGIN, shallow=False):
        atomic_copy(source, OPENCODE_PLUGIN)
        os.chmod(OPENCODE_PLUGIN, 0o644)
        log(f"OpenCode plugin installed: {OPENCODE_PLUGIN}")
    else:
        log("OpenCode plugin already current")
    data = {}
    if OPENCODE_CONFIG.exists():
        try:
            data = json.loads(OPENCODE_CONFIG.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise SystemExit(f"refusing to modify invalid opencode.json: {exc}")
        if not isinstance(data, dict):
            raise SystemExit("refusing to modify opencode.json: root is not an object")
    instructions = data.get("instructions")
    if not isinstance(instructions, list):
        instructions = []
    entry = str(home / "integrations" / "AGENT-INSTRUCTIONS.md")
    if entry not in instructions:
        instructions.append(entry)
        data["instructions"] = instructions
        backup(OPENCODE_CONFIG)
        atomic_write(OPENCODE_CONFIG, json.dumps(data, indent=2, ensure_ascii=False) + "\n", mode=0o600)
        log(f"OpenCode instructions updated: {entry}")
    else:
        log("OpenCode instructions already registered")


def install_agy(home: Path, bin_link: Path) -> None:
    instructions = home / "integrations" / "AGENT-INSTRUCTIONS.md"
    if not instructions.exists():
        raise SystemExit(f"missing instruction source: {instructions}")
    AGY_CONFIG.mkdir(parents=True, exist_ok=True)
    hooks = {}
    if AGY_HOOKS.exists():
        try:
            hooks = json.loads(AGY_HOOKS.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise SystemExit(f"refusing to modify invalid AGY hooks.json: {exc}")
        if not isinstance(hooks, dict):
            raise SystemExit("refusing to modify AGY hooks.json: root is not an object")
    hooks.pop("agent-handoff-probe", None)
    hooks["agent-handoff-report"] = {
        "Stop": [
            {
                "type": "command",
                "command": f"{bin_link} hook agy",
                "timeout": 25,
            }
        ]
    }
    text = json.dumps(hooks, indent=2, ensure_ascii=False) + "\n"
    if not AGY_HOOKS.exists() or AGY_HOOKS.read_text(encoding="utf-8") != text:
        backup(AGY_HOOKS)
        atomic_write(AGY_HOOKS, text, mode=0o600)
        log(f"AGY hook installed: {AGY_HOOKS}")
    else:
        log("AGY hook already installed")
    block = instructions.read_text(encoding="utf-8").strip()
    if not block.startswith(MARKER):
        raise SystemExit(f"instruction block marker is missing: {instructions}")
    existing = AGY_RULES.read_text(encoding="utf-8") if AGY_RULES.exists() else ""
    if MARKER in existing:
        start = existing.index(MARKER)
        end = existing.find(END_MARKER, start)
        if end != -1:
            end += len(END_MARKER)
        else:
            end = len(existing)
        new_text = existing[:start].rstrip() + ("\n\n" if existing[:start].strip() else "") + block + "\n" + existing[end:].lstrip()
        if new_text != existing:
            backup(AGY_RULES)
            atomic_write(AGY_RULES, new_text.rstrip() + "\n", mode=0o600)
            log("AGY instruction updated")
        else:
            log("AGY instruction already registered")
    else:
        if existing.strip():
            backup(AGY_RULES)
            new_text = existing.rstrip() + "\n\n" + block + "\n"
        else:
            new_text = block + "\n"
        atomic_write(AGY_RULES, new_text, mode=0o600)
        log(f"AGY instruction installed: {AGY_RULES}")


def validate_external_json(path: Path, label: str) -> None:
    if not path.exists():
        return
    if not path.is_file() or path.is_symlink():
        raise SystemExit(f"refusing to modify {label}: not a regular file: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"refusing to modify invalid {label}: {exc}")
    if not isinstance(data, dict):
        raise SystemExit(f"refusing to modify {label}: root is not an object")


def preflight(home: Path, source: Path, no_code: bool, do_opencode: bool, do_agy: bool) -> None:
    base = home if no_code else source
    executable = base / "bin" / "agent-handoff"
    if not executable.is_file() or executable.is_symlink():
        raise SystemExit(f"missing executable: {home / 'bin' / 'agent-handoff'}")
    if do_opencode:
        plugin = base / "integrations" / "opencode" / "agent-handoff.js"
        if not plugin.is_file() or plugin.is_symlink():
            raise SystemExit(f"missing OpenCode plugin source: {home / 'integrations' / 'opencode' / 'agent-handoff.js'}")
        validate_external_json(OPENCODE_CONFIG, "opencode.json")
    if do_agy:
        instructions = base / "integrations" / "AGENT-INSTRUCTIONS.md"
        if not instructions.is_file() or instructions.is_symlink():
            raise SystemExit(f"missing instruction source: {home / 'integrations' / 'AGENT-INSTRUCTIONS.md'}")
        try:
            block = instructions.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise SystemExit(f"cannot read instruction source: {exc}")
        if not block.startswith(MARKER):
            raise SystemExit(f"instruction block marker is missing: {instructions}")
        validate_external_json(AGY_HOOKS, "AGY hooks.json")
        if AGY_RULES.exists():
            if not AGY_RULES.is_file() or AGY_RULES.is_symlink():
                raise SystemExit(f"refusing to modify AGY instructions: not a regular file: {AGY_RULES}")
            try:
                AGY_RULES.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise SystemExit(f"cannot read AGY instructions: {exc}")


def external_paths(do_opencode: bool, do_agy: bool) -> list[Path]:
    paths: list[Path] = []
    if do_opencode:
        paths.extend([OPENCODE_PLUGIN, OPENCODE_CONFIG])
    if do_agy:
        paths.extend([AGY_HOOKS, AGY_RULES])
    return paths


def capture_external_state(paths: list[Path]) -> dict[Path, tuple[str, bytes | str | None, int | None]]:
    state: dict[Path, tuple[str, bytes | str | None, int | None]] = {}
    for path in paths:
        try:
            if path.is_symlink():
                state[path] = ("symlink", os.readlink(path), None)
            elif path.exists():
                if not path.is_file():
                    raise SystemExit(f"refusing to manage non-file integration path: {path}")
                state[path] = ("file", path.read_bytes(), path.stat().st_mode & 0o7777)
            else:
                state[path] = ("missing", None, None)
        except OSError as exc:
            raise SystemExit(f"cannot inspect integration path {path}: {exc}")
    return state


def restore_external_state(state: dict[Path, tuple[str, bytes | str | None, int | None]]) -> None:
    for path, (kind, payload, mode) in state.items():
        try:
            if kind == "missing":
                if path.is_symlink() or (path.exists() and path.is_file()):
                    path.unlink()
                elif path.exists():
                    log(f"recovery skipped non-file: {path}")
            elif kind == "symlink":
                if path.is_symlink() or (path.exists() and path.is_file()):
                    path.unlink()
                elif path.exists():
                    log(f"recovery skipped non-file: {path}")
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to(str(payload))
            else:
                if path.exists() and not path.is_file():
                    log(f"recovery skipped non-file: {path}")
                    continue
                assert isinstance(payload, bytes) and mode is not None
                atomic_write_bytes(path, payload, mode)
        except OSError as exc:
            log(f"recovery failed for {path}: {exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install or upgrade Agent Handoff without touching runtime data.")
    parser.add_argument("--home", default=str(DEFAULT_HOME), help="runtime installation directory")
    parser.add_argument("--bin-link", default=str(DEFAULT_BIN_LINK), help="PATH symlink location")
    parser.add_argument("--no-opencode", action="store_true", help="do not modify OpenCode configuration")
    parser.add_argument("--no-agy", action="store_true", help="do not modify AGY configuration")
    parser.add_argument("--no-code", action="store_true", help="do not synchronize source files")
    args = parser.parse_args(argv)
    home = Path(args.home).expanduser().resolve()
    bin_link = Path(args.bin_link).expanduser()
    log(f"source: {SOURCE}")
    log(f"home: {home}")
    do_opencode = not args.no_opencode
    do_agy = not args.no_agy
    preflight(home, SOURCE, args.no_code, do_opencode, do_agy)
    external_state = capture_external_state(external_paths(do_opencode, do_agy))
    try:
        if not args.no_code:
            sync_code(SOURCE, home)
        install_cli(home, bin_link)
        if do_opencode:
            install_opencode(home)
        if do_agy:
            install_agy(home, bin_link)
    except BaseException:
        restore_external_state(external_state)
        raise
    log("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
