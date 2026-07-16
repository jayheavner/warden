# Claude Enterprise remote policy vs the local managed-settings file

## The failure this document exists for

Warden's original Claude Code delivery mechanism was the root-owned file
`/Library/Application Support/ClaudeCode/managed-settings.json` — the
policy-settings layer. On a machine whose Claude Code install is signed into a
**Claude Enterprise** organization, that layer is owned by the org's *remote*
managed settings. At session startup Claude Code loads the remote policy and
**replaces the entire policy-settings destination with it**. If the org pushes
no policy (the common case), the layer ends up holding zero rules, zero hooks,
no env, and no sandbox — everything Warden rendered into the local file is
silently discarded.

Observed on 2026-07-16 (debug trace from a fresh headless session):

```
Watching for changes in setting files ... /Library/Application Support/ClaudeCode/managed-settings.json
[3P telemetry] Waiting for remote managed settings before telemetry init
Settings changed from policySettings, updating app state
Replacing all allow rules for destination 'policySettings' with 0 rule(s): []
Replacing all deny rules for destination 'policySettings' with 0 rule(s): []
```

Result: every Claude Code session on the machine ran ungoverned — no
`WARDEN_ACTIVE`, no guard hooks, no sandbox — while `warden status` reported
"installed", the Codex layer worked (Codex has no remote policy layer), and
the git-hook layer worked (delivered through `/etc/gitconfig`). The audit
trail was the tell: not one `session-start` entry from any Claude Code session
since install.

## The fix, in three parts

1. **User-settings fallback delivery** (`userfallback.py`). The user-settings
   layer (`~/.claude/settings.json`) is merged independently of the remote
   policy and survives the override — verified empirically for `env`, `hooks`,
   and `sandbox` (all three load and enforce from non-policy layers).
   `install.sh` and every `warden refresh` merge the same enforcement stack
   Warden renders into the managed file — `WARDEN_ACTIVE`, the guard hooks,
   `permissions.deny`, and the full sandbox deny-write list — into the user
   settings file, preserving everything else in it. The pre-warden shape is
   backed up once to `~/.claude/warden/settings.json.pre-warden` and recorded
   in `~/.claude/warden/fallback.json` so `uninstall.sh` restores it exactly.
   `warden disable` renders the fallback with the sandbox off, mirroring the
   managed disabled render.

2. **Detection** (`warden status`). Status now reports two new lines: whether
   the fallback is delivered and current against the managed render, and the
   audit-trail evidence — the age of the last governed Claude Code
   `session-start`, or a loud warning that none was ever observed.

3. **Live verification** (`warden verify-claude`). A file on disk is not
   enforcement. This command launches a real headless Claude Code session and
   requires a fresh governed `session-start` in the audit trail; it is the
   REQUIRED first post-install step and the check that would have caught this
   condition on day one. `warden selftest` (run inside a session) gained T16,
   which asserts the fallback is delivered.

## The honest trade-off

The user-settings file is user-owned, so fallback delivery is weaker than the
root-owned managed file: a sufficiently determined session could rewrite it.
The fallback narrows that hole by protecting itself — the settings file is on
its own sandbox deny-write list (blocks Bash), and the guard hook (root-owned
code) judges the file tools. But the tamper-PROOF path on an Enterprise
machine is the **Enterprise route**: have the org admins carry Warden's
settings (hooks, env, sandbox) in the remote managed policy itself, which is
the layer that actually wins. Until then, the fallback keeps sessions governed
and `warden verify-claude` keeps the machine honest.
