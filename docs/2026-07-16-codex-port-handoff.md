# Handoff: Codex port of warden

Written 2026-07-16 by the session that built warden v1. No Codex code exists
yet — the frontier is "research done, design mapped, nothing built." Read
`docs/superpowers/specs/2026-07-15-session-isolation-design.md` first; the
Codex port reuses its invariants (I1–I3), capabilities (C1–C4), enforcement
properties (E1–E5), and 10 acceptance tests verbatim.

## Goal

Same isolation, second harness: Codex sessions on this Mac (Jay runs Codex via
the ChatGPT desktop app; recent Codex sessions had cwd inside ~/claude repos)
must be bound to their own workspace exactly like Claude Code sessions are.

## Verified facts (with how they were verified)

- The installed Codex core is `/Applications/ChatGPT.app/Contents/Resources/codex`
  (248MB, runs as a live process). String-level verification (grep -ac) on that
  exact binary found the whole managed-config machinery present:
  `/etc/codex` (2), `requirements.toml` (16), `requirements_toml_base64` (1),
  `managed_config` (22), `allow_managed_hooks_only` (5), `PreToolUse` (34),
  `hooks.json` (6), `default_permissions` (28), `sandbox_mode` (32),
  `sandbox_workspace_write` (17).
- Escalation naming in THIS build: `with_escalated_permissions` = 0 hits; the
  live strings are `require_escalated`, `with_additional_permissions`, a retry
  string "command failed; retry without sandbox?", and a "guardian approval
  action" subsystem (core/src/tools/orchestrator.rs, network_approval.rs).
  Target these, not the older doc name.
- Docs research (learn.chatgpt.com — developers.openai.com redirects there;
  cross-checked with github.com/openai/codex, tag rust-v0.107.0 era):
  - Admin layer: `/etc/codex/requirements.toml` = enforced, user cannot
    override; `managed_config.toml` = reapplied defaults; MDM domain
    `com.openai.codex` keys `requirements_toml_base64`/`config_toml_base64`
    outrank files. CLI flags LOSE to managed layers.
  - Sandbox: Seatbelt on macOS. `sandbox_mode = workspace-write` scopes writes
    to workspace + `writable_roots`; `[permissions.<name>]` profiles support
    per-path glob rules with `deny` > `write` > `read`, more-specific-wins;
    `default_permissions = "<profile>"` selects one (must NOT be combined with
    sandbox_mode/[sandbox_workspace_write]).
  - Hooks: PreToolUse blocks with a reason returned to the model; managed
    hooks defined in requirements.toml under `[hooks]` with `managed_dir`;
    `allow_managed_hooks_only = true` disables all non-admin hooks.
  - KNOWN UPSTREAM BUG (our opportunity): sandboxed worktree sessions cannot
    commit — the shared `.git/worktrees/<name>` metadata is not writable
    (issues #14338, #15505). Warden's lab-proven carve-out set fixes this:
    `.git/{objects,refs,logs,worktrees/<own>,packed-refs,FETCH_HEAD}` — see
    `tests/lab/EVIDENCE-2026-07-16.txt`.
  - Audit: `~/.codex/sessions/YYYY/MM/DD/rollout-<session-id>.jsonl`
    (community-corroborated, not primary-doc confirmed).

## Unverified — do these FIRST (in order)

1. **Runtime proof that this Codex build reads `/etc/codex/requirements.toml`.**
   Strings prove capability, not behavior. Cheapest honest test: have Jay
   `sudo` install a minimal requirements.toml containing only a harmless
   observable constraint, restart the ChatGPT app, and observe the constraint
   in a Codex session. Design the probe before asking for the sudo.
2. **Hook stdin/stdout JSON schema.** Extract from the binary (string windows
   around `PreToolUse`/`hook_event`) or the codex-rs repo. Do NOT assume it
   matches Claude Code's shape.
3. **Whether `default_permissions` can be forced from requirements.toml**, and
   the exact escalation kill-switch combination (granular approval_policy +
   managed hook deny). Doc research says approval and sandbox are independent
   axes; issue #7909 says execpolicy is NOT manageable machine-wide yet.
4. **App vs CLI config surface**: Jay's `~/.codex/config.toml` is live app
   config (model gpt-5.6-terra, plugins). Confirm the app honors the same
   precedence chain the CLI docs describe.

## Design decisions already made (with why — don't re-litigate silently)

- **No execpolicy as an enforcement layer**: it is command-TEXT matching, the
  fragility the problem statement explicitly bans; also not admin-enforceable
  yet (#7909). Filesystem + hook layers only.
- **Same disjoint-deny composition as warden v1**: deny entries (`.git/index`,
  `HEAD`, `config`, `hooks`, protected-branch trio, tracked top-level entries)
  never collide with legitimate write paths, so no reliance on profile
  precedence internals. `render.py` gains `--format codex` emitting
  requirements.toml + a permission profile from the SAME registry — one
  scanner, two policy outputs.
- **guard.py classifier is reused as-is** (it's harness-agnostic, 27 passing
  unit tests); only the hook I/O adapter is new.
- **Same evidence ladder**: nothing is "done" until a codex-selftest runs
  inside a real fresh Codex session. Build `codex-selftest` mirroring
  selftest.sh T1–T10.

## Landmines from this session

- WorktreeCreate hooks in Claude Code are GENERATIVE (must return a worktree
  path) — wiring one observationally broke `claude -w` machine-wide (fixed in
  006d5ed). Check whether Codex hook events have similar contracts before
  wiring anything.
- Seatbelt matches REAL paths: `/tmp` is a symlink to `/private/tmp`; always
  `realpath` before putting a path in any profile or deny list (cost a full
  false-red lab run).
- `ps -axeww` cannot read other processes' env on modern macOS, and grepping
  for an env var matches your own grep's argv — worthless as binding evidence.
  Use the warden audit log (`~/.claude/warden/audit.jsonl`) instead.
- Headless `claude -p` is unusable on this machine (CLI OAuth expired; also
  the zsh `claude` alias points CLAUDE_CONFIG_DIR at ~/.claude-jay — a second
  config universe). Don't build validation plans that depend on nested
  headless sessions.
- This repo's root is frozen by warden itself: **create a worktree before
  writing anything** (`git worktree add` from a root-cwd session, or the
  app's worktree flow).

## Verification state of warden v1 (what you inherit)

- Proven 2026-07-16 in real sessions: selftest 10 pass / 0 fail / 2 skips
  (T6b, T8 opt-in), escape-hatch retry blocked. Unit suite 27/27
  (`python3 -m unittest discover -s tests`). Seatbelt lab 0 failures
  (`tests/lab/EVIDENCE-2026-07-16.txt`).
- Not run this session: nothing else claimed.

## First three actions

1. Create your worktree; read the design doc and this file.
2. Extract the Codex hook JSON schema and requirements.toml handling from the
   binary/repo (unverified items 1–2 above); write findings into
   `docs/superpowers/specs/` as a Codex-port design addendum.
3. Implement `render.py --format codex` + adapter, TDD, same task structure as
   `docs/superpowers/plans/2026-07-16-warden-implementation.md`; then
   `install-codex.sh` and `codex-selftest`, and stop at Jay's sudo gate.
