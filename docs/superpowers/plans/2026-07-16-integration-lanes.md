# Integration Lanes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `warden land` shape-aware: three lanes (`local`/`push`/`pr`) resolved per-repo as declared → learned → inferred, self-correcting from the remote's own policy denials.

**Architecture:** A new pure-resolution module `lanes.py` (reads git config, `.warden.json` via `git show HEAD:`, `learned.json`; no mutation, no network). `landd.py` becomes the executor: it dispatches the resolved lane, classifies push failures, writes lessons, and routes policy denials into the pr lane. `bin/warden` grows `forget` and a lane column in `status`. Spec: `docs/superpowers/specs/2026-07-16-integration-lanes-design.md` (v3.2).

**Tech Stack:** Python 3 stdlib only (repo convention), POSIX sh for `bin/warden`, `unittest` with offline bare-repo fixtures.

## Global Constraints

- Requirements (approved 2026-07-16): no repo gets PR ceremony unless its remote's rules or an explicit declaration require it; never assume a remote exists; zero integration involvement from Jay; warden never manufactures integration work nothing required; gh is multi-profile — accounts are per-host sets, never a scalar.
- Regression gate is literal: every test that exists in `tests/test_landd.py` before this plan must pass **unmodified**. New tests go in new test methods/files.
- All tests run offline: `python3 -m unittest discover -s tests`.
- Result statuses (spec §6): `landed`, `landed-remote-only`, `pr-opened`, `pr-exists`, `branch-pushed`, `rejected`. Every `rejected` reason names the fix, addressed to the session.
- Lessons are written only on unambiguous ref-update policy denials (spec §3.4): a `! [remote rejected]` porcelain line AND a curated pattern match. Ambiguous → reject without learning.
- Never print token values from any gh config file; only account names.
- `learned.json` default path: `/Library/Application Support/ClaudeCode/warden/learned.json`; every function touching it takes the path as a parameter so tests never touch the real one.

---

### Task 1: `lanes.py` — pure lane resolution

**Files:**
- Create: `lanes.py`
- Test: `tests/test_lanes.py`

**Interfaces:**
- Produces: `lanes.resolve(root, learned_path=LEARNED_DEFAULT) -> dict` with keys `lane` (`"local"|"push"|"pr"`), `provenance` (`"declared"|"learned"|"inferred: remoted"|"inferred: no remote"`), `remote` (str|None), `remote_url` (str|None), `default_branch` (str|None), `note` (str|None — set when a `.warden.json` was ignored as invalid).
- Produces: `lanes.gh_accounts(host, home) -> list[str]` (active account first).
- Produces: `lanes.LEARNED_DEFAULT` (str constant).
- Consumes: nothing from other tasks.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_lanes.py
import json
import os
import subprocess
import tempfile
import unittest

import lanes


def run(cwd, *cmd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


def make_repo(base, name):
    root = os.path.join(base, name)
    os.makedirs(root)
    run(root, "git", "init", "-q", "-b", "main")
    run(root, "git", "-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "-q", "--allow-empty", "-m", "init")
    return root


def add_origin(base, root):
    bare = os.path.join(base, os.path.basename(root) + ".git")
    run(base, "git", "init", "-q", "--bare", "-b", "main", bare)
    run(root, "git", "remote", "add", "origin", bare)
    run(root, "git", "push", "-q", "origin", "main")
    run(root, "git", "remote", "set-head", "origin", "main")
    return bare


class TestResolve(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.learned = os.path.join(self.base, "learned.json")

    def test_no_remote_infers_local(self):
        root = make_repo(self.base, "solo")
        res = lanes.resolve(root, learned_path=self.learned)
        self.assertEqual(res["lane"], "local")
        self.assertEqual(res["provenance"], "inferred: no remote")
        self.assertIsNone(res["remote"])

    def test_remoted_infers_push(self):
        root = make_repo(self.base, "mine")
        add_origin(self.base, root)
        res = lanes.resolve(root, learned_path=self.learned)
        self.assertEqual(res["lane"], "push")
        self.assertEqual(res["provenance"], "inferred: remoted")
        self.assertEqual(res["remote"], "origin")
        self.assertEqual(res["default_branch"], "main")

    def test_declared_beats_everything(self):
        root = make_repo(self.base, "conv")
        add_origin(self.base, root)
        with open(os.path.join(root, ".warden.json"), "w") as f:
            json.dump({"version": 1, "lane": "pr"}, f)
        run(root, "git", "add", ".warden.json")
        run(root, "git", "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-q", "-m", "declare")
        res = lanes.resolve(root, learned_path=self.learned)
        self.assertEqual((res["lane"], res["provenance"]), ("pr", "declared"))

    def test_declaration_reads_committed_content_not_working_tree(self):
        root = make_repo(self.base, "tamper")
        add_origin(self.base, root)
        with open(os.path.join(root, ".warden.json"), "w") as f:
            json.dump({"version": 1, "lane": "pr"}, f)  # uncommitted
        res = lanes.resolve(root, learned_path=self.learned)
        self.assertEqual(res["lane"], "push")  # stray file must not steer

    def test_invalid_declaration_falls_through_with_note(self):
        root = make_repo(self.base, "bad")
        add_origin(self.base, root)
        with open(os.path.join(root, ".warden.json"), "w") as f:
            f.write('{"lane": "yolo"}')
        run(root, "git", "add", ".warden.json")
        run(root, "git", "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-q", "-m", "bad")
        res = lanes.resolve(root, learned_path=self.learned)
        self.assertEqual(res["lane"], "push")
        self.assertIn("yolo", res["note"])

    def test_learned_lane_used_when_remote_url_matches(self):
        root = make_repo(self.base, "work")
        bare = add_origin(self.base, root)
        with open(self.learned, "w") as f:
            json.dump({"version": 1, "repos": {root: {
                "lane": "pr", "remote_url": bare,
                "learned_from": "GH006", "ts": "2026-07-16T00:00:00Z"}}}, f)
        res = lanes.resolve(root, learned_path=self.learned)
        self.assertEqual((res["lane"], res["provenance"]), ("pr", "learned"))

    def test_learned_lane_dropped_when_remote_url_changed(self):
        root = make_repo(self.base, "moved")
        add_origin(self.base, root)
        with open(self.learned, "w") as f:
            json.dump({"version": 1, "repos": {root: {
                "lane": "pr", "remote_url": "git@github.com:old/gone.git",
                "learned_from": "GH006", "ts": "2026-07-16T00:00:00Z"}}}, f)
        res = lanes.resolve(root, learned_path=self.learned)
        self.assertEqual(res["lane"], "push")  # stale lesson ignored


class TestGhAccounts(unittest.TestCase):
    def _write_hosts(self, home, text):
        d = os.path.join(home, ".config", "gh")
        os.makedirs(d)
        with open(os.path.join(d, "hosts.yml"), "w") as f:
            f.write(text)

    def test_multi_account_multi_host_active_first(self):
        home = tempfile.mkdtemp()
        self._write_hosts(home, (
            "github.com:\n"
            "    git_protocol: ssh\n"
            "    users:\n"
            "        jayheavner:\n"
            "            oauth_token: REDACTED1\n"
            "        jay-at-work:\n"
            "            oauth_token: REDACTED2\n"
            "    user: jay-at-work\n"
            "ghe.example.com:\n"
            "    users:\n"
            "        jheavner:\n"
            "            oauth_token: REDACTED3\n"
            "    user: jheavner\n"))
        self.assertEqual(lanes.gh_accounts("github.com", home),
                         ["jay-at-work", "jayheavner"])
        self.assertEqual(lanes.gh_accounts("ghe.example.com", home),
                         ["jheavner"])
        self.assertEqual(lanes.gh_accounts("gitlab.com", home), [])

    def test_hosts_file_absent(self):
        self.assertEqual(lanes.gh_accounts("github.com", tempfile.mkdtemp()), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_lanes -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lanes'`

- [ ] **Step 3: Write `lanes.py`**

```python
#!/usr/bin/env python3
"""warden lane resolution: which integration lane a repo lands through.

Pure reads: git config, the committed .warden.json, learned.json, gh's
hosts.yml (names only, never tokens). No mutation, no network. Precedence:
declared > learned > inferred (no remote -> local, any remote -> push).
The pr lane is reachable only by declaration or a recorded policy denial —
warden never manufactures PR ceremony nothing required.
"""
import json
import os
import subprocess

LEARNED_DEFAULT = "/Library/Application Support/ClaudeCode/warden/learned.json"
LANES = ("local", "push", "pr")


def _git(root, *args):
    p = subprocess.run(["git", "-C", root] + list(args),
                       capture_output=True, text=True, timeout=30)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def remote_of(root):
    rc, head, _ = _git(root, "symbolic-ref", "--short", "HEAD")
    if rc == 0:
        rc2, rem, _ = _git(root, "config", "branch.%s.remote" % head)
        if rc2 == 0 and rem:
            return rem
    rc, remotes, _ = _git(root, "remote")
    names = remotes.split() if rc == 0 else []
    return "origin" if "origin" in names else None


def remote_url_of(root, remote):
    rc, url, _ = _git(root, "remote", "get-url", remote)
    return url if rc == 0 else None


def default_branch(root, remote):
    rc, ref, _ = _git(root, "symbolic-ref", "--short",
                      "refs/remotes/%s/HEAD" % remote)
    if rc == 0 and ref.startswith(remote + "/"):
        return ref[len(remote) + 1:]
    rc, head, _ = _git(root, "symbolic-ref", "--short", "HEAD")
    return head if rc == 0 else None


def declared_lane(root):
    """Committed .warden.json only — never the working tree."""
    rc, blob, _ = _git(root, "show", "HEAD:.warden.json")
    if rc != 0:
        return None, None
    try:
        data = json.loads(blob)
    except ValueError:
        return None, ".warden.json is not valid JSON; ignored"
    lane = data.get("lane")
    if lane in LANES:
        return lane, None
    return None, ".warden.json lane %r is not one of %s; ignored" % (
        lane, "/".join(LANES))


def load_learned(path):
    try:
        return json.load(open(path)).get("repos", {})
    except (OSError, ValueError):
        return {}


def learned_lane(root, remote_url, learned_path):
    entry = load_learned(learned_path).get(root)
    if not entry or entry.get("remote_url") != remote_url:
        return None
    return entry.get("lane")


def resolve(root, learned_path=LEARNED_DEFAULT):
    root = os.path.realpath(root)
    lane, note = declared_lane(root)
    remote = remote_of(root)
    remote_url = remote_url_of(root, remote) if remote else None
    db = default_branch(root, remote) if remote else None
    if lane:
        return {"lane": lane, "provenance": "declared", "remote": remote,
                "remote_url": remote_url, "default_branch": db, "note": None}
    if remote:
        learned = learned_lane(root, remote_url, learned_path)
        if learned:
            return {"lane": learned, "provenance": "learned",
                    "remote": remote, "remote_url": remote_url,
                    "default_branch": db, "note": note}
        return {"lane": "push", "provenance": "inferred: remoted",
                "remote": remote, "remote_url": remote_url,
                "default_branch": db, "note": note}
    return {"lane": "local", "provenance": "inferred: no remote",
            "remote": None, "remote_url": None, "default_branch": None,
            "note": note}


def gh_accounts(host, home):
    """Account NAMES for one host from gh's hosts.yml, active first.

    hosts.yml is a multi-account, multi-host store; parse names only —
    token values must never leave this function (they are not read into
    any returned or logged structure).
    """
    path = os.path.join(home, ".config", "gh", "hosts.yml")
    try:
        lines = open(path).read().splitlines()
    except OSError:
        return []
    accounts, active, in_host, in_users = [], None, False, False
    for ln in lines:
        stripped = ln.strip()
        if not ln.startswith(" ") and stripped.endswith(":"):
            in_host = stripped.rstrip(":") == host
            in_users = False
            continue
        if not in_host:
            continue
        indent = len(ln) - len(ln.lstrip(" "))
        if indent == 4 and stripped == "users:":
            in_users = True
            continue
        if in_users and indent == 8 and stripped.endswith(":"):
            accounts.append(stripped.rstrip(":"))
            continue
        if indent == 4:
            in_users = False
            if stripped.startswith("user:"):
                active = stripped.split(":", 1)[1].strip()
    if active in accounts:
        accounts.remove(active)
    if active:
        accounts.insert(0, active)
    return accounts
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_lanes -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Full suite + commit**

Run: `python3 -m unittest discover -s tests` — Expected: OK (45 + 10)

```bash
git add lanes.py tests/test_lanes.py
git commit -m "feat(lanes): pure lane resolution — declared > learned > inferred"
```

---

### Task 2: push-failure classifier

**Files:**
- Modify: `landd.py` (add near top, after imports)
- Test: `tests/test_landd.py` (new test class appended; existing tests untouched)

**Interfaces:**
- Produces: `landd.classify_push_failure(porcelain: str, stderr: str) -> "policy"|"nonff"|"other"`.
- Consumes: nothing from other tasks.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_landd.py`)

```python
class TestClassifier(unittest.TestCase):
    # Captured GitHub transcript (GH006). Fixture text is real, not invented.
    GH006_PORCELAIN = ("To github.com:acme/app.git\n"
                       "!\trefs/heads/main:refs/heads/main\t"
                       "[remote rejected] (protected branch hook declined)\n"
                       "Done")
    GH006_STDERR = ("remote: error: GH006: Protected branch update failed "
                    "for refs/heads/main.\n"
                    "remote: error: Changes must be made through a pull "
                    "request.\n"
                    "error: failed to push some refs to "
                    "'github.com:acme/app.git'")

    def test_gh006_is_policy(self):
        self.assertEqual(
            landd.classify_push_failure(self.GH006_PORCELAIN,
                                        self.GH006_STDERR), "policy")

    def test_non_ff_is_nonff_not_policy(self):
        porcelain = ("To github.com:me/mine.git\n"
                     "!\trefs/heads/main:refs/heads/main\t"
                     "[rejected] (non-fast-forward)\nDone")
        stderr = ("hint: Updates were rejected because the remote contains "
                  "work that you do\nhint: not have locally.")
        self.assertEqual(landd.classify_push_failure(porcelain, stderr),
                         "nonff")

    def test_transport_failure_is_other_even_with_policy_words(self):
        # No "! ..." rejected ref line -> the push was never judged by the
        # remote; text mentioning pull requests must not cause learning.
        stderr = ("ssh: connect to host github.com port 22: Network is "
                  "unreachable\nPlease open a pull request instead? "
                  "fatal: Could not read from remote repository.")
        self.assertEqual(landd.classify_push_failure("", stderr), "other")

    def test_unrecognized_rejection_is_other(self):
        porcelain = ("To git.example.com:x/y.git\n"
                     "!\trefs/heads/main:refs/heads/main\t"
                     "[remote rejected] (pre-receive hook declined)\nDone")
        stderr = "remote: custom policy: deploy freeze until Friday"
        self.assertEqual(landd.classify_push_failure(porcelain, stderr),
                         "other")
```

Also add `import landd` if not present at the top of the test file (it is — existing tests use it).

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_landd.TestClassifier -v`
Expected: FAIL — `AttributeError: module 'landd' has no attribute 'classify_push_failure'`

- [ ] **Step 3: Implement in `landd.py`**

```python
# Curated policy-denial patterns. Extend ONLY by adding a captured real
# transcript to TestClassifier — never from memory (spec §3.4).
POLICY_PATTERNS = ("gh006", "protected branch", "pull request")


def classify_push_failure(porcelain, stderr):
    """policy | nonff | other.

    'policy' requires BOTH: a '! ...' rejected-ref porcelain line (the push
    reached the remote and was refused at ref-update time) AND a curated
    pattern match. Ambiguity -> 'other': a wrong ordinary rejection costs
    one retry; a wrong lesson silently changes a repo's behavior forever.
    """
    rejected = [l for l in porcelain.splitlines() if l.startswith("!")]
    if not rejected:
        return "other"
    text = (porcelain + "\n" + stderr).lower()
    if "non-fast-forward" in text or "fetch first" in text:
        return "nonff"
    if any(p in text for p in POLICY_PATTERNS):
        return "policy"
    return "other"
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_landd.TestClassifier -v` — Expected: PASS (4 tests)

- [ ] **Step 5: Full suite + commit**

Run: `python3 -m unittest discover -s tests` — Expected: OK

```bash
git add landd.py tests/test_landd.py
git commit -m "feat(landd): conservative push-failure classifier from captured transcripts"
```

---

### Task 3: remoted common prefix + `push` lane

**Files:**
- Modify: `landd.py` (`process_request` dispatches on lane; new helpers `_sync_from_origin`, `_land_push`; existing local behavior extracted unchanged into `_land_local`)
- Test: `tests/test_landd.py` (new class `TestPushLane`; fixture helpers)

**Interfaces:**
- Consumes: `lanes.resolve` (Task 1), `classify_push_failure` (Task 2).
- Produces: `process_request(req, registry, demote=True, learned_path=lanes.LEARNED_DEFAULT)` — same signature plus the `learned_path` kwarg; results now always carry `lane` and `provenance` keys. `_land_push` returns either a result dict or `{"verdict": "policy-denied", "evidence": str}` (consumed by Task 4's routing).

- [ ] **Step 1: Write the failing tests** (append; reuse the file's existing repo-fixture helpers where present, else these)

```python
def make_remoted_pair(base, name):
    """Shared checkout with a bare origin, one commit pushed, origin/HEAD set."""
    bare = os.path.join(base, name + ".git")
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", bare],
                   check=True)
    root = os.path.join(base, name)
    subprocess.run(["git", "clone", "-q", bare, root], check=True)
    subprocess.run(["git", "-C", root, "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-q", "--allow-empty",
                    "-m", "init"], check=True)
    subprocess.run(["git", "-C", root, "push", "-q", "origin", "main"],
                   check=True)
    subprocess.run(["git", "-C", root, "remote", "set-head", "origin",
                    "main"], check=True)
    return root, bare


def add_branch_commit(root, branch):
    subprocess.run(["git", "-C", root, "branch", branch], check=True)
    subprocess.run(["git", "-C", root, "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-q", "--allow-empty",
                    "-m", "work on " + branch], check=True)
    sha = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"],
                         check=True, capture_output=True,
                         text=True).stdout.strip()
    # move the work to the branch, restore main
    subprocess.run(["git", "-C", root, "update-ref",
                    "refs/heads/" + branch, sha], check=True)
    subprocess.run(["git", "-C", root, "reset", "-q", "--hard",
                    "HEAD~1"], check=True)
    return sha


class TestPushLane(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.learned = os.path.join(self.base, "learned.json")
        self.root, self.bare = make_remoted_pair(self.base, "mine")
        self.registry = {"repos": [{"root": os.path.realpath(self.root),
                                    "head_branch": "main"}]}

    def land(self, branch):
        return landd.process_request(
            {"repo": self.root, "branch": branch}, self.registry,
            demote=False, learned_path=self.learned)

    def bare_main(self):
        return subprocess.run(["git", "-C", self.bare, "rev-parse", "main"],
                              capture_output=True, text=True).stdout.strip()

    def test_push_lane_lands_and_pushes(self):
        sha = add_branch_commit(self.root, "feat")
        res = self.land("feat")
        self.assertEqual(res["status"], "landed")
        self.assertEqual(res["lane"], "push")
        self.assertEqual(self.bare_main(), sha)          # remote advanced
        local = subprocess.run(["git", "-C", self.root, "rev-parse", "main"],
                               capture_output=True, text=True).stdout.strip()
        self.assertEqual(local, sha)                      # local followed

    def test_sync_catches_local_up_before_landing(self):
        # someone else pushed to origin; local main is behind
        other = os.path.join(self.base, "other")
        subprocess.run(["git", "clone", "-q", self.bare, other], check=True)
        subprocess.run(["git", "-C", other, "-c", "user.email=o@o",
                        "-c", "user.name=o", "commit", "-q",
                        "--allow-empty", "-m", "upstream"], check=True)
        subprocess.run(["git", "-C", other, "push", "-q", "origin", "main"],
                       check=True)
        sha = add_branch_commit(self.root, "feat")
        res = self.land("feat")
        # feat does not contain the upstream commit -> must be rejected
        # against the TRUE tip, with the merge fix
        self.assertEqual(res["status"], "rejected")
        self.assertIn("merge", res["reason"])

    def test_local_ahead_of_origin_rejects_loudly(self):
        # invariant broken outside warden: local main has a commit origin lacks
        subprocess.run(["git", "-C", self.root, "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-q",
                        "--allow-empty", "-m", "rogue local"], check=True)
        sha = add_branch_commit(self.root, "feat")
        res = self.land("feat")
        self.assertEqual(res["status"], "rejected")
        self.assertIn("human", res["reason"])

    def test_no_remote_repo_still_lands_local_v1(self):
        solo = os.path.join(self.base, "solo")
        os.makedirs(solo)
        subprocess.run(["git", "-C", solo, "init", "-q", "-b", "main"],
                       check=True)
        subprocess.run(["git", "-C", solo, "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-q",
                        "--allow-empty", "-m", "init"], check=True)
        sha = add_branch_commit(solo, "feat")
        reg = {"repos": [{"root": os.path.realpath(solo),
                          "head_branch": "main"}]}
        res = landd.process_request({"repo": solo, "branch": "feat"}, reg,
                                    demote=False, learned_path=self.learned)
        self.assertEqual((res["status"], res["lane"]), ("landed", "local"))
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_landd.TestPushLane -v`
Expected: FAIL — `process_request` has no `learned_path` kwarg / results lack `lane`.

- [ ] **Step 3: Implement in `landd.py`**

Add `import lanes` at the top. Replace `process_request` and add helpers:

```python
def _land_local(repo, branch, sha, target, demote):
    rc, _, err = _git(repo, "merge", "--ff-only", sha, demote=demote)
    if rc != 0:
        return {"status": "rejected",
                "reason": "%s does not fast-forward %s (%s). In your own "
                          "worktree run: git merge %s, resolve, then land "
                          "again." % (branch, target, err[:200], target)}
    return {"status": "landed", "repo": repo, "branch": branch,
            "target": target, "sha": sha}


def _sync_from_origin(repo, res, demote):
    """Fetch + ff the shared HEAD branch to the remote tip. None on success."""
    remote, db = res["remote"], res["default_branch"]
    rc, _, err = _git(repo, "fetch", remote, db, demote=demote)
    if rc != 0:
        return {"status": "rejected",
                "reason": "fetch %s %s failed (%s); nothing changed" % (
                    remote, db, err[:200])}
    rc, _, err = _git(repo, "merge", "--ff-only",
                      "refs/remotes/%s/%s" % (remote, db), demote=demote)
    if rc != 0:
        return {"status": "rejected",
                "reason": "shared checkout does not fast-forward to %s/%s "
                          "(%s) — local history diverged outside warden; a "
                          "human must look before anything lands here" % (
                              remote, db, err[:120])}
    return None


def _land_push(repo, branch, sha, res, demote):
    err = _sync_from_origin(repo, res, demote)
    if err:
        return err
    remote, db = res["remote"], res["default_branch"]
    rc, _, _ = _git(repo, "merge-base", "--is-ancestor", db, sha,
                    demote=demote)
    if rc != 0:
        return {"status": "rejected",
                "reason": "%s does not fast-forward %s. In your own "
                          "worktree run: git merge %s, resolve, then land "
                          "again." % (branch, db, db)}
    rc, out, errs = _git(repo, "push", "--porcelain", remote,
                         "%s:refs/heads/%s" % (sha, db), demote=demote)
    if rc != 0:
        kind = classify_push_failure(out, errs)
        if kind == "policy":
            return {"verdict": "policy-denied",
                    "evidence": (out + " " + errs)[:300]}
        if kind == "nonff":
            return {"status": "rejected",
                    "reason": "%s moved during landing; land again to retry "
                              "against the new tip" % remote}
        return {"status": "rejected",
                "reason": "push to %s failed (%s); nothing changed" % (
                    remote, errs[:200])}
    rc, _, errs = _git(repo, "merge", "--ff-only", sha, demote=demote)
    if rc != 0:
        return {"status": "landed-remote-only", "sha": sha,
                "reason": "%s accepted %s but the shared checkout could not "
                          "fast-forward (%s); the next land heals it" % (
                              remote, sha[:12], errs[:120])}
    return {"status": "landed", "repo": repo, "branch": branch,
            "target": db, "sha": sha, "pushed": True}


def process_request(req, registry, demote=True,
                    learned_path=lanes.LEARNED_DEFAULT):
    repo = os.path.realpath(str(req.get("repo", "")))
    branch = str(req.get("branch", ""))
    roots = {r["root"]: r for r in registry.get("repos", [])}
    if repo not in roots:
        return {"status": "rejected",
                "reason": "%s is not an adopted repo in the registry" % repo}
    if not branch or branch.startswith("-") or ".." in branch:
        return {"status": "rejected", "reason": "invalid branch name"}
    rc, sha, err = _git(repo, "rev-parse", "--verify", "--quiet",
                        "refs/heads/%s^{commit}" % branch, demote=demote)
    if rc != 0:
        return {"status": "rejected",
                "reason": "branch %s does not exist in %s" % (branch, repo)}
    rc, target, _ = _git(repo, "symbolic-ref", "--short", "HEAD",
                         demote=demote)
    if rc != 0:
        return {"status": "rejected",
                "reason": "%s has a detached HEAD; nothing to land onto" % repo}
    rc, dirty, _ = _git(repo, "status", "--porcelain",
                        "--untracked-files=no", demote=demote)
    if rc != 0 or dirty:
        return {"status": "rejected",
                "reason": "shared checkout %s is dirty; refusing to merge "
                          "over local changes" % repo}
    res = lanes.resolve(repo, learned_path=learned_path)
    if res["lane"] == "local":
        out = _land_local(repo, branch, sha, target, demote)
    else:
        out = _land_push(repo, branch, sha, res, demote)
        # Task 4 replaces this stub with policy-denial routing to the pr lane
        if out.get("verdict") == "policy-denied":
            out = {"status": "rejected", "reason": out["evidence"]}
    out.setdefault("lane", res["lane"])
    out["provenance"] = res["provenance"]
    return out
```

Note: `_land_local` is the existing merge block moved verbatim; the v1 tests exercise it through `process_request` and must stay green.

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_landd -v`
Expected: PASS — all pre-existing tests AND TestPushLane. If any pre-existing test fails, the regression gate failed: fix the implementation, never the old test.

- [ ] **Step 5: Full suite + commit**

```bash
git add landd.py tests/test_landd.py
git commit -m "feat(landd): push lane — sync, verify, push-first, landed-remote-only"
```

---

### Task 4: lessons, `forget`, and policy-denial routing into the pr lane

**Files:**
- Modify: `landd.py`
- Test: `tests/test_landd.py` (new class `TestPolicyLearning`)

**Interfaces:**
- Consumes: `_land_push`'s `policy-denied` verdict (Task 3); `_land_pr` (Task 5 — this task lands first with a minimal `_land_pr` that only pushes the branch, returning `branch-pushed`; Task 5 completes it. That keeps each task shippable).
- Produces: `landd.learn(learned_path, repo, res, evidence)`, `landd.forget(repo, learned_path=lanes.LEARNED_DEFAULT) -> dict|None` (the removed entry), routing inside `process_request`.

- [ ] **Step 1: Write the failing tests**

```python
GH006_HOOK = r"""#!/bin/sh
echo "error: GH006: Protected branch update failed for refs/heads/main." >&2
echo "error: Changes must be made through a pull request." >&2
exit 1
"""


def protect_main(bare):
    hook = os.path.join(bare, "hooks", "pre-receive")
    with open(hook, "w") as f:
        f.write(GH006_HOOK)
    os.chmod(hook, 0o755)


class TestPolicyLearning(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.learned = os.path.join(self.base, "learned.json")
        self.root, self.bare = make_remoted_pair(self.base, "work")
        self.registry = {"repos": [{"root": os.path.realpath(self.root),
                                    "head_branch": "main"}]}

    def land(self, branch):
        return landd.process_request(
            {"repo": self.root, "branch": branch}, self.registry,
            demote=False, learned_path=self.learned)

    def test_policy_denial_falls_back_and_learns(self):
        protect_main(self.bare)
        add_branch_commit(self.root, "feat")
        res = self.land("feat")
        # falls into pr lane: at minimum the branch reached the remote
        self.assertIn(res["status"],
                      ("branch-pushed", "pr-opened", "pr-exists"))
        lesson = json.load(open(self.learned))["repos"][
            os.path.realpath(self.root)]
        self.assertEqual(lesson["lane"], "pr")
        self.assertIn("gh006", lesson["learned_from"].lower())
        # local main did NOT advance to the session sha
        local = subprocess.run(["git", "-C", self.root, "rev-parse", "main"],
                               capture_output=True, text=True).stdout.strip()
        feat = subprocess.run(["git", "-C", self.root, "rev-parse", "feat"],
                              capture_output=True, text=True).stdout.strip()
        self.assertNotEqual(local, feat)

    def test_second_land_goes_straight_to_pr_lane(self):
        protect_main(self.bare)
        add_branch_commit(self.root, "one")
        self.land("one")
        add_branch_commit(self.root, "two")
        res = self.land("two")
        self.assertEqual(res["provenance"], "learned")

    def test_forget_restores_push(self):
        protect_main(self.bare)
        add_branch_commit(self.root, "feat")
        self.land("feat")
        removed = landd.forget(self.root, learned_path=self.learned)
        self.assertEqual(removed["lane"], "pr")
        self.assertEqual(json.load(open(self.learned))["repos"], {})
        os.unlink(os.path.join(self.bare, "hooks", "pre-receive"))
        add_branch_commit(self.root, "next")
        res = self.land("next")
        self.assertEqual((res["status"], res["lane"]), ("landed", "push"))

    def test_ambiguous_rejection_writes_no_lesson(self):
        hook = os.path.join(self.bare, "hooks", "pre-receive")
        with open(hook, "w") as f:
            f.write("#!/bin/sh\necho 'deploy freeze until Friday' >&2\nexit 1\n")
        os.chmod(hook, 0o755)
        add_branch_commit(self.root, "feat")
        res = self.land("feat")
        self.assertEqual(res["status"], "rejected")
        self.assertFalse(os.path.exists(self.learned))
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_landd.TestPolicyLearning -v`
Expected: FAIL — `landd` has no `forget`; policy denial currently maps to `rejected`.

- [ ] **Step 3: Implement in `landd.py`**

```python
import datetime


def _write_learned(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def learn(learned_path, repo, res, evidence):
    try:
        data = json.load(open(learned_path))
    except (OSError, ValueError):
        data = {"version": 1, "repos": {}}
    data.setdefault("repos", {})[repo] = {
        "lane": "pr", "remote_url": res["remote_url"],
        "learned_from": evidence[:200],
        "ts": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")}
    _write_learned(learned_path, data)


def forget(repo, learned_path=lanes.LEARNED_DEFAULT):
    repo = os.path.realpath(repo)
    try:
        data = json.load(open(learned_path))
    except (OSError, ValueError):
        return None
    entry = data.get("repos", {}).pop(repo, None)
    if entry is not None:
        _write_learned(learned_path, data)
    return entry
```

Minimal `_land_pr` for this task (Task 5 completes it):

```python
def _land_pr(repo, branch, res, demote):
    err = _sync_from_origin(repo, res, demote)
    if err:
        return err
    remote = res["remote"]
    rc, out, errs = _git(repo, "push", "--porcelain", remote, branch,
                         demote=demote)
    if rc != 0:
        if any(l.startswith("!") for l in out.splitlines()):
            return {"status": "rejected",
                    "reason": "remote branch %s was refused (%s); reconcile "
                              "in your own worktree and land again" % (
                                  branch, errs[:200])}
        return {"status": "rejected",
                "reason": "push of %s to %s failed (%s); nothing changed" % (
                    branch, remote, errs[:200])}
    return {"status": "branch-pushed", "branch": branch,
            "reason": "branch is on %s; PR creation arrives in Task 5" % remote}
```

Replace the Task 3 routing stub inside `process_request`:

```python
    else:
        out = _land_push(repo, branch, sha, res, demote)
        if out.get("verdict") == "policy-denied":
            learn(learned_path, repo, res, out["evidence"])
            out = _land_pr(repo, branch, res, demote)
            out["learned"] = "pr"
    if res["lane"] == "pr":
        out = _land_pr(repo, branch, res, demote)
```

(Exact final dispatch block:)

```python
    res = lanes.resolve(repo, learned_path=learned_path)
    if res["lane"] == "local":
        out = _land_local(repo, branch, sha, target, demote)
    elif res["lane"] == "pr":
        out = _land_pr(repo, branch, res, demote)
    else:
        out = _land_push(repo, branch, sha, res, demote)
        if out.get("verdict") == "policy-denied":
            learn(learned_path, repo, res, out["evidence"])
            out = _land_pr(repo, branch, res, demote)
            out["learned"] = "pr"
    out.setdefault("lane", res["lane"])
    out["provenance"] = res["provenance"]
    return out
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_landd -v` — Expected: PASS, old tests untouched.

- [ ] **Step 5: Full suite + commit**

```bash
git add landd.py tests/test_landd.py
git commit -m "feat(landd): learn pr from policy denials, route fallback, warden forget backend"
```

---

### Task 5: pr lane — gh PR creation with identity reporting

**Files:**
- Modify: `landd.py` (`_land_pr` completed; `_gh` runner)
- Test: `tests/test_landd.py` (new class `TestPrLane` with a fake `gh` on PATH)

**Interfaces:**
- Consumes: `lanes.gh_accounts` (Task 1), `_sync_from_origin` (Task 3).
- Produces: final `_land_pr(repo, branch, res, demote)` returning `pr-opened` / `pr-exists` / `branch-pushed` / `rejected`, each carrying `account` when gh identity is known.

- [ ] **Step 1: Write the failing tests**

```python
FAKE_GH_OK = """#!/bin/sh
echo "$@" >> "$FAKE_GH_LOG"
echo "https://github.com/acme/app/pull/312"
"""

FAKE_GH_EXISTS = """#!/bin/sh
echo "$@" >> "$FAKE_GH_LOG"
case "$1 $2" in
  "pr create") echo "a pull request for branch \\"feat\\" already exists" >&2; exit 1;;
  "pr view")   echo "https://github.com/acme/app/pull/311";;
esac
"""


class TestPrLane(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.learned = os.path.join(self.base, "learned.json")
        self.root, self.bare = make_remoted_pair(self.base, "acme")
        self.registry = {"repos": [{"root": os.path.realpath(self.root),
                                    "head_branch": "main"}]}
        # declare pr so the lane is entered directly
        with open(os.path.join(self.root, ".warden.json"), "w") as f:
            json.dump({"version": 1, "lane": "pr"}, f)
        subprocess.run(["git", "-C", self.root, "add", ".warden.json"],
                       check=True)
        subprocess.run(["git", "-C", self.root, "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-q", "-m", "declare"],
                       check=True)
        subprocess.run(["git", "-C", self.root, "push", "-q", "origin",
                        "main"], check=True)
        self.ghlog = os.path.join(self.base, "gh.log")
        os.environ["FAKE_GH_LOG"] = self.ghlog

    def _fake_gh(self, script):
        bindir = os.path.join(self.base, "bin")
        os.makedirs(bindir, exist_ok=True)
        gh = os.path.join(bindir, "gh")
        with open(gh, "w") as f:
            f.write(script)
        os.chmod(gh, 0o755)
        self._old_path = os.environ["PATH"]
        os.environ["PATH"] = bindir + os.pathsep + self._old_path
        self.addCleanup(lambda: os.environ.__setitem__("PATH",
                                                       self._old_path))

    def land(self, branch):
        return landd.process_request(
            {"repo": self.root, "branch": branch}, self.registry,
            demote=False, learned_path=self.learned)

    def test_pr_opened(self):
        self._fake_gh(FAKE_GH_OK)
        add_branch_commit(self.root, "feat")
        res = self.land("feat")
        self.assertEqual(res["status"], "pr-opened")
        self.assertEqual(res["url"], "https://github.com/acme/app/pull/312")
        argv = open(self.ghlog).read()
        self.assertIn("pr create --base main --head feat --fill", argv)
        # branch reached the remote
        rc = subprocess.run(["git", "-C", self.bare, "rev-parse",
                             "refs/heads/feat"], capture_output=True)
        self.assertEqual(rc.returncode, 0)

    def test_pr_exists(self):
        self._fake_gh(FAKE_GH_EXISTS)
        add_branch_commit(self.root, "feat")
        res = self.land("feat")
        self.assertEqual(res["status"], "pr-exists")
        self.assertEqual(res["url"], "https://github.com/acme/app/pull/311")

    def test_gh_absent_degrades_to_branch_pushed(self):
        # PATH without gh: point PATH at an empty dir + git's dir
        gitdir = os.path.dirname(subprocess.run(
            ["which", "git"], capture_output=True,
            text=True).stdout.strip())
        old = os.environ["PATH"]
        os.environ["PATH"] = gitdir
        self.addCleanup(lambda: os.environ.__setitem__("PATH", old))
        add_branch_commit(self.root, "feat")
        res = self.land("feat")
        self.assertEqual(res["status"], "branch-pushed")
        self.assertIn("gh", res["reason"])
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_landd.TestPrLane -v`
Expected: FAIL — `pr-opened` never returned (Task 4 stub returns `branch-pushed` with the placeholder reason).

- [ ] **Step 3: Complete `_land_pr` in `landd.py`**

```python
def _gh(repo, *args, demote=True):
    kw = dict(capture_output=True, text=True, timeout=120)
    if demote and os.geteuid() == 0:
        st = os.stat(repo)
        pw = pwd.getpwuid(st.st_uid)

        def demote_fn():
            os.setgid(st.st_gid)
            os.setuid(st.st_uid)
        kw["preexec_fn"] = demote_fn
        kw["env"] = dict(os.environ, HOME=pw.pw_dir,
                         USER=pw.pw_name, LOGNAME=pw.pw_name)
    try:
        p = subprocess.run(["gh"] + list(args), cwd=repo, **kw)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", repr(exc)[:200]
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def _host_of(remote_url):
    u = remote_url or ""
    if "://" in u:
        u = u.split("://", 1)[1]
    if "@" in u:
        u = u.split("@", 1)[1]
    return u.split("/", 1)[0].split(":", 1)[0]


def _acting_account(repo, res, demote):
    st = os.stat(repo)
    home = pwd.getpwuid(st.st_uid).pw_dir if (demote and os.geteuid() == 0) \
        else os.path.expanduser("~")
    accounts = lanes.gh_accounts(_host_of(res["remote_url"]), home)
    return accounts[0] if accounts else None
```

Replace the tail of `_land_pr` (after the successful branch push) with:

```python
    account = _acting_account(repo, res, demote)
    db = res["default_branch"]
    rc, out, errs = _gh(repo, "pr", "create", "--base", db, "--head",
                        branch, "--fill", demote=demote)
    if rc == 0:
        url = out.splitlines()[-1] if out else ""
        return {"status": "pr-opened", "url": url, "branch": branch,
                "account": account}
    if "already exists" in (out + errs).lower():
        rc2, url, _ = _gh(repo, "pr", "view", branch, "--json", "url",
                          "--jq", ".url", demote=demote)
        return {"status": "pr-exists", "url": url if rc2 == 0 else "",
                "branch": branch, "account": account}
    return {"status": "branch-pushed", "branch": branch, "account": account,
            "reason": "branch is on %s but gh could not open the PR as %s "
                      "(%s)" % (res["remote"], account or "unknown",
                                errs[:200])}
```

Note for the `pr view --json url --jq .url` fake: `FAKE_GH_EXISTS` matches on `"$1 $2" = "pr view"` so extra flags are fine.

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_landd -v` — Expected: PASS (Task 4's `TestPolicyLearning` also still passes: with no `gh` on the test PATH its fallback result is `branch-pushed`, which that test accepts).

- [ ] **Step 5: Full suite + commit**

```bash
git add landd.py tests/test_landd.py
git commit -m "feat(landd): pr lane — gh pr create with acting-account reporting"
```

---

### Task 6: result-file sweep + refresh sync (`--sync-all`)

**Files:**
- Modify: `landd.py` (`scan_queue` sweeps old results; `main` gains `--sync-all`)
- Test: `tests/test_landd.py` (new class `TestQueueHygiene`)

**Interfaces:**
- Consumes: `_sync_from_origin` (Task 3), `lanes.resolve` (Task 1).
- Produces: `landd.sweep_results(queue, max_age_days=7)`; `landd.sync_all(registry, demote=True)` → list of per-repo dicts; CLI `python3 landd.py --sync-all` (used by Task 7).

- [ ] **Step 1: Write the failing tests**

```python
class TestQueueHygiene(unittest.TestCase):
    def test_old_results_swept_fresh_kept(self):
        q = tempfile.mkdtemp()
        old = os.path.join(q, "land-1-1.json.result")
        new = os.path.join(q, "land-2-2.json.result")
        for p in (old, new):
            with open(p, "w") as f:
                f.write("{}")
        past = time.time() - 8 * 86400
        os.utime(old, (past, past))
        landd.sweep_results(q, max_age_days=7)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))

    def test_sync_all_catches_up_remoted_repo(self):
        base = tempfile.mkdtemp()
        root, bare = make_remoted_pair(base, "r")
        other = os.path.join(base, "o")
        subprocess.run(["git", "clone", "-q", bare, other], check=True)
        subprocess.run(["git", "-C", other, "-c", "user.email=o@o",
                        "-c", "user.name=o", "commit", "-q",
                        "--allow-empty", "-m", "up"], check=True)
        subprocess.run(["git", "-C", other, "push", "-q", "origin", "main"],
                       check=True)
        reg = {"repos": [{"root": os.path.realpath(root),
                          "head_branch": "main"}]}
        out = landd.sync_all(reg, demote=False)
        self.assertEqual(out[0]["status"], "synced")
        local = subprocess.run(["git", "-C", root, "rev-parse", "main"],
                               capture_output=True, text=True).stdout.strip()
        upstream = subprocess.run(["git", "-C", bare, "rev-parse", "main"],
                                  capture_output=True,
                                  text=True).stdout.strip()
        self.assertEqual(local, upstream)
```

Add `import time` to the test file's imports.

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_landd.TestQueueHygiene -v`
Expected: FAIL — no `sweep_results` / `sync_all`.

- [ ] **Step 3: Implement in `landd.py`**

```python
import time


def sweep_results(queue, max_age_days=7):
    cutoff = time.time() - max_age_days * 86400
    for name in os.listdir(queue):
        if not name.endswith(".result"):
            continue
        p = os.path.join(queue, name)
        try:
            if os.path.getmtime(p) < cutoff:
                os.unlink(p)
        except OSError:
            pass


def sync_all(registry, demote=True):
    out = []
    for r in registry.get("repos", []):
        root = r["root"]
        res = lanes.resolve(root)
        if not res["remote"]:
            continue
        err = _sync_from_origin(root, res, demote)
        out.append({"repo": root,
                    "status": "synced" if err is None else "rejected",
                    "reason": None if err is None else err["reason"]})
    return out


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--sync-all":
        for line in sync_all(load_registry()):
            print(json.dumps(line))
        return 0
    queue = sys.argv[1] if len(sys.argv) > 1 else QUEUE_DEFAULT
    if os.path.isdir(queue):
        sweep_results(queue)
    scan_queue(queue, load_registry())
    return 0
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_landd -v` — Expected: PASS.

- [ ] **Step 5: Full suite + commit**

```bash
git add landd.py tests/test_landd.py
git commit -m "feat(landd): 7-day result sweep, --sync-all for refresh"
```

---

### Task 7: `bin/warden` — forget, lane-aware land output, status lanes, refresh sync

**Files:**
- Modify: `bin/warden`
- Test: `tests/test_land_cli.sh` (extend; it already exercises the CLI against a fake queue)

**Interfaces:**
- Consumes: `landd.forget` (Task 4), `landd.py --sync-all` (Task 6), `lanes.resolve` (Task 1).
- Produces: `warden forget <repo>`, `warden status` lane lines, `warden land` exit 0 on any of `landed|landed-remote-only|pr-opened|pr-exists|branch-pushed`.

- [ ] **Step 1: Write the failing CLI test** (append to `tests/test_land_cli.sh`, which runs `bin/warden` with `WARDEN_LAND_QUEUE` pointed at a temp dir and fakes results)

```sh
# --- lanes: pr-opened is a success and the URL is printed
REQ_DIR=$(mktemp -d); export WARDEN_LAND_QUEUE="$REQ_DIR"
(
  cd "$WORK_REPO"
  "$WARDEN" land feat >"$OUT" 2>&1 &
  LAND_PID=$!
  sleep 1
  REQ=$(ls "$REQ_DIR"/land-*.json)
  printf '{"status":"pr-opened","url":"https://github.com/a/b/pull/9","account":"jayheavner"}' \
    > "$REQ.result"
  wait $LAND_PID
  RC=$?
  grep -q "pr-opened" "$OUT" || fail "pr-opened not printed"
  grep -q "pull/9" "$OUT" || fail "PR URL not printed"
  [ $RC -eq 0 ] || fail "pr-opened should exit 0"
)

# --- forget requires a repo argument
"$WARDEN" forget 2>/dev/null && fail "forget without repo should fail"
echo ok-lanes-cli
```

(Use the file's existing `fail`, `$WARDEN`, `$WORK_REPO`, `$OUT` helpers; if a helper name differs, follow the file's local convention — the assertions above are the contract.)

- [ ] **Step 2: Run to verify failure**

Run: `bash tests/test_land_cli.sh`
Expected: FAIL — `pr-opened` exits 1 under the current `[ status = landed ]` check; `forget` is an unknown subcommand (usage error but exit 0 path differs).

- [ ] **Step 3: Implement in `bin/warden`**

In the `land)` block, replace the result-handling lines:

```sh
      if [ -f "$REQ.result" ]; then
        SUMMARY=$(python3 - "$REQ.result" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1]))
ok = d["status"] in ("landed", "landed-remote-only", "pr-opened",
                     "pr-exists", "branch-pushed")
line = d["status"]
if d.get("url"):
    line += " " + d["url"]
if d.get("account"):
    line += " (as %s)" % d["account"]
print("OK" if ok else "NO")
print(line)
print(d.get("reason") or d.get("sha") or "")
EOF
)
        rm -f "$REQ.result"
        VERDICT=$(echo "$SUMMARY" | sed -n 1p)
        echo "$SUMMARY" | sed -n '2s/^/warden land: /p;3s/^/  /p'
        [ "$VERDICT" = "OK" ] && exit 0 || exit 1
      fi
```

Add the `forget` subcommand (after `land)`):

```sh
  forget)
    # drop a learned lane lesson so inference runs fresh next land
    [ -n "${2:-}" ] || { echo "usage: warden forget <repo-root>" >&2; exit 2; }
    exec python3 - "$2" <<'EOF'
import sys, landd, json
entry = landd.forget(sys.argv[1])
if entry is None:
    print("warden forget: no lesson recorded for %s" % sys.argv[1])
else:
    print("warden forget: dropped %s" % json.dumps(entry))
EOF
    ;;
```

(`landd.py` sits next to `bin/warden`'s install dir; the script already computes `WD` — run python with `cd "$WD"` or `PYTHONPATH="$WD"`, following how `refresh` already invokes `render.py`.)

In the `status)` block, append per-repo lane lines:

```sh
    PYTHONPATH="$WD" python3 - <<'EOF'
import json, lanes
for path in ("/Library/Application Support/ClaudeCode/warden/registry.json",
             "/etc/codex/warden/registry.json"):
    try:
        repos = json.load(open(path)).get("repos", [])
    except (OSError, ValueError):
        continue
    for r in repos:
        res = lanes.resolve(r["root"])
        print("  lane %-5s (%s)  %s" % (res["lane"], res["provenance"],
                                        r["root"]))
EOF
```

In the `refresh)` block, after the render step succeeds, add:

```sh
    PYTHONPATH="$WD" python3 "$WD/landd.py" --sync-all || true
```

Update the usage line:

```sh
    echo "usage: warden {refresh|land [branch]|forget <repo>|selftest|codex-selftest|status}" >&2
```

- [ ] **Step 4: Run to verify pass**

Run: `bash tests/test_land_cli.sh` — Expected: all assertions pass, `ok-lanes-cli` printed.
Run: `python3 -m unittest discover -s tests` — Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add bin/warden tests/test_land_cli.sh
git commit -m "feat(cli): warden forget, lane-aware land output and status, refresh sync"
```

---

### Task 8: selftest + docs

**Files:**
- Modify: `selftest.sh` (one new check), `README.md` (lanes row in the enforcement table area + audit examples), `uninstall.sh` (verify managed-root removal covers `learned.json`; add `rm -f` only if the dir isn't already removed wholesale)

**Interfaces:**
- Consumes: `warden status` lane lines (Task 7).

- [ ] **Step 1: Add the selftest check** (following the existing `T<n>` check pattern in `selftest.sh`)

```sh
# T11: every adopted repo resolves to an integration lane with provenance
if warden status 2>/dev/null | grep -q "lane "; then
  pass "T11 lanes resolved for adopted repos"
else
  fail "T11 warden status shows no lane lines"
fi
```

- [ ] **Step 2: Verify uninstall covers learned.json**

Run: `grep -n "warden" uninstall.sh | grep -i "rm\|Application Support"`
Expected: the managed root `/Library/Application Support/ClaudeCode/warden` is removed recursively (learned.json lives inside it — nothing to add). If it removes individual files instead, add `rm -f "$MANAGED_ROOT/learned.json"` beside them.

- [ ] **Step 3: README** — add to the audit/queries section:

```markdown
## Integration lanes

`warden land` resolves each repo's lane — declared (`.warden.json`) >
learned (the remote's own policy denials, `warden forget` to drop) >
inferred (no remote → local ff; remoted → direct push). PR ceremony only
ever happens when the remote's rules or a committed declaration require
it. `warden status` shows every repo's resolved lane and why.
```

- [ ] **Step 4: Full suite, then commit**

Run: `python3 -m unittest discover -s tests && bash tests/test_land_cli.sh`
Expected: OK / all pass.

```bash
git add selftest.sh README.md uninstall.sh
git commit -m "feat(selftest,docs): lane provenance check T11, README lanes section"
```

---

## Self-Review (performed at plan-writing time)

- **Spec coverage:** §2 resolution → Task 1; §3.1 → Task 3 (`_land_local` extraction + v1 gate); §3.2 → Task 3; §3.3 → Tasks 4–5; §3.4 → Task 2; §4 → Task 4; §5 → Task 1; §6 → Tasks 6–7; §9 fixtures → each task's tests; selftest → Task 8. Not planned (spec-listed, deliberately): audit-payload tally of repeated rejections (§3.2.4) — folded into Task 4's `learn`/result plumbing via the existing `logger` call, no extra task needed; verify during Task 4 that the result dict reaching `scan_queue`'s logger includes `lane`/`learned`.
- **Placeholder scan:** Task 4's `_land_pr` returns a reason naming Task 5 — that is a real intermediate deliverable (branch-pushed is a legitimate terminal status), not a TBD; Task 5 replaces the wording.
- **Type consistency:** `process_request(req, registry, demote, learned_path)` used identically in Tasks 3–5 tests; `res` dict keys from Task 1 (`lane/provenance/remote/remote_url/default_branch/note`) consumed by Tasks 3–5; statuses match the Global Constraints list everywhere.
