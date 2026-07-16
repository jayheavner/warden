#!/bin/bash
# warden Codex installer — run: sudo ./install-codex.sh
# Delivers the same isolation to Codex sessions via /etc/codex:
# requirements.toml (managed policy, outranks all user config) + managed
# hooks dir. Idempotent; re-running refreshes the policy. Nothing changes
# until this script runs as root.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="/etc/codex"
WD="$DEST/warden"
SCAN_HOME="${SUDO_USER:+/Users/$SUDO_USER}"
SCAN_HOME="${SCAN_HOME:-$HOME}"
SCAN_DIR="${WARDEN_SCAN_DIR:-$SCAN_HOME/claude}"
FILES=(guard.py codex_guard.py render.py codex-selftest uninstall-codex.sh templates/requirements.base.toml)

if [ "${1:-}" = "--print-plan" ]; then
  printf 'would install to %s:\n' "$WD"
  printf '  %s\n' "${FILES[@]}"
  printf 'would render policy scanning: %s\n' "$SCAN_DIR"
  printf 'would write: %s/requirements.toml and %s/registry.json\n' "$DEST" "$WD"
  exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "warden install-codex: must run as root (sudo ./install-codex.sh)" >&2
  exit 1
fi

mkdir -p "$WD"
for f in "${FILES[@]}"; do
  install -m 0644 "$SRC/$f" "$WD/$(basename "$f")"
done
chmod 0755 "$WD/codex-selftest" "$WD/uninstall-codex.sh"

python3 "$WD/render.py" --format codex \
  --scan "$SCAN_DIR" \
  --base "$WD/requirements.base.toml" \
  --write-settings "$DEST/requirements.toml" \
  --write-registry "$WD/registry.json"

chown -R root:wheel "$DEST"
chmod 0755 "$DEST" "$WD"
chmod 0644 "$DEST/requirements.toml" "$WD/registry.json"

python3 - "$DEST/requirements.toml" <<'EOF'
import sys, tomllib
d = tomllib.loads(open(sys.argv[1]).read())
assert d["default_permissions"] == "warden"
assert d["allowed_permission_profiles"] == ["warden"]
assert "danger-full-access" not in d["allowed_sandbox_modes"]
assert d["hooks"]["managed_dir"] == "/etc/codex/warden"
assert d["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
rules = d["permissions"]["warden"]["filesystem"]
print("policy verified: default_permissions=warden, hooks wired,",
      len(rules), "filesystem rules")
EOF

echo
echo "warden (codex) installed."
echo "next: 1) restart the ChatGPT app / codex CLI (sessions bind at start)"
echo "      2) verify the requirements layer loads:"
echo "         codex app-server <<< JSON configRequirements/read must be non-null"
echo "         (or /debug-config inside a Codex session)"
echo "      3) in a fresh Codex worktree session, run: warden codex-selftest"
echo "      4) after cloning new repos: sudo warden refresh (renders both harnesses)"
