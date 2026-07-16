# warden

Enforced session isolation for concurrent Claude Code sessions on one Mac.
Sessions can write only inside their own git worktree; every repo's shared
checkout and every other session's worktree are read-only — enforced at the
OS sandbox layer and the tool layer, not by prose. Inert until installed;
one command installs, one removes.

Design and full requirements mapping:
[docs/superpowers/specs/2026-07-15-session-isolation-design.md](docs/superpowers/specs/2026-07-15-session-isolation-design.md)

## Install / rollback

```
cd ~/claude/warden
sudo ./install.sh        # copies to /Library, renders policy, verifies
# restart running clones (sessions bind at start)
# in a fresh worktree session:  warden selftest
sudo warden refresh      # after cloning new repos / repo layout changes
sudo "/Library/Application Support/ClaudeCode/warden/uninstall.sh"   # full rollback
```

## How it enforces

| Layer | Mechanism | What it covers |
|---|---|---|
| L1 wall | managed-settings.json (root-owned, absolute precedence): native sandbox on, fail-closed, escape hatch dead; rendered `denyWrite` list disjoint from all legitimate write paths | every Bash write, however addressed (cwd, `cd`, `-C`, absolute paths, redirection, subprocesses) |
| L2 judgment | guard.py PreToolUse hook (non-removable): path classifier | Edit/Write/NotebookEdit targets; loud deny reasons naming I2/I3/E3 |
| L3 truth | render.py: registry + policy derived only from disk state | machine-wide adoption of present and future repos |
| L5 audit | unified log (`logger -t warden`) + `~/.claude/warden/audit.jsonl` | attribution: ts, session_id, cwd, tool, target, verdict, rule |

Bash is never judged by parsing command text — the sandbox sees the actual
filesystem operations.

## Audit queries

```
tail -f ~/.claude/warden/audit.jsonl
log show --last 1h --predicate 'eventMessage CONTAINS "warden"'
log show --last 1h --predicate 'sender == "Sandbox"'   # raw EPERM denials
```

## Residual gaps (from the design doc, §7)

- R1: raw ref plumbing (`update-ref`) against a *sibling worktree's* branch is
  not FS-blockable (shared refs); the shared checkout's HEAD branch IS blocked.
  Audited; v1.1 can render per-worktree protected refs.
- R2: root-cwd sessions can create new untracked top-level files at a shared
  root (litter, not corruption).
- R3: root-cwd sessions' Bash vs. live sibling worktrees — file tools are
  guard-denied; Bash residual until v1.1. Root sessions can't do normal work
  anyway (tracked tree frozen), so they are rare-by-force.
- R4: repos cloned after the last `sudo warden refresh` are unprotected against
  root-cwd sessions in them until the next refresh.
- R5: non-Claude processes are out of scope (the constrained population is
  Claude Code sessions).
- R6: sessions already running at install time bind on restart.
- R7: no network egress restrictions in v1.

## Evidence

- `tests/` — 27 unit tests (classifier, hook contract, renderer) + installer
  dry-run suite: `python3 -m unittest discover -s tests`.
- `tests/lab/derive.sh` — seatbelt semantics lab proving the isolation model
  under git 2.22 and 2.50 (14 legitimate ops pass, 13 violations block);
  output kept in `tests/lab/EVIDENCE-2026-07-16.txt`.
- `warden selftest` — activation-day acceptance suite (T1–T10), run inside a
  real fresh session.
