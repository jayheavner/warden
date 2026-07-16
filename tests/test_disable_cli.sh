#!/bin/bash
# Exercises bin/warden disable/enable/status against a temp DEST (no root needed).
set -u
SRC="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d "${TMPDIR:-/tmp}/warden-disable-cli.XXXXXX")"
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
export WARDEN_AUDIT_FILE="$T/audit.jsonl"
export WARDEN_CODEX_DEST="$T/no-codex-here"  # keep this test off the real /etc/codex install

# initial refresh to render a normal (enabled) baseline
bash "$SRC/bin/warden" refresh >/dev/null 2>&1

# 1: disable succeeds, sentinel written, settings disabled, banner text present
out="$(bash "$SRC/bin/warden" disable)"; rc=$?
[ "$rc" -eq 0 ] && pass "disable exit 0" || fail "disable exit 0" "rc=$rc"
[ -f "$WD/DISABLED" ] && pass "sentinel exists" || fail "sentinel exists"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert "disabled_at" in d' \
  "$WD/DISABLED" 2>/dev/null && pass "sentinel parses with disabled_at" || fail "sentinel parses with disabled_at"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["sandbox"]["enabled"] is False' \
  "$WARDEN_DEST/managed-settings.json" 2>/dev/null && pass "sandbox disabled" || fail "sandbox disabled"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["hooks"]["PreToolUse"]' \
  "$WARDEN_DEST/managed-settings.json" 2>/dev/null && pass "hooks kept" || fail "hooks kept"
echo "$out" | grep -q "warden is DISABLED" && pass "disable banner text" || fail "disable banner text" "$out"
echo "$out" | grep -q "stay sandboxed until those sessions restart" && pass "disable stale-session note" || fail "disable stale-session note" "$out"

# 2: disable again is idempotent
out="$(bash "$SRC/bin/warden" disable)"; rc=$?
[ "$rc" -eq 0 ] && pass "disable again exit 0" || fail "disable again exit 0" "rc=$rc"
echo "$out" | grep -q "already DISABLED" && pass "disable idempotent message" || fail "disable idempotent message" "$out"

# 3: status reports DISABLED and exits 2
out="$(bash "$SRC/bin/warden" status)"; rc=$?
[ "$rc" -eq 2 ] && pass "status exit 2" || fail "status exit 2" "rc=$rc"
echo "$out" | head -1 | grep -qE '^state: DISABLED since .* \(sudo warden enable to re-arm\)$' \
  && pass "status DISABLED first line" || fail "status DISABLED first line" "$out"

# 4: enable restores enforcement
out="$(bash "$SRC/bin/warden" enable)"; rc=$?
[ "$rc" -eq 0 ] && pass "enable exit 0" || fail "enable exit 0" "rc=$rc"
[ ! -f "$WD/DISABLED" ] && pass "sentinel gone" || fail "sentinel gone"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["sandbox"]["enabled"] is True and d["sandbox"]["failIfUnavailable"] is True' \
  "$WARDEN_DEST/managed-settings.json" 2>/dev/null && pass "sandbox re-enabled" || fail "sandbox re-enabled"
echo "$out" | grep -q "unenforced until restart" && pass "enable stale-session note" || fail "enable stale-session note" "$out"

# 5: enable again is idempotent
out="$(bash "$SRC/bin/warden" enable)"; rc=$?
[ "$rc" -eq 0 ] && pass "enable again exit 0" || fail "enable again exit 0" "rc=$rc"
echo "$out" | grep -q "already" && pass "enable idempotent message" || fail "enable idempotent message" "$out"

# 6: audit log has one disable and one enable event
[ "$(grep -c '"event": "disable"' "$WARDEN_AUDIT_FILE" 2>/dev/null)" -eq 1 ] \
  && pass "audit disable event" || fail "audit disable event"
[ "$(grep -c '"event": "enable"' "$WARDEN_AUDIT_FILE" 2>/dev/null)" -eq 1 ] \
  && pass "audit enable event" || fail "audit enable event"

# 7: rollback — a failed render during disable must not leave the sentinel
WARDEN_BASE_OVERRIDE="$T/nope-does-not-exist.json" bash "$SRC/bin/warden" disable >/dev/null 2>&1
rc=$?
[ "$rc" -ne 0 ] && pass "failed disable exits nonzero" || fail "failed disable exits nonzero" "rc=$rc"
[ ! -f "$WD/DISABLED" ] && pass "failed disable leaves no sentinel" || fail "failed disable leaves no sentinel"

echo "== $PASSN pass, $FAILN fail"
[ "$FAILN" -eq 0 ]
