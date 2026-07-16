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
rm -f "$DEST/managed-settings.json" /usr/local/bin/warden
rm -rf "$DEST/warden"
rmdir "$DEST" 2>/dev/null || true
echo "warden removed; sessions revert to pre-warden behavior on restart."
