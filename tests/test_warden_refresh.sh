#!/bin/bash
# Exercises bin/warden refresh + status against a temp DEST (no root needed).
set -u
SRC="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d "${TMPDIR:-/tmp}/warden-cli.XXXXXX")"
trap 'rm -rf "$T"' EXIT
PASSN=0; FAILN=0
pass() { echo "PASS $1"; PASSN=$((PASSN+1)); }
fail() { echo "FAIL $1 ${2:-}"; FAILN=$((FAILN+1)); }
export WARDEN_DEST="$T/dest"; WD="$WARDEN_DEST/warden"
mkdir -p "$WD"
cp "$SRC/render.py" "$WD/"
cp "$SRC/templates/managed-settings.base.json" "$WD/managed-settings.base.json"
mkdir -p "$T/scan/repo1" && git -C "$T/scan/repo1" init -q \
  && git -C "$T/scan/repo1" -c user.email=t@t -c user.name=t commit -q --allow-empty -m i
export WARDEN_SCAN_DIR="$T/scan"

# 1: refresh succeeds, writes all four surfaces + ok health
bash "$SRC/bin/warden" refresh >/dev/null 2>&1
{ [ -f "$WARDEN_DEST/managed-settings.json" ] && [ -f "$WD/registry.json" ] \
  && [ -f "$WD/warden.gitconfig" ]; } && pass "refresh writes surfaces" || fail "refresh writes surfaces"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["ok"] and d["repos"]==1' \
  "$WD/last-refresh.json" 2>/dev/null && pass "health ok" || fail "health ok"
# 2: failed refresh writes ok:false and exits nonzero
WARDEN_BASE_OVERRIDE="$T/nope.json" bash "$SRC/bin/warden" refresh >/dev/null 2>&1 \
  && fail "failed refresh exits nonzero" || pass "failed refresh exits nonzero"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["ok"] is False and d["error"]' \
  "$WD/last-refresh.json" 2>/dev/null && pass "health records failure" || fail "health records failure"
# 3: log cap — oversized log is truncated to a tail
python3 -c "open('$WD/refresh.log','w').write('x'*600000)"
bash "$SRC/bin/warden" refresh >/dev/null 2>&1
[ "$(stat -f%z "$WD/refresh.log")" -le 66000 ] && pass "log capped" || fail "log capped"
# 4: status reports health + registry age + hook delivery, exits 0
out="$(bash "$SRC/bin/warden" status)"
echo "$out" | grep -q "refresh: ok" && pass "status shows refresh health" || fail "status shows refresh health" "$out"
echo "$out" | grep -q "registry:" && pass "status shows registry age" || fail "status shows registry age"
echo "$out" | grep -q "hook delivery:" && pass "status shows hook delivery" || fail "status shows hook delivery"

echo "== $PASSN pass, $FAILN fail"
[ "$FAILN" -eq 0 ]
