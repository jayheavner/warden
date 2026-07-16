# Warden

Enforced session isolation for concurrent Claude Code and Codex sessions on
one Mac.

When you run several coding-agent sessions at once — Claude Code, Codex, or
both — each in its own git worktree, nothing normally stops one session from
writing into another's files, or into the shared checkout they all branched
from. Warden makes each session able to write only inside its own worktree.
Every repo's shared checkout and every other session's worktree are
read-only, enforced at the OS sandbox layer, not by prose or convention.

Warden is inert until installed. Installing or removing it is a single command
per agent.

## Requirements

- macOS
- Claude Code, Codex, or both
- Python 3
- git (2.28+ for the git-ref hardening layer)

## Install

Warden governs each agent through that agent's own root-owned managed-config
layer. Install the one(s) you use:

```sh
cd ~/claude/warden
sudo ./install.sh          # Claude Code
sudo ./install-codex.sh    # Codex
```

Each installer copies Warden into that agent's config location
(`/Library/Application Support/ClaudeCode` for Claude Code, `/etc/codex` for
Codex), renders the policy from your current repo layout, and verifies the
result. Restart any sessions that were already running — a session binds its
policy at startup.

To confirm enforcement is live, start a fresh session and ask it to run the
matching self-test — `warden selftest` for Claude Code, `warden codex-selftest`
for Codex. A self-test only works from inside a session's sandboxed shell,
where Warden is active; run in a plain terminal it detects that enforcement is
off and exits without testing.

### Uninstall

```sh
sudo "/Library/Application Support/ClaudeCode/warden/uninstall.sh"   # Claude Code
sudo /etc/codex/warden/uninstall-codex.sh                           # Codex
```

## Usage

```sh
warden status                    # resolved lane per repo, daemon and refresh health
warden selftest                  # Claude Code acceptance suite (run it from a session)
warden codex-selftest            # Codex acceptance suite (run it from a session)
sudo warden refresh              # re-derive policy after cloning or restructuring repos
warden land <branch>             # integrate finished work (see Integration lanes)
warden remote add <name> <url>   # add a git remote to a shared checkout (daemon-mediated)
warden forget <repo>             # drop a learned integration lesson for a repo
```

`sudo warden refresh` re-scans your repos and regenerates the policy. A
LaunchDaemon runs it automatically when your repo layout changes, so you
rarely need to run it by hand.

## How it works

Warden enforces in layers, so no single bypass defeats it:

| Layer | Mechanism | Covers |
|---|---|---|
| Sandbox | Root-owned managed settings turn on Claude Code's native sandbox with a rendered deny-write list | Every Bash write, however addressed — cwd, `cd`, `-C`, absolute paths, redirection, subprocesses |
| Tool guard | A non-removable `PreToolUse` hook classifies write targets | `Edit` / `Write` / `NotebookEdit`, with loud deny reasons |
| Policy | Policy is derived only from on-disk repo state | Present and future repos, machine-wide |
| Audit | Unified log plus `~/.claude/warden/audit.jsonl` | Attribution: timestamp, session, cwd, tool, target, verdict, rule |

Bash writes are never judged by parsing command text — the sandbox observes
the actual filesystem operations, so obfuscated paths and subshells can't slip
past.

The table above describes Claude Code. Codex gets the same isolation through
its own managed-config layer: a root-owned `/etc/codex/requirements.toml` that
outranks all user config and delivers an equivalent deny-write policy plus a
managed `PreToolUse` hook. The path classifier is shared byte-for-byte between
the two harnesses — one renderer, two policy formats.

## Integration lanes

When a session's work is done, `warden land <branch>` integrates it and picks
the right path automatically per repo:

- **Local** — no remote: fast-forward the shared checkout.
- **Push** — remote that accepts direct pushes: push to the HEAD branch.
- **PR** — remote whose rules require review: push a branch and open a pull
  request.

Warden resolves the lane from, in order: a committed `.warden.json`
declaration, a lesson learned from the remote's own past denials, or an
inference from the repo's remote configuration. A push that the remote rejects
falls back to a pull request in the same command, and Warden remembers the
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
python3 -m unittest discover -s tests   # unit suite: classifier, hooks, renderers, lanes, daemon, Codex adapter
warden selftest                         # Claude Code end-to-end acceptance suite (run from a session)
warden codex-selftest                   # Codex end-to-end acceptance suite (run from a session)
```

`tests/lab/` holds a seatbelt-semantics lab that proves the isolation model
against multiple git versions, with recorded evidence.

## Documentation

- [Scope and limitations](docs/limitations.md) — the edges of what Warden governs
- [Session isolation](docs/session-isolation.md) — the core design: threat model, enforcement layers, evidence
- [Integration lanes](docs/integration-lanes.md) — how `warden land` resolves each repo's integration path
- [v1.1 hardening](docs/v1.1-hardening.md) — the git-ref hook, auto-refresh daemon, and their design
- [Codex port](docs/codex-port.md) — how the same isolation is delivered to Codex sessions
