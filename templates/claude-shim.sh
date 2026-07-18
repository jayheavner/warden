#!/bin/bash
# warden claude shim — wraps Claude Code in warden's Seatbelt wall.
#
# IRON RULE: this shim must NEVER stop Claude from launching. Governance is
# best-effort; launching is non-negotiable. Every failure path — missing
# binary, missing profile, sandbox-exec absent, PROFILE THAT FAILS TO LOAD
# — falls through to launching Claude ungoverned with a loud warning. A
# launcher that can't launch is worse than no warden at all.
set -u
WD="${WARDEN_DEST:-/Library/Application Support/ClaudeCode}/warden"
PROFILE="$WD/session.sb"
NO_WT_SENTINEL="$WD/.no-worktree-this-session"

resolve_real() {
  if [ -n "${WARDEN_REAL_CLAUDE:-}" ] && [ -x "$WARDEN_REAL_CLAUDE" ]; then
    printf '%s' "$WARDEN_REAL_CLAUDE"; return 0
  fi
  if [ -f "$WD/claude-real" ]; then
    R="$(cat "$WD/claude-real" 2>/dev/null)"
    [ -n "$R" ] && [ -x "$R" ] && { printf '%s' "$R"; return 0; }
  fi
  R="$(ls -t "$HOME/.local/share/claude/versions/"* 2>/dev/null | head -1)"
  [ -n "$R" ] && [ -x "$R" ] && { printf '%s' "$R"; return 0; }
  return 1
}

REAL="$(resolve_real || true)"
if [ -z "$REAL" ]; then
  # cannot find Claude at all — nothing we can do but say so. This is the
  # ONLY path that doesn't launch, because there is nothing to launch.
  echo "warden: cannot locate the real claude binary; set WARDEN_REAL_CLAUDE" >&2
  exit 127
fi

# Disabled failsafe, or nothing to wrap with: run ungoverned by design.
if [ -f "$WD/DISABLED" ]; then exec "$REAL" "$@"; fi
if [ ! -f "$PROFILE" ]; then
  echo "⚠ warden: launching Claude UNGOVERNED — no profile at $PROFILE; run: sudo warden refresh" >&2
  logger -t warden '{"event":"shim-ungoverned","why":"no-profile"}' 2>/dev/null || true
  exec "$REAL" "$@"
fi
if ! command -v sandbox-exec >/dev/null 2>&1; then
  echo "⚠ warden: launching Claude UNGOVERNED — sandbox-exec unavailable" >&2
  logger -t warden '{"event":"shim-ungoverned","why":"no-sandbox-exec"}' 2>/dev/null || true
  exec "$REAL" "$@"
fi

# Resolve THIS session's own worktree (nearest cwd ancestor whose .git is a
# file), matching the guard hook. Trunk/root sessions get the sentinel.
OWN_WT="$(python3 "$WD/session_worktree.py" "$PWD" 2>/dev/null || true)"
[ -n "$OWN_WT" ] || OWN_WT="$NO_WT_SENTINEL"

# PROVE the composed profile actually LOADS before we rely on it. This is
# the guard that would have prevented bricking Claude: a bad profile makes
# sandbox-exec exit non-zero on a trivial probe, and we fall through to
# ungoverned instead of failing the whole launch. We do NOT `exec` the
# probe, so control returns here on failure.
if ! /usr/bin/sandbox-exec -D "WARDEN_OWN_WT=$OWN_WT" -f "$PROFILE" /usr/bin/true 2>/dev/null; then
  echo "⚠ warden: launching Claude UNGOVERNED — seatbelt profile failed to load (report to warden maintainer); run: sudo warden refresh" >&2
  logger -t warden '{"event":"shim-ungoverned","why":"profile-load-failed"}' 2>/dev/null || true
  exec "$REAL" "$@"
fi

# Profile loads. Wrap the real launch.
export WARDEN_SEATBELT=1
export WARDEN_OWN_WORKTREE="$OWN_WT"
exec /usr/bin/sandbox-exec -D "WARDEN_OWN_WT=$OWN_WT" -f "$PROFILE" "$REAL" "$@"
