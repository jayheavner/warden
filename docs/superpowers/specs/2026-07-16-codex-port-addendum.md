# Codex Port — Design Addendum

Date: 2026-07-16
Status: findings verified against the installed build; build plan below
Parent: 2026-07-15-session-isolation-design.md (invariants I1–I3, capabilities
C1–C4, enforcement E1–E5, acceptance tests 1–10 apply verbatim)
Target binary: `/Applications/ChatGPT.app/Contents/Resources/codex`,
version **0.144.0-alpha.4** (confirmed live: `codex doctor` reads Jay's real
`~/.codex/config.toml`, model gpt-5.6-terra).

## 1. Handoff unknowns — resolved

### U1. Does this build read `/etc/codex/requirements.toml`? — YES (loader proven live; enforcement proof at sudo gate)

Proven this session without sudo:

- `codex app-server` (stdio JSON-RPC) answered
  `configRequirements/read` with `{"requirements":null}` — the documented
  "Null if no requirements are configured (e.g. no requirements.toml/MDM
  entries)" response. The requirements load path executes in this build.
- Binary contains the exact literals `/etc/codex/requirements.toml`,
  `/etc/codex/config.toml`, `/etc/codex/managed_config.toml`, layer sources
  `system/project/mdm/session_flags/plugin/cloud_requirements/
  cloud_managed_config/legacy_managed_config_file/legacy_managed_config_mdm`,
  and runtime fallback messages ("configured value is disallowed by
  requirements; falling back to required value", "Failed to read requirements
  file", "failed to parse merged requirements").
- The TUI has a `/debug-config` command printing "Config layer stack (lowest
  precedence first)" with "Enterprise-managed config value" / "MDM value"
  markers (strings; no CLI subcommand exposes it — `codex debug` only has
  models/app-server/prompt-input).

**Activation-day probe (Jay, sudo):** install the rendered
`/etc/codex/requirements.toml`, then (a) `codex app-server` ⇒
`configRequirements/read` must echo the parsed requirements instead of null;
(b) in a Codex session, `/debug-config` must show the enterprise layer;
(c) behavioral: `codex -s danger-full-access exec 'true'` must refuse/fall
back (requirements allow only read-only/workspace-write). No app restart
ritual assumed — codex-selftest re-checks in a fresh session.

### U2. Hook stdin/stdout JSON schema — extracted verbatim from the binary

Full draft-07 JSON Schemas for every hook event are embedded in the binary
and preserved at `docs/codex-evidence/hook-schemas-0.144.0-alpha.4.txt`.

PreToolUse **input** (stdin, one JSON object): snake_case, required fields
`cwd`, `hook_event_name` ("PreToolUse"), `model`, `permission_mode`,
`session_id`, `tool_input`, `tool_name`, `tool_use_id`, `transcript_path`
(nullable), `turn_id`; optional `agent_id`, `agent_type`. This is a superset
of Claude Code's shape with the same core field names — guard.py's reader
works unchanged.

PreToolUse **output** (stdout, exit 0): camelCase
`hookSpecificOutput.{hookEventName, permissionDecision,
permissionDecisionReason, additionalContext, updatedInput}` — same wire shape
as Claude Code. **This build is deny-only**: explicit error strings reject
`permissionDecision:allow`, `permissionDecision:ask`, `decision:approve`,
`continue:false`, `stopReason`, `suppressOutput`, and require a non-empty
`permissionDecisionReason` with every deny. Alternative denial channel: exit
code 2 with the reason on stderr. SessionStart supports
`hookSpecificOutput.additionalContext` (same as Claude Code).

Consequence: **guard.py's existing `_deny` and SessionStart JSON are wire-
compatible with Codex.** The adapter's job is tool-name/tool-input mapping
and event routing, not a new protocol.

### U3. Can `default_permissions` be forced from requirements.toml? — YES

`ConfigRequirementsToml` (24 fields, from the binary) includes:
`default_permissions`, `permissions` (profile definitions inside
requirements), `allowed_permission_profiles`, `allowed_sandbox_modes`,
`allowed_approval_policies`, `allowed_approvals_reviewers`, `hooks`
(ManagedHooksRequirementsToml: `managed_dir`, `windows_managed_dir`),
`allow_managed_hooks_only`, `rules` (execpolicy), plus a filesystem
requirements surface with `deny_read`. Runtime messages confirm enforcement:
"requirements.toml default_permissions …", "requirements.toml permissions
profile …", "allowed_permission_profiles refers to undefined profile",
"Configured value for `permission_profile` is disallowed by requirements;
falling back", "`approval_policy = "never"` cannot be used because
requirements do not allow `sandbox_mode = "danger-full-access"`".

Mutual-exclusion rules (binary): `sandbox_mode`, `permission_profile`, and
`default_permissions` overrides cannot be combined — our requirements set
only `default_permissions` + profiles, so user configs that set
`sandbox_mode` lose to the managed layer (with the "disallowed by
requirements" fallback), not conflict.

### U4. Escalation kill-switch

The escalated-retry lane ("command failed; retry without sandbox?") is
governed by approval policy + sandbox mode, and this build names the request
fields `require_escalated` / `with_additional_permissions`
(AdditionalPermissionProfile: "Partial overlay used for per-command
permission requests"). Composition that closes it:

1. `allowed_sandbox_modes = ["read-only", "workspace-write"]` — kills
   `danger-full-access` machine-wide (and thereby `approval_policy="never"`
   with full access, per the binary's own error message).
2. Managed PreToolUse hook denies any shell/exec `tool_input` whose
   `require_escalated` or `with_additional_permissions` field is set —
   structural field inspection, not command-text parsing. Managed hooks are
   "trusted by policy and can't be disabled from the user hook browser".
3. execpolicy (`rules`/prefix_rules in requirements) exists in this build but
   is command-TEXT matching — stays unused (design decision from v1; #7909).

## 2. Verified config grammar (binary structs + vendor docs)

Permission profiles: `[permissions.<name>]` with `description`, `extends`
(built-ins are addressable as `:workspace` etc. — "Identifier from
`default_permissions` or the implicit built-in default, such as `:workspace`
or a user-defined `[permissions.<id>]` profile"), `workspace_roots`,
`filesystem`, `network`. Filesystem is a map of path/glob → access, or an
`entries` list; access enum `FileSystemAccessMode = read | write | deny`
("none" is a legacy alias). Special roots: `:project_roots`, `:tmpdir`,
`:slash_tmp` (binary kinds: root, minimal, project_roots, subpath, tmpdir,
slash_tmp). Precedence: deny > write > read, more-specific-wins — but as in
v1, warden's deny set is disjoint from legitimate write paths except the
deliberate protected-branch trio inside the `.git/refs|logs` carve-outs,
where deny-wins is guaranteed by both rules.

Managed hooks in requirements.toml (vendor doc, corroborated by binary
structs HooksFile/HookEventsToml/HookHandlerConfig::Command
{command, command_windows, timeout, async, statusMessage} + matcher groups):

```toml
[hooks]
managed_dir = "/etc/codex/warden"        # must be absolute and exist

[[hooks.PreToolUse]]
matcher = ""                              # empty/omitted = all tools
[[hooks.PreToolUse.hooks]]
type = "command"
command = "python3 /etc/codex/warden/codex_guard.py"
timeout = 30
```

`allow_managed_hooks_only` stays **false** in v1 of the port: it would
no-op Jay's own user/project hooks and plugins; warden's hooks are managed
(non-removable) either way. Revisit as a hardening flag.

## 3. Architecture delta vs warden v1

Same three layers, new delivery surface. One scanner (`render.py`), two
policy outputs.

```
/etc/codex/                                (root:wheel after install)
  requirements.toml        L1  rendered: default_permissions + [permissions.warden]
                               + allowed_* clamps + managed [hooks]
  warden/
    codex_guard.py         L2  Codex I/O adapter (new, thin)
    guard.py               L2  classifier — byte-identical copy of warden v1's
    registry.json          L3  same schema as v1's
    codex-selftest         L5  acceptance suite for Codex sessions
    uninstall-codex.sh         full rollback
~/claude/warden/               versioned source + install-codex.sh
```

**L1 — `[permissions.warden]` (the wall).** `extends = ":workspace"`
inherits workspace-write semantics: each session writes its own project root
(its worktree) + tmp; siblings and other repos are out of scope (I1, I3).
On top, rendered from the same registry scan as v1:

- write carve-outs per adopted repo (fixes upstream #14338/#15505 — sandboxed
  worktree sessions can't commit because shared `.git/worktrees/<name>` isn't
  writable; warden's lab-proven set): `.git/objects/**`, `.git/refs/**`,
  `.git/logs/**`, `.git/worktrees/**`, `.git/packed-refs`,
  `.git/packed-refs.lock`, `.git/FETCH_HEAD`.
- deny frozen zone per repo (identical to v1's denyWrite): `.git/index`,
  `.git/HEAD`, `.git/config`, `.git/hooks/**`, `.git/info/**`, protected-
  branch trio for the checkout's HEAD branch, every top-level tracked entry,
  `.claude/settings.json`, plus `.codex/**` at the repo root (Codex's own
  project-config tamper surface — the Codex analog of root
  `.claude/settings.json`).
- machine entries: deny `/etc/codex/**` (test 10 belt; root ownership is the
  wall) and deny `~/.codex/config.toml` + `~/.codex/hooks.json`? — NO: those
  are Jay's user config, out of scope by R5/problem statement; sessions
  editing their own user config is governed by requirements precedence, which
  managed layers win regardless.

Differences from v1 acknowledged: worktree carve-outs here are glob-wide
(`.git/worktrees/**`, all worktrees) rather than per-own-worktree, because a
static machine-wide file cannot know each session's identity. Sibling
worktree *working trees* are still unwritable (outside project roots); their
shared `.git/worktrees/<sib>` metadata is writable from a repo-root-cwd
session — same residual class as v1's R3, audited.

**L2 — codex_guard.py (the judgment).** Imports `classify()` from the
byte-identical guard.py (27 passing unit tests, harness-agnostic). New logic
is only:

- tool-name/tool-input mapping: Codex sessions mutate files via `apply_patch`
  (FileChange items carry paths) and shell tools, not Edit/Write. The adapter
  extracts candidate paths structurally from `tool_input` (recursive scan for
  path-shaped fields and FileChange maps), classifies each, denies on any
  positive verdict. Shell/exec tools: audit-only (the sandbox owns Bash) —
  EXCEPT escalation fields (§1 U4), which deny.
- deny-only wire discipline (§1 U2); non-deny = print nothing, exit 0.
- audit records gain `"harness": "codex"` in the same
  `~/.claude/warden/audit.jsonl` + `logger -t warden` streams (E5, one audit
  trail for both harnesses).
- SessionStart: same additionalContext announcement.

Failure mode unchanged: allow-and-audit on internal error; the sandbox
remains the wall.

**L3 — renderer.** `render.py --format codex` renders requirements.toml from
the same `scan_repos()` truth. `sudo warden refresh` refreshes both harness
policies in one invocation once install-codex.sh has run (marker: /etc/codex
/warden exists).

**L5 — codex-selftest.** Mirrors selftest.sh T1–T10, run inside a fresh
Codex session; probes are filesystem-truth, harness-independent (sentinel
writes, `git -C`, update-ref no-op). Session-binding indicator: Codex has no
managed env injection analog to WARDEN_ACTIVE, so the selftest verifies
activation directly — `/etc/codex/requirements.toml` exists AND a probe deny
actually fires (T1) — and additionally asks the session to confirm the
SessionStart announcement appeared.

## 4. Coverage deltas and residuals (beyond v1's R1–R7)

- **R8 (new): profile-grammar drift.** The map-form filesystem grammar and
  `:workspace` extends are corroborated by binary structs + vendor docs but
  not yet executed on this machine. Bounded by: install-codex.sh validates
  the rendered TOML parses (tomllib) and codex-selftest T1 fails loudly if
  the profile didn't take. This is the same tier-2→tier-3 ladder v1 used.
- **R9 (new): deny-only hooks can't rewrite.** No updatedInput lane in this
  build; fine — warden never rewrites.
- **R10 (new): `~/.codex/config.toml` remains user-writable** (out of
  problem-statement scope, R5) — but requirements outrank it by design, so
  session edits to it cannot lift enforcement; they can only break the
  session's own comfort settings. Audited via hook layer.

## 4b. Integration lane: `warden land` (policy change, 2026-07-16)

Jay's standing policy (recorded in memory): sessions must be able to land
work on the shared HEAD branch with zero involvement from him — no remote,
no PR flow, personal machine. v1 conflated "protect main from
sibling/accidental mutation" with "gate integration"; only the former was
ever wanted.

Mechanism (session sandboxes still cannot write shared checkouts — that
wall is untouched): a session runs `warden land [branch]`, which drops a
JSON request into `/tmp/claude/warden-land/` (session-writable in both
harnesses) and polls for the result. A root LaunchDaemon
(`com.warden.landd`, WatchPaths + 60s interval) validates the request
against the registry and fast-forwards the shared checkout's HEAD branch,
running git demoted to the repo owner's uid so no root-owned files appear.
ff-only: diverged requests are rejected with the fix (merge the HEAD branch
into your branch in your own worktree, then re-land). Dirty shared
checkouts (tracked changes) are refused. Every landing is logged via
`logger -t warden` (E5).

Trust model: any session can advance any adopted repo's HEAD branch to any
existing local branch that fast-forwards it. That is the accepted policy on
this machine, not a hole — corruption vectors (non-ff rewrites, dirty-tree
clobbering, unregistered repos) remain closed, and sessions still cannot
touch the checkout directly.

## 5. Build plan (TDD, mirroring 2026-07-16-warden-implementation.md)

1. `tests/test_render_codex.py` — codex-format rendering: profile contains
   carve-outs/denies per fixture repo, parses as TOML, `--check` writes
   nothing, atomic write, hooks block present, allowed_sandbox_modes clamp.
2. `render.py --format codex` (+ `templates/requirements.base.toml`).
3. `tests/test_codex_guard.py` — adapter: path extraction from apply_patch
   shapes, deny JSON wire format (deny-only, non-empty reason), escalation
   deny, audit records with harness=codex, SessionStart additionalContext,
   fail-open on bad stdin.
4. `codex_guard.py`.
5. `install-codex.sh` (sudo; idempotent; validates rendered TOML; chowns
   root:wheel) + `uninstall-codex.sh` + `bin/warden` gains codex awareness
   (refresh both, `warden codex-selftest`).
6. `codex-selftest` (T1–T10 analog). Activation and the runtime probe (§1 U1)
   remain at Jay's sudo gate; nothing is "done" until codex-selftest passes
   in a real fresh Codex session.
