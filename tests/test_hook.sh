#!/bin/bash
# Unit tests for githooks/reference-transaction. Builds a throwaway fixture
# repo + worktree in $TMPDIR and wires core.hooksPath at the fixture's local
# config (delivery via includeIf is tested by test_render.py + selftest).
set -u
SRC="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d "${TMPDIR:-/tmp}/warden-hook.XXXXXX")"
trap 'rm -rf "$T"' EXIT
PASSN=0; FAILN=0
pass() { echo "PASS $1"; PASSN=$((PASSN+1)); }
fail() { echo "FAIL $1 ${2:-}"; FAILN=$((FAILN+1)); }
export WARDEN_AUDIT_FILE="$T/audit.jsonl"
export GIT_CONFIG_NOSYSTEM=1 HOME="$T/home"; mkdir -p "$HOME"
git() { command git -c user.email=t@t -c user.name=t "$@"; }

# fixture: main repo, side branch + worktree, detached worktree, bare remote
git init -q "$T/main"; cd "$T/main"
git commit -q --allow-empty -m init
git branch side
git worktree add -q "$T/side-wt" side
git worktree add -q --detach "$T/detached-wt" HEAD
git init -q --bare "$T/remote.git"
git push -q "$T/remote.git" HEAD:refs/heads/main HEAD:refs/heads/r1 HEAD:refs/heads/r2
git remote add origin "$T/remote.git"
bash "$SRC/githooks/make-dispatchers.sh" "$T/githooks"
install -m 0755 "$SRC/githooks/reference-transaction" "$T/githooks/reference-transaction"
git config core.hooksPath "$T/githooks"

# 1: owner commit on its own branch passes
git commit -q --allow-empty -m ok && pass "owner commit" || fail "owner commit"
# 2: sibling update-ref aborted + audit record
if git update-ref refs/heads/side HEAD 2>"$T/err"; then
  fail "sibling update-ref aborted" "succeeded"
else
  grep -q "warden R1" "$T/err" && pass "sibling update-ref aborted" || fail "sibling update-ref aborted" "no named reason"
fi
{ grep -q '"rule": "R1"' "$WARDEN_AUDIT_FILE" 2>/dev/null || grep -q '"rule":"R1"' "$WARDEN_AUDIT_FILE" 2>/dev/null; } \
  && pass "audit record written" || fail "audit record written"
# 3: sibling worktree commits its own branch
(cd "$T/side-wt" && git commit -q --allow-empty -m ok2) && pass "sibling own commit" || fail "sibling own commit"
# 4: fetch updating many remote-tracking refs passes
git fetch -q origin && pass "fetch remote-tracking refs" || fail "fetch remote-tracking refs"
# 5: branch create/delete (not checked out elsewhere) passes
git branch newb && git branch -D newb >/dev/null && pass "branch create/delete" || fail "branch create/delete"
# 6: packed refs — sibling still aborted, owner commit still passes
git pack-refs --all
git update-ref refs/heads/side HEAD 2>/dev/null && fail "packed: sibling aborted" "succeeded" || pass "packed: sibling aborted"
git commit -q --allow-empty -m ok3 && pass "packed: owner commit" || fail "packed: owner commit"
# 7: owner rebase passes (detached worktree present throughout)
git rebase -q HEAD~1 >/dev/null 2>&1 && pass "owner rebase" || fail "owner rebase"
# 8: chaining — repo-local pre-commit runs and can fail the commit
mkdir -p "$T/main/.git/hooks"
printf '#!/bin/bash\nexit 1\n' > "$T/main/.git/hooks/pre-commit"; chmod +x "$T/main/.git/hooks/pre-commit"
git commit -q --allow-empty -m nope 2>/dev/null && fail "chained pre-commit fails commit" "commit succeeded" || pass "chained pre-commit fails commit"
rm "$T/main/.git/hooks/pre-commit"
# 9: chaining — repo-local reference-transaction still consulted
printf '#!/bin/bash\n[ "$1" = prepared ] && { cat >/dev/null; exit 1; }\ncat >/dev/null\nexit 0\n' \
  > "$T/main/.git/hooks/reference-transaction"; chmod +x "$T/main/.git/hooks/reference-transaction"
git commit -q --allow-empty -m nope2 2>/dev/null && fail "chained ref-tx aborts" "succeeded" || pass "chained ref-tx aborts"
rm "$T/main/.git/hooks/reference-transaction"
# 10: fail-open — not a repo at all: exits 0
(cd "$T" && printf 'a b refs/heads/side\n' | "$T/githooks/reference-transaction" prepared) \
  && pass "fail-open outside repo" || fail "fail-open outside repo"
# 11: deny survives unwritable audit path
WARDEN_AUDIT_FILE=/dev/null/nope git update-ref refs/heads/side HEAD 2>/dev/null \
  && fail "deny with broken audit" "succeeded" || pass "deny with broken audit"

echo "== $PASSN pass, $FAILN fail"
[ "$FAILN" -eq 0 ]
