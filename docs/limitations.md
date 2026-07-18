# Scope and limitations

Warden governs one specific population: Claude Code and Codex sessions running
on one Mac, each in its own git worktree. Its job is to stop a session from
writing outside that worktree. Everything below is a deliberate edge of that
job, not a bug. The full derivation lives in
[session-isolation.md](session-isolation.md); the Codex specifics are in
[codex-port.md](codex-port.md).

## What Warden does not govern

- **Anything that isn't a governed agent session.** Your own shells, editors,
  and tools are untouched. Warden constrains Claude Code and Codex sessions,
  not the machine as a whole.
- **The network.** Warden governs filesystem writes only. There are no egress
  restrictions from Warden itself, but the native sandbox it enables applies
  Claude Code's own per-domain network approval flow; a headless or
  non-interactive session may see network commands fail rather than prompt.
- **Everything outside the protected surfaces.** The write scope is
  **deny-only, by design invariant**: Warden never enumerates the
  directories tools may write, because any such list is a standing bet that
  no new tool ever appears — and it loses that bet the first time one does
  (2026-07-17: a curated carve-out list silently blocked the Azure CLI and
  global agent memory). The Claude template grants `allowWrite: ["/"]` and
  everything Warden protects is a deny: the managed root, the per-repo git
  tamper surfaces, and the governance files (`~/.claude/settings.json`,
  `settings.local.json`). Claude Code's own built-in protections (its
  settings, hooks, and skills files are write-denied at every scope) apply
  on top and cannot be overridden by the blanket allow. The Codex render
  carries the same invariant: one `~/**` write grant, with only the
  governance surfaces (`~/.codex/config.toml`, the Claude user settings)
  held read-only. **The rule for future work: a blocked tool is never fixed
  by extending an allow list — there is no allow list. Diagnose with
  `warden doctor <path>`; if Warden is the blocker, the path is a protected
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
  gets a session-start warning telling it to enter a worktree, and the
  guard's I4 rule denies every Bash command it issues (file tools were
  already denied by I2) until it moves into a worktree. I4 is scoped to
  *adopted* repos (the rendered registry), so it inherits the registry's
  refresh lag. The git-shaped version is closed by the ref hook
  above, whose R2 rule also refuses agent-session moves or deletes of any
  existing branch at a shared checkout root — including from headless or
  pre-install sessions the Bash sandbox never bound to, since the hook rides
  on git itself. R2 recognizes agent sessions by their environment markers
  (`CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, `WARDEN_ACTIVE`, `CODEX_SANDBOX`);
  a session that scrubs those from its environment is not classified.

- **Tracked bytes in trees a session shouldn't touch: git-level, not
  byte-level, on the Claude side.** Under the deny-only write scope, a
  governed session's *raw shell* could write bytes into a shared checkout's
  tracked tree or a sibling worktree. The current sandbox cannot express
  "writable machine minus the repo trees": enumerating tree entries blows
  the exec-argument budget (the E2BIG incident), and a repo-root deny
  freezes the repo's own worktrees and shared-`.git` because a write deny
  beats every allow beneath it (both proven live 2026-07-17; the fix is an
  upstream sandbox capability, allow-within-deny for writes — Codex's
  engine already has it, which is why the Codex side keeps its per-entry
  byte-level tree freeze; the filed-ready request with evidence is
  [upstream-asks.md](upstream-asks.md)). What holds everywhere regardless:
  file tools are
  guard-denied outside the session's own worktree (I1–I3), commits and ref
  moves against protected trees are impossible (tamper surfaces frozen, R2
  hook), so stray bytes cannot enter history or reach origin and `git
  checkout` recovers them; and every Bash command is audited. Warden's
  wall for git history is absolute; its wall for working-tree bytes on the
  Claude side is judgment (guard) plus audit, not syscalls.

- **Mixed fleets during a disable/enable flip.** `sudo warden disable` and
  `sudo warden enable` take effect live for the tool guard, the Codex guard,
  and the git reference-transaction hook, but the Bash sandbox is bound at
  session start and can't be changed under a running session. In both
  directions, a session keeps the policy it started with: a session already
  running when you disable stays sandboxed until it restarts, and a session
  started while disabled stays unsandboxed after you re-enable, until it
  restarts. See [the disable failsafe](disable-failsafe.md) for the full
  design.

- **Profile size is a hard budget.** The sandbox profile is passed to every
  Bash spawn as one exec argument; past roughly 400 filesystem rules it
  exceeds the OS argument limit and **every shell command in every governed
  session fails to start** (observed 2026-07-17 at 18 repos under the old
  per-file rendering). Two facts constrain the fix: the tracked tree cannot
  be enumerated per-file (that is the E2BIG blowup), and it cannot be
  frozen with one repo-root deny either — **a write deny always beats any
  allow beneath it** (empirically proven the same day; the docs'
  most-specific-path-wins language applies to read rules only), so a root
  deny freezes the repo's own worktrees and the shared-`.git` writes their
  commits need. The renderer therefore denies only the git tamper surfaces
  per repo (`.git/index`, `HEAD`, `config`, `hooks`, `info`, the HEAD-branch
  ref trio, `.claude/settings.json`), and the guard's I4 rule denies all
  Bash in sessions whose cwd is inside an adopted shared checkout — work
  happens in worktrees, structurally. The renderer refuses to render more
  than `WARDEN_MAX_FS_RULES` (default 250) rules; if a refresh hits that
  ceiling, thin out the scan directory rather than raising the ceiling
  blind.

- **Freshly cloned repos.** A repo cloned since the last policy refresh isn't
  protected against a root-directory session until the next refresh. A
  background daemon watches for new repos and refreshes within seconds (with a
  15-minute fallback), and `sudo warden refresh` closes the gap immediately.
