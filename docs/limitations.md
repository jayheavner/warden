# Scope and limitations

Warden governs one specific population: Claude Code and Codex sessions running
on one Mac, each in its own git worktree. Its job is to stop a session from
writing outside that worktree. Everything below is a deliberate edge of that
job, not a bug. The full derivation lives in
[session-isolation.md](session-isolation.md); the Codex specifics are in
[codex-port.md](codex-port.md).

**This document is not allowed to rot.** Every entry is a *design
invariant* enforced by a named test or selftest probe that fails if the
entry is violated: T17 (deny-only, no allow-list), T20 (network
unrestricted), T21 (session actually wrapped in the wall), the
`tests/lab/probe-session-profile.sh` live wall proof, and the guard/render
unit suites. An entry with no test is a defect in this document. Harness
constraints that were once open (native-sandbox limitations) are retired
in [upstream-asks.md](upstream-asks.md), which also records the one
watched durability risk (`sandbox-exec` deprecation) and its detector.

## What Warden does not govern

- **Anything that isn't a governed agent session.** Your own shells, editors,
  and tools are untouched. Warden constrains Claude Code and Codex sessions,
  not the machine as a whole.
- **The network — Warden touches it ZERO.** This is a hard invariant
  (selftest T20). Warden governs filesystem writes only; a governed session
  reaches the network exactly as an ungoverned shell does. This is *why*
  Warden delivers its own Seatbelt profile instead of enabling Claude
  Code's native sandbox: that native sandbox cannot be made
  filesystem-only — it forces a network proxy that MITM-breaks Go-TLS tools
  (`gh`, `gcloud`, `terraform`) and Node's `fetch`, and denies keychain
  writes so in-session credential refreshes fail (all proven live
  2026-07-18; no config disables it — GitHub issues #56959, #28954,
  #30619). Warden's profile has no network, credential, or process rules
  at all. **The rule for future work: Warden never blocks a network host or
  a command — only filesystem writes to protected surfaces. If a tool's
  network breaks in a governed session, Warden is not the cause; check that
  the native sandbox has not been re-enabled (`sandbox.enabled` must be
  `false`; `warden status` reports the wall).**
- **Everything outside the protected surfaces.** Warden never enumerates
  the directories tools may write, because any such list is a standing bet
  that no new tool ever appears — and it loses that bet the first time one
  does (2026-07-17: a curated carve-out list silently blocked the Azure CLI
  and global agent memory). The wall is a Seatbelt profile that **allows by
  default** and denies only what Warden protects: per repo, the whole
  shared checkout (with the session's worktree and the shared-`.git` write
  set re-opened inside it, and the protected HEAD-branch refs re-closed
  inside those), plus the managed root and the governance files. This
  allow-inside-deny nesting is what Claude Code's settings compiler could
  not express and raw Seatbelt can (proven in `tests/lab`, 66 recorded
  passes) — so the wall is now byte-level, not judgment-level: a session's
  shell physically cannot write another repo's tree or tamper with
  `.git/config`/hooks. The Codex render carries the same invariant through
  its own expressive filesystem map. **The rule for future work: a blocked
  tool is never fixed by extending an allow list — there is no allow list.
  Diagnose with `warden doctor <path>`; if Warden is the blocker, the path
  is a protected
  surface and that is the intended behavior.** Selftest T17 enforces the
  invariant live by probing a novel, never-enumerated home path.
- **Sessions started before installation or a policy change.** A session binds
  its policy when it starts. After installing Warden — or after adding or
  restructuring repos — restart any running sessions so they pick up the
  current policy.
- **Claude Enterprise remote policy owns the managed-settings layer.** On a
  machine signed into a Claude Enterprise org, Claude Code replaces the local
  `managed-settings.json` layer with the org's remote policy — discarding
  Warden's env, hooks, and sandbox. Warden therefore also delivers through the
  user-settings layer (which survives the override), `warden status` reports
  the delivery and governed-session evidence, and `warden verify-claude`
  proves enforcement live. The tamper-proof fix is org-side; see
  [enterprise-override.md](enterprise-override.md).

## Narrow edges within scope

- **Raw git ref plumbing across worktrees.** Git refs are shared between a
  repo and its worktrees, so they can't be protected by a per-path write rule.
  The shared checkout's own branch is protected, and a machine-wide
  `reference-transaction` hook refuses cross-worktree ref moves on the normal
  git path. That hook does not fire if a session overrides `core.hooksPath`
  (via an environment variable, a command-line flag, or a pre-existing local
  git setting).

- **Sessions whose working directory is a shared repo root.** Such a session
  gets a session-start warning telling it to enter a worktree. Its Bash
  commands are **not blocked** (Warden blocks zero commands) — they run,
  but any write they attempt to the checkout bounces off the Seatbelt wall,
  and the guard tags them I4 in the audit trail. The git-shaped version is
  closed by the `reference-transaction` hook, whose R2 rule refuses
  agent-session moves or deletes of any existing branch at a shared
  checkout root — including from headless or pre-install sessions, since
  the hook rides on git itself. R2 recognizes agent sessions by their
  environment markers (`CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`,
  `WARDEN_ACTIVE`, `CODEX_SANDBOX`); a session that scrubs those from its
  environment is not classified.

- **Mixed fleets during a disable/enable flip, and the launcher.** Sessions
  bind Warden's Seatbelt profile at launch (via the `claude` shim), so a
  disable/enable flip takes full effect only for sessions started after it.
  The guard hook and the git `reference-transaction` hook do flip live
  (they read the DISABLED sentinel on each call); the Seatbelt wall does
  not change under a running session. A session started while disabled has
  no wall until it restarts. Separately: governance depends on the `claude`
  launcher pointing at Warden's shim — `warden status` reports whether it
  does, and if the symlink is re-pointed, new sessions start ungoverned
  (the shim, not a re-render, is the enforcement handle). App auto-updates
  are handled: the shim re-resolves the newest installed version.

- **Freshly cloned repos.** A repo cloned since the last policy refresh isn't
  protected against a root-directory session until the next refresh. A
  background daemon watches for new repos and refreshes within seconds (with a
  15-minute fallback), and `sudo warden refresh` closes the gap immediately.
