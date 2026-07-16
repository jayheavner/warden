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
  restrictions.
- **Sessions started before installation or a policy change.** A session binds
  its policy when it starts. After installing Warden — or after adding or
  restructuring repos — restart any running sessions so they pick up the
  current policy.

## Narrow edges within scope

- **Raw git ref plumbing across worktrees.** Git refs are shared between a
  repo and its worktrees, so they can't be protected by a per-path write rule.
  The shared checkout's own branch is protected, and a machine-wide
  `reference-transaction` hook refuses cross-worktree ref moves on the normal
  git path. That hook does not fire if a session overrides `core.hooksPath`
  (via an environment variable, a command-line flag, or a pre-existing local
  git setting).

- **Sessions whose working directory is a shared repo root.** Such a session
  has its tracked tree frozen, so it can't do ordinary work there. It can
  still create brand-new, untracked top-level files (litter, not corruption),
  and its shell writes into a sibling worktree are recorded in the audit log
  rather than blocked. The git-shaped version of that is closed by the ref
  hook above.

- **Freshly cloned repos.** A repo cloned since the last policy refresh isn't
  protected against a root-directory session until the next refresh. A
  background daemon watches for new repos and refreshes within seconds (with a
  15-minute fallback), and `sudo warden refresh` closes the gap immediately.
