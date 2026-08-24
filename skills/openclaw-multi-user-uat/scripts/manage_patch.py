#!/usr/bin/env python3
"""Safely manage the OpenClaw Lark multi-user UAT runtime patch."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any


PATCH_ID = "openclaw-lark-multi-user-uat-v1"
TARGETS = ("src/core/tool-client.js", "src/tools/oauth.js")

TOOL_IMPORT = 'const owner_policy_1 = require("./owner-policy.js");\n'
TOOL_GATE = (
    "        // Owner 检查：非 owner 用户直接拒绝（从 uat-client.ts 迁移至此）\n"
    "        await (0, owner_policy_1.assertOwnerAccessStrict)(this.account, this.sdk, userOpenId);\n"
)
TOOL_PATCH = (
    "        // Every allowed conversation user operates with their own UAT.\n"
    "        // Tokens are isolated by appId + userOpenId in token-store.\n"
)

OAUTH_IMPORT = 'const owner_policy_1 = require("../core/owner-policy.js");\n'
OAUTH_GATE = (
    "    // 0. Check if the user is the app owner (fail-close: 安全优先).\n"
    "    const sdk = lark_client_1.LarkClient.fromAccount(account).sdk;\n"
    "    try {\n"
    "        await (0, owner_policy_1.assertOwnerAccessStrict)(account, sdk, senderOpenId);\n"
    "    }\n"
    "    catch (err) {\n"
    "        if (err instanceof owner_policy_1.OwnerAccessDeniedError) {\n"
    "            log.warn(`non-owner user ${senderOpenId} attempted to authorize`);\n"
    "            return json({\n"
    "                error: 'permission_denied',\n"
    "                message: '当前应用仅限所有者（App Owner）使用。您没有权限发起授权，无法使用相关功能。',\n"
    "            });\n"
    "        }\n"
    "        throw err;\n"
    "    }\n"
)
OAUTH_PATCH = (
    "    // User authorization is scoped to the current message sender. The tool\n"
    "    // does not accept a user_open_id argument, and token-store isolates tokens\n"
    "    // by appId + senderOpenId. App-level permission management remains an\n"
    "    // administrator responsibility.\n"
)

INVARIANTS = {
    "src/core/tool-client.js": (
        "const stored = await (0, token_store_1.getStoredToken)(this.account.appId, userOpenId);",
    ),
    "src/tools/oauth.js": (
        "const flowKey = `${appId}:${senderOpenId}`;",
        "userOpenId: senderOpenId,",
    ),
    "src/core/token-store.js": (
        "function accountKey(appId, userOpenId) {",
        "return `${appId}:${userOpenId}`;",
    ),
}


class PatchError(RuntimeError):
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
        raise PatchError(f"required command is unavailable: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise PatchError(f"command timed out: {' '.join(command[:3])}") from exc


def parse_json_output(raw: str) -> Any:
    start = raw.find("{")
    if start < 0:
        raise PatchError("command did not return JSON")
    try:
        return json.loads(raw[start:])
    except json.JSONDecodeError as exc:
        raise PatchError("command returned invalid JSON") from exc


def discover_plugin_root(explicit: str | None) -> tuple[Path, str]:
    if explicit:
        root = Path(explicit).expanduser().resolve()
        package_file = root / "package.json"
        version = "unknown"
        if package_file.is_file():
            try:
                version = str(json.loads(package_file.read_text(encoding="utf-8")).get("version", "unknown"))
            except (OSError, json.JSONDecodeError):
                pass
        return root, version

    result = run(["openclaw", "plugins", "inspect", "openclaw-lark", "--json"])
    if result.returncode != 0:
        raise PatchError("could not inspect the installed openclaw-lark plugin")
    payload = parse_json_output(result.stdout)
    plugin = payload.get("plugin", {})
    root_value = plugin.get("rootDir") or payload.get("install", {}).get("installPath")
    if not root_value:
        raise PatchError("openclaw-lark inspection did not include a plugin root")
    version = str(plugin.get("version") or payload.get("install", {}).get("version") or "unknown")
    return Path(root_value).expanduser().resolve(), version


def read_sources(root: Path) -> dict[str, str]:
    sources: dict[str, str] = {}
    for relative in (*TARGETS, "src/core/token-store.js"):
        path = root / relative
        if not path.is_file():
            raise PatchError(f"required plugin file is missing: {relative}")
        sources[relative] = path.read_text(encoding="utf-8")
    return sources


def count_exact(text: str, value: str) -> int:
    return text.count(value)


def ensure_invariants(sources: dict[str, str]) -> None:
    failures: list[str] = []
    for relative, patterns in INVARIANTS.items():
        for pattern in patterns:
            count = count_exact(sources[relative], pattern)
            if count != 1:
                failures.append(f"{relative}: expected one identity-isolation invariant, found {count}")
    if failures:
        raise PatchError("source invariants are not supported:\n  " + "\n  ".join(failures))


def classify_file(text: str, upstream_parts: tuple[str, str], patch: str) -> str:
    upstream_counts = [count_exact(text, part) for part in upstream_parts]
    patch_count = count_exact(text, patch)
    if upstream_counts == [1, 1] and patch_count == 0:
        return "upstream"
    if upstream_counts == [0, 0] and patch_count == 1:
        return "patched"
    return "drifted"


def detect_state(sources: dict[str, str]) -> tuple[str, dict[str, str]]:
    states = {
        TARGETS[0]: classify_file(sources[TARGETS[0]], (TOOL_IMPORT, TOOL_GATE), TOOL_PATCH),
        TARGETS[1]: classify_file(sources[TARGETS[1]], (OAUTH_IMPORT, OAUTH_GATE), OAUTH_PATCH),
    }
    unique = set(states.values())
    if unique == {"upstream"}:
        return "upstream", states
    if unique == {"patched"}:
        return "patched", states
    return "mixed/drifted", states


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def build_patched(sources: dict[str, str]) -> dict[str, str]:
    ensure_invariants(sources)
    state, _ = detect_state(sources)
    if state != "upstream":
        raise PatchError(f"apply requires a fully upstream state, found {state}")
    tool = sources[TARGETS[0]].replace(TOOL_IMPORT, "", 1).replace(TOOL_GATE, TOOL_PATCH, 1)
    oauth = sources[TARGETS[1]].replace(OAUTH_IMPORT, "", 1).replace(OAUTH_GATE, OAUTH_PATCH, 1)
    result = {**sources, TARGETS[0]: tool, TARGETS[1]: oauth}
    next_state, _ = detect_state(result)
    if next_state != "patched":
        raise PatchError("internal patch generation did not produce the expected state")
    ensure_invariants(result)
    return result


def default_backup_root() -> Path:
    return Path.home() / ".openclaw" / "patch-backups"


def create_backup(
    root: Path,
    version: str,
    sources: dict[str, str],
    patched: dict[str, str],
    backup_root: Path,
) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = backup_root / f"openclaw-lark-{version}-multi-user-uat-{stamp}"
    if backup.exists():
        raise PatchError(f"backup path already exists: {backup}")
    backup.mkdir(parents=True, mode=0o700)
    files: dict[str, dict[str, str]] = {}
    for relative in TARGETS:
        before_data = sources[relative].encode("utf-8")
        after_data = patched[relative].encode("utf-8")
        backup_file = backup / f"{relative}.before"
        backup_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(backup_file, before_data, 0o600)
        files[relative] = {
            "backup": f"{relative}.before",
            "before_sha256": sha256_bytes(before_data),
            "after_sha256": sha256_bytes(after_data),
        }
    metadata = {
        "patch_id": PATCH_ID,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "plugin_root": str(root),
        "plugin_version": version,
        "files": files,
    }
    atomic_write(backup / "metadata.json", (json.dumps(metadata, indent=2) + "\n").encode("utf-8"), 0o600)
    return backup


def write_targets(root: Path, contents: dict[str, str]) -> None:
    originals = {relative: (root / relative).read_bytes() for relative in TARGETS}
    written: list[str] = []
    try:
        for relative in TARGETS:
            path = root / relative
            mode = path.stat().st_mode & 0o777
            atomic_write(path, contents[relative].encode("utf-8"), mode)
            written.append(relative)
    except Exception:
        for relative in reversed(written):
            path = root / relative
            atomic_write(path, originals[relative], path.stat().st_mode & 0o777)
        raise


def check_command(label: str, command: list[str], *, timeout: int = 60) -> None:
    result = run(command, timeout=timeout)
    if result.returncode != 0:
        raise PatchError(f"{label} failed (exit {result.returncode})")
    print(f"ok: {label}")


def run_local_checks(root: Path) -> None:
    for relative in TARGETS:
        check_command(f"JavaScript syntax: {relative}", ["node", "--check", str(root / relative)])
    check_command("OpenClaw configuration", ["openclaw", "config", "validate"])
    check_command("OpenClaw plugin doctor", ["openclaw", "plugins", "doctor"])


def restart_gateway() -> None:
    check_command("Gateway restart", ["openclaw", "gateway", "restart"], timeout=90)


def live_probe() -> None:
    result = run(
        ["openclaw", "channels", "status", "--channel", "feishu", "--probe", "--json"],
        timeout=90,
    )
    if result.returncode != 0:
        raise PatchError(f"Feishu channel probe failed (exit {result.returncode})")
    payload = parse_json_output(result.stdout)
    accounts = payload.get("channelAccounts", {}).get("feishu", [])
    enabled = [item for item in accounts if item.get("enabled")]
    passed = [item for item in enabled if item.get("probe", {}).get("ok")]
    failed = len(enabled) - len(passed)
    print(f"probe: {len(passed)} passed, {failed} failed, {len(enabled)} enabled")
    if failed:
        raise PatchError("one or more enabled Feishu account probes failed")


def print_status(root: Path, version: str, sources: dict[str, str]) -> str:
    ensure_invariants(sources)
    state, file_states = detect_state(sources)
    print(f"plugin: @larksuite/openclaw-lark {version}")
    print(f"root: {root}")
    print(f"state: {state}")
    for relative in TARGETS:
        print(f"  {relative}: {file_states[relative]}")
    return state


def command_status(args: argparse.Namespace) -> None:
    root, version = discover_plugin_root(args.plugin_root)
    print_status(root, version, read_sources(root))


def command_apply(args: argparse.Namespace) -> None:
    root, version = discover_plugin_root(args.plugin_root)
    sources = read_sources(root)
    state = print_status(root, version, sources)
    if state == "patched":
        print("result: already patched; no files changed")
        if not args.dry_run:
            run_local_checks(root)
            if args.restart:
                restart_gateway()
        return
    if state != "upstream":
        raise PatchError("refusing to modify mixed or drifted source")
    patched = build_patched(sources)
    if args.dry_run:
        print("result: supported upstream structure; apply would change 2 files")
        return

    backup_root = Path(args.backup_root).expanduser().resolve() if args.backup_root else default_backup_root()
    backup = create_backup(root, version, sources, patched, backup_root)
    write_targets(root, patched)
    try:
        post_sources = read_sources(root)
        post_state, _ = detect_state(post_sources)
        ensure_invariants(post_sources)
        if post_state != "patched":
            raise PatchError(f"post-write state is {post_state}")
        run_local_checks(root)
    except Exception:
        write_targets(root, sources)
        raise PatchError("post-apply validation failed; original files were restored")
    print(f"backup: {backup}")
    print("result: patch applied")
    if args.restart:
        restart_gateway()


def load_metadata(backup: Path) -> dict[str, Any]:
    metadata_file = backup / "metadata.json"
    if not metadata_file.is_file():
        raise PatchError(f"backup metadata is missing: {metadata_file}")
    try:
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PatchError("backup metadata is invalid") from exc
    if metadata.get("patch_id") != PATCH_ID:
        raise PatchError("backup belongs to a different patch")
    return metadata


def compatible_backup(backup: Path, root: Path, version: str, sources: dict[str, str]) -> bool:
    try:
        metadata = load_metadata(backup)
        if Path(metadata.get("plugin_root", "")).resolve() != root or str(metadata.get("plugin_version")) != version:
            return False
        for relative in TARGETS:
            record = metadata["files"][relative]
            if sha256_bytes(sources[relative].encode("utf-8")) != record["after_sha256"]:
                return False
            backup_file = backup / record["backup"]
            if sha256_bytes(backup_file.read_bytes()) != record["before_sha256"]:
                return False
        return True
    except (KeyError, OSError, PatchError, TypeError):
        return False


def select_backup(args: argparse.Namespace, root: Path, version: str, sources: dict[str, str]) -> Path:
    if args.backup:
        candidate = Path(args.backup).expanduser().resolve()
        if not compatible_backup(candidate, root, version, sources):
            raise PatchError("specified backup is not compatible with the active patched files")
        return candidate
    backup_root = Path(args.backup_root).expanduser().resolve() if args.backup_root else default_backup_root()
    candidates = sorted(backup_root.glob("openclaw-lark-*-multi-user-uat-*"), reverse=True)
    for candidate in candidates:
        if compatible_backup(candidate, root, version, sources):
            return candidate
    raise PatchError("no compatible rollback backup was found")


def command_rollback(args: argparse.Namespace) -> None:
    root, version = discover_plugin_root(args.plugin_root)
    sources = read_sources(root)
    state = print_status(root, version, sources)
    if state != "patched":
        raise PatchError(f"rollback requires a fully patched state, found {state}")
    backup = select_backup(args, root, version, sources)
    metadata = load_metadata(backup)
    restored = {**sources}
    for relative in TARGETS:
        restored[relative] = (backup / metadata["files"][relative]["backup"]).read_text(encoding="utf-8")
    ensure_invariants(restored)
    restored_state, _ = detect_state(restored)
    if restored_state != "upstream":
        raise PatchError("backup does not restore the exact supported upstream state")
    write_targets(root, restored)
    try:
        run_local_checks(root)
    except Exception:
        write_targets(root, sources)
        raise PatchError("rollback validation failed; patched files were restored")
    print(f"backup: {backup}")
    print("result: patch rolled back")
    if args.restart:
        restart_gateway()


def command_verify(args: argparse.Namespace) -> None:
    root, version = discover_plugin_root(args.plugin_root)
    sources = read_sources(root)
    state = print_status(root, version, sources)
    if state != "patched":
        raise PatchError(f"verification requires a fully patched state, found {state}")
    run_local_checks(root)
    if args.live:
        live_probe()
        print("note: channel probes are not end-to-end user OAuth or tool-operation evidence")
    print("result: verification passed")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--plugin-root",
        help="override discovery (primarily for isolated testing)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser("status", help="detect the installed patch state")
    status_parser.set_defaults(handler=command_status)

    apply_parser = subparsers.add_parser("apply", help="apply the patch to exact supported source")
    apply_parser.add_argument("--dry-run", action="store_true", help="validate without writing")
    apply_parser.add_argument("--restart", action="store_true", help="restart the Gateway after success")
    apply_parser.add_argument("--backup-root", help="override the backup parent directory")
    apply_parser.set_defaults(handler=command_apply)

    verify_parser = subparsers.add_parser("verify", help="validate the patched installation")
    verify_parser.add_argument("--live", action="store_true", help="also probe enabled Feishu accounts")
    verify_parser.set_defaults(handler=command_verify)

    rollback_parser = subparsers.add_parser("rollback", help="restore a compatible pre-patch backup")
    rollback_parser.add_argument("--backup", help="use a specific backup directory")
    rollback_parser.add_argument("--backup-root", help="search a different backup parent directory")
    rollback_parser.add_argument("--restart", action="store_true", help="restart the Gateway after success")
    rollback_parser.set_defaults(handler=command_rollback)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.handler(args)
        return 0
    except PatchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (OSError, UnicodeError) as exc:
        print(f"error: local file operation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
