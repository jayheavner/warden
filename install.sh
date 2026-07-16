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
FILES=(guard.py render.py landd.py selftest.sh uninstall.sh templates/managed-settings.base.json)

if [ "${1:-}" = "--print-plan" ]; then
  printf 'would install to %s:\n' "$WD"
  printf '  %s\n' "${FILES[@]}"
  printf '  bin/warden -> /usr/local/bin/warden\n'
  printf '  com.warden.landd.plist -> /Library/LaunchDaemons (landing daemon)\n'
  printf 'would render policy scanning: %s\n' "$SCAN_DIR"
  printf 'would write: %s/managed-settings.json and %s/registry.json\n' "$DEST" "$WD"
  exit 0
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

# landing daemon: zero-tax `warden land` merges into shared HEAD branches
install -m 0644 "$SRC/templates/com.warden.landd.plist" \
  /Library/LaunchDaemons/com.warden.landd.plist
mkdir -p /tmp/claude/warden-land
chmod 1777 /tmp/claude /tmp/claude/warden-land 2>/dev/null || true
launchctl bootout system/com.warden.landd 2>/dev/null || true
launchctl bootstrap system /Library/LaunchDaemons/com.warden.landd.plist

python3 "$WD/render.py" \
  --scan "$SCAN_DIR" \
  --base "$WD/managed-settings.base.json" \
  --write-settings "$DEST/managed-settings.json" \
  --write-registry "$WD/registry.json"

chown -R root:wheel "$DEST"
chmod 0755 "$DEST" "$WD"
chmod 0644 "$DEST/managed-settings.json" "$WD/registry.json"

python3 - "$DEST/managed-settings.json" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["sandbox"]["enabled"] and d["sandbox"]["failIfUnavailable"]
assert d["sandbox"]["allowUnsandboxedCommands"] is False
assert d["hooks"]["PreToolUse"], "hooks missing"
print("policy verified: sandbox fail-closed, hooks wired,",
      len(d["sandbox"]["filesystem"]["denyWrite"]), "denyWrite entries")
EOF

echo
echo "warden installed."
echo "next: 1) restart running clones (sessions bind at start)"
echo "      2) in a fresh worktree session, run: warden selftest"
echo "      3) after cloning new repos or changing repo layouts: sudo warden refresh"
