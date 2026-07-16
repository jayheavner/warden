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

# --- lanes: pr-opened is a success and the URL is printed
q2="$tmp/queue2"
export WARDEN_LAND_QUEUE="$q2"
mkdir -p "$q2"
( cd "$wt" && bash "$SRC/bin/warden" land feat > "$tmp/out2" 2>&1; echo $? > "$tmp/rc2" ) &
LAND=$!
for i in $(seq 1 20); do
  REQ=$(ls "$q2"/land-*.json 2>/dev/null | head -1) && [ -n "$REQ" ] && break
  sleep 0.5
done
[ -n "${REQ:-}" ] || fail "land request never appeared in queue2"
printf '{"status":"pr-opened","url":"https://github.com/a/b/pull/9","account":"jayheavner"}' > "$REQ.result"
wait $LAND
rc2=$(cat "$tmp/rc2")
grep -q "pr-opened" "$tmp/out2" || fail "pr-opened not printed: $(cat "$tmp/out2")"
grep -q "pull/9" "$tmp/out2" || fail "PR URL not printed: $(cat "$tmp/out2")"
grep -q "as jayheavner" "$tmp/out2" || fail "acting account not printed: $(cat "$tmp/out2")"
[ "$rc2" = "0" ] || fail "pr-opened should exit 0, got $rc2"

# --- lanes: rejected still exits nonzero
q3="$tmp/queue3"
export WARDEN_LAND_QUEUE="$q3"
mkdir -p "$q3"
( cd "$wt" && bash "$SRC/bin/warden" land feat > "$tmp/out3" 2>&1; echo $? > "$tmp/rc3" ) &
LAND=$!
for i in $(seq 1 20); do
  REQ=$(ls "$q3"/land-*.json 2>/dev/null | head -1) && [ -n "$REQ" ] && break
  sleep 0.5
done
printf '{"status":"rejected","reason":"nope"}' > "$REQ.result"
wait $LAND
[ "$(cat "$tmp/rc3")" = "1" ] || fail "rejected should exit 1"

# --- forget requires a repo argument
bash "$SRC/bin/warden" forget >/dev/null 2>&1 && fail "forget without repo should fail"

echo "warden land CLI test PASS"
