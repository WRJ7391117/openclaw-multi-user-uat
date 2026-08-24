# Patch design and failure boundaries

## Intended behavior

The patch changes only two owner gates in `@larksuite/openclaw-lark`:

- `src/core/tool-client.js`: ordinary UAT-backed tool calls may proceed for any sender already admitted by OpenClaw's Feishu conversation policy.
- `src/tools/oauth.js`: sender-bound OAuth may be initiated by that same sender without requiring App Owner identity.

The strict owner-policy implementation remains present and continues to protect administrative paths. No second Feishu channel plugin is registered.

## Required invariants

The management script refuses to apply unless all of these are recognizable:

- Tool lookup uses `getStoredToken(account.appId, userOpenId)`.
- OAuth flow keys use `appId + senderOpenId`.
- Credential-store account keys use `appId + userOpenId`.
- Each removable owner import/gate appears exactly once.
- Both target files are entirely upstream or entirely patched; a mixed state is not repaired automatically.

This is deliberately structure-based rather than version-only. A newer package can be patched only if its relevant structure is still exact; source drift fails closed for human review.

## State and evidence

- `upstream`: both exact owner gates are present and patch markers are absent.
- `patched`: both owner gates are absent and both exact patch markers are present.
- `mixed/drifted`: partial patching, duplicate patterns, or unfamiliar source. Do not edit automatically.
- Syntax/config/plugin checks: local consistency evidence.
- Channel probe: application credentials and channel reachability evidence.
- Real sender authorization plus the requested operation: end-to-end user-service evidence.

## Backup and rollback

Each successful apply creates a timestamped backup under `~/.openclaw/patch-backups/` with a metadata file containing the plugin version and before/after SHA-256 hashes. It contains source files only, not configuration or credentials.

Rollback selects the newest compatible backup unless one is supplied explicitly. It refuses restoration when plugin version, plugin root, or active patched hashes do not match the metadata. This prevents an old backup from overwriting a later upstream release.

Official package upgrades may replace the runtime patch. Prefer an upstream-supported multi-user UAT implementation when one exists; otherwise rerun detection and review any drift before updating this Skill's exact patterns.
