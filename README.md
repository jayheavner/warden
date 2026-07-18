# Warden

Enforced session isolation for concurrent Claude Code and Codex sessions on
one Mac.

Agent sessions on one Mac are a dev team sharing one box. Dev teams solved
isolation decades ago — private branches, protected trunk, integration only
by merge — but that model assumes every dev has their own machine. Agents
don't, so worktrees play the role of branches. Warden enforces the same
discipline: a session writes only in its own worktree (its branch); other
sessions' worktrees are other devs' branches (read freely, write never);
every repo's shared checkout is trunk (read-only, moved only by
`warden land`); and a repo the session isn't pegged to is another team's
repo entirely (writes there are never legitimate). The boundary marker is
`.git`: folders inside a repo are governed territory, vanilla folders are
just the machine and stay freely writable. All of it is enforced at the OS
sandbox and hook layers, not by prose or convention — a developer is not
blocked from his own computer; he's blocked from other people's branches.

See [goals.md](docs/goals.md) for the phase-1 goal statement this design
serves.

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
sudo warden disable               # pause all enforcement machine-wide (failsafe)
sudo warden enable                # re-arm enforcement with a fresh policy render
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

## Failsafe

If Warden's policy is wrong or its refresh daemon is wedged, `sudo warden
disable` is a machine-wide pause switch that Warden itself cannot block —
it runs from a plain, unsandboxed terminal. It's sticky until you run
`sudo warden enable`; there's no timer and no auto-re-arm.

Disabling takes effect live for the tool guard, the Codex guard, and the
git reference-transaction hook. Sessions that were already running keep
their original Bash sandbox until they're restarted — a disabled session
stays sandboxed, and (symmetrically) a session started while disabled stays
unsandboxed after you re-enable, until you restart it. `sudo warden enable`
always re-derives the policy from a fresh scan rather than re-arming
whatever was rendered before the disable, since your repo layout may have
changed in the meantime.

While disabled, `warden status` reports `state: DISABLED since <date>` as
its first line and exits with status 2, so scripts can detect it. For a
wrong denial caused by stale policy, try `sudo warden refresh` first — it's
cheaper than a full disable and is Warden's first-line remediation.

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
warden doctor <path>   # who blocks a write at <path>, rule by rule, all layers
warden doctor          # enforcement state + most recent denials from the audit trail
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
- [Disable failsafe](docs/disable-failsafe.md) — the machine-wide pause switch
- [Session isolation](docs/session-isolation.md) — the core design: threat model, enforcement layers, evidence
- [Integration lanes](docs/integration-lanes.md) — how `warden land` resolves each repo's integration path
- [v1.1 hardening](docs/v1.1-hardening.md) — the git-ref hook, auto-refresh daemon, and their design
- [Codex port](docs/codex-port.md) — how the same isolation is delivered to Codex sessions
