#!/bin/bash
# Writes chaining dispatchers for the standard git hook names into $1.
# core.hooksPath replaces a repo's own hooks dir wholesale; these shims give
# repo-local hooks their turn back. reference-transaction is NOT written here
# (the policy script owns that name and chains itself).
set -euo pipefail
dest="${1:?usage: make-dispatchers.sh <dest-dir>}"
mkdir -p "$dest"
for h in applypatch-msg pre-applypatch post-applypatch pre-commit \
         prepare-commit-msg commit-msg post-commit pre-rebase post-checkout \
         post-merge pre-push pre-auto-gc post-rewrite fsmonitor-watchman \
         post-index-change; do
  cat > "$dest/$h" <<'DISPATCH'
#!/bin/bash
# warden chaining dispatcher: run the repo's own hook of this name, if any.
common="$(git rev-parse --git-common-dir 2>/dev/null)" || exit 0
h="$common/hooks/$(basename "$0")"
[ -x "$h" ] && exec "$h" "$@"
exit 0
DISPATCH
  chmod 0755 "$dest/$h"
done
