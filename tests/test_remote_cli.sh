#!/bin/bash
# End-to-end `warden remote add` against landd.scan_queue run as the current
# user: a session configures origin on the shared checkout through the queue.
set -u
cd "$(dirname "$0")/.."
SRC=$(pwd)
fail() { echo "FAIL: $1"; exit 1; }

tmp=$(mktemp -d "${TMPDIR:-/tmp}/warden-remote-test.XXXXXX")
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

reg="$tmp/registry.json"
python3 - "$reg" "$repo" <<'EOF'
import json, os, sys
json.dump({"repos": [{"root": os.path.realpath(sys.argv[2]),
                      "head_branch": "main"}]}, open(sys.argv[1], "w"))
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

out=$( cd "$wt" && bash "$SRC/bin/warden" remote add origin \
       https://github.com/jayheavner/warden.git ) \
  || { kill $DAEMON 2>/dev/null; fail "warden remote add exited nonzero: $out"; }
echo "$out" | grep -q "remote-added" || fail "expected remote-added, got: $out"
[ "$(git -C "$repo" remote get-url origin)" = \
  "https://github.com/jayheavner/warden.git" ] \
  || fail "origin not configured on shared checkout"

# idempotent re-run succeeds and reports unchanged
out=$( cd "$wt" && bash "$SRC/bin/warden" remote add origin \
       https://github.com/jayheavner/warden.git ) \
  || { kill $DAEMON 2>/dev/null; fail "re-run exited nonzero: $out"; }
echo "$out" | grep -q "unchanged" || fail "expected unchanged, got: $out"

# rejected request exits nonzero
( cd "$wt" && bash "$SRC/bin/warden" remote add origin \
  "file:///etc/passwd" >/dev/null 2>&1 ) && \
  { kill $DAEMON 2>/dev/null; fail "invalid URL should exit nonzero"; }

kill $DAEMON 2>/dev/null; wait $DAEMON 2>/dev/null || true

# usage errors need no daemon
bash "$SRC/bin/warden" remote >/dev/null 2>&1 && fail "bare remote should fail"
bash "$SRC/bin/warden" remote add origin >/dev/null 2>&1 \
  && fail "missing url should fail"

echo "warden remote CLI test PASS"
