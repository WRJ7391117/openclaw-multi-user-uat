---
name: openclaw-multi-user-uat
description: Install, detect, verify, or roll back the OpenClaw Lark Feishu multi-user UAT patch when ordinary users need sender-bound access to user-scoped tools such as calendars, tasks, docs, or messages.
---

# OpenClaw Multi-User UAT

Use the bundled deterministic script instead of recreating source edits by hand:

```bash
python3 scripts/manage_patch.py status
python3 scripts/manage_patch.py apply --dry-run
python3 scripts/manage_patch.py apply --restart
python3 scripts/manage_patch.py verify --live
python3 scripts/manage_patch.py rollback --restart
```

Resolve `scripts/manage_patch.py` relative to this Skill directory. `status` and `verify` are read-only. Run `apply`, `rollback`, or a Gateway restart only when the user has requested the corresponding mutation or fix.

## Workflow

1. Run `status` first. Treat `upstream`, `patched`, and `mixed/drifted` as materially different states.
2. For installation or repair, run `apply --dry-run`. Continue with `apply --restart` only when the dry run reports an exact supported upstream structure. The script refuses partial or unfamiliar source shapes.
3. Run `verify --live` after an apply or OpenClaw/plugin upgrade. Report local validation and channel probe separately from a real user round trip.
4. Use `rollback --restart` only when the user requests removal/recovery. The script restores only a compatible backup whose recorded patched hashes still match the active files.

An already patched result is success and must not rewrite files. After an official `openclaw-lark` upgrade, rerun `status`; apply only if the new installed source still matches all structural and identity-isolation invariants.

## Security boundaries

- Never request, print, copy, or persist App Secrets, access tokens, refresh tokens, or user `open_id` values.
- This patch removes the App Owner gate only from ordinary user-scoped tool execution and sender-bound OAuth. It does not change conversation admission, agent/account binding, app permission administration, onboarding, or bulk `/feishu auth` ownership rules.
- The acting identity must remain the current message sender. Never add a model-controlled `user_open_id` override.
- Preserve token isolation by `appId + userOpenId` and OAuth-flow isolation by `appId + senderOpenId`.
- Request user scopes on demand for the operation; do not batch-grant every scope to every user.
- A successful Gateway/channel probe proves credentials and channel health, not that a non-owner user completed OAuth or that a calendar mutation succeeded. Require an actual user round trip for end-to-end evidence.

Read [references/design.md](references/design.md) when reviewing the patch model, diagnosing a refused apply, or deciding whether a newer upstream version already supersedes this patch.
