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
cp "$SRC/render.py" "$SRC/userfallback.py" "$WD/"
cp "$SRC/templates/managed-settings.base.json" "$WD/managed-settings.base.json"
mkdir -p "$T/scan/repo1" && git -C "$T/scan/repo1" init -q \
  && git -C "$T/scan/repo1" -c user.email=t@t -c user.name=t commit -q --allow-empty -m i
export WARDEN_SCAN_DIR="$T/scan"
export WARDEN_USER_SETTINGS="$T/user-settings.json"
export WARDEN_FALLBACK_STATE="$T/fallback-state/fallback.json"

# 1: refresh succeeds, writes all four surfaces + ok health
bash "$SRC/bin/warden" refresh >/dev/null 2>&1
{ [ -f "$WARDEN_DEST/managed-settings.json" ] && [ -f "$WD/registry.json" ] \
  && [ -f "$WD/warden.gitconfig" ]; } && pass "refresh writes surfaces" || fail "refresh writes surfaces"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["ok"] and d["repos"]==1' \
  "$WD/last-refresh.json" 2>/dev/null && pass "health ok" || fail "health ok"
# 1b: refresh delivers the user-settings fallback (Enterprise-override stopgap)
python3 -c '
import json, sys
u = json.load(open(sys.argv[1]))
assert u["env"]["WARDEN_ACTIVE"] == "1"
assert any("warden/guard.py" in h.get("command", "")
           for ev in u["hooks"].values() for g in ev for h in g["hooks"])
assert u["sandbox"]["enabled"] is True
assert sys.argv[1] in u["sandbox"]["filesystem"]["denyWrite"]' \
  "$WARDEN_USER_SETTINGS" 2>/dev/null \
  && pass "refresh delivers user-settings fallback" \
  || fail "refresh delivers user-settings fallback"
# 1c: fallback refuses corrupt user settings and fails the refresh loudly
cp "$WARDEN_USER_SETTINGS" "$T/user-settings.good"
echo '{corrupt' > "$WARDEN_USER_SETTINGS"
bash "$SRC/bin/warden" refresh >/dev/null 2>&1 \
  && fail "corrupt user settings fails refresh" || pass "corrupt user settings fails refresh"
[ "$(cat "$WARDEN_USER_SETTINGS")" = '{corrupt' ] \
  && pass "corrupt user settings untouched" || fail "corrupt user settings untouched"
cp "$T/user-settings.good" "$WARDEN_USER_SETTINGS"
bash "$SRC/bin/warden" refresh >/dev/null 2>&1
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

# 5: multi-git governance probe — governed, ungoverned, and too-old gits
mkdir -p "$T/fakegits"
cat > "$T/fakegits/governed-git" <<FAKE
#!/bin/bash
case "\$1" in
  --version) echo "git version 2.50.1";;
  config) echo "$WD/warden.gitconfig";;
esac
FAKE
cat > "$T/fakegits/ungoverned-git" <<'FAKE'
#!/bin/bash
case "$1" in
  --version) echo "git version 2.45.0";;
  config) exit 1;;
esac
FAKE
cat > "$T/fakegits/old-git" <<'FAKE'
#!/bin/bash
case "$1" in
  --version) echo "git version 2.22.0";;
  config) exit 1;;
esac
FAKE
chmod +x "$T/fakegits/"*
out="$(WARDEN_GIT_CANDIDATES="$T/fakegits/governed-git:$T/fakegits/ungoverned-git:$T/fakegits/old-git:$T/fakegits/missing-git" bash "$SRC/bin/warden" status)"
echo "$out" | grep -q "governed-git 2.50.1 GOVERNED" && pass "governed git detected" || fail "governed git detected" "$out"
echo "$out" | grep -q "ungoverned-git 2.45.0 NOT GOVERNED" && pass "ungoverned git flagged" || fail "ungoverned git flagged"
echo "$out" | grep -q "old-git 2.22.0 TOO OLD" && pass "old git flagged" || fail "old git flagged"
echo "$out" | grep -q "missing-git" && fail "missing git skipped" "listed nonexistent" || pass "missing git skipped"

echo "== $PASSN pass, $FAILN fail"
[ "$FAILN" -eq 0 ]
