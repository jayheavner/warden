#!/bin/bash
# Unit tests for gitconfig-include.sh helpers against a temp file.
set -u
SRC="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d "${TMPDIR:-/tmp}/warden-inc.XXXXXX")"
trap 'rm -rf "$T"' EXIT
PASSN=0; FAILN=0
pass() { echo "PASS $1"; PASSN=$((PASSN+1)); }
fail() { echo "FAIL $1 ${2:-}"; FAILN=$((FAILN+1)); }
. "$SRC/gitconfig-include.sh" || exit 1
INC="/Library/Application Support/ClaudeCode/warden/warden.gitconfig"
F="$T/etcgitconfig"

# 1: add creates the file with exactly one include
warden_include_add "$F" "$INC"
[ "$(git config --file "$F" --get-all include.path | grep -cxF "$INC")" = 1 ] \
  && pass "add creates include" || fail "add creates include"
# 2: add is idempotent
warden_include_add "$F" "$INC"
[ "$(git config --file "$F" --get-all include.path | grep -cxF "$INC")" = 1 ] \
  && pass "add idempotent" || fail "add idempotent"
# 3: remove deletes only our line, preserves other content
git config --file "$F" user.name keepme
warden_include_remove "$F" "$INC"
git config --file "$F" --get-all include.path 2>/dev/null | grep -qxF "$INC" \
  && fail "remove deletes include" || pass "remove deletes include"
[ "$(git config --file "$F" user.name)" = keepme ] \
  && pass "remove preserves other content" || fail "remove preserves other content"
[ -f "$F" ] && pass "file with content kept" || fail "file with content kept"
# 4: remove deletes the file when nothing else remains
rm "$F"; warden_include_add "$F" "$INC"; warden_include_remove "$F" "$INC"
[ ! -f "$F" ] && pass "empty file removed" || fail "empty file removed"

echo "== $PASSN pass, $FAILN fail"
[ "$FAILN" -eq 0 ]
