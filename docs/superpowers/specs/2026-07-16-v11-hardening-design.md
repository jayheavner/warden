# warden v1.1 hardening — G2 auto-refresh + G3 (R1/R3) closure

Date: 2026-07-16. Builds on `2026-07-15-session-isolation-design.md` (v1).
Scope: the two open gates named in v1 §9 — G2 (LaunchDaemon auto-refresh)
and G3 (v1.1 hardening: R1/R3 closure) — plus the honest re-statement of
what closure means. Reviewed by spec panel 2026-07-16; panel
recommendations 1–6 accepted, contested points decided by the implementer
(rationale inline).

## 0. Prerequisite (done)

The stale 2019 git 2.22.0 at `/usr/local/bin/git` (and its
`/usr/local/git` payload) was removed 2026-07-16. The machine's only git
is Apple git 2.50.1 at `/usr/bin/git`, which supports the
`reference-transaction` hook (>=2.28) and reads system config at
`/etc/gitconfig` (verified empirically; the file does not pre-exist).
`git-filter-repo` (user-owned) was preserved. Side effect observed:
already-running Claude Code sessions cached the deleted git path for
internal calls until restart (harmless; shell git re-resolves).

## 1. What v1.1 adds

Three components, one honest re-statement:

- **D (daemon)** — LaunchDaemon auto-refresh, closing R4 (refresh lag)
  and making the registry/denyWrite/includeIf surfaces self-maintaining.
- **H (hook)** — a root-owned git `reference-transaction` hook, scoped to
  adopted repos via rendered `includeIf`, closing R1 (raw ref plumbing
  against a sibling worktree's branch) for the normal git path.
- **S (status/health)** — `warden status` gains daemon health and
  registry age; refresh logging is capped.
- **R3 re-statement** — H closes the git-shaped half of R3; arbitrary
  Bash file writes into sibling worktrees from a root-cwd session remain
  audit-only (§6).

## 2. D — LaunchDaemon auto-refresh (G2)

`/Library/LaunchDaemons/com.warden.refresh.plist`, root-owned 0644:

- `ProgramArguments`: `["/usr/local/bin/warden", "refresh"]`.
- `WatchPaths`: `[<scan dir>]` (default `/Users/<user>/claude`) — a new
  clone or deleted repo triggers a refresh within seconds.
- `StartInterval`: 900 — fallback bound when WatchPaths misses (e.g.
  changes inside an existing repo such as a branch switch, which
  WatchPaths on the parent does not see).
- `ThrottleInterval`: 30 — coalesces bursts (a clone touches the watch
  path many times).
- `EnvironmentVariables`: explicit `PATH=/usr/bin:/bin:/usr/sbin:/sbin`
  (daemons inherit no shell PATH; this pins the git the renderer uses)
  and `WARDEN_SCAN_DIR` if the installer was given one.
- `StandardOutPath`/`StandardErrorPath`:
  `/Library/Application Support/ClaudeCode/warden/refresh.log`.

`warden refresh` (the existing subcommand) gains:

- Log cap: before running, if `refresh.log` exceeds 512 KiB, truncate to
  its last 64 KiB (tail-preserving).
- Health drop: on success write
  `/Library/Application Support/ClaudeCode/warden/last-refresh.json`
  (`{"ok": true, "ts": ..., "repos": N, "deny": N}`); on failure write
  `{"ok": false, "ts": ..., "error": "..."}` and exit nonzero. Written
  atomically (same tmp+rename discipline as render.py).
- Renders the includeIf surface (§3) in addition to settings + registry.

Install: `launchctl bootout system/com.warden.refresh 2>/dev/null || true`
then `launchctl bootstrap system <plist>` — idempotent on re-run.
Uninstall: bootout + delete plist.

Decision record — WatchPaths+interval over interval-only: contested at
panel (Hightower vs simplicity); chosen because it simultaneously closes
R4 and neutralizes the one real argument for machine-global hook
delivery (new-clone lag), enabling the lower-blast-radius includeIf
design in §3.

## 3. H — reference-transaction hook (R1 closure)

### Mechanism (prototype-validated on git 2.50.1, 2026-07-16)

Git runs the `reference-transaction` hook inside every ref update; in
the `prepared` state a nonzero exit aborts the transaction. Unlike the
sandbox (global, path-shaped), the hook knows both the ref being moved
and the worktree it is being moved from — exactly the discrimination R1
needs and denyWrite cannot express. Validated semantics:

| scenario | result |
|---|---|
| owner commit on its own branch | passes |
| `update-ref refs/heads/<sibling-branch>` from another worktree | aborted |
| sibling worktree commit on its own branch | passes |
| remote-tracking ref update | passes (policy ignores non-`refs/heads/`) |
| branch create / delete (not checked out elsewhere) | passes |

### Delivery: rendered includeIf, not global hooksPath

Panel-contested; decided for includeIf. `/etc/gitconfig` receives
exactly one warden-owned line pair at install:

```
[include]
	path = /Library/Application Support/ClaudeCode/warden/warden.gitconfig
```

If `/etc/gitconfig` pre-exists (it currently does not), install appends
only that include and uninstall removes only it; the file's other
content is never touched. `warden.gitconfig` is rendered at every
refresh (atomic write), one stanza per adopted repo:

```
[includeIf "gitdir:<repo-root>/"]
	path = /Library/Application Support/ClaudeCode/warden/hookpath.gitconfig
```

`hookpath.gitconfig` contains only
`core.hooksPath = .../warden/githooks`. The trailing `/` on the gitdir
pattern makes it match the repo's `.git` and every linked worktree's
gitdir (`<repo>/.git/worktrees/<name>`), so both root-cwd and worktree
sessions are governed. Repos outside the registry — and every non-repo
git invocation on the machine — are untouched.

Decision record: global `core.hooksPath` would be lag-free for brand-new
clones but takes over hook dispatch for every repo and every user
process on the machine (silent R5 scope creep, flagged by panel). With
§2's WatchPaths refresh, the includeIf lag window is seconds; blast
radius wins.

### The hooks directory

`/Library/Application Support/ClaudeCode/warden/githooks/`, root-owned:

- `reference-transaction` — the policy script (bash). Behavior:
  - States other than `prepared`: drain stdin, exit 0.
  - One `git worktree list --porcelain` per transaction (not per ref),
    parsed into branch->worktree map; detached worktrees produce no
    `branch` line and are never matched (no false positive).
  - For each stdin line whose ref is `refs/heads/<b>`: if `<b>` is
    checked out in a worktree other than the invoking one
    (`git rev-parse --show-toplevel`), record a deny.
  - Any deny: append a JSONL record to the warden audit file
    (`~/.claude/warden/audit.jsonl` of the invoking user; rule `R1`),
    print a named-rule reason to stderr, exit 1 (transaction aborts).
  - **Fail open**: the entire script body runs under a trap/guard such
    that any internal error (worktree-list failure, parse error, audit
    I/O error) exits 0. Nonzero exit occurs only on positive
    classification. Rationale (panel, Nygard): a broken hook must never
    become a machine-wide git outage; the sandbox and audit remain
    below it.
- Chaining dispatchers for the standard hook names (`pre-commit`,
  `prepare-commit-msg`, `commit-msg`, `post-commit`, `pre-push`,
  `pre-rebase`, `post-checkout`, `post-merge`, `pre-auto-gc`,
  `post-rewrite`, `pre-applypatch`, `post-applypatch`, `applypatch-msg`,
  `fsmonitor-watchman`, `post-index-change`; `reference-transaction`
  chains too): each dispatcher execs `"$GIT_DIR/hooks/<name>"` when that
  file exists and is executable, else exits 0. The
  `reference-transaction` script runs its policy first, then chains.
  Within adopted repos, repo-local hooks therefore keep working.

### Honest claim (replaces v1 R1 wording)

R1 is **enforced for the normal git path**: any git >=2.28 invocation
that reads system config. Bypasses remain: `GIT_CONFIG_NOSYSTEM=1` /
`GIT_CONFIG_SYSTEM=...` env overrides; a repo-local `core.hooksPath`
(frozen-zone-denied in adopted repos' `.git/config`, but a pre-existing
setting — e.g. husky — silently wins over the include); and any
`git -c core.hooksPath=...` command-line override — observed in the
wild: Claude Code's own internal git calls pass
`-c core.hooksPath=/dev/null`, so harness-internal git operations bypass
H by construction (session Bash git, the actual R1 threat, does not).
**Bypassed invocations produce no audit record** — the audit sees
attempts through the governed path only. This is hardening + in-band
detection, not physics; the seatbelt layers of v1 are unchanged
underneath. Refresh warns (log + health file) when an adopted repo's
local config already sets `core.hooksPath`.

## 4. S — status and health

`warden status` additionally reports:

- daemon: loaded/not-loaded (`launchctl print system/com.warden.refresh`),
  plus `last-refresh.json` verdict and age (`ok 4m ago` /
  `FAILED 2h ago: <error>` / `never ran`).
- registry age (`generated_at` vs now) and repo/deny counts.
- hook delivery: whether `/etc/gitconfig` carries the warden include and
  `warden.gitconfig` exists.
- multi-git governance probe (added post-panel, 2026-07-16): for each git
  binary at the known install prefixes (`/usr/bin`, `/usr/local/bin`,
  `/opt/homebrew/bin`, `/opt/local/bin`), report GOVERNED / NOT GOVERNED /
  TOO OLD by checking its version and whether the warden include resolves
  in *that binary's* system-config chain — a Homebrew git reads its own
  prefix's etc/gitconfig, so this converts silent bypass-by-new-git into
  visible drift. Selftest T14 fails loudly on any ungoverned git.

## 5. Renderer changes

`render.py` gains `--write-gitconfig <path>` writing `warden.gitconfig`
from the same scan (atomic, same validation discipline). While scanning,
it checks each repo's `.git/config` for a local `core.hooksPath` and
records the finding in the registry (`"hookspath_override": true`) so
refresh can warn and selftest can report.

## 6. R3 — narrowed, residual re-stated

Closed by H: git-shaped writes from a root-cwd session against sibling
worktree branches (the plumbing lane). Remaining open: arbitrary
non-git Bash file writes from a root-cwd session into
`.claude/worktrees/*` under its own cwd. Alternatives considered and
rejected: guard denying all Bash at a shared root (breaks C3 read
inspection); denyWrite on the worktrees container (global — breaks
every owner session). Remains audit-only. Post-activation root-cwd
sessions stay rare-by-force (v1 §7 R3 argument stands). README/spec
residual tables updated to this wording.

## 7. Test plan (additions)

- `tests/test_hook.sh` (new; same fixture style as selftest):
  - positive: sibling `update-ref` aborts; audit record written.
  - negative (panel, Crispin — enforcement must NOT fire): owner commit;
    owner rebase; `git fetch` from a local bare remote updating many
    remote-tracking refs; branch create/delete; packed-refs case
    (`git pack-refs --all` then sibling `update-ref` still aborts, owner
    commit still passes); detached-HEAD worktree present during all of
    the above.
  - chaining: a repo-local executable `pre-commit` still runs under the
    hookpath; a failing repo-local hook still fails the commit.
  - fail-open: hook invoked with a sabotaged environment (unreadable
    audit dir, broken `git` shim for the inner worktree-list call) exits
    0 and does not block.
- selftest additions: R1 fixture probe (abort expected); real-repo
  non-destructive probe (`update-ref` no-op against a live sibling
  worktree branch must abort); daemon loaded + `last-refresh.json`
  fresh; `/etc/gitconfig` include present; degraded path — if the
  session's `git --version` < 2.28 or config chain lacks the include,
  report `R1: NOT ENFORCED` (loud verdict, not a skip).
- `tests/test_render.py` additions: includeIf rendering; hookspath
  override detection.

## 8. Install / uninstall / rollback deltas

Install additionally: writes githooks dir + dispatchers (0755), renders
`warden.gitconfig` + `hookpath.gitconfig`, adds the single include line
to `/etc/gitconfig` (creating the file if absent, appending if present,
skipping if already there), installs + bootstraps the LaunchDaemon.
Verifies: hook script executable, include resolvable
(`git config --system --get include.path` context check), daemon loaded.

Uninstall additionally: bootout + remove plist; remove the include line
from `/etc/gitconfig` (delete the file only if warden created it and
nothing else remains); remove githooks/rendered configs with the rest of
the warden dir. Restores exactly the pre-v1.1 world; v1 uninstall
semantics unchanged.

## 9. Coverage deltas

| v1 gap | v1.1 state |
|---|---|
| R1 ref plumbing vs sibling branch | closed for the governed git path (H); bypass via env/local-config/cmdline possible, not audit-visible; stated honestly |
| R3 root-cwd Bash vs sibling worktrees | git-shaped half closed (H); arbitrary file writes remain audit-only |
| R4 refresh lag | closed to seconds (WatchPaths) with 15-min worst-case fallback (D) |
| R2, R5–R7 | unchanged from v1 |

Panel synthesis 2026-07-16: recommendations 1–6 all incorporated (fail-open
hook, includeIf delivery, gitconfig write-target specified, negative
tests, status health, weakened R1 claim); recommendation 7 (stale git)
resolved by removal, §0.
