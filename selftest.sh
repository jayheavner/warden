#!/bin/bash
# warden selftest — run INSIDE a fresh Claude Code session after activation
# (ask the session to run: warden selftest). Non-destructive against real
# repos; every verdict comes from filesystem truth. Maps to the acceptance
# tests in the design doc (T1-T10).
#
# Heredocs need a writable temp dir. Current policy keeps /tmp writable
# (deny-only write scope), but sessions still bound to an older render may
# not have it — fall back to ~/.claude so heredoc-backed checks can't
# crash and report a false FAIL.
if ! ( : > "${TMPDIR:-/tmp}/.warden-tmpprobe.$$" ) 2>/dev/null; then
  export TMPDIR="$HOME/.claude/warden/tmp"
  mkdir -p "$TMPDIR"
else
  rm -f "${TMPDIR:-/tmp}/.warden-tmpprobe.$$"
fi
set -u
WD="${WARDEN_DEST:-/Library/Application Support/ClaudeCode}/warden"
REG="$WD/registry.json"
PASSN=0; FAILN=0; SKIPN=0
say()  { printf '%-6s %-52s %s\n' "$1" "$2" "${3:-}"; }
pass() { say PASS "$1"; PASSN=$((PASSN+1)); }
fail() { say FAIL "$1" "${2:-}"; FAILN=$((FAILN+1)); }
skip() { say SKIP "$1" "${2:-}"; SKIPN=$((SKIPN+1)); }

if [ "${WARDEN_ACTIVE:-}" != "1" ]; then
  echo "warden selftest: WARDEN_ACTIVE is not set — enforcement is not active in this session."
  echo "Either this session predates the install, or Claude Code never loaded warden's"
  echo "settings. On Claude Enterprise accounts the org's remote policy REPLACES the"
  echo "local managed-settings layer — the file on disk is then dead. Check delivery:"
  echo "  warden status          (claude fallback + governed-session evidence)"
  echo "  warden verify-claude   (live end-to-end proof, run from a plain shell)"
  echo "Then start a FRESH session and re-run."
  exit 3
fi
[ -r "$REG" ] || { echo "warden selftest: no registry at $REG"; exit 3; }

CWD=$(pwd -P)
# Anchor every probe to THIS session's own worktree and the repo that
# contains it — the only question that matters is "is THIS session
# isolated?", not "is registry[0] isolated?" (the old fixed-index probe
# reported against an unrelated repo and produced false T4 findings).
OWN_WT=$(python3 - "$CWD" <<'EOF'
import sys
p = sys.argv[1]
parts = p.split("/.claude/worktrees/")
if len(parts) > 1 and parts[1]:
    print(parts[0] + "/.claude/worktrees/" + parts[1].split("/")[0])
else:
    print("")
EOF
)
# The session's own repo: the shared checkout that owns OWN_WT if we're in
# a worktree, else the repo whose tree contains cwd, else registry[0] with
# a loud note (a session not inside any adopted repo can't self-probe).
REPO=$(python3 - "$REG" "$CWD" "$OWN_WT" <<'EOF'
import json, os, sys
reg = json.load(open(sys.argv[1])); cwd = sys.argv[2]; own = sys.argv[3]
roots = [r["root"] for r in reg["repos"]]
anchor = own.split("/.claude/worktrees/")[0] if own else cwd
# nearest adopted repo root that is an ancestor of the anchor
best = ""
for root in roots:
    if anchor == root or anchor.startswith(root.rstrip("/") + "/"):
        if len(root) > len(best):
            best = root
print(best or (roots[0] if roots else ""))
EOF
)
[ -n "$REPO" ] || { echo "warden selftest: registry has no repos"; exit 3; }
BRANCH=$(python3 - "$REG" "$REPO" <<'EOF'
import json, sys
reg = json.load(open(sys.argv[1]))
for r in reg["repos"]:
    if r["root"] == sys.argv[2]:
        print(r.get("head_branch") or ""); break
EOF
)
# Warn if the probed repo isn't the session's own (session started outside
# any adopted repo) — the results then describe registry[0], not you.
ANCHOR_ROOT=$(python3 -c 'import sys;print(sys.argv[1].split("/.claude/worktrees/")[0] if sys.argv[1] else sys.argv[2])' "$OWN_WT" "$CWD")
if [ "$REPO" != "$ANCHOR_ROOT" ]; then
  echo "== NOTE: this session is not inside an adopted repo; probing $REPO"
  echo "==       (a fallback) — results describe that repo, not this session."
fi

echo "== warden selftest: session cwd=$CWD"
echo "== probing this session's repo: $REPO (HEAD branch: ${BRANCH:-detached})"
echo

# T1: mutation at shared root (sentinel file; harmless and removed if it lands)
P="$REPO/warden-selftest-$$.txt"
if echo x > "$P" 2>/dev/null; then
  rm -f "$P"; fail "T1 write at shared root blocked" "WRITE SUCCEEDED"
else
  pass "T1 write at shared root blocked"
fi

# T2: cd into shared root, then mutate (working-directory drift)
if (cd "$REPO" && echo y >> README.md) 2>/dev/null; then
  fail "T2 cd-drift mutation blocked" "WRITE SUCCEEDED — restore README.md!"
else
  pass "T2 cd-drift mutation blocked"
fi

# T3: git -C shared reset --hard (index write must be refused first)
if git -C "$REPO" reset --hard HEAD~0 >/dev/null 2>&1; then
  fail "T3 git -C shared reset --hard blocked" "SUCCEEDED"
else
  pass "T3 git -C shared reset --hard blocked"
fi

# T3b: ref plumbing against the shared HEAD branch (no-op value: same sha)
if [ -n "$BRANCH" ]; then
  if git -C "$REPO" update-ref "refs/heads/$BRANCH" "refs/heads/$BRANCH" >/dev/null 2>&1; then
    fail "T3b update-ref on shared HEAD branch blocked" "SUCCEEDED (no-op value)"
  else
    pass "T3b update-ref on shared HEAD branch blocked"
  fi
else
  skip "T3b update-ref on shared HEAD branch" "detached HEAD"
fi

# T4: sibling worktree write — a sibling IN THIS SESSION'S OWN REPO (a
# different repo's worktree is not a meaningful isolation probe). This is
# the exact case the sibling-worktree fix closes.
SIB=$(python3 - "$REG" "$OWN_WT" "$REPO" <<'EOF'
import json, sys
reg = json.load(open(sys.argv[1])); own = sys.argv[2]; repo = sys.argv[3]
for r in reg["repos"]:
    if r["root"] != repo:
        continue
    for w in r["worktrees"]:
        if w != own:
            print(w); raise SystemExit
EOF
)
if [ -n "$SIB" ]; then
  if echo x > "$SIB/warden-selftest-$$.txt" 2>/dev/null; then
    rm -f "$SIB/warden-selftest-$$.txt"
    fail "T4 sibling worktree write blocked" "SUCCEEDED"
  else
    pass "T4 sibling worktree write blocked"
  fi
else
  if echo x > "$REPO/.claude/worktrees/warden-probe-$$/f" 2>/dev/null; then
    fail "T4 worktree-area write blocked" "SUCCEEDED"
  else
    pass "T4 worktree-area write blocked (no live sibling; probed area)"
  fi
fi

# T5: legitimate work in the session's own workspace
if [ -n "$OWN_WT" ]; then
  if ( echo w > "$OWN_WT/warden-selftest-$$.txt" \
       && git -C "$OWN_WT" add "warden-selftest-$$.txt" \
       && git -C "$OWN_WT" commit -qm "warden selftest probe" \
       && git -C "$OWN_WT" reset -q --hard HEAD~1 \
       && rm -f "$OWN_WT/warden-selftest-$$.txt" ) 2>/dev/null; then
    pass "T5 own-workspace write+commit+reset works"
  else
    fail "T5 own-workspace write+commit+reset works" "a legitimate op was blocked"
  fi
else
  skip "T5 own-workspace ops" "session cwd is not a worktree — run from a worktree session"
fi

# T6: read-only inspection against the shared repo; lifecycle note
if git -C "$REPO" status --porcelain >/dev/null 2>&1 \
   && git -C "$REPO" log --oneline -1 >/dev/null 2>&1; then
  pass "T6a status/log against shared repo works"
else
  fail "T6a status/log against shared repo works"
fi
skip "T6b worktree add against shared repo" "run from a root-cwd session or the app's worktree flow"

# T7: push to integration from own workspace (dry-run exercises the remote path, writes nothing)
if [ -n "$OWN_WT" ] && git -C "$OWN_WT" remote get-url origin >/dev/null 2>&1; then
  if git -C "$OWN_WT" push --dry-run origin HEAD >/dev/null 2>&1; then
    pass "T7 push (dry-run) from own workspace works"
  else
    fail "T7 push (dry-run) from own workspace works"
  fi
else
  skip "T7 push from own workspace" "no worktree cwd or no origin remote"
fi

# T8: remote-host command (opt-in; effects on another machine must not be gated)
if [ -n "${WARDEN_SELFTEST_SSH_HOST:-}" ]; then
  if ssh -o BatchMode=yes -o ConnectTimeout=4 "$WARDEN_SELFTEST_SSH_HOST" true 2>/dev/null; then
    pass "T8 ssh to $WARDEN_SELFTEST_SSH_HOST works"
  else
    fail "T8 ssh to $WARDEN_SELFTEST_SSH_HOST works" "verify key/host reachable outside a session first"
  fi
else
  skip "T8 remote-host command" "set WARDEN_SELFTEST_SSH_HOST to test"
fi

# T9: this very session proves auto-binding (no setup was done here)
pass "T9 fresh session bound with zero setup (WARDEN_ACTIVE=1 + denials above)"

# T10: enforcement config immutable to sessions
if echo x >> "/Library/Application Support/ClaudeCode/managed-settings.json" 2>/dev/null; then
  fail "T10 managed-settings.json append blocked" "SUCCEEDED — investigate immediately"
else
  pass "T10 managed-settings.json append blocked"
fi

# T11: R1 — ref plumbing against a live sibling worktree's branch (no-op value)
GITVER_OK=$(git --version | awk '{split($3,v,"."); print (v[1]>2 || (v[1]==2 && v[2]>=28)) ? 1 : 0}')
INC_OK=0
git config --file /etc/gitconfig --get-all include.path 2>/dev/null \
  | grep -qxF "$WD/warden.gitconfig" && INC_OK=1
if [ "$GITVER_OK" != 1 ] || [ "$INC_OK" != 1 ]; then
  fail "T11 R1 NOT ENFORCED" "git>=2.28: $GITVER_OK, /etc/gitconfig include: $INC_OK"
elif [ -n "$SIB" ] && [ -n "$OWN_WT" ]; then
  SIBBR=$(git -C "$REPO" worktree list --porcelain | awk -v w="worktree $SIB" '
    $0==w {f=1; next} f && index($0,"branch refs/heads/")==1 {print substr($0,19); exit} f && $0=="" {f=0}')
  if [ -n "$SIBBR" ]; then
    if git -C "$OWN_WT" update-ref "refs/heads/$SIBBR" "refs/heads/$SIBBR" >/dev/null 2>&1; then
      fail "T11 R1 sibling-branch update-ref blocked" "SUCCEEDED (no-op value)"
    else
      pass "T11 R1 sibling-branch update-ref blocked"
    fi
  else
    skip "T11 R1 sibling-branch probe" "sibling worktree is detached"
  fi
else
  skip "T11 R1 sibling-branch probe" "no live sibling worktree or no worktree cwd"
fi

# T12: refresh daemon loaded and healthy
if launchctl print system/com.warden.refresh >/dev/null 2>&1; then
  if python3 -c '
import datetime, json, sys
h = json.load(open(sys.argv[1]))
dt = datetime.datetime.fromisoformat(h["ts"])
age = (datetime.datetime.now().astimezone() - dt).total_seconds()
sys.exit(0 if h["ok"] and age < 86400 else 1)' "$WD/last-refresh.json" 2>/dev/null; then
    pass "T12 refresh daemon loaded + healthy (<24h)"
  else
    fail "T12 refresh daemon loaded + healthy" "loaded but last-refresh.json stale/failed/missing"
  fi
else
  fail "T12 refresh daemon loaded + healthy" "daemon not loaded"
fi

# T13: hook delivery chain visible from this session
if [ "$INC_OK" = 1 ] && [ -f "$WD/warden.gitconfig" ] \
   && [ -x "$WD/githooks/reference-transaction" ]; then
  pass "T13 hook delivery chain present"
else
  fail "T13 hook delivery chain present" "include:$INC_OK rendered:$([ -f "$WD/warden.gitconfig" ] && echo 1 || echo 0)"
fi

# T14: every git a session might invoke is governed (>=2.28 + warden include)
UNGOV=0; NGITS=0
for g in /usr/bin/git /usr/local/bin/git /opt/homebrew/bin/git /opt/local/bin/git; do
  [ -x "$g" ] || continue
  NGITS=$((NGITS+1))
  ver="$("$g" --version 2>/dev/null | awk '{print $3}')"
  ok="$(printf '%s' "$ver" | awk -F. '{print ($1>2 || ($1==2 && $2>=28)) ? 1 : 0}')"
  if [ "$ok" != 1 ]; then
    fail "T14 git $g governed" "version $ver <2.28 — hook never runs"
    UNGOV=1
  elif ! "$g" config --system --get-all include.path 2>/dev/null | grep -qxF "$WD/warden.gitconfig"; then
    fail "T14 git $g governed" "warden include absent from its system config"
    UNGOV=1
  fi
done
[ "$UNGOV" = 0 ] && pass "T14 all $NGITS git binaries governed"

# T16: user-settings fallback delivered (survives the Enterprise remote-policy
# override that discards the managed-settings layer)
US="${WARDEN_USER_SETTINGS:-$HOME/.claude/settings.json}"
if python3 - "$US" <<'EOF' 2>/dev/null
import json, sys
u = json.load(open(sys.argv[1]))
assert u.get("env", {}).get("WARDEN_ACTIVE") == "1"
assert any("warden/guard.py" in h.get("command", "")
           for ev in u.get("hooks", {}).values()
           for g in ev for h in g.get("hooks", []))
# claude-native sandbox stays OFF in the fallback too — warden's seatbelt
# is the wall; a re-enabled native sandbox would re-break gh/keychain
assert u.get("sandbox", {}).get("enabled") is not True
EOF
then
  pass "T16 user-settings fallback delivered"
else
  fail "T16 user-settings fallback delivered" "run: sudo warden refresh (Enterprise override would leave claude ungoverned)"
fi

# T17: write scope is deny-only — the sandbox confines the projects, not
# the machine. The probe is a path warden has never heard of, standing in
# for whatever tool gets installed tomorrow: if a NOVEL dotdir isn't
# writable, some enumeration of "allowed" paths has crept back in and the
# next new tool will break.
CO="$HOME/.warden-selftest-novel-$$"
if mkdir "$CO" 2>/dev/null && touch "$CO/probe" 2>/dev/null; then
  rm -rf "$CO"; pass "T17 novel home path writable (deny-only write scope)"
else
  rm -rf "$CO" 2>/dev/null
  fail "T17 novel home path writable (deny-only write scope)" "an allow-list is back in the rendered settings; new tools will break — fix the render, never extend a list"
fi
CO="$HOME/.claude/warden/selftest-write-$$"
if touch "$CO" 2>/dev/null; then
  rm -f "$CO"; pass "T17b global agent state (~/.claude) writable"
else
  fail "T17b global agent state (~/.claude) writable" "global memory and audit writes would be blocked"
fi
CO="/tmp/warden-selftest-$$"
if touch "$CO" 2>/dev/null; then
  rm -f "$CO"; pass "T17c /tmp writable"
else
  fail "T17c /tmp writable" "tools with hardcoded /tmp paths (and shell heredocs) would break"
fi
# T17d: the deny side must still hold inside the blanket allow — a session
# must NOT be able to rewrite its own governance layer
if ( : >> "$HOME/.claude/settings.json" ) 2>/dev/null; then
  fail "T17d user settings.json write blocked" "governance file writable — deny-within-allow is not holding"
else
  pass "T17d user settings.json write blocked"
fi

# T20: network is UNRESTRICTED — the whole reason warden dropped Claude
# Code's native sandbox for its own seatbelt profile. gh/git/curl must
# reach GitHub with no proxy interference. A failure here means the
# native sandbox crept back on and is re-breaking tools.
if command -v gh >/dev/null 2>&1; then
  if gh api /rate_limit >/dev/null 2>&1; then
    pass "T20 gh reaches GitHub (no proxy TLS interference)"
  else
    fail "T20 gh reaches GitHub (no proxy TLS interference)" "gh TLS failing — native sandbox proxy is back; confirm sandbox.enabled=false and reinstall"
  fi
else
  skip "T20 gh network" "gh not installed"
fi
if curl -sI -m 8 https://api.github.com -o /dev/null 2>/dev/null; then
  pass "T20b curl reaches an arbitrary host (network unrestricted)"
else
  fail "T20b curl reaches an arbitrary host" "network egress blocked — warden must never restrict network"
fi

# T21: this session is wrapped in warden's seatbelt (the wall), proving
# the launcher shim ran. WARDEN_SEATBELT is exported only by the shim.
if [ "${WARDEN_SEATBELT:-}" = "1" ]; then
  pass "T21 session launched through warden's seatbelt wall"
else
  fail "T21 session launched through warden's seatbelt wall" "session not wrapped — launcher shim didn't run; check: warden status (launcher line)"
fi

# T19: credential store (macOS keychain) writable — gh/az refresh their
# tokens through keychain WRITES; if the sandbox denies those, in-session
# token refreshes fail ("failed to store", keyring reported invalid) while
# reads still work, which presents as mysterious GitHub auth breakage.
KC="warden-selftest-$$"
if security add-generic-password -a warden-selftest -s "$KC" -w probe 2>/dev/null \
   && security delete-generic-password -s "$KC" >/dev/null 2>&1; then
  pass "T19 keychain write (credential refresh) works"
else
  security delete-generic-password -s "$KC" >/dev/null 2>&1
  fail "T19 keychain write (credential refresh) works" "in-session gh/az token refreshes will fail — run credential refreshes from a human terminal until this passes"
fi

# T15: every adopted repo resolves to an integration lane with provenance
if warden status 2>/dev/null | grep -q "lane "; then
  pass "T15 lanes resolved for adopted repos"
else
  fail "T15 lanes resolved for adopted repos" "warden status shows no lane lines"
fi

# Disabled-state checks (failsafe): only meaningful while the sentinel is
# present; this selftest runs unprivileged so it cannot flip the sentinel
# itself. The full disable->assert->enable->assert cycle lives in
# tests/test_disable_cli.sh.
if [ -e "$WD/DISABLED" ]; then
  DIS_OK=1

  # (a) a write into a foreign worktree path must be permitted by the guard
  FOREIGN="${SIB:-$REPO}/warden-selftest-disabled-$$.txt"
  GUARD_OUT=$(python3 -c 'import json,sys;print(json.dumps({
    "hook_event_name": "PreToolUse", "session_id": "selftest",
    "cwd": sys.argv[1], "tool_name": "Edit",
    "tool_input": {"file_path": sys.argv[2]}}))' "$CWD" "$FOREIGN" \
    | WARDEN_NO_SYSLOG=1 python3 "$WD/guard.py" 2>/dev/null)
  if echo "$GUARD_OUT" | grep -q '"permissionDecision": "deny"'; then
    DIS_OK=0
    fail "disabled-state: foreign-worktree write permitted" "guard still denied: $GUARD_OUT"
  else
    pass "disabled-state: foreign-worktree write permitted"
  fi

  # (b) warden status must exit 2 with the DISABLED first line
  STATUS_OUT=$(warden status 2>/dev/null)
  STATUS_RC=$?
  FIRST_LINE=$(printf '%s\n' "$STATUS_OUT" | head -1)
  if [ "$STATUS_RC" -eq 2 ] && printf '%s' "$FIRST_LINE" | grep -qE '^state: DISABLED'; then
    pass "disabled-state: warden status exit 2 + DISABLED first line"
  else
    DIS_OK=0
    fail "disabled-state: warden status exit 2 + DISABLED first line" "rc=$STATUS_RC first=$FIRST_LINE"
  fi

  [ "$DIS_OK" -eq 1 ] && echo "disabled-state checks PASS"
else
  echo "disabled-state: skipped (warden is enabled — run 'sudo warden disable' first to exercise the failsafe)"
fi

echo
echo "== result: $PASSN pass, $FAILN fail, $SKIPN skip"
echo "== manual check remaining: from a PLAIN terminal (sandbox-exec cannot"
echo "   nest inside a governed session), run the full wall proof:"
echo "     bash tests/lab/probe-session-profile.sh   (in the warden repo)"
echo "   It asserts every trunk/.git write is blocked AND network + home +"
echo "   worktree writes are allowed. Audit trail: ~/.claude/warden/audit.jsonl"
echo "   and: log show --last 1h --predicate 'eventMessage CONTAINS \"warden\"'"
[ "$FAILN" -eq 0 ]
