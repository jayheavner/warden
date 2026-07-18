#!/bin/bash
# Proves selftest.sh anchors its probes to THIS session's own repo, not a
# fixed registry index. The wrong-repo T4 false-alarm (the selftest-ramble
# incident) came from probing registry[0]; this guards against regression.
# Does NOT run the live sandbox probes — only checks which repo the header
# names, by driving selftest far enough to print its "probing" line.
set -u
SRC="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d "${TMPDIR:-/tmp}/warden-anchor.XXXXXX")"
trap 'rm -rf "$T"' EXIT
PASSN=0; FAILN=0
pass() { echo "PASS $1"; PASSN=$((PASSN+1)); }
fail() { echo "FAIL $1 ${2:-}"; FAILN=$((FAILN+1)); }

# two adopted repos; registry[0] is alpha, but the session lives in beta
mk() {
  local root="$T/$1"
  git init -q -b main "$root"
  git -C "$root" -c user.email=t@t -c user.name=t commit -q --allow-empty -m i
  git -C "$root" worktree add -q "$root/.claude/worktrees/w1" -b wt1 2>/dev/null
  echo "$root"
}
ALPHA="$(mk alpha)"; BETA="$(mk beta)"
mkdir -p "$T/managed/warden"
cat > "$T/managed/warden/registry.json" <<JSON
{"repos":[
 {"root":"$(cd "$ALPHA" && pwd -P)","head_branch":"main","worktrees":["$(cd "$ALPHA" && pwd -P)/.claude/worktrees/w1"]},
 {"root":"$(cd "$BETA" && pwd -P)","head_branch":"main","worktrees":["$(cd "$BETA" && pwd -P)/.claude/worktrees/w1"]}
]}
JSON

# Run selftest from inside BETA's worktree; it must name BETA, not ALPHA.
# WARDEN_ACTIVE + a temp managed root let it get past the activation gate.
OUT="$(cd "$(cd "$BETA" && pwd -P)/.claude/worktrees/w1" && \
  WARDEN_ACTIVE=1 WARDEN_DEST="$T/managed" \
  bash "$SRC/selftest.sh" 2>&1 | head -8 || true)"

BETA_REAL="$(cd "$BETA" && pwd -P)"
ALPHA_REAL="$(cd "$ALPHA" && pwd -P)"
if echo "$OUT" | grep -q "probing this session's repo: $BETA_REAL"; then
  pass "selftest anchors to the session's own repo (beta)"
else
  fail "selftest anchors to the session's own repo" "$(echo "$OUT" | grep probing || echo "$OUT" | head -2)"
fi
if echo "$OUT" | grep -q "probing this session's repo: $ALPHA_REAL"; then
  fail "selftest must NOT probe registry[0] (alpha) from a beta session"
else
  pass "selftest does not fall back to registry[0] when in an adopted repo"
fi

echo "== anchor: $PASSN pass, $FAILN fail"
[ "$FAILN" -eq 0 ]
