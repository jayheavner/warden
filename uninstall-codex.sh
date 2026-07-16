#!/bin/bash
# warden Codex rollback — run: sudo /etc/codex/warden/uninstall-codex.sh
# Removes the managed requirements and the warden dir; restores exactly the
# pre-warden Codex world. Sessions started before this keep old policy until
# restarted.
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then
  echo "must run as root" >&2
  exit 1
fi
rm -f /etc/codex/requirements.toml
rm -rf /etc/codex/warden
rmdir /etc/codex 2>/dev/null || true
echo "warden (codex) uninstalled. Restart Codex sessions to unbind."
