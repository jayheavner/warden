#!/bin/bash
# warden installer — run: sudo ./install.sh
# Copies enforcement into root-owned paths, renders policy from disk truth,
# verifies the result, and prints the post-activation checklist. Idempotent;
# re-running refreshes the policy. Nothing on this machine changes until
# this script runs as root.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="/Library/Application Support/ClaudeCode"
WD="$DEST/warden"
SCAN_HOME="${SUDO_USER:+/Users/$SUDO_USER}"
SCAN_HOME="${SCAN_HOME:-$HOME}"
SCAN_DIR="${WARDEN_SCAN_DIR:-$SCAN_HOME/claude}"
FILES=(guard.py render.py landd.py lanes.py selftest.sh uninstall.sh gitconfig-include.sh templates/managed-settings.base.json templates/hookpath.gitconfig)

if [ "${1:-}" = "--print-plan" ]; then
  printf 'would install to %s:\n' "$WD"
  printf '  %s\n' "${FILES[@]}"
  printf '  bin/warden -> /usr/local/bin/warden\n'
  printf '  githooks/ (reference-transaction + dispatchers)\n'
  printf '  /Library/LaunchDaemons/com.warden.refresh.plist (WatchPaths %s)\n' "$SCAN_DIR"
  printf '  /Library/LaunchDaemons/com.warden.landd.plist (landing daemon)\n'
  printf '  /etc/gitconfig include -> %s/warden.gitconfig\n' "$WD"
  printf 'would render policy scanning: %s\n' "$SCAN_DIR"
  printf 'would write: %s/managed-settings.json and %s/registry.json\n' "$DEST" "$WD"
  exit 0
fi

if [ "$(uname -s)" != "Darwin" ]; then
  echo "warden install: macOS only." >&2
  echo "  Enforcement is built on launchd, the macOS Seatbelt sandbox, and" >&2
  echo "  /Library/Application Support/ClaudeCode — none of which exist here" >&2
  echo "  ($(uname -s)). A Linux/Windows port is not a config flag; see the" >&2
  echo "  design doc's platform scope. Aborting before any change is made." >&2
  exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "warden install: must run as root (sudo ./install.sh)" >&2
  exit 1
fi

mkdir -p "$WD"
for f in "${FILES[@]}"; do
  install -m 0644 "$SRC/$f" "$WD/$(basename "$f")"
done
chmod 0755 "$WD/selftest.sh" "$WD/uninstall.sh"
mkdir -p /usr/local/bin
install -m 0755 "$SRC/bin/warden" /usr/local/bin/warden

# v1.1: hook payload
mkdir -p "$WD/githooks"
install -m 0755 "$SRC/githooks/reference-transaction" "$WD/githooks/reference-transaction"
bash "$SRC/githooks/make-dispatchers.sh" "$WD/githooks"

# v1.1: /etc/gitconfig include (single warden-owned line; content preserved)
. "$SRC/gitconfig-include.sh"
warden_include_add /etc/gitconfig "$WD/warden.gitconfig"

python3 "$WD/render.py" \
  --scan "$SCAN_DIR" \
  --base "$WD/managed-settings.base.json" \
  --write-settings "$DEST/managed-settings.json" \
  --write-registry "$WD/registry.json" \
  --write-gitconfig "$WD/warden.gitconfig"

chown -R root:wheel "$DEST"
chmod 0755 "$DEST" "$WD"
chmod 0644 "$DEST/managed-settings.json" "$WD/registry.json" "$WD/warden.gitconfig"

python3 - "$DEST/managed-settings.json" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["sandbox"]["enabled"] and d["sandbox"]["failIfUnavailable"]
assert d["sandbox"]["allowUnsandboxedCommands"] is False
assert d["hooks"]["PreToolUse"], "hooks missing"
print("policy verified: sandbox fail-closed, hooks wired,",
      len(d["sandbox"]["filesystem"]["denyWrite"]), "denyWrite entries")
EOF

# v1.1: LaunchDaemon
PLIST=/Library/LaunchDaemons/com.warden.refresh.plist
sed "s|@SCAN_DIR@|$SCAN_DIR|g" "$SRC/templates/com.warden.refresh.plist" > "$PLIST"
chown root:wheel "$PLIST"; chmod 0644 "$PLIST"
launchctl bootout system/com.warden.refresh 2>/dev/null || true
launchctl bootstrap system "$PLIST"

# landing daemon: zero-tax `warden land` integrates through per-repo lanes
install -m 0644 "$SRC/templates/com.warden.landd.plist" \
  /Library/LaunchDaemons/com.warden.landd.plist
mkdir -p /tmp/claude/warden-land
chmod 1777 /tmp/claude /tmp/claude/warden-land 2>/dev/null || true
launchctl bootout system/com.warden.landd 2>/dev/null || true
launchctl bootstrap system /Library/LaunchDaemons/com.warden.landd.plist
launchctl print system/com.warden.landd >/dev/null 2>&1 \
  || { echo "FATAL: landing daemon not loaded" >&2; exit 1; }

# v1.1 verification: hook executable, include resolvable, daemon loaded
[ -x "$WD/githooks/reference-transaction" ] || { echo "FATAL: hook not executable" >&2; exit 1; }
git config --file /etc/gitconfig --get-all include.path | grep -qxF "$WD/warden.gitconfig" \
  || { echo "FATAL: /etc/gitconfig include missing" >&2; exit 1; }
launchctl print system/com.warden.refresh >/dev/null 2>&1 \
  || { echo "FATAL: daemon not loaded" >&2; exit 1; }
echo "v1.1: hook + includeIf delivery + refresh daemon verified"

echo
echo "warden installed."
echo "next: 1) restart running clones (sessions bind at start)"
echo "      2) in a fresh worktree session, run: warden selftest"
echo "      3) after cloning new repos or changing repo layouts: sudo warden refresh"
