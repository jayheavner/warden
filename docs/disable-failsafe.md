# Warden disable failsafe

A machine-wide pause switch for Warden enforcement. Motivation: if Warden's
policy is wrong or its refresh daemon is wedged, the user must have a fast,
reliable way out that Warden itself cannot block.

## Commands

```sh
sudo warden disable    # pause all enforcement, sticky until re-enabled
sudo warden enable     # re-arm: fresh policy render + refresh, then enforce
```

Both run from a plain (unsandboxed) terminal. Both are idempotent: `disable`
when already disabled and `enable` when already enabled report the state and
exit 0 without changes.

## Source of truth

A root-owned sentinel file:

```
/Library/Application Support/ClaudeCode/warden/DISABLED
```

Content: one JSON line `{"disabled_at": <ISO8601>, "by_uid": <uid>}`.

The sentinel is the single source of truth for the disabled state. Everything
else (settings renders, daemon state, status output) is derived from it.
Its path is inside the managed root, which is already in the sandbox
deny-write list and covered by the tool guard — a sandboxed session cannot
create or remove it. `guard.py` must explicitly deny writes targeting the
sentinel path even when evaluating otherwise-permitted paths.

## What `disable` does (in order; verify each step, roll back on failure)

1. Write the sentinel (atomic: temp file + rename, root:wheel 0644).
2. Re-render `managed-settings.json` in disabled mode: same renderer
   (`render.py --disabled`), producing `sandbox.enabled: false`,
   `failIfUnavailable: false`, with all hooks **kept** (the guard and the
   session-start notice still fire; the guard permits everything and warns).
3. If Codex is installed (`/etc/codex/warden` exists), re-render
   `/etc/codex/requirements.toml` in the equivalent disabled mode.
4. `launchctl bootout system/com.warden.refresh`. **`com.warden.landd` stays
   loaded** — `warden land` is a service, not enforcement, and keeps working.
5. Append `{"event": "disable", ...}` to `~/.claude/warden/audit.jsonl`.
6. Print a verified summary of exactly what changed, ending with:
   `warden is DISABLED. Bash writes in already-running sessions stay
   sandboxed until those sessions restart.`

Rollback rule: if any step fails, restore the prior state of all earlier
steps (remove sentinel, re-render enforcing settings, re-bootstrap daemon)
and exit nonzero. Every intermediate state fails safe: enforcement on.

## What `enable` does (in order)

1. Remove the sentinel.
2. **Unconditionally** run a full policy refresh (fresh scan + render for
   both harnesses), never re-arming a pre-disable render — repo layout may
   have changed while the refresh daemon was down.
3. Re-bootstrap `com.warden.refresh`.
4. Verify the render exactly as `install.sh` does (sandbox fail-closed,
   hooks wired, denyWrite count).
5. Append `{"event": "enable", ...}` to the audit log; print the verified
   summary and remind that running sessions keep their old (unenforced)
   sandbox until restarted.

If a step fails, re-write the sentinel and exit nonzero — a half-enabled
state must report itself as disabled, not as enforcing.

## Live behavior while disabled

Layer by layer:

- **Tool guard (`guard.py`)** — re-executed per tool call; checks the
  sentinel first and permits everything, emitting once per session (tracked
  via a session-scoped marker) the warning banner below. Takes effect live.
- **Git reference-transaction hook** — checks the sentinel and exits 0,
  printing one line to stderr: `warden: disabled — ref protection off`.
  Takes effect live.
- **Refresh daemon** — booted out. Live.
- **Sandbox deny-write list** — bound at session start; running sessions
  keep enforcing Bash-write denials until restarted. This is a platform
  constraint (managed settings are read once at startup). New sessions start
  unenforced.

## Visibility (no launchd reminder — rejected as too heavy)

The disabled state announces itself in the conversation:

- **Session start:** a `SessionStart` hook entry (kept in the disabled
  render) checks the sentinel and, when present, injects:
  `⚠ Warden enforcement is DISABLED (since <date>). Session isolation is
  off. Re-enable with: sudo warden enable`
  When the sentinel is absent it emits nothing.
- **First guarded tool call per session** (belt-and-suspenders, covers
  sessions whose harness lacks SessionStart): the guard emits the same
  banner once, plus: `Bash writes in sessions started before the disable
  are still sandboxed until restart.`
- **`warden status`:** first line becomes
  `state: DISABLED since <date> (sudo warden enable to re-arm)` and the
  exit code is distinct (2) so scripts can detect it.
- **Codex:** equivalent notice via its managed hook layer.

No timers, no auto-re-arm, no background reminder jobs. Sticky until
`sudo warden enable`.

## Mixed-fleet honesty

`warden status` must render the mixed state truthfully: when disabled, it
notes that sessions started before the flip still enforce Bash-write denials;
when re-enabled, that sessions started while disabled remain unenforced
until restarted.

## First-line remediation note

For stale-policy wrong denials, `sudo warden refresh` is the first-line fix
and is cheaper than a full disable. `warden status` output and the disable
summary both mention it. `disable` remains the unconditional failsafe.

## Testing

- Unit: sentinel-present branches in `guard.py`, the git hook, and the
  renderer's `--disabled` mode; idempotency of both commands; sentinel-is-a-
  directory and unreadable-sentinel treated as *enabled* (fail safe).
- Acceptance (`warden selftest` gains a disabled-state path, run on demand):
  `disable → assert foreign-worktree Edit permitted + banner text present →
  enable → assert denial restored`. Banner assertions include the
  Bash-restart caveat verbatim.
- Codex acceptance mirror in `warden codex-selftest`.

## Decisions and their status

- Re-render (one renderer, `--disabled` flag) over moving the settings file
  aside — keeps one code path and keeps hooks alive for the banner. (Ruled.)
- Machine-wide scope covering both harnesses. (Ruled.)
- `landd` survives disable. (Ruled.)
- Sticky-forever with conversation-level visibility only; the panel's
  dead-man-reminder objection (Nygard) was considered and overruled by the
  user — no launchd reminder job. (User decision, 2026-07-16.)
