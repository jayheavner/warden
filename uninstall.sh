#!/bin/bash
# warden rollback — run: sudo ./uninstall.sh
# Removes the managed policy and warden entirely. Sessions revert to
# pre-warden behavior when restarted. Audit JSONLs in user homes are left
# in place (they are the user's records).
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then
  echo "warden uninstall: must run as root (sudo ./uninstall.sh)" >&2
  exit 1
fi
DEST="/Library/Application Support/ClaudeCode"
WD="$DEST/warden"

# v1.1: daemon + gitconfig include first (order: stop triggers, then payload)
launchctl bootout system/com.warden.refresh 2>/dev/null || true
rm -f /Library/LaunchDaemons/com.warden.refresh.plist
launchctl bootout system/com.warden.landd 2>/dev/null || true
rm -f /Library/LaunchDaemons/com.warden.landd.plist
if [ -f "$WD/gitconfig-include.sh" ]; then
  . "$WD/gitconfig-include.sh"
  warden_include_remove /etc/gitconfig "$WD/warden.gitconfig"
fi
# user-settings fallback out (restores the pre-warden shape via state file)
FB_HOME="${SUDO_USER:+/Users/$SUDO_USER}"; FB_HOME="${FB_HOME:-$HOME}"
if [ -f "$WD/userfallback.py" ] && [ -f "$FB_HOME/.claude/settings.json" ]; then
  python3 "$WD/userfallback.py" \
    --managed-settings "$DEST/managed-settings.json" \
    --user-settings "$FB_HOME/.claude/settings.json" \
    --state "$FB_HOME/.claude/warden/fallback.json" --remove || true
  [ -n "${SUDO_USER:-}" ] && chown "$SUDO_USER" "$FB_HOME/.claude/settings.json" || true
fi

# launcher shim out: restore ~/.local/bin/claude to the real binary
LAUNCHER="$FB_HOME/.local/bin/claude"
if [ -L "$LAUNCHER" ] && [ "$(readlink "$LAUNCHER")" = "$WD/claude-shim" ] \
   && [ -f "$WD/claude-real" ]; then
  ln -sfn "$(cat "$WD/claude-real")" "$LAUNCHER"
  [ -n "${SUDO_USER:-}" ] && chown -h "$SUDO_USER" "$LAUNCHER" || true
fi

rm -f "$DEST/managed-settings.json" /usr/local/bin/warden
rm -rf "$DEST/warden"
rmdir "$DEST" 2>/dev/null || true
echo "warden removed; sessions revert to pre-warden behavior on restart."
