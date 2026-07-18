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
FILES=(guard.py render.py doctor.py session_worktree.py landd.py lanes.py userfallback.py selftest.sh uninstall.sh gitconfig-include.sh templates/managed-settings.base.json templates/hookpath.gitconfig)

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
  printf 'would merge user-settings fallback (Enterprise-override stopgap) into ~/.claude/settings.json\n'
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
  --write-gitconfig "$WD/warden.gitconfig" \
  --write-seatbelt "$WD/session.sb"

chown -R root:wheel "$DEST"
chmod 0755 "$DEST" "$WD"
chmod 0644 "$DEST/managed-settings.json" "$WD/registry.json" "$WD/warden.gitconfig" "$WD/session.sb"

python3 - "$DEST/managed-settings.json" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["sandbox"]["enabled"] is False, "claude-native sandbox must be OFF"
assert d["hooks"]["PreToolUse"], "hooks missing"
print("policy verified: claude-native sandbox off (warden seatbelt is the "
      "wall), hooks wired")
EOF

# The wall itself, proven live before activation — runs unsandboxed (root,
# plain shell), the context the lab used to prove profile semantics. The
# session profile is parameterized: sandbox-exec -D WARDEN_OWN_WT=<wt>
# re-opens ONE worktree. The smoke test drives that with a live worktree
# and asserts own-worktree allowed, sibling denied, trunk denied, and home
# writable — all under one composed session profile.
SENTINEL="$WD/.no-worktree-this-session"
sandbox-exec -D "WARDEN_OWN_WT=$SENTINEL" -f "$WD/session.sb" /usr/bin/true \
  || { echo "FATAL: seatbelt profile does not load (parameterized)" >&2; exit 1; }
FIRST_REPO="$(python3 -c 'import json,sys;r=json.load(open(sys.argv[1]))["repos"];print(r[0]["root"] if r else "")' "$WD/registry.json")"
if [ -n "$FIRST_REPO" ]; then
  OWN_WT="$(ls -d "$FIRST_REPO"/.claude/worktrees/*/ 2>/dev/null | head -1)"; OWN_WT="${OWN_WT%/}"
  SIB_WT="$(ls -d "$FIRST_REPO"/.claude/worktrees/*/ 2>/dev/null | sed -n 2p)"; SIB_WT="${SIB_WT%/}"
  # param = own worktree if we found one, else the sentinel
  PARAM="${OWN_WT:-$SENTINEL}"
  se() { sandbox-exec -D "WARDEN_OWN_WT=$PARAM" -f "$WD/session.sb" "$@"; }
  if se /usr/bin/touch "$FIRST_REPO/.git/HEAD" 2>/dev/null; then
    echo "FATAL: profile failed to deny a trunk .git write" >&2; exit 1
  fi
  se /bin/sh -c ": > \"\$HOME/.warden-install-probe\" && rm \"\$HOME/.warden-install-probe\"" \
    || { echo "FATAL: profile over-blocks (home not writable)" >&2; exit 1; }
  if [ -n "$OWN_WT" ] && ! se /bin/sh -c ": > \"$OWN_WT/.warden-own-probe\" && rm \"$OWN_WT/.warden-own-probe\"" 2>/dev/null; then
    echo "FATAL: profile blocks the session's OWN worktree" >&2; exit 1
  fi
  if [ -n "$SIB_WT" ] && se /usr/bin/touch "$SIB_WT/.warden-sib-probe" 2>/dev/null; then
    rm -f "$SIB_WT/.warden-sib-probe"
    echo "FATAL: profile allowed a SIBLING worktree write — isolation breached" >&2; exit 1
  fi
  echo "seatbelt verified live: trunk denied, own worktree allowed${SIB_WT:+, sibling denied}, home allowed"
fi

# Launcher: sessions must start wrapped. Point ~/.local/bin/claude at the
# root-owned shim; record the real binary for the shim and for uninstall.
FB_HOME="${SUDO_USER:+/Users/$SUDO_USER}"; FB_HOME="${FB_HOME:-$HOME}"
install -m 0755 "$SRC/templates/claude-shim.sh" "$WD/claude-shim"
LAUNCHER="$FB_HOME/.local/bin/claude"
if [ -e "$LAUNCHER" ]; then
  REAL="$(readlink "$LAUNCHER" || true)"
  if [ -n "$REAL" ] && [ "$REAL" != "$WD/claude-shim" ]; then
    printf '%s' "$REAL" > "$WD/claude-real"
    chmod 0644 "$WD/claude-real"
  fi
  ln -sfn "$WD/claude-shim" "$LAUNCHER"
  [ -n "${SUDO_USER:-}" ] && chown -h "$SUDO_USER" "$LAUNCHER" || true
  echo "launcher governed: $LAUNCHER -> warden shim -> $(cat "$WD/claude-real" 2>/dev/null || echo '(version auto-resolve)')"
else
  echo "WARNING: no claude launcher at $LAUNCHER — sessions will start ungoverned."
  echo "  Launch governed sessions via: \"$WD/claude-shim\""
fi

# Enterprise-override stopgap: on Claude Enterprise accounts the remote org
# policy replaces the policySettings layer, silently discarding the file we
# just rendered. Deliver the same enforcement through the user-settings
# layer, which survives that override (proven per-layer; see docs).
FB_HOME="${SUDO_USER:+/Users/$SUDO_USER}"; FB_HOME="${FB_HOME:-$HOME}"
python3 "$WD/userfallback.py" \
  --managed-settings "$DEST/managed-settings.json" \
  --user-settings "$FB_HOME/.claude/settings.json" \
  --state "$FB_HOME/.claude/warden/fallback.json"
if [ -n "${SUDO_USER:-}" ]; then
  for f in "$FB_HOME/.claude/settings.json" \
           "$FB_HOME/.claude/warden/fallback.json" \
           "$FB_HOME/.claude/warden/settings.json.pre-warden"; do
    [ -f "$f" ] && chown "$SUDO_USER" "$f" || true
  done
fi

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
echo "next: 1) REQUIRED — prove live enforcement end-to-end: warden verify-claude"
echo "         (catches the Claude Enterprise remote-policy override; a file on"
echo "         disk is not enforcement)"
echo "      2) restart running clones (sessions bind at start)"
echo "      3) in a fresh worktree session, run: warden selftest"
echo "      4) after cloning new repos or changing repo layouts: sudo warden refresh"
