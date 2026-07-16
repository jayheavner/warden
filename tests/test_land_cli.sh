#!/bin/bash
# End-to-end `warden land` against landd.scan_queue run as the current user:
# a worktree branch lands on main in the fixture's shared checkout.
set -u
cd "$(dirname "$0")/.."
SRC=$(pwd)
fail() { echo "FAIL: $1"; exit 1; }

tmp=$(mktemp -d "${TMPDIR:-/tmp}/warden-land-test.XXXXXX")
trap 'rm -rf "$tmp"' EXIT
export WARDEN_LAND_QUEUE="$tmp/queue"

repo="$tmp/alpha"
git init -q -b main "$repo"
git -C "$repo" config user.email t@t
git -C "$repo" config user.name t
echo x > "$repo/README.md"
git -C "$repo" add -A && git -C "$repo" commit -qm init
wt="$repo/.claude/worktrees/w1"
git -C "$repo" worktree add -q "$wt" -b feat
echo y > "$wt/new.txt"
git -C "$wt" add -A && git -C "$wt" commit -qm feat

reg="$tmp/registry.json"
python3 - "$reg" "$repo" "$wt" <<'EOF'
import json, os, sys
json.dump({"repos": [{"root": os.path.realpath(sys.argv[2]),
                      "head_branch": "main", "top_entries": ["README.md"],
                      "worktrees": [sys.argv[3]]}]}, open(sys.argv[1], "w"))
EOF

# fake daemon: poll the queue like launchd's WatchPaths+interval would
( for i in $(seq 1 20); do
    python3 - "$SRC/landd.py" "$WARDEN_LAND_QUEUE" "$reg" <<'EOF'
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("landd", sys.argv[1])
landd = importlib.util.module_from_spec(spec); spec.loader.exec_module(landd)
landd.scan_queue(sys.argv[2], json.load(open(sys.argv[3])), demote=False)
EOF
    sleep 0.5
  done ) &
DAEMON=$!

out=$( cd "$wt" && bash "$SRC/bin/warden" land ) || { kill $DAEMON 2>/dev/null; fail "warden land exited nonzero: $out"; }
kill $DAEMON 2>/dev/null; wait $DAEMON 2>/dev/null || true
echo "$out" | grep -q "landed" || fail "expected landed, got: $out"
[ "$(git -C "$repo" rev-parse main)" = "$(git -C "$repo" rev-parse feat)" ] \
  || fail "main did not advance to feat"
[ -f "$repo/new.txt" ] || fail "shared working tree not updated"

echo "warden land CLI test PASS"
