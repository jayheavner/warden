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
- **The rest of the home directory — mostly.** The native sandbox's default
  write scope is the session's working directory plus temp. That default is
  broader than Warden's job (confine the projects, not the machine), so the
  base template grants `allowWrite` carve-outs for the directories agent
  tooling legitimately writes: `~/.claude` (global memory, audit, plugins;
  its `settings.json` stays denied), `~/.claude.json`, `~/.azure`, `~/.aws`,
  `~/.config`, `~/.cache`, `~/.local`, `~/Library/Caches`, `~/Library/Logs`.
  Extend the list in `templates/managed-settings.base.json` and run
  `sudo warden refresh` if a CLI you use writes somewhere else. Selftest T17
  asserts the carve-outs are live. (The Codex port does not yet mirror these
  carve-outs.)
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
  refresh lag, and it reads the session's cwd — a session whose cwd is an
  *ancestor* of the repos (e.g. the scan directory itself) can still
  shell-write tracked files, though it cannot commit them or move refs
  (tamper surfaces frozen; every write audited). The git-shaped version is
  closed by the ref hook
  above, whose R2 rule also refuses agent-session moves or deletes of any
  existing branch at a shared checkout root — including from headless or
  pre-install sessions the Bash sandbox never bound to, since the hook rides
  on git itself. R2 recognizes agent sessions by their environment markers
  (`CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, `WARDEN_ACTIVE`, `CODEX_SANDBOX`);
  a session that scrubs those from its environment is not classified.

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
