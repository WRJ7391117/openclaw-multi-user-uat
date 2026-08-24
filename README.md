# OpenClaw Multi-User UAT

A reusable Codex plugin and Skill for safely detecting, applying, verifying, and rolling back multi-user Feishu/Lark UAT and IM recipient guards for OpenClaw.

The patch lets each user admitted by OpenClaw's existing conversation policy authorize and operate user-scoped tools with their own identity. It does not create another Feishu channel plugin and does not weaken administrator-only application management.

## What it preserves

- The acting identity is always the current message sender.
- Tokens remain isolated by `appId + userOpenId`.
- OAuth flows remain isolated by `appId + senderOpenId`.
- App onboarding, application permission administration, and bulk `/feishu auth` remain owner/admin responsibilities.
- Source drift fails closed instead of applying a best-effort rewrite.

No App Secret, access token, refresh token, OpenClaw configuration, or user identifier is bundled or requested by this project.

## Use as a Codex Skill

Install the repository through your Codex marketplace workflow, or copy `skills/openclaw-multi-user-uat` into your personal Codex skills directory. Start a new Codex task, then ask:

```text
Use $openclaw-multi-user-uat to check or restore the OpenClaw Feishu multi-user UAT patch.
```

The Skill runs the bundled deterministic manager:

```bash
python3 skills/openclaw-multi-user-uat/scripts/manage_patch.py status
python3 skills/openclaw-multi-user-uat/scripts/manage_patch.py apply --dry-run
python3 skills/openclaw-multi-user-uat/scripts/manage_patch.py apply --restart
python3 skills/openclaw-multi-user-uat/scripts/manage_patch.py verify --live
python3 skills/openclaw-multi-user-uat/scripts/manage_patch.py rollback --restart

python3 skills/openclaw-multi-user-uat/scripts/manage_im_receive_id_guard.py status
python3 skills/openclaw-multi-user-uat/scripts/manage_im_receive_id_guard.py apply --dry-run
python3 skills/openclaw-multi-user-uat/scripts/manage_im_receive_id_guard.py apply --restart
python3 skills/openclaw-multi-user-uat/scripts/manage_im_receive_id_guard.py verify
python3 skills/openclaw-multi-user-uat/scripts/manage_im_receive_id_guard.py rollback --restart
```

`status` and `verify` are read-only. `apply`, `rollback`, and Gateway restart change the local OpenClaw runtime and should be run only when explicitly intended.

## Supported patch shape

The manager locates the active `openclaw-lark` installation with:

```bash
openclaw plugins inspect openclaw-lark --json
```

It applies only when both owner gates and all sender/token isolation invariants match exactly. Official plugin upgrades may overwrite the runtime patch. After an upgrade, run `status` again; unfamiliar upstream source is reported as `mixed/drifted` and is never edited automatically.

See [the design reference](skills/openclaw-multi-user-uat/references/design.md) for the exact security and rollback model.

The IM guard prevents an omitted or mismatched `receive_id` from being forwarded to Feishu as a misleading `invalid receive_id` request. It also reminds agents that private-message `open_id` values must come from a user search performed under the same application.

## Verification limits

JavaScript syntax, OpenClaw configuration validation, plugin doctor, and channel probes are local/runtime evidence. A real non-owner user must still complete the Feishu authorization card and perform the requested user-scoped operation for end-to-end proof.

## License

MIT. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
