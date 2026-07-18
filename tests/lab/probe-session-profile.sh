#!/bin/bash
# Live proof of warden's session Seatbelt profile — the wall that replaces
# Claude Code's native sandbox. Runs the profile against a real fixture
# repo and asserts every isolation line AND every must-not-block line.
# Run from a PLAIN terminal (sandbox-exec cannot nest inside a governed
# session). Also invoked by install.sh as a smoke test.
set -u
cd "$(dirname "$0")/../.." || exit 2
LAB="$(mktemp -d "${TMPDIR:-/tmp}/warden-session-probe.XXXXXX")"
LAB="$(cd "$LAB" && pwd -P)"
REPO="$LAB/alpha"; WT="$REPO/.claude/worktrees/w1"; SIB="$REPO/.claude/worktrees/w2"
git init -q -b main "$REPO"; git -C "$REPO" config user.email t@t
git -C "$REPO" config user.name t; echo x > "$REPO/README.md"
git -C "$REPO" add -A; git -C "$REPO" commit -qm init
git -C "$REPO" worktree add -q "$WT" -b wt1
git -C "$REPO" worktree add -q "$SIB" -b wt2         # a sibling session's worktree
PROFILE="$LAB/session.sb"
python3 render.py --scan "$LAB" --managed-root "$LAB/managed" \
  --base templates/managed-settings.base.json \
  --write-settings "$LAB/ms.json" --write-registry "$LAB/reg.json" \
  --write-seatbelt "$PROFILE" >/dev/null || { echo "render failed"; exit 2; }

# THIS session is scoped to $WT via the -D parameter, exactly as the shim
# does at launch. Sibling $SIB must stay denied.
P=0; F=0
run() { sandbox-exec -D "WARDEN_OWN_WT=$WT" -f "$PROFILE" /bin/sh -c "$1" >/dev/null 2>&1; }
ok()  { if run "$1"; then echo "PASS $2"; P=$((P+1)); else echo "FAIL $2 (was blocked, must be allowed)"; F=$((F+1)); fi; }
no()  { if run "$1"; then echo "FAIL $2 (succeeded, must be blocked)"; F=$((F+1)); else echo "PASS $2"; P=$((P+1)); fi; }

# MUST BLOCK — the isolation walls
no "echo x > '$REPO/README.md'"            "trunk tracked-file write blocked"
no "touch '$REPO/newfile'"                 "trunk new-file write blocked"
no "touch '$REPO/.git/HEAD'"               "trunk .git/HEAD write blocked"
no "touch '$REPO/.git/config'"             "trunk .git/config write blocked (hook-tamper hole closed)"
no "touch '$REPO/.git/hooks/pre-commit'"   "trunk .git/hooks write blocked"
no "touch '$REPO/.git/refs/heads/main'"    "protected HEAD ref write blocked"
no "echo x > '$SIB/README.md'"             "SIBLING worktree tracked-file write blocked"
no "touch '$SIB/intruder'"                 "SIBLING worktree new-file write blocked"
# MUST ALLOW — real work and the whole machine
ok "echo y > '$WT/newwork'"                "own worktree write allowed"
ok "cd '$WT' && git add -A && git commit -qm w" "own worktree commit allowed (shared .git writes)"
ok "touch \"\$HOME/.warden-probe-$$\" && rm \"\$HOME/.warden-probe-$$\"" "home write allowed"
ok "touch '$LAB/vanilla-file'"             "vanilla folder write allowed"
ok "curl -sI -m 6 https://api.github.com -o /dev/null" "network unrestricted (gh/curl reach GitHub)"

# a trunk session (sentinel param) must load AND write nothing in the repo
SENT="$LAB/managed/warden/.no-worktree-this-session"
if sandbox-exec -D "WARDEN_OWN_WT=$SENT" -f "$PROFILE" /usr/bin/true 2>/dev/null; then
  echo "PASS trunk-session profile loads with sentinel param"; P=$((P+1))
else
  echo "FAIL trunk-session profile loads with sentinel param"; F=$((F+1))
fi
if sandbox-exec -D "WARDEN_OWN_WT=$SENT" -f "$PROFILE" /usr/bin/touch "$WT/should-not" 2>/dev/null; then
  rm -f "$WT/should-not"; echo "FAIL trunk session cannot write any worktree"; F=$((F+1))
else
  echo "PASS trunk session cannot write any worktree"; P=$((P+1))
fi

rm -rf "$LAB"
echo "== session-profile: $P pass, $F fail"
[ "$F" -eq 0 ]
