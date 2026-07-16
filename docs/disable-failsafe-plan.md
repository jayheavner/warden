# Warden Disable Failsafe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `sudo warden disable` / `sudo warden enable` — a sticky, machine-wide enforcement pause that Warden itself cannot block, with live effect on the hook layers and loud in-conversation visibility.

**Architecture:** A root-owned sentinel file (`$WD/DISABLED`) is the single source of truth. `guard.py`, `codex_guard.py`, and the git reference-transaction hook check it live on every invocation. `render.py --disabled` produces a valid managed-settings render with the sandbox off but all hooks kept, so new sessions get the disabled banner. `bin/warden` gains `disable`/`enable` subcommands with verify-or-rollback transitions; every intermediate state fails safe (enforcing, or reporting disabled).

**Tech Stack:** Python 3 (stdlib only), bash, launchd, existing unittest + shell-test suites.

Spec: `docs/disable-failsafe.md`. Read it before starting.

## Global Constraints

- Sentinel path: `/Library/Application Support/ClaudeCode/warden/DISABLED`; test override env var `WARDEN_SENTINEL`.
- Fail safe: sentinel missing, a directory, unreadable, or bad JSON ⇒ treated as **enabled** (enforcing).
- Banner texts are exact (tests assert on them verbatim):
  - Session start (disabled): `⚠ Warden enforcement is DISABLED (since {ts}). Session isolation is off. Re-enable with: sudo warden enable`
  - Guard first-tool-call addendum: `Bash writes in sessions started before the disable are still sandboxed until those sessions restart.`
  - Git hook (disabled): `warden: disabled — ref protection off`
  - `warden status` first line (disabled): `state: DISABLED since {ts} (sudo warden enable to re-arm)` and status exits 2.
- `com.warden.landd` is never touched by disable/enable; only `com.warden.refresh` is booted out/in.
- `enable` always runs a full fresh refresh (both harnesses); it never re-arms a stale render.
- Audit events `{"event": "disable"|"enable", ...}` appended to the user's `~/.claude/warden/audit.jsonl` (respect `WARDEN_AUDIT_FILE`; under sudo use `/Users/$SUDO_USER`).
- All Python is stdlib-only; match existing code style (no type annotations, 4-space indent, files stay under ~300 lines — `bin/warden` is already over, add compactly).
- Test mode: when `WARDEN_DEST` is set, root is not required and `launchctl` calls are skipped — the same convention `refresh` already uses.

---

### Task 1: Sentinel helpers + disabled behavior in `guard.py`

**Files:**
- Modify: `guard.py` (helpers after `MANAGED_ROOT_DEFAULT`, branches in `main()`)
- Test: `tests/test_guard_main.py` (append a test class)

**Interfaces:**
- Produces: `guard.sentinel_path(managed_root=MANAGED_ROOT_DEFAULT) -> str`; `guard.disabled_since(managed_root=MANAGED_ROOT_DEFAULT) -> str|None` (ISO timestamp when disabled, `None` when enforcing). Task 4 (CLI) and Task 5 (codex) rely on these names.
- Consumes: nothing new.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_guard_main.py`:

```python
class TestDisabled(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.audit = os.path.join(self.tmp.name, "audit.jsonl")
        self.sentinel = os.path.join(self.tmp.name, "DISABLED")
        json.dump({"disabled_at": "2026-07-16T10:00:00-04:00", "by_uid": 0},
                  open(self.sentinel, "w"))
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(os.path.join(self.repo, ".git"))
        self.wt = os.path.join(self.repo, ".claude", "worktrees", "w1")
        os.makedirs(self.wt)
        open(os.path.join(self.wt, ".git"), "w").write("gitdir: x\n")
        self.notify_dir = os.path.join(self.tmp.name, "notified")

    def tearDown(self):
        self.tmp.cleanup()

    def run_disabled(self, payload_dict):
        env = dict(os.environ, WARDEN_AUDIT_FILE=self.audit,
                   WARDEN_NO_SYSLOG="1", WARDEN_SENTINEL=self.sentinel,
                   WARDEN_NOTIFY_DIR=self.notify_dir)
        return subprocess.run(["python3", GUARD],
                              input=json.dumps(payload_dict),
                              capture_output=True, text=True, env=env)

    def test_foreign_edit_permitted_with_banner_once(self):
        p = payload("Edit", {"file_path": os.path.join(self.repo, "a.md")},
                    self.wt)
        r1 = self.run_disabled(p)
        self.assertEqual(r1.returncode, 0)
        out = json.loads(r1.stdout)
        self.assertNotIn("hookSpecificOutput", out)      # no deny
        self.assertIn("Warden enforcement is DISABLED", out["systemMessage"])
        self.assertIn("still sandboxed until those sessions restart",
                      out["systemMessage"])
        rec = [json.loads(l) for l in open(self.audit)][-1]
        self.assertEqual(rec["verdict"], "disabled-allow")
        r2 = self.run_disabled(p)                        # banner only once
        self.assertEqual(r2.stdout.strip(), "")

    def test_session_start_banner(self):
        r = self.run_disabled(payload("", {}, self.wt, event="SessionStart"))
        ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("⚠ Warden enforcement is DISABLED (since "
                      "2026-07-16T10:00:00-04:00)", ctx)
        self.assertIn("sudo warden enable", ctx)

    def test_sentinel_anomalies_mean_enabled(self):
        for spoil in ("dir", "badjson"):
            os.remove(self.sentinel) if os.path.isfile(self.sentinel) else None
            if spoil == "dir":
                os.mkdir(self.sentinel)
            else:
                os.rmdir(self.sentinel)
                open(self.sentinel, "w").write("not json")
            r = self.run_disabled(payload(
                "Edit", {"file_path": os.path.join(self.repo, "a.md")},
                self.wt))
            hso = json.loads(r.stdout)["hookSpecificOutput"]
            self.assertEqual(hso["permissionDecision"], "deny",
                             "anomaly %s must fail safe" % spoil)
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_guard_main.TestDisabled -v` (from repo root)
Expected: FAIL — `KeyError: 'systemMessage'` / deny still emitted.

- [ ] **Step 3: Implement in `guard.py`.** After the `Verdict` definition add:

```python
DISABLED_BANNER = ("⚠ Warden enforcement is DISABLED (since %s). Session "
                   "isolation is off. Re-enable with: sudo warden enable")
DISABLED_ADDENDUM = ("Bash writes in sessions started before the disable "
                     "are still sandboxed until those sessions restart.")


def sentinel_path(managed_root=MANAGED_ROOT_DEFAULT):
    return (os.environ.get("WARDEN_SENTINEL")
            or os.path.join(managed_root, "warden", "DISABLED"))


def disabled_since(managed_root=MANAGED_ROOT_DEFAULT):
    """ISO timestamp if warden is disabled, else None. Any anomaly —
    directory, unreadable, bad JSON — reads as enabled (fail safe)."""
    p = sentinel_path(managed_root)
    try:
        if not os.path.isfile(p):
            return None
        return str(json.load(open(p))["disabled_at"])
    except (OSError, ValueError, KeyError):
        return None


def _notify_once(sid, since):
    """Emit the disabled banner at most once per session."""
    d = (os.environ.get("WARDEN_NOTIFY_DIR")
         or os.path.expanduser("~/.claude/warden/notified"))
    mark = os.path.join(d, sid or "no-session")
    try:
        if os.path.exists(mark):
            return
        os.makedirs(d, exist_ok=True)
        open(mark, "w").write(since + "\n")
    except OSError:
        return
    print(json.dumps({"systemMessage":
                      DISABLED_BANNER % since + " " + DISABLED_ADDENDUM}))
```

In `main()`, compute `since = disabled_since()` right after `base = ...`, then:
- In the `PreToolUse`/`FILE_TOOLS` branch: when `since`, audit `verdict="disabled-allow", rule=""` (still record the target), call `_notify_once(sid, since)`, and skip classification entirely.
- In the `Bash` branch: when `since`, keep the audit line but set `verdict="disabled-audit"` and call `_notify_once(sid, since)`.
- In the `SessionStart` branch: when `since`, audit `verdict="session-start-disabled"` and print `additionalContext` = `DISABLED_BANNER % since` instead of the active-enforcement text.

- [ ] **Step 4: Run tests**

Run: `python3 -m unittest tests.test_guard_main -v`
Expected: all PASS (old classes included — enabled behavior unchanged).

- [ ] **Step 5: Commit** — `git add guard.py tests/test_guard_main.py && git commit -m "feat(guard): live disabled mode via root-owned sentinel"`

---

### Task 2: `render.py --disabled` for both formats

**Files:**
- Modify: `render.py` (`main()` argparse + the two render paths)
- Test: `tests/test_render.py`, `tests/test_render_codex.py` (append one test each)

**Interfaces:**
- Produces: CLI flag `--disabled`. Claude format: rendered settings with `sandbox.enabled == False`, `sandbox.failIfUnavailable == False`, everything else (denyWrite list, permissions, hooks, env) identical to the enforcing render. Codex format: the `[permissions.warden.filesystem]` table contains **only** the managed-root deny rule (`"<managed_root>/**" = "deny"`), keeping the switch itself protected.
- Consumes: nothing new.

- [ ] **Step 1: Failing tests.** In `tests/test_render.py` (match the file's existing fixture style for building a fake repo tree and base file; the assertion core):

```python
def test_disabled_render_flips_sandbox_only(self):
    on = self.render()                       # existing helper/pattern
    off = self.render(extra_args=["--disabled"])
    self.assertFalse(off["sandbox"]["enabled"])
    self.assertFalse(off["sandbox"]["failIfUnavailable"])
    off["sandbox"]["enabled"] = on["sandbox"]["enabled"]
    off["sandbox"]["failIfUnavailable"] = on["sandbox"]["failIfUnavailable"]
    self.assertEqual(on, off)                # nothing else changed
```

In `tests/test_render_codex.py`:

```python
def test_disabled_codex_keeps_only_managed_root_rule(self):
    text = self.render_text(extra_args=["--disabled"])   # existing pattern
    import tomllib
    rules = tomllib.loads(text)["permissions"]["warden"]["filesystem"]
    self.assertEqual(list(rules.values()), ["deny"])
    (path,) = rules.keys()
    self.assertTrue(path.endswith("/**"))
```

If those files have no such helper, write the test with an inline `subprocess` call to `render.py --check --disabled` mirroring how the existing tests in the same file invoke the renderer — copy their invocation verbatim and add the flag.

- [ ] **Step 2: Run to verify failure** — `python3 -m unittest tests.test_render tests.test_render_codex -v` → FAIL (`unrecognized arguments: --disabled`).

- [ ] **Step 3: Implement.** In `main()` add `ap.add_argument("--disabled", action="store_true")`. In the codex path, pass it through: change `codex_fs_rules(repos, a.managed_root)` call sites to `codex_fs_rules(repos, a.managed_root, disabled=a.disabled)` and:

```python
def codex_fs_rules(repos, managed_root, disabled=False):
    rules = {managed_root.rstrip("/") + "/**": "deny"}
    if disabled:
        return rules
    ...  # existing body unchanged
```

(`render_codex_requirements` gains and forwards the same keyword.) In the claude path, after `settings = render_settings(...)`:

```python
if a.disabled:
    settings["sandbox"]["enabled"] = False
    settings["sandbox"]["failIfUnavailable"] = False
```

- [ ] **Step 4: Run** — `python3 -m unittest tests.test_render tests.test_render_codex -v` → PASS.
- [ ] **Step 5: Commit** — `git commit -am "feat(render): --disabled mode, sandbox off with hooks kept"`

---

### Task 3: Git reference-transaction hook honors the sentinel

**Files:**
- Modify: `githooks/reference-transaction`
- Test: `tests/test_hook.sh` (append a case, following the file's existing harness)

**Interfaces:**
- Consumes: sentinel path convention; env override `WARDEN_SENTINEL` (same as guard.py).
- Produces: nothing downstream.

- [ ] **Step 1: Failing test.** Append to `tests/test_hook.sh`, reusing its existing fixture (worktree checked out elsewhere, prepared-state stdin that today yields the R1 deny). New case: export `WARDEN_SENTINEL` pointing at a temp file containing `{"disabled_at":"x","by_uid":0}`; run the hook the same way; assert exit 0 and stderr contains exactly `warden: disabled — ref protection off`; then unset and assert the deny still fires (regression).

- [ ] **Step 2: Run to verify failure** — `bash tests/test_hook.sh` → new case FAILS (hook still denies).

- [ ] **Step 3: Implement.** In `githooks/reference-transaction`, after the `input=` line:

```bash
sentinel="${WARDEN_SENTINEL:-/Library/Application Support/ClaudeCode/warden/DISABLED}"
warden_disabled=""
[ -f "$sentinel" ] && warden_disabled=1
```

Change the classification gate `if [ "$state" = "prepared" ]; then` to:

```bash
if [ "$state" = "prepared" ] && [ -z "$warden_disabled" ]; then
```

And just before the chain-to-repo-hook block:

```bash
if [ -n "$warden_disabled" ] && [ "$state" = "prepared" ]; then
  printf 'warden: disabled — ref protection off\n' >&2
fi
```

(Chaining to the repo's own hook still happens — disable pauses Warden, not the repo's hooks.)

- [ ] **Step 4: Run** — `bash tests/test_hook.sh` → all cases PASS.
- [ ] **Step 5: Commit** — `git commit -am "feat(githook): sentinel check, one-line notice when disabled"`

---

### Task 4: `warden disable` / `warden enable` / status integration in `bin/warden`

**Files:**
- Modify: `bin/warden` (two new subcommand cases, status header, usage line)
- Test: `tests/test_disable_cli.sh` (new; model it on `tests/test_warden_refresh.sh`, which already exercises `bin/warden` with `WARDEN_DEST` pointing at a temp tree)

**Interfaces:**
- Consumes: `render.py --disabled` (Task 2); sentinel JSON shape `{"disabled_at", "by_uid"}` (Task 1).
- Produces: exit code 2 from `warden status` when disabled; sentinel file at `$WD/DISABLED`.

- [ ] **Step 1: Failing test.** Create `tests/test_disable_cli.sh` following `test_warden_refresh.sh`'s setup (temp `WARDEN_DEST`, fake repo under a temp scan dir, `WARDEN_BASE_OVERRIDE` to the repo template, initial `warden refresh` to render). Then assert, in order:
  1. `warden disable` → exit 0; `$WD/DISABLED` exists and parses as JSON with a `disabled_at` key; `managed-settings.json` now has `sandbox.enabled == false` but still has non-empty `hooks.PreToolUse`; output contains `warden is DISABLED` and `stay sandboxed until those sessions restart`.
  2. `warden disable` again → exit 0, output contains `already DISABLED` (idempotent).
  3. `warden status` → exit 2; first status line matches `state: DISABLED since .* (sudo warden enable to re-arm)`.
  4. `warden enable` → exit 0; sentinel gone; `managed-settings.json` back to `sandbox.enabled == true` and `failIfUnavailable == true`; output mentions sessions started while disabled staying unenforced until restart.
  5. `warden enable` again → exit 0, `already` in output.
  6. Audit: `WARDEN_AUDIT_FILE` (exported in the test) contains one `"event": "disable"` and one `"event": "enable"` line.
  7. Rollback: make the render fail (point `WARDEN_BASE_OVERRIDE` at a nonexistent file), run `warden disable` → nonzero exit AND `$WD/DISABLED` absent (a failed disable must not leave the sentinel).

- [ ] **Step 2: Run to verify failure** — `bash tests/test_disable_cli.sh` → FAILS at step 1 (unknown subcommand, usage printed).

- [ ] **Step 3: Implement.** In `bin/warden`, add two cases before `status)`. Shared conventions: root check identical to `refresh`'s; `LAUNCHCTL=launchctl` skipped entirely when `WARDEN_DEST` is set; audit helper:

```bash
audit_event() {  # audit_event <disable|enable>
  AUD="${WARDEN_AUDIT_FILE:-/Users/${SUDO_USER:-$USER}/.claude/warden/audit.jsonl}"
  python3 - "$AUD" "$1" <<'PY' || true
import datetime, json, os, sys
rec = {"ts": datetime.datetime.now().astimezone().isoformat(),
       "event": sys.argv[2], "by_uid": os.getuid(),
       "tool": "warden-cli", "verdict": sys.argv[2]}
os.makedirs(os.path.dirname(sys.argv[1]), exist_ok=True)
open(sys.argv[1], "a").write(json.dumps(rec) + "\n")
PY
}
```

`disable)` case, exactly this order with rollback:

```bash
  disable)
    if [ -z "${WARDEN_DEST:-}" ] && [ "$(id -u)" -ne 0 ]; then
      echo "run: sudo warden disable" >&2; exit 1
    fi
    S="$WD/DISABLED"
    if [ -f "$S" ]; then
      echo "warden: already DISABLED (since $(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["disabled_at"])' "$S" 2>/dev/null || echo '?'))"
      exit 0
    fi
    # 1) sentinel (atomic: tmp + rename)
    python3 - "$S" <<'PY'
import datetime, json, os, sys
tmp = sys.argv[1] + ".tmp"
json.dump({"disabled_at": datetime.datetime.now().astimezone().isoformat(),
           "by_uid": int(os.environ.get("SUDO_UID") or os.getuid())},
          open(tmp, "w"))
os.replace(tmp, sys.argv[1])
PY
    [ "$(id -u)" -eq 0 ] && { chown root:wheel "$S"; chmod 0644 "$S"; }
    rollback() {
      rm -f "$S"
      "$0" refresh >/dev/null 2>&1 || true
      echo "warden disable: FAILED — rolled back, enforcement still ON" >&2
      exit 1
    }
    # 2) disabled renders (claude; codex if installed)
    SCAN_HOME="${SUDO_USER:+/Users/$SUDO_USER}"; SCAN_HOME="${SCAN_HOME:-$HOME}"
    python3 "$WD/render.py" --disabled \
      --scan "${WARDEN_SCAN_DIR:-$SCAN_HOME/claude}" \
      --base "${WARDEN_BASE_OVERRIDE:-$WD/managed-settings.base.json}" \
      --write-settings "$DEST/managed-settings.json" \
      --write-registry "$WD/registry.json" \
      --write-gitconfig "$WD/warden.gitconfig" || rollback
    CWD_CODEX="${WARDEN_CODEX_DEST:-/etc/codex}/warden"
    if [ -d "$CWD_CODEX" ]; then
      python3 "$CWD_CODEX/render.py" --format codex --disabled \
        --scan "${WARDEN_SCAN_DIR:-$SCAN_HOME/claude}" \
        --base "$CWD_CODEX/requirements.base.toml" \
        --write-settings "${WARDEN_CODEX_DEST:-/etc/codex}/requirements.toml" \
        --write-registry "$CWD_CODEX/registry.json" || rollback
    fi
    # 3) refresh daemon out (landd stays — it is a service, not enforcement)
    if [ -z "${WARDEN_DEST:-}" ]; then
      launchctl bootout "system/$DAEMON_LABEL" 2>/dev/null || true
    fi
    audit_event disable
    echo "sentinel: $S"
    echo "settings: sandbox off, hooks kept (banner active)"
    [ -d "$CWD_CODEX" ] && echo "codex: requirements.toml disabled render"
    echo "daemon: $DAEMON_LABEL booted out (com.warden.landd untouched)"
    echo "note: for stale-policy denials, 'sudo warden refresh' is the lighter fix."
    echo "warden is DISABLED. Bash writes in already-running sessions stay sandboxed until those sessions restart."
    ;;
```

`enable)` case:

```bash
  enable)
    if [ -z "${WARDEN_DEST:-}" ] && [ "$(id -u)" -ne 0 ]; then
      echo "run: sudo warden enable" >&2; exit 1
    fi
    S="$WD/DISABLED"
    if [ ! -f "$S" ]; then echo "warden: already enabled"; exit 0; fi
    SINCE="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["disabled_at"])' "$S" 2>/dev/null || echo '?')"
    rm -f "$S"
    reinstate() {
      python3 - "$S" <<'PY'
import datetime, json, os, sys
json.dump({"disabled_at": datetime.datetime.now().astimezone().isoformat(),
           "by_uid": int(os.environ.get("SUDO_UID") or os.getuid()),
           "note": "enable failed; re-disabled"}, open(sys.argv[1], "w"))
PY
      echo "warden enable: FAILED — state remains DISABLED" >&2
      exit 1
    }
    "$0" refresh || reinstate          # always a FRESH render, never stale
    python3 - "$DEST/managed-settings.json" <<'PY' || reinstate
import json, sys
d = json.load(open(sys.argv[1]))
assert d["sandbox"]["enabled"] and d["sandbox"]["failIfUnavailable"]
assert d["sandbox"]["allowUnsandboxedCommands"] is False
assert d["hooks"]["PreToolUse"]
print("policy verified: sandbox fail-closed, hooks wired,",
      len(d["sandbox"]["filesystem"]["denyWrite"]), "denyWrite entries")
PY
    if [ -z "${WARDEN_DEST:-}" ]; then
      launchctl bootstrap system /Library/LaunchDaemons/com.warden.refresh.plist 2>/dev/null || true
      launchctl print "system/$DAEMON_LABEL" >/dev/null 2>&1 || reinstate
    fi
    audit_event enable
    echo "warden is ENABLED (was disabled since $SINCE)."
    echo "note: sessions started while disabled stay unenforced until restarted."
    ;;
```

`status)` case — immediately after the `not installed` check:

```bash
    STATUS_RC=0
    if [ -f "$WD/DISABLED" ]; then
      STATUS_RC=2
      echo "state: DISABLED since $(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["disabled_at"])' "$WD/DISABLED" 2>/dev/null || echo '?') (sudo warden enable to re-arm)"
      echo "  sessions started before the disable still enforce Bash-write denials until restarted"
    fi
```

and end the case with `exit "$STATUS_RC"` (also update the usage string to `{disable|enable|refresh|...}`).

- [ ] **Step 4: Run** — `bash tests/test_disable_cli.sh` → PASS; also rerun `bash tests/test_warden_refresh.sh` (no regression).
- [ ] **Step 5: Commit** — `git commit -am "feat(cli): warden disable/enable with rollback; status reports DISABLED"`

---

### Task 5: Codex guard honors the sentinel

**Files:**
- Modify: `codex_guard.py` (its `main()`; read the whole file first — it is 120 lines)
- Test: `tests/test_codex_guard.py` (append one test mirroring that file's existing deny-case invocation)

**Interfaces:**
- Consumes: `guard.disabled_since()` (Task 1). The Codex sentinel default must be the **Claude-side** path — one machine-wide switch — so call `guard.disabled_since()` with its default; `WARDEN_SENTINEL` still overrides for tests.

- [ ] **Step 1: Failing test.** Copy the file's existing "foreign write is denied" test, set `WARDEN_SENTINEL` in the subprocess env to a temp sentinel file (same JSON as Task 1), and assert the hook now emits **no deny** and the audit record's verdict is `disabled-allow`.
- [ ] **Step 2: Run** — `python3 -m unittest tests.test_codex_guard -v` → new test FAILS.
- [ ] **Step 3: Implement.** In `codex_guard.py`'s `main()`, right after parsing stdin: `since = guard.disabled_since()`; when set, write the audit record with `verdict="disabled-allow"` and return without emitting a deny, whatever the tool. (Codex's hook format is deny-only, so "no output" is the permit; there is no banner channel here — the disabled render from Task 2 is Codex's visibility story.)
- [ ] **Step 4: Run** — `python3 -m unittest tests.test_codex_guard -v` → PASS.
- [ ] **Step 5: Commit** — `git commit -am "feat(codex): guard honors machine-wide disable sentinel"`

---

### Task 6: Selftest coverage + docs

**Files:**
- Modify: `selftest.sh` (a disabled-state section that only runs when the sentinel is present), `README.md` (Usage + How-it-works), `docs/limitations.md` (mixed-fleet note)

**Interfaces:** consumes everything above; produces nothing downstream.

- [ ] **Step 1: Selftest addition.** Read `selftest.sh` first and follow its existing assert/report helpers. Add a final section: if `$WD/DISABLED` exists, assert (a) a write into a foreign worktree path via the guard (invoke `guard.py` the way existing selftest cases do) is permitted, (b) `warden status` exits 2 and its first line starts with `state: DISABLED`, then print `disabled-state checks PASS`. If the sentinel is absent, print one line: `disabled-state: skipped (warden is enabled — run 'sudo warden disable' first to exercise the failsafe)`. The full cycle test (`disable → assert → enable → assert`) lives in `tests/test_disable_cli.sh` (Task 4) because selftest runs unprivileged inside a session.
- [ ] **Step 2: Run** — `bash selftest.sh` outside a session exits early by design; instead verify syntax with `bash -n selftest.sh` and run `python3 -m unittest discover -s tests` + all `tests/*.sh` for full regression.
- [ ] **Step 3: Docs.** README Usage block gains `sudo warden disable` / `sudo warden enable` lines with one-phrase descriptions; a short "Failsafe" subsection states: sticky until re-enabled, live for hook layers, Bash sandbox in running sessions until restart, `enable` always re-renders fresh, `warden status` exit 2 when disabled. `docs/limitations.md` gains the mixed-fleet paragraph (sessions keep the policy they started with, in both directions).
- [ ] **Step 4: Full suite** — `python3 -m unittest discover -s tests` and `for t in tests/test_*.sh; do bash "$t" || echo "FAIL $t"; done` → all PASS.
- [ ] **Step 5: Commit** — `git commit -am "test+docs: disabled-state selftest section, failsafe docs"`

---

## Self-review notes

- Spec coverage: sentinel/source-of-truth (T1), disabled renders both harnesses (T2), git hook (T3), CLI transitions with rollback + audit + status exit 2 + idempotency + refresh-first note (T4), Codex guard (T5), selftest + mixed-fleet docs (T6). Banner texts pinned in Global Constraints.
- The spec's "guard denies writes targeting the sentinel" is already satisfied structurally: the sentinel lives under the managed root, which classify() rule E3 denies and the sandbox denyWrite covers; no new code needed. Task 1's tests keep E3 intact by asserting enabled behavior is unchanged.
- Deliberate deviation from spec: no `--disabled` change to the denyWrite list (harmless with sandbox off; keeps renders diffable) — matches the "one code path" ruling.
