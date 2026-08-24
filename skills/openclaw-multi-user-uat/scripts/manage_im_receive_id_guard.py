#!/usr/bin/env python3
"""Safely manage the OpenClaw Lark IM receive_id fail-fast guard."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any


PATCH_ID = "openclaw-lark-im-receive-id-guard-v1"
TARGET = "src/tools/oapi/im/message.js"

UPSTREAM_DESCRIPTION = (
    "            '\\n- send（发送消息）：发送消息到私聊或群聊。私聊用 receive_id_type=open_id，群聊用 receive_id_type=chat_id' +\n"
)
PATCHED_DESCRIPTION = (
    "            '\\n- send（发送消息）：发送消息到私聊或群聊。私聊用 receive_id_type=open_id，群聊用 receive_id_type=chat_id；receive_id 必填，必须使用同一应用中 feishu_search_user 返回的 open_id，禁止复用其他应用的 open_id' +\n"
)
UPSTREAM_EXECUTION = (
    "                        log.info(`send: receive_id_type=${p.receive_id_type}, receive_id=${p.receive_id}, msg_type=${p.msg_type}`);\n"
)
PATCHED_EXECUTION = (
    "                        const expectedPrefix = p.receive_id_type === 'open_id' ? 'ou_' : 'oc_';\n"
    "                        if (typeof p.receive_id !== 'string' || !p.receive_id.startsWith(expectedPrefix)) {\n"
    "                            log.warn(`send rejected locally: missing or mismatched receive_id for type=${p.receive_id_type}`);\n"
    "                            return (0, helpers_1.json)({\n"
    "                                error: 'invalid_request',\n"
    "                                message: '发送消息缺少或错误的 receive_id。私聊时先用 feishu_search_user 查找收件人，并把同一应用返回的 open_id 原样填入 receive_id；不要复用其他飞书应用的 open_id。',\n"
    "                            });\n"
    "                        }\n"
    "                        log.info(`send: receive_id_type=${p.receive_id_type}, msg_type=${p.msg_type}`);\n"
)

INVARIANTS = (
    "receive_id: typebox_1.Type.String({",
    "receive_id: p.receive_id,",
    "params: { receive_id_type: p.receive_id_type },",
)


class GuardError(RuntimeError):
    pass


def run(command: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GuardError(f"required command is unavailable: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GuardError(f"command timed out: {' '.join(command[:3])}") from exc


def parse_json_output(raw: str) -> Any:
    start = raw.find("{")
    if start < 0:
        raise GuardError("command did not return JSON")
    try:
        return json.loads(raw[start:])
    except json.JSONDecodeError as exc:
        raise GuardError("command returned invalid JSON") from exc


def discover_plugin_root(explicit: str | None) -> tuple[Path, str]:
    if explicit:
        root = Path(explicit).expanduser().resolve()
        version = "unknown"
        package_file = root / "package.json"
        if package_file.is_file():
            try:
                version = str(json.loads(package_file.read_text(encoding="utf-8")).get("version", "unknown"))
            except (OSError, json.JSONDecodeError):
                pass
        return root, version

    result = run(["openclaw", "plugins", "inspect", "openclaw-lark", "--json"])
    if result.returncode != 0:
        raise GuardError("could not inspect the installed openclaw-lark plugin")
    payload = parse_json_output(result.stdout)
    plugin = payload.get("plugin", {})
    root_value = plugin.get("rootDir") or payload.get("install", {}).get("installPath")
    if not root_value:
        raise GuardError("openclaw-lark inspection did not include a plugin root")
    version = str(plugin.get("version") or payload.get("install", {}).get("version") or "unknown")
    return Path(root_value).expanduser().resolve(), version


def read_source(root: Path) -> str:
    path = root / TARGET
    if not path.is_file():
        raise GuardError(f"required plugin file is missing: {TARGET}")
    return path.read_text(encoding="utf-8")


def ensure_invariants(source: str) -> None:
    failures = [pattern for pattern in INVARIANTS if source.count(pattern) != 1]
    if failures:
        raise GuardError("source invariants are not supported")


def detect_state(source: str) -> str:
    upstream = (source.count(UPSTREAM_DESCRIPTION), source.count(UPSTREAM_EXECUTION))
    patched = (source.count(PATCHED_DESCRIPTION), source.count(PATCHED_EXECUTION))
    if upstream == (1, 1) and patched == (0, 0):
        return "upstream"
    if upstream == (0, 0) and patched == (1, 1):
        return "patched"
    return "mixed/drifted"


def build_patched(source: str) -> str:
    ensure_invariants(source)
    if detect_state(source) != "upstream":
        raise GuardError("apply requires the exact supported upstream state")
    patched = source.replace(UPSTREAM_DESCRIPTION, PATCHED_DESCRIPTION, 1)
    patched = patched.replace(UPSTREAM_EXECUTION, PATCHED_EXECUTION, 1)
    ensure_invariants(patched)
    if detect_state(patched) != "patched":
        raise GuardError("internal patch generation did not produce the expected state")
    return patched


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: bytes, mode: int | None = None) -> None:
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def default_backup_root() -> Path:
    return Path.home() / ".openclaw" / "patch-backups"


def create_backup(root: Path, version: str, source: str, patched: str, backup_root: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = backup_root / f"openclaw-lark-{version}-im-receive-id-guard-{stamp}"
    backup.mkdir(parents=True, mode=0o700)
    before = source.encode("utf-8")
    after = patched.encode("utf-8")
    backup_file = backup / f"{TARGET}.before"
    backup_file.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(backup_file, before, 0o600)
    metadata = {
        "patch_id": PATCH_ID,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "plugin_root": str(root),
        "plugin_version": version,
        "file": TARGET,
        "backup": f"{TARGET}.before",
        "before_sha256": sha256(before),
        "after_sha256": sha256(after),
    }
    atomic_write(backup / "metadata.json", (json.dumps(metadata, indent=2) + "\n").encode("utf-8"), 0o600)
    return backup


def write_source(root: Path, source: str) -> None:
    path = root / TARGET
    atomic_write(path, source.encode("utf-8"), path.stat().st_mode & 0o777)


def check_command(label: str, command: list[str], *, timeout: int = 60) -> None:
    result = run(command, timeout=timeout)
    if result.returncode != 0:
        raise GuardError(f"{label} failed (exit {result.returncode})")
    print(f"ok: {label}")


def run_checks(root: Path) -> None:
    check_command("JavaScript syntax", ["node", "--check", str(root / TARGET)])
    check_command("OpenClaw configuration", ["openclaw", "config", "validate"])
    check_command("OpenClaw plugin doctor", ["openclaw", "plugins", "doctor"])


def restart_gateway() -> None:
    check_command("Gateway restart", ["openclaw", "gateway", "restart"], timeout=90)


def print_status(root: Path, version: str, source: str) -> str:
    ensure_invariants(source)
    state = detect_state(source)
    print(f"plugin: @larksuite/openclaw-lark {version}")
    print(f"root: {root}")
    print(f"state: {state}")
    print(f"  {TARGET}: {state}")
    return state


def load_metadata(backup: Path) -> dict[str, Any]:
    metadata_file = backup / "metadata.json"
    if not metadata_file.is_file():
        raise GuardError("backup metadata is missing")
    try:
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GuardError("backup metadata is invalid") from exc
    if metadata.get("patch_id") != PATCH_ID:
        raise GuardError("backup belongs to a different patch")
    return metadata


def compatible_backup(backup: Path, root: Path, version: str, source: str) -> bool:
    try:
        metadata = load_metadata(backup)
        if Path(metadata.get("plugin_root", "")).resolve() != root:
            return False
        if str(metadata.get("plugin_version")) != version or metadata.get("file") != TARGET:
            return False
        if sha256(source.encode("utf-8")) != metadata.get("after_sha256"):
            return False
        backup_file = backup / str(metadata["backup"])
        return sha256(backup_file.read_bytes()) == metadata.get("before_sha256")
    except (KeyError, OSError, GuardError, TypeError):
        return False


def select_backup(args: argparse.Namespace, root: Path, version: str, source: str) -> Path:
    if args.backup:
        candidate = Path(args.backup).expanduser().resolve()
        if not compatible_backup(candidate, root, version, source):
            raise GuardError("specified backup is not compatible with the active guard")
        return candidate
    backup_root = Path(args.backup_root).expanduser().resolve() if args.backup_root else default_backup_root()
    for candidate in sorted(backup_root.glob("openclaw-lark-*-im-receive-id-guard-*"), reverse=True):
        if compatible_backup(candidate, root, version, source):
            return candidate
    raise GuardError("no compatible rollback backup was found")


def command_status(args: argparse.Namespace) -> None:
    root, version = discover_plugin_root(args.plugin_root)
    print_status(root, version, read_source(root))


def command_apply(args: argparse.Namespace) -> None:
    root, version = discover_plugin_root(args.plugin_root)
    source = read_source(root)
    state = print_status(root, version, source)
    if state == "patched":
        print("result: already patched; no files changed")
        if not args.dry_run:
            run_checks(root)
            if args.restart:
                restart_gateway()
        return
    if state != "upstream":
        raise GuardError("refusing to modify mixed or drifted source")
    patched = build_patched(source)
    if args.dry_run:
        print("result: supported upstream structure; apply would change 1 file")
        return

    backup_root = Path(args.backup_root).expanduser().resolve() if args.backup_root else default_backup_root()
    backup = create_backup(root, version, source, patched, backup_root)
    write_source(root, patched)
    try:
        current = read_source(root)
        ensure_invariants(current)
        if detect_state(current) != "patched":
            raise GuardError("post-write state is not patched")
        run_checks(root)
    except Exception:
        write_source(root, source)
        raise GuardError("post-apply validation failed; original file was restored")
    print(f"backup: {backup}")
    print("result: IM receive_id guard applied")
    if args.restart:
        restart_gateway()


def command_verify(args: argparse.Namespace) -> None:
    root, version = discover_plugin_root(args.plugin_root)
    source = read_source(root)
    state = print_status(root, version, source)
    if state != "patched":
        raise GuardError(f"verification requires a fully patched state, found {state}")
    run_checks(root)
    print("result: verification passed")


def command_rollback(args: argparse.Namespace) -> None:
    root, version = discover_plugin_root(args.plugin_root)
    source = read_source(root)
    state = print_status(root, version, source)
    if state != "patched":
        raise GuardError(f"rollback requires a fully patched state, found {state}")
    backup = select_backup(args, root, version, source)
    metadata = load_metadata(backup)
    restored = (backup / str(metadata["backup"])).read_text(encoding="utf-8")
    ensure_invariants(restored)
    if detect_state(restored) != "upstream":
        raise GuardError("backup does not restore the exact supported upstream state")
    write_source(root, restored)
    try:
        run_checks(root)
    except Exception:
        write_source(root, source)
        raise GuardError("rollback validation failed; guarded file was restored")
    print(f"backup: {backup}")
    print("result: IM receive_id guard rolled back")
    if args.restart:
        restart_gateway()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-root", help="override discovery (primarily for isolated testing)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser("status", help="detect the installed guard state")
    status_parser.set_defaults(handler=command_status)

    apply_parser = subparsers.add_parser("apply", help="apply the guard to exact supported source")
    apply_parser.add_argument("--dry-run", action="store_true", help="validate without writing")
    apply_parser.add_argument("--restart", action="store_true", help="restart the Gateway after success")
    apply_parser.add_argument("--backup-root", help="override the backup parent directory")
    apply_parser.set_defaults(handler=command_apply)

    verify_parser = subparsers.add_parser("verify", help="validate the guarded installation")
    verify_parser.set_defaults(handler=command_verify)

    rollback_parser = subparsers.add_parser("rollback", help="restore a compatible pre-guard backup")
    rollback_parser.add_argument("--backup", help="use a specific backup directory")
    rollback_parser.add_argument("--backup-root", help="search a different backup parent directory")
    rollback_parser.add_argument("--restart", action="store_true", help="restart the Gateway after success")
    rollback_parser.set_defaults(handler=command_rollback)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        args.handler(args)
        return 0
    except GuardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (OSError, UnicodeError) as exc:
        print(f"error: local file operation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
