# Upstream asks — Claude Code sandbox

Two capability gaps in Claude Code's native Bash sandbox force Warden's
Claude-side tree protection down to git-level (guard judgment + audit)
instead of byte-level syscall denial. Both were demonstrated live on
2026-07-17 (Claude Code v2.1.212, macOS 15 Seatbelt backend). Each section
below is written to be filed verbatim as a GitHub issue on
`anthropics/claude-code` (or via `/feedback`). Status: **drafted, not yet
filed** — filing publishes machine details, so it is the repo owner's call.

## Ask 1: allow-within-deny for write rules

`sandbox.filesystem` read rules support both directions: a broad
`denyRead` can contain a narrower `allowRead` (documented: "the narrower
allow re-opens that part of the denied region"). Write rules support only
deny-within-allow: a `denyWrite` beats every `allowWrite` at or beneath
it, so a narrower allow can never re-open part of a denied region.

**Why it matters:** the policy "the whole machine is writable except these
repo trees" is expressible today (`allowWrite: ["/"]` + per-repo
`denyWrite`) — but only if nothing inside a denied repo may ever be
written. Real repos contain things sessions must write: their own linked
worktree under `<repo>/.claude/worktrees/<name>`, and the shared
`.git/objects`, `.git/refs`, `.git/worktrees/<id>` that any worktree
commit writes. With no allow-within-deny, a repo-root `denyWrite` freezes
every worktree and kills `git commit` from all of them.

**Observed:** with `denyWrite: [<repo-root>]` and
`allowWrite: [<repo-root>/.git/objects, ...]` rendered into managed
settings, `git reset` from inside a freshly created worktree of that repo
failed writing `.git/worktrees/<name>/index.lock` — the allow beneath the
deny never took effect.

**Ask:** apply the documented read-rule precedence (most specific path
wins) to write rules as well.

**This is a compiler gap, not an OS gap:** raw Seatbelt profiles express
the nesting directly — Warden's own semantics lab runs
`(deny file-write* (subpath <repo>))` followed by
`(allow file-write* (subpath <worktree>))` and the full positive git op
suite passes inside the re-opened subtree
(`tests/lab/derive.sh`, recorded in `tests/lab/EVIDENCE-2026-07-16.txt`).
The capability exists in the sandbox engine Claude Code already uses; the
settings-to-profile compiler just doesn't emit it for write rules.

**Retirement trigger:** `tests/lab/probe-write-precedence.sh` reports
`RETIRED`. Then upgrade `render.py` to the byte-level tree freeze (one
deny per repo root plus worktree/shared-`.git` allows) and delete the
git-level-residual entry from [limitations.md](limitations.md).

## Ask 2: sandbox profile delivered by file, not exec argument

The generated Seatbelt profile is passed to every Bash spawn as a single
exec argument. Rule lists expand ~3 KB per filesystem rule in the profile,
so at roughly 400 rules the command line exceeds ARG_MAX and **every Bash
command in every governed session fails to start** (`E2BIG` — observed at
383 `denyWrite` entries producing a 1.3 MB argument; even `echo ok` could
not spawn). `failIfUnavailable: true` turns this into a total outage that
the session cannot diagnose or repair from inside.

**Why it matters:** per-path deny rules are the only byte-level protection
primitive available (see Ask 1), and enumerating even one directory level
of 18 repositories already crosses the ceiling. The rule budget, not the
policy intent, becomes the design constraint.

**Ask:** write the profile to a temp file and pass a path (Seatbelt
supports profile-by-path), or otherwise remove rule-count from the exec
argument budget. Failing that, refuse to start with a clear diagnostic
instead of per-command `E2BIG`.

**Retirement trigger:** a Claude Code release whose Bash spawns no longer
carry the profile as an exec argument (re-test by raising
`WARDEN_MAX_FS_RULES` in a disposable render and spawning from a fresh
session). Then delete the ceiling (`MAX_FS_RULES_DEFAULT`) from
`render.py` and the profile-size entry from
[limitations.md](limitations.md).

## What Warden does until these land

Deny-only write scope with git tamper surfaces denied per repo
(~9 rules/repo), guard-hook denial of file tools outside the session's own
worktree, machine-wide `reference-transaction` hook for branch protection,
full Bash audit, and `warden doctor` stray-byte detection at shared
checkout roots. See [limitations.md](limitations.md).
