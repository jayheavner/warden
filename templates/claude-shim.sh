#!/bin/bash
# warden claude shim — sessions launch wrapped in warden's own Seatbelt
# profile (filesystem-only: zero network, credential, or command rules).
# Installed root-owned; ~/.local/bin/claude points here. The real binary
# path is recorded at install time and re-resolved as a fallback so app
# auto-updates keep working.
set -u
WD="${WARDEN_DEST:-/Library/Application Support/ClaudeCode}/warden"
PROFILE="$WD/session.sb"

resolve_real() {
  if [ -n "${WARDEN_REAL_CLAUDE:-}" ] && [ -x "$WARDEN_REAL_CLAUDE" ]; then
    printf '%s' "$WARDEN_REAL_CLAUDE"; return 0
  fi
  if [ -f "$WD/claude-real" ]; then
    R="$(cat "$WD/claude-real")"
    [ -x "$R" ] && { printf '%s' "$R"; return 0; }
  fi
  # fallback: newest installed version (the app's own layout)
  R="$(ls -t "$HOME/.local/share/claude/versions/"* 2>/dev/null | head -1)"
  [ -n "$R" ] && [ -x "$R" ] && { printf '%s' "$R"; return 0; }
  return 1
}

REAL="$(resolve_real)" || {
  echo "warden claude shim: cannot locate the real claude binary" >&2
  echo "  set WARDEN_REAL_CLAUDE or reinstall: sudo ./install.sh" >&2
  exit 127
}

# disabled failsafe: sentinel means run ungoverned, no questions
if [ -f "$WD/DISABLED" ]; then
  exec "$REAL" "$@"
fi

if [ ! -f "$PROFILE" ] || ! command -v sandbox-exec >/dev/null 2>&1; then
  # never fail-broken: launch ungoverned but say so loudly and leave a
  # trail — detection over bricking (same posture as the Enterprise
  # override handling)
  if [ -f "$PROFILE" ]; then WHY="sandbox-exec missing"; else WHY="no rendered profile at $PROFILE"; fi
  echo "⚠ warden: session launching UNGOVERNED ($WHY) — run: sudo warden refresh" >&2
  logger -t warden '{"event":"shim-ungoverned-launch"}' 2>/dev/null || true
  exec "$REAL" "$@"
fi

export WARDEN_SEATBELT=1
exec /usr/bin/sandbox-exec -f "$PROFILE" "$REAL" "$@"
