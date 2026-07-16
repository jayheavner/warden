#!/bin/bash
# Seatbelt carve-out derivation lab.
# Builds a fixture repo mirroring the real layout (worktrees at <repo>/.claude/worktrees/<n>),
# then runs the git op suite under a seatbelt profile whose only writable paths are
# the session's own worktree + a candidate carve-out list for the shared .git dir.
# Every "Operation not permitted" tells us a path git legitimately needs (positive ops)
# or a path correctly blocked (negative ops).
set -u
LAB="${WARDEN_LAB_DIR:-$(mktemp -d /tmp/warden-lab.XXXXXX)}"
mkdir -p "$LAB"
LAB="$(cd "$LAB" && pwd -P)"   # seatbelt matches real paths; /tmp is a symlink
GIT="${GIT_BIN:-/usr/local/bin/git}"
ORIGIN="$LAB/fix/origin.git"
ALPHA="$LAB/fix/alpha"
WT1="$ALPHA/.claude/worktrees/wt1"
WT2="$ALPHA/.claude/worktrees/wt2"
PROFILE="$LAB/lab.sb"

fixture() {
  rm -rf "$LAB/fix"
  mkdir -p "$LAB/fix"
  $GIT init --bare -q "$ORIGIN"
  $GIT clone -q "$ORIGIN" "$ALPHA" 2>/dev/null
  cd "$ALPHA"
  $GIT config user.email lab@example.com
  $GIT config user.name Lab
  $GIT config gc.auto 0
  echo hello > README.md
  $GIT add README.md && $GIT commit -qm init && $GIT push -q origin HEAD:main 2>/dev/null
  $GIT branch --set-upstream-to=origin/main main 2>/dev/null || true
  $GIT worktree add -q "$WT1" -b t1
  $GIT worktree add -q "$WT2" -b t2
  echo "fixture ready: $($GIT --version)"
}

profile() {
  # $1 = space-separated extra carve-out paths (subpaths unless suffixed :lit)
  {
    echo '(version 1)'
    echo '(allow default)'
    # Freeze the whole repo dir...
    echo "(deny file-write* (subpath \"$ALPHA\"))"
    # ...then re-open the session's own workspace (tests last-match-wins nesting)
    echo "(allow file-write* (subpath \"$WT1\"))"
    for p in $1; do
      case "$p" in
        *:lit) echo "(allow file-write* (literal \"${p%:lit}\"))" ;;
        *)     echo "(allow file-write* (subpath \"$p\"))" ;;
      esac
    done
    echo '(allow file-write-data (literal "/dev/null"))'
    echo '(allow file-write* (regex #"^/dev/ttys[0-9]+$"))'
  } > "$PROFILE"
}

sb() { # sb <label> <expect:ok|deny> <shell command run with cwd=WT1>
  local label="$1" expect="$2" cmd="$3"
  out=$(sandbox-exec -f "$PROFILE" /bin/sh -c "cd '$WT1' && $cmd" 2>&1)
  rc=$?
  if [ "$expect" = ok ] && [ $rc -eq 0 ]; then verdict="PASS"
  elif [ "$expect" = deny ] && [ $rc -ne 0 ]; then verdict="PASS(blocked)"
  else verdict="**FAIL** (rc=$rc, expected $expect)"
  fi
  printf '%-34s %s\n' "$label" "$verdict"
  if [[ "$verdict" == **FAIL** ]]; then echo "$out" | grep -iE "permitted|denied|error|fatal|cannot|unable" | head -4 | sed 's/^/    | /'; fi
}

positive() {
  echo "== POSITIVE (must all PASS with carve-outs: $1)"
  profile "$1"
  sb "status"                ok   "$GIT status --porcelain"
  sb "log"                   ok   "$GIT log --oneline -1"
  sb "commit"                ok   "echo w1 >> f.txt && $GIT add f.txt && $GIT commit -qm c1"
  sb "branch create"         ok   "$GIT branch side1 && $GIT branch -D side1"
  sb "checkout -b"           ok   "$GIT checkout -qb t1b && $GIT checkout -q t1 && $GIT branch -D t1b"
  sb "fetch"                 ok   "$GIT fetch -q origin"
  sb "push own branch"       ok   "$GIT push -q origin HEAD:t1-pub"
  sb "push integration"      ok   "$GIT push -q origin HEAD:main"
  sb "stash push+pop"        ok   "echo s >> stash-probe.txt && $GIT add stash-probe.txt && $GIT stash -q && $GIT stash pop -q && $GIT reset -q"
  sb "rebase onto origin"    ok   "$GIT rebase -q origin/main"
  sb "merge + reset own"     ok   "$GIT branch m1 HEAD~0 && $GIT merge -q --no-edit m1 && $GIT branch -D m1 && $GIT reset -q --hard HEAD"
  sb "clean own tree"        ok   "touch junk.txt && $GIT clean -qfd"
  sb "status -C shared (read)" ok "$GIT -C '$ALPHA' status --porcelain >/dev/null && $GIT -C '$ALPHA' log --oneline -1 >/dev/null"
  sb "fetch -C shared"       ok   "$GIT -C '$ALPHA' fetch -q origin"
}

negative() {
  echo "== NEGATIVE (must all be blocked)"
  sb "write file in shared root"   deny "echo x > '$ALPHA/intrude.txt'"
  sb "rm tracked file in shared"   deny "rm '$ALPHA/README.md'"
  sb "git -C shared commit"        deny "$GIT -C '$ALPHA' commit -qam nope"
  sb "git -C shared reset --hard"  deny "$GIT -C '$ALPHA' reset --hard HEAD~0"
  sb "git -C shared merge"         deny "$GIT -C '$ALPHA' merge -q --no-edit t1-pub"
  sb "git -C shared checkout -b"   deny "$GIT -C '$ALPHA' checkout -qb hijack"
  sb "cd shared && commit"         deny "cd '$ALPHA' && echo y >> README.md && $GIT commit -qam drift"
  sb "write into sibling wt2"      deny "echo x > '$WT2/intrude.txt'"
  sb "git -C sibling wt2 commit"   deny "$GIT -C '$WT2' commit -qam nope --allow-empty"
  sb "edit shared .git/config"     deny "echo '[x]' >> '$ALPHA/.git/config'"
  sb "edit shared .git/index"      deny "$GIT -C '$ALPHA' add -A"
  echo "== KNOWN-RESIDUAL probes (documenting, not asserting)"
  sb "update-ref shared branch"    deny "$GIT -C '$ALPHA' update-ref refs/heads/main refs/heads/main~0"
  sb "worktree add from sandbox"   deny "$GIT -C '$ALPHA' worktree add -q '$ALPHA/.claude/worktrees/wtX' -b tX"
}

protected_branch_probe() {
  # Layer on top of full carve-outs: deny the loose-ref + reflog paths of the shared
  # checkout's HEAD branch (main). Own-branch ops must still pass; moving shared main must fail.
  echo "== PROTECTED-BRANCH probe (deny main's loose ref + reflog on top of full carve-outs)"
  {
    echo '(version 1)'
    echo '(allow default)'
    echo "(deny file-write* (subpath \"$ALPHA\"))"
    echo "(allow file-write* (subpath \"$WT1\"))"
    for p in $1; do
      case "$p" in
        *:lit) echo "(allow file-write* (literal \"${p%:lit}\"))" ;;
        *)     echo "(allow file-write* (subpath \"$p\"))" ;;
      esac
    done
    echo "(deny file-write* (literal \"$ALPHA/.git/refs/heads/main\"))"
    echo "(deny file-write* (literal \"$ALPHA/.git/refs/heads/main.lock\"))"
    echo "(deny file-write* (literal \"$ALPHA/.git/logs/refs/heads/main\"))"
    echo '(allow file-write-data (literal "/dev/null"))'
    echo '(allow file-write* (regex #"^/dev/ttys[0-9]+$"))'
  } > "$PROFILE"
  sb "own commit still works"      ok   "echo pb >> f.txt && $GIT add f.txt && $GIT commit -qm pb"
  sb "own push still works"        ok   "$GIT push -q origin HEAD:t1-pub -f"
  sb "update-ref shared main"      deny "$GIT -C '$ALPHA' update-ref refs/heads/main refs/heads/main~0"
  sb "reset --soft moves shared main" deny "$GIT -C '$ALPHA' reset -q --soft HEAD~0"
  sb "branch -f shared main"       deny "$GIT branch -f main HEAD"
}

full_run() {
  fixture
  echo
  echo "#### FULL CARVE-OUTS ($($GIT --version))"
  CARVE="$ALPHA/.git/objects $ALPHA/.git/refs $ALPHA/.git/logs $ALPHA/.git/worktrees/wt1 $ALPHA/.git/packed-refs.lock:lit $ALPHA/.git/packed-refs:lit $ALPHA/.git/FETCH_HEAD:lit"
  positive "$CARVE"
  echo
  negative
  echo
  protected_branch_probe "$CARVE"
}

full_run
echo; echo "######## SECOND GIT ########"
GIT=/usr/bin/git full_run
