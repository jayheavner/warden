# warden

Enforced session isolation for concurrent Claude Code sessions on one Mac.

When you run several Claude Code sessions at once, each in its own git
worktree, nothing normally stops one session from writing into another's
files — or into the shared checkout they all branched from. warden makes each
session able to write only inside its own worktree. Every repo's shared
checkout and every other session's worktree are read-only, enforced at the OS
sandbox layer, not by prose or convention.

warden is inert until installed. One command installs it, one removes it.

## Requirements

- macOS
- Claude Code
- Python 3
- git (2.28+ for the git-ref hardening layer)

## Install

```sh
cd ~/claude/warden
sudo ./install.sh
```

The installer copies warden into `/Library/Application Support/ClaudeCode`,
renders the policy from your current repo layout, and verifies the result.
Restart any Claude Code sessions that were already running — a session binds
its policy at startup.

To confirm enforcement is live, start a fresh Claude Code session and ask it
to run `warden selftest`. The test only works from inside a session's
sandboxed shell, where warden is active — run in a plain terminal it detects
that enforcement is off and exits without testing.

### Uninstall

```sh
sudo "/Library/Application Support/ClaudeCode/warden/uninstall.sh"
```

## Usage

```sh
warden status              # resolved lane per repo, daemon and refresh health
warden selftest            # acceptance suite; ask a Claude Code session to run it
sudo warden refresh        # re-derive policy after cloning or restructuring repos
warden land <branch>       # integrate finished work (see Integration lanes)
warden forget <repo>       # drop a learned integration lesson for a repo
```

`sudo warden refresh` re-scans your repos and regenerates the policy. A
LaunchDaemon runs it automatically when your repo layout changes, so you
rarely need to run it by hand.

## How it works

warden enforces in layers, so no single bypass defeats it:

| Layer | Mechanism | Covers |
|---|---|---|
| Sandbox | Root-owned managed settings turn on Claude Code's native sandbox with a rendered deny-write list | Every Bash write, however addressed — cwd, `cd`, `-C`, absolute paths, redirection, subprocesses |
| Tool guard | A non-removable `PreToolUse` hook classifies write targets | `Edit` / `Write` / `NotebookEdit`, with loud deny reasons |
| Policy | Policy is derived only from on-disk repo state | Present and future repos, machine-wide |
| Audit | Unified log plus `~/.claude/warden/audit.jsonl` | Attribution: timestamp, session, cwd, tool, target, verdict, rule |

Bash writes are never judged by parsing command text — the sandbox observes
the actual filesystem operations, so obfuscated paths and subshells can't slip
past.

## Integration lanes

When a session's work is done, `warden land <branch>` integrates it and picks
the right path automatically per repo:

- **Local** — no remote: fast-forward the shared checkout.
- **Push** — remote that accepts direct pushes: push to the HEAD branch.
- **PR** — remote whose rules require review: push a branch and open a pull
  request.

warden resolves the lane from, in order: a committed `.warden.json`
declaration, a lesson learned from the remote's own past denials, or an
inference from the repo's remote configuration. A push that the remote rejects
falls back to a pull request in the same command, and warden remembers the
lesson so it goes straight to a PR next time. `warden forget <repo>` clears
that memory; `warden status` shows each repo's resolved lane and why.

## Audit queries

```sh
tail -f ~/.claude/warden/audit.jsonl
log show --last 1h --predicate 'eventMessage CONTAINS "warden"'
log show --last 1h --predicate 'sender == "Sandbox"'   # raw sandbox denials
```

## Testing

```sh
python3 -m unittest discover -s tests   # unit suite: classifier, hook, renderer, lanes, daemon
warden selftest                         # end-to-end acceptance suite; ask a Claude Code session to run it
```

`tests/lab/` holds a seatbelt-semantics lab that proves the isolation model
against multiple git versions, with recorded evidence.

## Documentation

- [Known limitations](docs/limitations.md) — the edges of the threat model
- [Design specs](docs/superpowers/specs/) — full requirements and derivation
