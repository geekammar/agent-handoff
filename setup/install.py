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

SOURCE = Path(__file__).resolve().parents[1]
DEFAULT_HOME = Path(os.environ.get("AGENT_HANDOFF_HOME", "/root/agent-handoff"))
DEFAULT_BIN_LINK = Path(os.environ.get("AGENT_HANDOFF_BIN_LINK", "/usr/local/bin/agent-handoff"))
HOME = Path.home()
OPENCODE_DIR = HOME / ".config" / "opencode"
OPENCODE_PLUGIN = OPENCODE_DIR / "plugin" / "agent-handoff.js"
OPENCODE_CONFIG = OPENCODE_DIR / "opencode.json"
AGY_CONFIG = HOME / ".gemini" / "config"
AGY_HOOKS = AGY_CONFIG / "hooks.json"
AGY_RULES = AGY_CONFIG / "AGENTS.md"
MARKER = "<!-- AGENT HANDOFF REPORTING v1.0 -->"
END_MARKER = "<!-- END AGENT HANDOFF REPORTING -->"
MANAGED_TOP = {"bin", "lib", "integrations", "setup", "tests", "docs", "config", "README.md"}


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


def tracked_files(root: Path) -> list[Path] | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True,
            text=False,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return [Path(item.decode("utf-8")) for item in result.stdout.split(b"\0") if item]


def walk_files(root: Path) -> list[Path]:
    files = []
    ignored = {".git", "data", "queue", "state", "locks", "logs", "tmp", "releases", "__pycache__", ".pytest_cache"}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in ignored for part in relative.parts):
            continue
        if path.suffix in {".pyc"}:
            continue
        files.append(relative)
    return files


def source_files(source: Path) -> list[Path]:
    files = tracked_files(source) if source != HOME else None
    if files is None:
        files = walk_files(source)
    return [
        item
        for item in files
        if item.parts and item.parts[0] in MANAGED_TOP and (source / item).is_file()
    ]


def sync_code(source: Path, home: Path) -> None:
    if source.resolve() == home.resolve():
        log("source is the installation home; code sync skipped")
        return
    home.mkdir(parents=True, exist_ok=True)
    copied = 0
    for relative in source_files(source):
        src = source / relative
        dst = home / relative
        if dst.exists() and dst.is_file() and filecmp.cmp(src, dst, shallow=False):
            continue
        atomic_copy(src, dst)
        copied += 1
    log(f"code synchronized: {copied} file(s) updated in {home}")


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
    if not args.no_code:
        sync_code(SOURCE, home)
    install_cli(home, bin_link)
    if not args.no_opencode:
        install_opencode(home)
    if not args.no_agy:
        install_agy(home, bin_link)
    log("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
