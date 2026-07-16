# Known limitations

warden's threat model is narrow on purpose: the constrained population is
Claude Code sessions running on one Mac, and the goal is to stop one session
from writing outside its own worktree. The items below are the edges of that
model. The full derivation lives in the design specs under
[`docs/superpowers/specs/`](superpowers/specs/); this file is the short,
current summary.

## Out of scope by design

- **Non-Claude processes.** Anything that isn't a Claude Code session is not
  governed. warden constrains sessions, not the machine.
- **Network egress.** warden governs filesystem writes, not the network. There
  are no egress restrictions.
- **Sessions already running at install (or refresh) time.** A session binds
  its policy at startup. Restart running sessions after installing or after a
  layout change so they pick up the current policy.

## Narrow residual edges

- **Raw git ref plumbing against a sibling worktree.** Shared refs aren't
  filesystem-blockable, so `git update-ref` against another worktree's branch
  can't be stopped at the sandbox layer. The shared checkout's own HEAD branch
  *is* blocked. The governed git path is additionally covered by a root-owned
  `reference-transaction` hook installed via `/etc/gitconfig`; that hook does
  not fire when a session overrides `core.hooksPath` (via env var, command
  line, or a pre-existing local setting).
- **Root-cwd sessions.** A session started at a shared repo root has its
  tracked tree frozen, so it can't do normal work there. It can still create
  new untracked top-level files (litter, not corruption), and its arbitrary
  Bash writes against live sibling worktrees are audit-only rather than blocked.
  The git-shaped path is closed by the ref hook above.
- **Newly cloned repos.** A repo cloned after the last refresh is unprotected
  against root-cwd sessions until the next refresh. A LaunchDaemon
  (`com.warden.refresh`) watches the scan directory and refreshes
  automatically, with a 15-minute fallback interval, so this window is
  normally seconds. Run `sudo warden refresh` to close it immediately.
