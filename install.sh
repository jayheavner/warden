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

# Prove the wall live before declaring success. The entire block runs with
# `set -e` OFF and every probe's exit code captured, so a probe result can
# NEVER kill the install silently. If the wall is wrong we print FATAL and
# exit on purpose; otherwise we always reach "warden installed".
set +e
SENTINEL="$WD/.no-worktree-this-session"
# The install may be launched from a shell already under the wall, where
# sandbox-exec cannot nest. If the loader probe fails for ANY reason, skip
# the live proof with a clear note instead of dying — the profile is
# installed regardless, and `warden verify` proves it from a fresh session.
sandbox-exec -D "WARDEN_OWN_WT=$SENTINEL" -f "$WD/session.sb" /usr/bin/true 2>/dev/null
if [ $? -ne 0 ]; then
  echo "note: skipped the live wall proof here (sandbox-exec could not run —"
  echo "      commonly because this install ran from inside a governed session)."
  echo "      The wall is installed. Prove it from a FRESH session: warden verify"
else
  FIRST_REPO="$(python3 -c 'import json,sys;r=json.load(open(sys.argv[1]))["repos"];print(r[0]["root"] if r else "")' "$WD/registry.json" 2>/dev/null)"
  WALL_OK=1
  if [ -n "$FIRST_REPO" ]; then
    OWN_WT="$(ls -d "$FIRST_REPO"/.claude/worktrees/*/ 2>/dev/null | head -1)"; OWN_WT="${OWN_WT%/}"
    SIB_WT="$(ls -d "$FIRST_REPO"/.claude/worktrees/*/ 2>/dev/null | sed -n 2p)"; SIB_WT="${SIB_WT%/}"
    PARAM="${OWN_WT:-$SENTINEL}"
    se() { sandbox-exec -D "WARDEN_OWN_WT=$PARAM" -f "$WD/session.sb" "$@" 2>/dev/null; }
    se /usr/bin/touch "$FIRST_REPO/.git/HEAD" && { echo "FATAL: profile failed to deny a trunk .git write" >&2; WALL_OK=0; }
    se /bin/sh -c ': > "$HOME/.warden-install-probe" && rm "$HOME/.warden-install-probe"' || { echo "FATAL: profile over-blocks (home not writable)" >&2; WALL_OK=0; }
    if [ -n "$OWN_WT" ]; then
      se /bin/sh -c ": > \"$OWN_WT/.warden-own-probe\" && rm \"$OWN_WT/.warden-own-probe\"" || { echo "FATAL: profile blocks the session's OWN worktree" >&2; WALL_OK=0; }
    fi
    if [ -n "$SIB_WT" ]; then
      se /usr/bin/touch "$SIB_WT/.warden-sib-probe" && { rm -f "$SIB_WT/.warden-sib-probe"; echo "FATAL: profile allowed a SIBLING worktree write — isolation breached" >&2; WALL_OK=0; }
    fi
    [ "$WALL_OK" -eq 1 ] || exit 1
    echo "seatbelt verified live: trunk denied, own worktree allowed${SIB_WT:+, sibling denied}, home allowed"
  else
    echo "seatbelt profile loads (no adopted repos to probe against)"
  fi
fi
set -e

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
  # PRE-FLIGHT: prove the shim actually launches Claude BEFORE repointing
  # the launcher. This is the check that would have prevented bricking
  # Claude — a shim that can't launch must never replace the symlink.
  # `claude --version` reaches the real binary through the shim; if it
  # can't, we refuse to repoint and leave the user's launcher untouched.
  set +e
  SHIM_OUT="$(sudo -u "${SUDO_USER:-$USER}" env WARDEN_DEST="$DEST" \
    bash "$WD/claude-shim" --version </dev/null 2>&1)"
  SHIM_RC=$?
  set -e
  if [ "$SHIM_RC" -ne 0 ]; then
    echo "FATAL: the warden shim could not launch Claude (rc=$SHIM_RC):" >&2
    echo "  $SHIM_OUT" >&2
    echo "  REFUSING to repoint $LAUNCHER — your Claude launcher is UNCHANGED." >&2
    echo "  Warden's hooks and git protection are installed; the seatbelt wall" >&2
    echo "  is not active until the shim works. Report this output." >&2
    exit 1
  fi
  ln -sfn "$WD/claude-shim" "$LAUNCHER"
  [ -n "${SUDO_USER:-}" ] && chown -h "$SUDO_USER" "$LAUNCHER" || true
  echo "launcher governed: shim launch-tested OK, $LAUNCHER -> warden shim"
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
