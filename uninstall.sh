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
rm -f "$DEST/managed-settings.json" /usr/local/bin/warden
rm -rf "$DEST/warden"
rmdir "$DEST" 2>/dev/null || true
echo "warden removed; sessions revert to pre-warden behavior on restart."
