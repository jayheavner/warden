# Session Isolation on a Shared Development Machine — Design

Date: 2026-07-15 (finalized 2026-07-16)
Status: approved-for-build pending Jay's activation gate (§9)
System name: **Warden** — source at ~/claude/warden

## 1. Problem (plain language)

Many Claude Code sessions run concurrently on this Mac under one OS user, in
the same git repositories. The intended discipline — each session works only
inside its own git worktree; the shared checkout at the repo root belongs to
nobody — exists only as prose. Prose has failed four observed ways:
working-directory drift after failed compound commands, stale assumptions
about shared git state, path-addressed mutation (`git -C`, absolute-path
edits), and zero feedback at violation time. Consequences included corrupted
shared checkouts, a sibling's unfinished work published, and destroyed user
data. Advisory measures are explicitly rejected; this is an enforcement
system.

Requirements are enumerated in the problem statement (invariants I1–I3,
capabilities C1–C4, enforcement properties E1–E5, acceptance tests 1–10).
§6 maps every one to a mechanism; §7 names what stays open.

## 2. Approaches considered

**A. Per-session OS users + POSIX permissions.** Rejected: sessions launch as
Jay's user from the desktop app and CLI; per-session user switching needs
sudo orchestration on every launch, breaks Keychain/app session management,
and still can't express "writable only by the worktree's owner" inside one
shared `.git` without git-aware logic. Fails E2.

**B. Hand-rolled seatbelt wrapper around every session shell.** The
semantics are right (proven, §4) but nothing binds a wrapper to sessions the
human launches tomorrow (fails E2), and user-writable wrappers fail E3.
Retained as the semantic model and validation instrument, not the vehicle.

**C. Claude Code's native enforcement surfaces, delivered via root-owned
managed settings (CHOSEN).** `/Library/Application Support/ClaudeCode/
managed-settings.json` has absolute precedence over user/project/local
settings and, once root-owned, sessions cannot edit it. It delivers:
native per-session Bash sandboxing (Seatbelt) with fail-closed mode and a
dead escape hatch (`allowUnsandboxedCommands: false` — the Bash tool's
`dangerouslyDisableSandbox` parameter is documented as "completely
ignored"); non-removable hooks with per-call `session_id`/`cwd`/`tool_input`
and deny-with-reason; machine-wide permission deny rules. Chosen because it
is the only layer that binds every current and future session automatically,
is externally owned, rejects loudly at execution time, and sees actual tool
calls and file operations instead of command text.

**Rejected sub-approach: git-level enforcement as a primary layer** (global
`core.hooksPath`, `reference-transaction`). Bypassable via `git -c`, and the
machine's PATH git is 2.22.0, which predates `reference-transaction`.
Filesystem-level enforcement subsumes it.

## 3. Architecture

```
/Library/Application Support/ClaudeCode/        (root:wheel after install)
  managed-settings.json      L1  policy: sandbox + hooks + permission denies (rendered)
  warden/
    guard.py                 L2  PreToolUse / SessionStart / Worktree* hook logic
    registry.json            L3  adopted repos: root, HEAD branch, tracked top entries
    render.py                L3  scanner/renderer: disk truth -> registry + settings
    selftest.sh              L5  post-activation acceptance suite
    uninstall.sh                 full rollback
~/claude/warden/                 versioned source of all of the above + installer
```

**L1 — managed sandbox policy (the wall).** Sandbox on machine-wide,
`failIfUnavailable: true`, `allowUnsandboxedCommands: false`. A session's
writable scope is its own working directory + session tmp; sibling worktrees
and other repos are simply outside it (I1, I3). The native worktree
carve-out grants shared-`.git` access from linked worktrees, excluding
`hooks/` and `config` (both tamper surfaces).

On top, a rendered `filesystem.denyWrite` list that is **disjoint from every
legitimate write path**, so no allow/deny precedence question can arise.
Per adopted repo:

- `.git/index`, `.git/HEAD`, `.git/config`, `.git/hooks`, `.git/info` —
  the shared checkout's mutable identity. My lab (§4) shows worktree
  operations never write these; only shared-checkout mutations do.
- the protected-branch trio for the checkout's HEAD branch:
  `refs/heads/<HEAD>`, `refs/heads/<HEAD>.lock`, `logs/refs/heads/<HEAD>` —
  closes ref plumbing (`update-ref`, `reset --soft`, `branch -f`) against
  the shared branch at the filesystem layer (lab-proven).
- every top-level tracked entry of the working tree (`git ls-tree HEAD
  --name-only`, rendered as subpaths) plus root `.claude/settings.json` —
  freezes the shared working tree even for a session whose cwd IS the root
  (whose own cwd would otherwise be auto-writable). Worktrees live under
  `<repo>/.claude/worktrees/…`, which never collides with these entries.
- plus one machine entry: `/Library/Application Support/ClaudeCode` (test 10
  belt; root ownership is the actual wall).

**L2 — guard hook (the judgment).** Root-owned python3 wired as managed
hooks. No command-text parsing anywhere — file tools expose their target
structurally; Bash filesystem effects are enforced by L1 at the syscall
layer.

- PreToolUse[Edit|Write|NotebookEdit]: resolve the target path (realpath);
  deny with a rule-naming reason if it lands (a) inside another session's
  worktree — any `…/.claude/worktrees/<X>/…` where the session's own cwd is
  not inside `<X>` — zero registry lag, brand-new worktrees included;
  (b) inside any git working tree the session's cwd does not belong to
  (covers unadopted repos conservatively); (c) inside the shared checkout's
  frozen zone when the session cwd is the root or elsewhere; (d) under
  `/Library/Application Support/ClaudeCode`. Otherwise: no opinion.
- PreToolUse[Bash]: audit-only (never denies — the sandbox owns Bash).
- SessionStart: one context line announcing enforcement + the session's
  writable scope (legibility; not load-bearing).
- WorktreeCreate/WorktreeRemove: NOT wired (learned at activation: Claude Code
  treats WorktreeCreate as generative — a registered hook must create the
  worktree and return its path, replacing built-in creation; an observational
  hook breaks `claude -w` machine-wide). Registry refresh is manual
  (`sudo warden refresh`) or daemon-driven (gate G2). guard.py retains the
  event handlers in case a future version adds observational worktree events.

Guard failure mode: allow-and-audit on internal error (availability), deny
only on positive classification; the sandbox remains the wall regardless.

**L3 — registry + renderer.** `render.py` derives everything from disk
truth (scan of `~/claude` for repos with `.git` directories, `git worktree
list`, `git symbolic-ref`, `git ls-tree`), never from session input.
Root-invoked: `sudo warden refresh` (alias for render + validate + atomic
swap). Optional root LaunchDaemon runs it on an interval. New repos and new
top-level entries enter denyWrite at the next refresh (lag documented, §7);
the guard-hook rules (b)/(a) are lag-free because they're computed per call.

**L4 — worktree lifecycle lane (C2).** Worktree creation happens (i)
natively by the app/EnterWorktree (app-side, sanctioned), and (ii) via
`git worktree add` from a session whose cwd is the shared root — such a
session can write `.git/worktrees/*` and `.claude/worktrees/<new>` (both
outside denyWrite) but nothing in the frozen zone. Worktree-cwd sessions
cannot run lifecycle against the shared repo (out of scope of their sandbox)
— by design; a new task gets a new session/worktree. No `excludedCommands`
entries in v1: their compound-command semantics are unverified and an
unsandboxed lane is not worth the risk.

**L5 — audit (E5).** Every guard decision and every Bash invocation emits a
structured record: ts, session_id, cwd, tool, target, verdict, rule — via
`logger -t warden` into the unified log (user cannot truncate it) plus a
convenience JSONL at `~/.claude/warden/audit.jsonl`. Sandbox EPERM denials
additionally appear in the unified log from the Sandbox subsystem itself.

## 4. Evidence

**Tier 1 — proven by experiment on this machine** (seatbelt lab, throwaway
fixtures, identical under git 2.22.0 and 2.50.1):

With writable scope = own worktree + carve-outs {`.git/objects`, `.git/refs`,
`.git/logs`, `.git/worktrees/<own>`, `.git/packed-refs{,.lock}`,
`.git/FETCH_HEAD`}: all 14 legitimate ops pass — status/log/diff, commit,
branch create, checkout -b, fetch, push own branch, **push to integration**
(C1), stash, rebase, merge + own reset --hard, clean, read-only inspection of
the shared checkout, `fetch -C <shared>` (C3). All 13 violation probes
blocked with EPERM at execution time: root file create/delete; `git -C
shared` commit/reset --hard/merge/checkout -b; `cd shared && commit` (drift);
sibling worktree writes; sibling `git -C` commit; shared `.git/config` and
index writes. Protected-branch trio: own commit/push unaffected;
`update-ref`/`reset --soft`/`branch -f` on the shared branch blocked.
The lab profile denies the whole repo then re-allows nested paths — but the
production composition (§3) avoids relying on that ordering entirely.

**Tier 2 — vendor-documented and corroborated in the installed binary**
(docs research + string-level verification in `versions/2.1.208`):
managed-settings path & precedence, `failIfUnavailable`,
`allowUnsandboxedCommands`, hook events incl. `WorktreeCreate`,
`hookSpecificOutput.permissionDecision`, deny-reason feedback, native
worktree `.git` carve-out excluding `hooks/` and `config`.

**Tier 3 — deferred to activation-day selftest** (end-to-end behavior of the
harness sandbox under the rendered policy, in a real fresh session). Reason:
headless `claude -p` probing is currently impossible on this machine — the
CLI's stored OAuth is expired and cannot refresh non-interactively (side
finding for Jay: anything headless, e.g. scheduled agents, is likely broken
until `claude login` is re-run). The selftest gates reliance: activation is
not "done" until it passes in a real session.

## 5. What changed versus the intended-discipline prose

Nothing conceptually; the discipline becomes physics. One behavioral change:
a session whose cwd is a shared checkout root can no longer edit files there
even "just this once" — it must create/enter a worktree (the app does this
per session already). Read-only inspection from anywhere is preserved.

## 6. Coverage map

| Requirement | Mechanism (layer) | Evidence tier |
|---|---|---|
| I1 writes only in own workspace | sandbox scope (L1); file-tool guard (L2) | 1 + 2 (+3) |
| I2 shared checkout read-only however addressed | outside scope + denyWrite frozen zone (L1); guard (L2) | 1 + 2 (+3) |
| I3 sibling workspaces read-only | outside scope (L1); dynamic guard rule (L2) | 1 + 2 (+3) |
| C1 unrestricted integration push | carve-outs (lab-proven); no push gating anywhere | 1 |
| C2 workspace lifecycle | app-native + root-cwd lane (§3 L4) | 2 (+3) |
| C3 read-only inspection + fetches anywhere | read paths open; FETCH_HEAD/refs/logs carve-outs | 1 |
| C4 remote-host commands untouched | no local writes ⇒ invisible to sandbox; no text matching exists | 1 (by construction) |
| E1 machine-wide, future repos | managed settings; renderer scan; lag-free guard rules | 2 |
| E2 zero session cooperation | managed settings bind at session start | 2 |
| E3 external, tamper-resistant | root ownership; hooks/config excluded from carve-out; escape hatch dead; managed hooks non-removable | 2 |
| E4 loud, named-rule rejection | EPERM at exec + guard deny reasons | 1 + 2 |
| E5 attributable audit | unified log + JSONL keyed by session_id | 2 (+3) |

Acceptance tests 1–10 → §8 test plan: 1,2,3,4,5 lab-proven semantics +
selftest end-to-end; 6,7 selftest; 8 by construction + selftest; 9,10
selftest (10 also by root ownership).

## 7. Residual gaps (named, not hidden)

- **R1 ref plumbing against a sibling worktree's branch.** `update-ref
  refs/heads/<sibling-branch>` from a session's own worktree writes only
  carved-out paths (refs are shared). Git porcelain refuses moving
  checked-out branches; raw plumbing remains. Not closable at FS granularity
  without breaking legitimate ref writes (packed-refs is one file). The
  shared checkout's own branch IS closed (protected trio). Mitigation:
  audit; optionally render protected trios for live worktree branches at
  refresh (closes it to within refresh lag) — v1.1 flag.
  Update 2026-07-16: closed for the governed git path by the v1.1
  reference-transaction hook — see v1.1-hardening.md §3
  (the per-trio rendering idea was rejected there: it would break owner
  commits).
- **R2 root-cwd session may create NEW untracked top-level files at a shared
  root** (frozen zone covers tracked entries + `.git` + settings). Litter,
  not corruption: tracked state, index, HEAD, refs all denied. Next refresh
  can absorb new tracked entries.
- **R3 root-cwd session vs. sibling worktrees via Bash.** A root session's
  sandbox scope includes `.claude/worktrees/*` (its cwd subtree). File tools
  are guard-denied dynamically; Bash writes into a live sibling worktree from
  a root-cwd session remain possible until v1.1 (per-command rendered denies
  or root-session Bash wrapping). Post-activation, root sessions are
  rare-by-force (they can't do normal work at the root). Audited meanwhile.
  Update 2026-07-16: git-shaped lane closed by the v1.1 hook; arbitrary Bash
  file writes remain audit-only — v1.1 design §6.
- **R4 refresh lag.** A repo cloned after the last refresh has no denyWrite
  entries; its root working tree is unprotected against a root-cwd session in
  it until refresh. Worktree sessions are scope-limited regardless; the
  file-tool guard is lag-free. Bounded by the LaunchDaemon interval.
  Update 2026-07-16: closed to seconds by the v1.1 WatchPaths daemon
  (15-min fallback) — v1.1 design §2.
- **R5 non-Claude processes** (Jay's own shells/tools) are out of scope by
  the problem statement.
- **R6 pre-activation sessions** keep old behavior until restarted.
- **R7 network egress** is not restricted in v1 (C1 requires pushes; adding
  domain allow-lists is orthogonal hardening).

## 8. Test plan

- `tests/test_guard.py` — unit tests for the classifier (pure function:
  (target, session_cwd, registry) → verdict+rule), covering every acceptance
  test's file-tool analog + symlink and relative-path traps.
- `tests/lab/derive.sh` — the seatbelt semantics suite (already passing),
  kept as regression evidence for the carve-out model.
- `warden selftest` — activation-day, inside a real fresh session: builds a
  throwaway fixture repo + worktrees in tmp, then runs the full acceptance
  matrix end-to-end (tests 1–9), plus non-destructive probes against a real
  adopted repo (sentinel-file write at root must EPERM; `update-ref` no-op
  against the protected branch must EPERM; worktree commit must succeed),
  plus test 10 (edit attempt on managed-settings.json must fail and appear
  in the audit log). Emits a per-test verdict table.

## 9. Activation, rollback, gates (Jay decides)

Nothing in this repo has any effect until installed. Activation:

```
cd ~/claude/warden && sudo ./install.sh     # copies, chowns root, renders, verifies
# restart running clones at your convenience (R6)
# then, in any fresh session: "run warden selftest" and read the table
```

Rollback: `sudo /Library/Application\ Support/ClaudeCode/warden/uninstall.sh`
(or delete the managed-settings file) — restores exactly the pre-Warden world.

Open gates for Jay: (G1) activate at all; (G2) LaunchDaemon auto-refresh
on/off + interval; (G3) v1.1 hardening items (R1/R3 closure); (G4) whether
Warden gets a GitHub remote. Side finding to act on independently:
CLI OAuth is expired — headless/scheduled `claude` runs will fail until
`claude login`.
