#!/bin/bash
# The shim's IRON RULE: it must ALWAYS launch Claude, even when the wall is
# broken. These tests drive the shim with a fake "claude" and a fake
# sandbox-exec to prove every failure path still launches. This is the
# regression guard for the night the shim bricked Claude (a bad profile +
# `exec sandbox-exec` meant Claude never started).
set -u
SRC="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d "${TMPDIR:-/tmp}/warden-shim.XXXXXX")"
trap 'rm -rf "$T"' EXIT
PASSN=0; FAILN=0
pass() { echo "PASS $1"; PASSN=$((PASSN+1)); }
fail() { echo "FAIL $1 ${2:-}"; FAILN=$((FAILN+1)); }

WD="$T/managed/warden"; mkdir -p "$WD"
export WARDEN_DEST="$T/managed"
# a fake real-claude that prints a marker so we can detect a launch
FAKE_CLAUDE="$T/fake-claude"
cat > "$FAKE_CLAUDE" <<'EOF'
#!/bin/bash
echo "CLAUDE-LAUNCHED args=$*"
EOF
chmod +x "$FAKE_CLAUDE"
echo "$FAKE_CLAUDE" > "$WD/claude-real"
cp "$SRC/session_worktree.py" "$SRC/guard.py" "$WD/"
# a shimmable copy of the launcher
SHIM="$T/claude-shim"; cp "$SRC/templates/claude-shim.sh" "$SHIM"; chmod +x "$SHIM"

run_shim() {  # run the shim with a chosen PATH (to inject fake sandbox-exec)
  ( cd "$T" && PATH="$1:$PATH" bash "$SHIM" --flag 2>&1 )
}

# a fake sandbox-exec we can make pass or fail
mkfake_sandbox() {  # $1 = exit code for the load probe
  local dir="$T/bin-$1"; mkdir -p "$dir"
  cat > "$dir/sandbox-exec" <<EOF
#!/bin/bash
# args: -D K=V -f profile [-- ] CMD...
# the LOAD PROBE runs /usr/bin/true; the real launch runs the fake claude.
for a in "\$@"; do :; done
# find the command (last non-flag chain): if it ends in 'true' it's the probe
case "\$*" in
  *"/usr/bin/true"*) exit $1 ;;         # probe result
  *) shift 4; exec "\$@" ;;             # real launch: run wrapped cmd
esac
EOF
  chmod +x "$dir/sandbox-exec"
  echo "$dir"
}

# 1) profile is missing -> ungoverned launch
rm -f "$WD/session.sb"
out="$(run_shim "$(mkfake_sandbox 0)")"
echo "$out" | grep -q "CLAUDE-LAUNCHED" && echo "$out" | grep -q "UNGOVERNED" \
  && pass "missing profile -> launches ungoverned" || fail "missing profile -> launches ungoverned" "$out"

# 2) profile present but FAILS to load (the 'got boolean' bug) -> ungoverned launch
echo '(version 1)(allow default)(allow file-write* (subpath (param "WARDEN_OWN_WT")))' > "$WD/session.sb"
out="$(run_shim "$(mkfake_sandbox 65)")"     # 65 = load failure
echo "$out" | grep -q "CLAUDE-LAUNCHED" \
  && pass "profile load failure -> STILL launches (the bricking bug)" \
  || fail "profile load failure -> STILL launches" "$out"

# 3) profile loads fine -> wrapped launch still runs claude
out="$(run_shim "$(mkfake_sandbox 0)")"
echo "$out" | grep -q "CLAUDE-LAUNCHED" \
  && pass "profile loads -> launches wrapped" || fail "profile loads -> launches wrapped" "$out"

# 4) DISABLED sentinel -> ungoverned launch
: > "$WD/DISABLED"
out="$(run_shim "$(mkfake_sandbox 0)")"
echo "$out" | grep -q "CLAUDE-LAUNCHED" \
  && pass "DISABLED -> launches ungoverned" || fail "DISABLED -> launches" "$out"
rm -f "$WD/DISABLED"

# 5) sandbox-exec entirely absent from PATH -> ungoverned launch
out="$( cd "$T" && PATH="/usr/bin:/bin" WARDEN_DEST="$T/managed" bash "$SHIM" --flag 2>&1 )"
# /usr/bin has the real sandbox-exec on macOS, so this instead exercises the
# real loader against our test profile; either way it must launch.
echo "$out" | grep -q "CLAUDE-LAUNCHED" \
  && pass "real environment -> launches (governed or not)" || fail "real environment -> launches" "$out"

echo "== shim: $PASSN pass, $FAILN fail"
[ "$FAILN" -eq 0 ]
