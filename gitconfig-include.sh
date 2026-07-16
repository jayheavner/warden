#!/bin/bash
# Sourceable helpers: the ONLY code that touches /etc/gitconfig. Edits via
# `git config --file` exclusively; never touches other content (v1.1 spec §3).

warden_include_add() {  # <etcfile> <includepath>
  local f="$1" inc="$2"
  if ! git config --file "$f" --get-all include.path 2>/dev/null | grep -qxF "$inc"; then
    git config --file "$f" --add include.path "$inc"
  fi
}

warden_include_remove() {  # <etcfile> <includepath>
  local f="$1" inc="$2" esc
  [ -f "$f" ] || return 0
  esc="$(printf '%s' "$inc" | sed 's/[][\.*^$/]/\\&/g')"
  git config --file "$f" --unset-all include.path "^${esc}$" 2>/dev/null || true
  # drop a now-empty [include] section header / whole file
  if ! grep -q '[^[:space:]]' "$f" 2>/dev/null \
     || [ "$(grep -cv '^\[include\]$\|^[[:space:]]*$' "$f")" = 0 ]; then
    rm -f "$f"
  fi
}
