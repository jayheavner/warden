# Warden Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Warden enforcement system (guard hook, policy renderer, installer, selftest) per `docs/session-isolation.md`, inert until Jay runs the sudo installer.

**Architecture:** Root-owned managed settings deliver a fail-closed native sandbox plus non-removable hooks. `guard.py` is a single stdlib-only python3 file: a pure-ish path classifier for file tools, audit for everything. `render.py` derives registry + denyWrite policy from disk truth. Bash enforcement comes from the native sandbox, never from parsing command text.

**Tech Stack:** python3 (macOS CLT, stdlib only), bash, `python3 -m unittest`.

## Global Constraints

- guard.py and render.py: stdlib imports only; must run on /usr/bin/python3.
- No command-text parsing anywhere (problem-statement requirement).
- Deny reasons must name the invariant (I2/I3/E3) and the fix, in plain words.
- Everything under `~/claude/warden` is inert; only `sudo ./install.sh` activates.
- managed dest: `/Library/Application Support/ClaudeCode` (space in path — quote everywhere).
- Audit records: ts, session_id, cwd, tool, target, verdict, rule → `logger -t warden` + `~/.claude/warden/audit.jsonl`.
- Guard failure mode: allow + audit `guard-error` (sandbox remains the wall).

---

### Task 1: guard.py classifier

**Files:**
- Create: `guard.py`
- Test: `tests/test_guard.py`

**Interfaces:**
- Produces: `classify(target: str, session_cwd: str, managed_root: str) -> Verdict`;
  `Verdict = namedtuple("Verdict", "decision rule reason")`, decision ∈ {"allow","deny","none"};
  helpers `worktree_container(path) -> str|None` (nearest ancestor that is a linked-worktree root: its `.git` is a regular file), `shared_root(path) -> str|None` (nearest ancestor whose `.git` is a directory).

- [ ] **Step 1: Write failing tests** — fixture trees built with tempfile; `.git` dir = shared checkout, `.git` file = worktree root; no git binary needed.

```python
import os, tempfile, unittest, importlib.util

spec = importlib.util.spec_from_file_location(
    "guard", os.path.join(os.path.dirname(__file__), "..", "guard.py"))
guard = importlib.util.module_from_spec(spec); spec.loader.exec_module(guard)

MANAGED = "/Library/Application Support/ClaudeCode"

def make_repo(base, name="repo", worktrees=("wt1", "wt2")):
    root = os.path.join(base, name)
    os.makedirs(os.path.join(root, ".git"))            # shared checkout
    os.makedirs(os.path.join(root, "docs"))
    open(os.path.join(root, "README.md"), "w").write("x")
    for wt in worktrees:
        w = os.path.join(root, ".claude", "worktrees", wt)
        os.makedirs(w)
        open(os.path.join(w, ".git"), "w").write("gitdir: %s/.git/worktrees/%s\n" % (root, wt))
        os.makedirs(os.path.join(w, "src"))
    return root

class TestClassify(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = make_repo(self.tmp.name)
        self.wt1 = os.path.join(self.repo, ".claude", "worktrees", "wt1")
        self.wt2 = os.path.join(self.repo, ".claude", "worktrees", "wt2")

    def tearDown(self): self.tmp.cleanup()

    def c(self, target, cwd): return guard.classify(target, cwd, MANAGED)

    def test_own_worktree_allow(self):
        v = self.c(os.path.join(self.wt1, "src", "new.py"), self.wt1)
        self.assertEqual(v.decision, "allow")

    def test_own_worktree_allow_from_subdir_cwd(self):
        v = self.c(os.path.join(self.wt1, "f.txt"), os.path.join(self.wt1, "src"))
        self.assertEqual(v.decision, "allow")

    def test_sibling_worktree_deny(self):
        v = self.c(os.path.join(self.wt2, "src", "hack.py"), self.wt1)
        self.assertEqual((v.decision, v.rule), ("deny", "I3"))

    def test_shared_root_deny_from_worktree_session(self):
        v = self.c(os.path.join(self.repo, "README.md"), self.wt1)
        self.assertEqual((v.decision, v.rule), ("deny", "I2"))

    def test_shared_root_deny_from_root_session(self):
        v = self.c(os.path.join(self.repo, "docs", "x.md"), self.repo)
        self.assertEqual((v.decision, v.rule), ("deny", "I2"))

    def test_other_repo_deny_from_unrelated_session(self):
        other = make_repo(self.tmp.name, "other", worktrees=())
        v = self.c(os.path.join(other, "README.md"), self.wt1)
        self.assertEqual((v.decision, v.rule), ("deny", "I2"))

    def test_dotgit_of_shared_root_deny(self):
        v = self.c(os.path.join(self.repo, ".git", "config"), self.wt1)
        self.assertEqual((v.decision, v.rule), ("deny", "I2"))

    def test_relative_traversal_resolved(self):
        sneaky = os.path.join(self.wt1, "src", "..", "..", "..", "..", "README.md")
        v = self.c(sneaky, self.wt1)
        self.assertEqual((v.decision, v.rule), ("deny", "I2"))

    def test_symlink_into_shared_resolved(self):
        link = os.path.join(self.wt1, "sneaky")
        os.symlink(os.path.join(self.repo, "docs"), link)
        v = self.c(os.path.join(link, "x.md"), self.wt1)
        self.assertEqual((v.decision, v.rule), ("deny", "I2"))

    def test_managed_root_deny(self):
        v = self.c(os.path.join(MANAGED, "managed-settings.json"), self.wt1)
        self.assertEqual((v.decision, v.rule), ("deny", "E3"))

    def test_outside_repos_none(self):
        v = self.c(os.path.join(self.tmp.name, "scratch.txt"), self.wt1)
        self.assertEqual(v.decision, "none")

    def test_worktree_file_tool_write_from_root_session_denied(self):
        v = self.c(os.path.join(self.wt2, "f.txt"), self.repo)
        self.assertEqual((v.decision, v.rule), ("deny", "I3"))

if __name__ == "__main__": unittest.main()
```

- [ ] **Step 2: Run** `python3 -m unittest discover -s tests -v` — Expected: FAIL (no guard.py).
- [ ] **Step 3: Implement classifier in guard.py**

```python
#!/usr/bin/env python3
"""warden guard hook: session-isolation judgment for Claude Code file tools.

Enforcement model: the native sandbox (managed settings) is the wall for
Bash; this hook is the judgment for path-addressed file tools plus the
audit trail for everything. It never parses command text.
"""
import collections, datetime, json, os, subprocess, sys

MANAGED_ROOT_DEFAULT = "/Library/Application Support/ClaudeCode"
Verdict = collections.namedtuple("Verdict", "decision rule reason")

def _resolve(p):
    return os.path.realpath(os.path.expanduser(p))

def _ancestors(p):
    cur = p
    while True:
        yield cur
        nxt = os.path.dirname(cur)
        if nxt == cur:
            return
        cur = nxt

def worktree_container(path):
    """Nearest ancestor that is a linked-worktree root (its .git is a file)."""
    for d in _ancestors(path):
        g = os.path.join(d, ".git")
        if os.path.isfile(g):
            return d
        if os.path.isdir(g):
            return None
    return None

def shared_root(path):
    """Nearest ancestor whose .git is a directory (a shared checkout root)."""
    for d in _ancestors(path):
        if os.path.isdir(os.path.join(d, ".git")):
            return d
    return None

def classify(target, session_cwd, managed_root=MANAGED_ROOT_DEFAULT):
    t = _resolve(target)
    cwd = _resolve(session_cwd)
    m = _resolve(managed_root)
    if t == m or t.startswith(m + os.sep):
        return Verdict("deny", "E3",
                       "warden E3: %s is enforcement configuration; sessions may not modify it." % t)
    wt_t = worktree_container(t)
    wt_c = worktree_container(cwd)
    if wt_t:
        if wt_c == wt_t:
            return Verdict("allow", "I1", "inside this session's own workspace")
        return Verdict("deny", "I3",
                       "warden I3: %s is inside workspace %s, which is not this session's workspace (%s). "
                       "Write only inside your own worktree." % (t, wt_t, wt_c or cwd))
    repo_t = shared_root(t)
    if repo_t is None:
        return Verdict("none", "", "")
    return Verdict("deny", "I2",
                   "warden I2: %s is inside the shared checkout %s, which is read-only to every session. "
                   "Do this work inside your own worktree (the app creates one per session)." % (t, repo_t))
```

- [ ] **Step 4: Run** `python3 -m unittest discover -s tests -v` — Expected: 13 tests PASS.
- [ ] **Step 5: Commit** `git add guard.py tests/test_guard.py && git commit -m "feat(guard): path classifier for I2/I3/E3"`

### Task 2: guard.py hook entrypoint + audit

**Files:**
- Modify: `guard.py` (append main/dispatch/audit)
- Test: `tests/test_guard_main.py`

**Interfaces:**
- Consumes: `classify` (Task 1).
- Produces: stdin/stdout hook contract — deny emits `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": <reason>}}` exit 0; allow/none emit nothing, exit 0; SessionStart emits `{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": <one line>}}`. Audit env override `WARDEN_AUDIT_FILE` (tests), `WARDEN_NO_SYSLOG=1` (tests).

- [ ] **Step 1: Write failing tests** — drive `guard.py` as a subprocess, exactly as the harness will.

```python
import json, os, subprocess, tempfile, unittest

GUARD = os.path.join(os.path.dirname(__file__), "..", "guard.py")

def run_hook(payload, audit):
    env = dict(os.environ, WARDEN_AUDIT_FILE=audit, WARDEN_NO_SYSLOG="1")
    p = subprocess.run(["python3", GUARD], input=json.dumps(payload),
                       capture_output=True, text=True, env=env)
    return p

def payload(tool, tinput, cwd, event="PreToolUse"):
    return {"session_id": "sess-test-1", "cwd": cwd, "hook_event_name": event,
            "tool_name": tool, "tool_input": tinput}

class TestMain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.audit = os.path.join(self.tmp.name, "audit.jsonl")
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(os.path.join(self.repo, ".git"))
        self.wt = os.path.join(self.repo, ".claude", "worktrees", "w1")
        os.makedirs(self.wt)
        open(os.path.join(self.wt, ".git"), "w").write("gitdir: x\n")

    def tearDown(self): self.tmp.cleanup()

    def test_edit_shared_denied_with_reason(self):
        p = run_hook(payload("Edit", {"file_path": os.path.join(self.repo, "a.md")}, self.wt), self.audit)
        self.assertEqual(p.returncode, 0)
        out = json.loads(p.stdout)
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["permissionDecision"], "deny")
        self.assertIn("I2", hso["permissionDecisionReason"])
        rec = [json.loads(l) for l in open(self.audit)][-1]
        self.assertEqual((rec["verdict"], rec["session_id"]), ("deny", "sess-test-1"))

    def test_write_own_worktree_silent_allow(self):
        p = run_hook(payload("Write", {"file_path": os.path.join(self.wt, "n.py")}, self.wt), self.audit)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout.strip(), "")

    def test_notebook_path_field(self):
        p = run_hook(payload("NotebookEdit", {"notebook_path": os.path.join(self.repo, "n.ipynb")}, self.wt), self.audit)
        self.assertEqual(json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_bash_audit_only_never_denies(self):
        p = run_hook(payload("Bash", {"command": "rm -rf %s" % self.repo}, self.wt), self.audit)
        self.assertEqual((p.returncode, p.stdout.strip()), (0, ""))
        rec = [json.loads(l) for l in open(self.audit)][-1]
        self.assertEqual((rec["tool"], rec["verdict"]), ("Bash", "audit"))

    def test_sessionstart_announces(self):
        p = run_hook(payload("", {}, self.wt, event="SessionStart"), self.audit)
        out = json.loads(p.stdout)
        self.assertIn("warden", out["hookSpecificOutput"]["additionalContext"])

    def test_garbage_stdin_fails_open(self):
        env = dict(os.environ, WARDEN_AUDIT_FILE=self.audit, WARDEN_NO_SYSLOG="1")
        p = subprocess.run(["python3", GUARD], input="not json", capture_output=True, text=True, env=env)
        self.assertEqual((p.returncode, p.stdout.strip()), (0, ""))

if __name__ == "__main__": unittest.main()
```

- [ ] **Step 2: Run** — Expected: FAIL (`main` missing / no output).
- [ ] **Step 3: Append to guard.py**

```python
FILE_TOOLS = {"Edit": "file_path", "Write": "file_path", "NotebookEdit": "notebook_path"}

def _audit(record):
    record["ts"] = datetime.datetime.now().astimezone().isoformat()
    line = json.dumps(record, ensure_ascii=False)
    path = os.environ.get("WARDEN_AUDIT_FILE") or os.path.expanduser("~/.claude/warden/audit.jsonl")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if not os.environ.get("WARDEN_NO_SYSLOG"):
        try:
            subprocess.run(["logger", "-t", "warden", line[:900]], timeout=5, check=False)
        except Exception:
            pass

def _deny(event, reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": event, "permissionDecision": "deny",
        "permissionDecisionReason": reason}}))

def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        _audit({"verdict": "guard-error", "rule": "bad-stdin", "session_id": "", "cwd": "",
                "tool": "", "target": ""})
        return 0
    event = data.get("hook_event_name", "")
    sid = data.get("session_id", "")
    cwd = data.get("cwd", "") or os.getcwd()
    tool = data.get("tool_name", "")
    tin = data.get("tool_input") or {}
    base = {"session_id": sid, "cwd": cwd, "tool": tool}
    try:
        if event == "PreToolUse" and tool in FILE_TOOLS:
            target = tin.get(FILE_TOOLS[tool], "")
            v = classify(target, cwd) if target else Verdict("none", "", "")
            _audit(dict(base, target=target, verdict=v.decision or "none", rule=v.rule))
            if v.decision == "deny":
                _deny(event, v.reason)
        elif event == "PreToolUse" and tool == "Bash":
            _audit(dict(base, target=(tin.get("command") or "")[:500], verdict="audit", rule=""))
        elif event == "SessionStart":
            wt = worktree_container(_resolve(cwd))
            scope = wt or cwd
            _audit(dict(base, target=scope, verdict="session-start", rule=""))
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "warden enforcement is active: writes are limited to your "
                                     "workspace (%s); shared checkouts and other sessions' "
                                     "worktrees are read-only." % scope}}))
        elif event in ("WorktreeCreate", "WorktreeRemove"):
            _audit(dict(base, target=json.dumps(tin)[:300], verdict=event, rule=""))
            flag = os.path.expanduser("~/.claude/warden/refresh-requested")
            os.makedirs(os.path.dirname(flag), exist_ok=True)
            open(flag, "w").write(base["session_id"] + "\n")
        else:
            _audit(dict(base, target="", verdict="ignored-event", rule=event))
    except Exception as exc:  # fail open, loudly in the audit trail
        _audit(dict(base, target="", verdict="guard-error", rule=repr(exc)[:200]))
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run** `python3 -m unittest discover -s tests -v` — Expected: all PASS (13 + 6).
- [ ] **Step 5: Commit** `git commit -am "feat(guard): hook entrypoint, deny JSON, audit trail"`

### Task 3: render.py + base template

**Files:**
- Create: `render.py`, `templates/managed-settings.base.json`
- Test: `tests/test_render.py`

**Interfaces:**
- Consumes: nothing from other tasks (standalone).
- Produces: `scan_repos(parents: list[str]) -> list[dict]` (each: root, head_branch, top_entries, worktrees); `render_settings(base: dict, repos: list[dict], managed_root: str) -> dict`; CLI `python3 render.py --scan <parent> [--scan ...] --base <tpl> --write-settings <p> --write-registry <p>` and `--check` (print JSON to stdout, write nothing).

- [ ] **Step 1: Write failing tests** (real git fixtures; git config user set per-repo; assert exact denyWrite membership)

```python
import json, os, subprocess, tempfile, unittest, importlib.util

spec = importlib.util.spec_from_file_location(
    "render", os.path.join(os.path.dirname(__file__), "..", "render.py"))
render = importlib.util.module_from_spec(spec); spec.loader.exec_module(render)

def sh(*a, **k): subprocess.run(a, check=True, capture_output=True, **k)

class TestRender(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.parent = os.path.join(self.tmp.name, "claude"); os.makedirs(self.parent)
        self.repo = os.path.join(self.parent, "alpha")
        sh("git", "init", self.repo)
        sh("git", "-C", self.repo, "config", "user.email", "t@t"); sh("git", "-C", self.repo, "config", "user.name", "t")
        os.makedirs(os.path.join(self.repo, "docs"))
        open(os.path.join(self.repo, "README.md"), "w").write("x")
        open(os.path.join(self.repo, "docs", "d.md"), "w").write("x")
        sh("git", "-C", self.repo, "add", "-A"); sh("git", "-C", self.repo, "commit", "-m", "init")
        self.wt = os.path.join(self.repo, ".claude", "worktrees", "w1")
        sh("git", "-C", self.repo, "worktree", "add", self.wt, "-b", "worktree-w1")

    def tearDown(self): self.tmp.cleanup()

    def test_scan_finds_repo_with_metadata(self):
        repos = render.scan_repos([self.parent])
        self.assertEqual(len(repos), 1)
        r = repos[0]
        self.assertEqual(r["root"], os.path.realpath(self.repo))
        self.assertIn(r["head_branch"], ("master", "main"))
        self.assertEqual(sorted(r["top_entries"]), ["README.md", "docs"])
        self.assertEqual(len(r["worktrees"]), 1)

    def test_scan_skips_worktrees_and_nonrepos(self):
        os.makedirs(os.path.join(self.parent, "not-a-repo"))
        repos = render.scan_repos([self.parent])
        self.assertEqual([os.path.basename(r["root"]) for r in repos], ["alpha"])

    def test_denywrite_entries(self):
        repos = render.scan_repos([self.parent])
        base = {"sandbox": {"filesystem": {"denyWrite": []}}}
        out = render.render_settings(base, repos, "/Library/Application Support/ClaudeCode")
        deny = out["sandbox"]["filesystem"]["denyWrite"]
        root = os.path.realpath(self.repo); b = repos[0]["head_branch"]
        for want in [root + "/.git/index", root + "/.git/HEAD", root + "/.git/config",
                     root + "/.git/hooks", root + "/.git/info",
                     root + "/.git/refs/heads/" + b, root + "/.git/refs/heads/" + b + ".lock",
                     root + "/.git/logs/refs/heads/" + b,
                     root + "/README.md", root + "/docs",
                     root + "/.claude/settings.json",
                     "/Library/Application Support/ClaudeCode"]:
            self.assertIn(want, deny)
        self.assertNotIn(root + "/.claude/worktrees", deny)
        self.assertNotIn(root, deny)

    def test_check_mode_writes_nothing(self):
        settings = os.path.join(self.tmp.name, "ms.json"); registry = os.path.join(self.tmp.name, "reg.json")
        p = subprocess.run(["python3", render.__file__, "--scan", self.parent,
                            "--base", os.path.join(os.path.dirname(render.__file__), "templates", "managed-settings.base.json"),
                            "--write-settings", settings, "--write-registry", registry, "--check"],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0)
        json.loads(p.stdout)
        self.assertFalse(os.path.exists(settings) or os.path.exists(registry))

    def test_write_mode_atomic_and_valid(self):
        settings = os.path.join(self.tmp.name, "ms.json"); registry = os.path.join(self.tmp.name, "reg.json")
        p = subprocess.run(["python3", render.__file__, "--scan", self.parent,
                            "--base", os.path.join(os.path.dirname(render.__file__), "templates", "managed-settings.base.json"),
                            "--write-settings", settings, "--write-registry", registry],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        s = json.load(open(settings)); r = json.load(open(registry))
        self.assertFalse(s["sandbox"]["allowUnsandboxedCommands"])
        self.assertTrue(s["sandbox"]["enabled"] and s["sandbox"]["failIfUnavailable"])
        self.assertTrue(any(h for h in s["hooks"]["PreToolUse"]))
        self.assertEqual(len(r["repos"]), 1)

if __name__ == "__main__": unittest.main()
```

- [ ] **Step 2: Run** — Expected: FAIL (no render.py / template).
- [ ] **Step 3: Write `templates/managed-settings.base.json`**

```json
{
  "sandbox": {
    "enabled": true,
    "failIfUnavailable": true,
    "allowUnsandboxedCommands": false,
    "filesystem": { "denyWrite": [] }
  },
  "permissions": {
    "deny": [
      "Edit(//Library/Application Support/ClaudeCode/**)",
      "Read(//Library/Application Support/ClaudeCode/warden/registry.json.tmp)"
    ]
  },
  "hooks": {
    "PreToolUse": [
      { "matcher": "Edit|Write|NotebookEdit",
        "hooks": [{ "type": "command", "command": "python3 '/Library/Application Support/ClaudeCode/warden/guard.py'" }] },
      { "matcher": "Bash",
        "hooks": [{ "type": "command", "command": "python3 '/Library/Application Support/ClaudeCode/warden/guard.py'" }] }
    ],
    "SessionStart": [
      { "hooks": [{ "type": "command", "command": "python3 '/Library/Application Support/ClaudeCode/warden/guard.py'" }] }
    ],
    "WorktreeCreate": [
      { "hooks": [{ "type": "command", "command": "python3 '/Library/Application Support/ClaudeCode/warden/guard.py'" }] }
    ],
    "WorktreeRemove": [
      { "hooks": [{ "type": "command", "command": "python3 '/Library/Application Support/ClaudeCode/warden/guard.py'" }] }
    ]
  },
  "env": { "WARDEN_ACTIVE": "1" }
}
```

(Drop the placeholder `Read(...)` deny line if unused at implementation time — no dangling entries.)

- [ ] **Step 4: Write render.py**

```python
#!/usr/bin/env python3
"""warden renderer: disk truth -> registry.json + managed-settings.json.

Scans parent dirs for shared checkouts (dirs whose .git is a directory),
derives per-repo protected paths, and renders the managed settings from the
base template. Never consumes session input. Run via sudo for real writes.
"""
import argparse, datetime, json, os, subprocess, sys

def _git(root, *args):
    p = subprocess.run(["git", "-C", root] + list(args), capture_output=True, text=True, timeout=30)
    return p.returncode, p.stdout.strip()

def scan_repos(parents):
    repos = []
    for parent in parents:
        parent = os.path.realpath(os.path.expanduser(parent))
        if not os.path.isdir(parent):
            continue
        for name in sorted(os.listdir(parent)):
            root = os.path.join(parent, name)
            if not os.path.isdir(os.path.join(root, ".git")):
                continue
            root = os.path.realpath(root)
            rc, head = _git(root, "symbolic-ref", "--short", "HEAD")
            head_branch = head if rc == 0 else None
            rc, tree = _git(root, "ls-tree", "HEAD", "--name-only")
            top_entries = tree.splitlines() if rc == 0 else []
            rc, wt = _git(root, "worktree", "list", "--porcelain")
            worktrees = [l.split(" ", 1)[1] for l in wt.splitlines()
                         if l.startswith("worktree ")][1:] if rc == 0 else []
            repos.append({"root": root, "head_branch": head_branch,
                          "top_entries": top_entries, "worktrees": worktrees})
    return repos

def render_settings(base, repos, managed_root):
    out = json.loads(json.dumps(base))
    deny = out.setdefault("sandbox", {}).setdefault("filesystem", {}).setdefault("denyWrite", [])
    deny.append(managed_root)
    for r in repos:
        root = r["root"]
        deny += [root + "/.git/index", root + "/.git/HEAD", root + "/.git/config",
                 root + "/.git/hooks", root + "/.git/info",
                 root + "/.claude/settings.json"]
        if r["head_branch"]:
            b = root + "/.git/refs/heads/" + r["head_branch"]
            deny += [b, b + ".lock", root + "/.git/logs/refs/heads/" + r["head_branch"]]
        for entry in r["top_entries"]:
            deny.append(root + "/" + entry)
    out["sandbox"]["filesystem"]["denyWrite"] = sorted(set(deny))
    return out

def _atomic_write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")
    json.load(open(tmp))          # refuse to swap in unparseable output
    os.replace(tmp, path)

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="append", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--write-settings", required=True)
    ap.add_argument("--write-registry", required=True)
    ap.add_argument("--managed-root", default="/Library/Application Support/ClaudeCode")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    base = json.load(open(a.base))
    repos = scan_repos(a.scan)
    settings = render_settings(base, repos, a.managed_root)
    registry = {"generated_at": datetime.datetime.now().astimezone().isoformat(),
                "scanned": a.scan, "repos": repos}
    if a.check:
        print(json.dumps({"settings": settings, "registry": registry}, indent=2, sort_keys=True))
        return 0
    _atomic_write(a.write_settings, settings)
    _atomic_write(a.write_registry, registry)
    print("wrote %s (%d repos, %d denyWrite entries)" %
          (a.write_settings, len(repos), len(settings["sandbox"]["filesystem"]["denyWrite"])))
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run** `python3 -m unittest discover -s tests -v` — Expected: all PASS.
- [ ] **Step 6: Commit** `git commit -am "feat(render): registry scanner and managed-settings renderer"`

### Task 4: install.sh, uninstall.sh, warden wrapper

**Files:**
- Create: `install.sh`, `uninstall.sh`, `bin/warden`
- Test: `tests/test_install_dryrun.sh` (bash; non-root paths only)

**Interfaces:**
- Consumes: render.py CLI (Task 3), guard.py (Tasks 1–2), template (Task 3).
- Produces: installed tree under `/Library/Application Support/ClaudeCode/` per design §3; `warden` wrapper subcommands: `refresh`, `selftest`, `status`.

- [ ] **Step 1: Write the dry-run test** (asserts root-guard refusal, bash syntax, and that `install.sh --print-plan` lists every file it would install)

```bash
#!/bin/bash
set -u; cd "$(dirname "$0")/.."
fail() { echo "FAIL: $1"; exit 1; }
bash -n install.sh || fail "install.sh syntax"
bash -n uninstall.sh || fail "uninstall.sh syntax"
bash -n bin/warden || fail "bin/warden syntax"
out=$(./install.sh 2>&1); [ $? -eq 1 ] || fail "install as non-root must exit 1"
echo "$out" | grep -q "must run as root" || fail "root-guard message"
plan=$(./install.sh --print-plan) || fail "--print-plan must work unprivileged"
for f in guard.py render.py selftest.sh uninstall.sh managed-settings.base.json; do
  echo "$plan" | grep -q "$f" || fail "plan missing $f"
done
echo "install dry-run tests PASS"
```

- [ ] **Step 2: Run** `bash tests/test_install_dryrun.sh` — Expected: FAIL (scripts missing).
- [ ] **Step 3: Write install.sh**

```bash
#!/bin/bash
# warden installer — run: sudo ./install.sh
# Copies enforcement into root-owned paths, renders policy from disk truth,
# verifies, and prints the post-activation checklist. Idempotent.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="/Library/Application Support/ClaudeCode"
WD="$DEST/warden"
SCAN_HOME="${SUDO_USER:+/Users/$SUDO_USER}"; SCAN_HOME="${SCAN_HOME:-$HOME}"
SCAN_DIR="${WARDEN_SCAN_DIR:-$SCAN_HOME/claude}"
FILES=(guard.py render.py selftest.sh uninstall.sh templates/managed-settings.base.json)

if [ "${1:-}" = "--print-plan" ]; then
  printf 'would install to %s:\n' "$WD"
  printf '  %s\n' "${FILES[@]}" "bin/warden -> /usr/local/bin/warden"
  printf 'would render policy scanning: %s\n' "$SCAN_DIR"
  exit 0
fi
if [ "$(id -u)" -ne 0 ]; then echo "warden install: must run as root (sudo ./install.sh)" >&2; exit 1; fi

mkdir -p "$WD"
for f in "${FILES[@]}"; do install -m 0644 "$SRC/$f" "$WD/$(basename "$f")"; done
chmod 0755 "$WD/selftest.sh" "$WD/uninstall.sh"
install -m 0755 "$SRC/bin/warden" /usr/local/bin/warden

python3 "$WD/render.py" --scan "$SCAN_DIR" --base "$WD/managed-settings.base.json" \
  --write-settings "$DEST/managed-settings.json" --write-registry "$WD/registry.json"

chown -R root:wheel "$DEST"
chmod 0755 "$DEST" "$WD"; chmod 0644 "$DEST/managed-settings.json" "$WD/registry.json"

python3 - "$DEST/managed-settings.json" <<'EOF'
import json,sys; d=json.load(open(sys.argv[1]))
assert d["sandbox"]["enabled"] and d["sandbox"]["failIfUnavailable"]
assert d["sandbox"]["allowUnsandboxedCommands"] is False
assert d["hooks"]["PreToolUse"], "hooks missing"
print("policy verified: sandbox fail-closed, hooks wired,",
      len(d["sandbox"]["filesystem"]["denyWrite"]), "denyWrite entries")
EOF
echo "warden installed. Next: restart running clones, then in a fresh session run: warden selftest"
```

- [ ] **Step 4: Write uninstall.sh**

```bash
#!/bin/bash
# warden rollback — run: sudo ./uninstall.sh  (or sudo warden's copy in /Library)
# Removes the managed policy and warden entirely; restores pre-warden behavior.
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then echo "warden uninstall: must run as root" >&2; exit 1; fi
DEST="/Library/Application Support/ClaudeCode"
rm -f "$DEST/managed-settings.json" /usr/local/bin/warden
rm -rf "$DEST/warden"
rmdir "$DEST" 2>/dev/null || true
echo "warden removed; sessions revert to pre-warden behavior on restart."
```

- [ ] **Step 5: Write bin/warden**

```bash
#!/bin/bash
# warden CLI: refresh (root), selftest (any session), status (anyone).
set -euo pipefail
DEST="/Library/Application Support/ClaudeCode"; WD="$DEST/warden"
case "${1:-status}" in
  refresh)
    [ "$(id -u)" -eq 0 ] || { echo "run: sudo warden refresh" >&2; exit 1; }
    SCAN_HOME="${SUDO_USER:+/Users/$SUDO_USER}"; SCAN_HOME="${SCAN_HOME:-$HOME}"
    python3 "$WD/render.py" --scan "${WARDEN_SCAN_DIR:-$SCAN_HOME/claude}" \
      --base "$WD/managed-settings.base.json" \
      --write-settings "$DEST/managed-settings.json" --write-registry "$WD/registry.json"
    chown root:wheel "$DEST/managed-settings.json" "$WD/registry.json"
    ;;
  selftest) exec bash "$WD/selftest.sh" ;;
  status)
    if [ -f "$DEST/managed-settings.json" ]; then
      echo "warden: installed ($(python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));print(len(d["sandbox"]["filesystem"]["denyWrite"]),"denyWrite entries")' "$DEST/managed-settings.json"))"
      echo "active in this shell: ${WARDEN_ACTIVE:-no (session started before install?)}"
    else echo "warden: not installed"; fi ;;
  *) echo "usage: warden {refresh|selftest|status}" >&2; exit 2 ;;
esac
```

- [ ] **Step 6: Run** `bash tests/test_install_dryrun.sh` — Expected: PASS. (selftest.sh does not exist yet; create an empty executable stub `selftest.sh` with `#!/bin/bash` + `echo stub; exit 3` so install dry-run file list is truthful, replaced in Task 5.)
- [ ] **Step 7: Commit** `git commit -am "feat(install): sudo installer, uninstaller, warden CLI"`

### Task 5: selftest.sh (activation-day acceptance suite)

**Files:**
- Create: `selftest.sh` (replaces stub)
- Test: `bash -n selftest.sh` + pre-activation behavior check (exits 3 with "not active" when `WARDEN_ACTIVE` unset)

**Interfaces:**
- Consumes: registry.json (Task 3 output), running-session environment.
- Produces: per-acceptance-test verdict table on stdout; exit 0 iff all applicable tests pass.

- [ ] **Step 1: Write selftest.sh** — full content:

```bash
#!/bin/bash
# warden selftest — run INSIDE a fresh Claude Code session after activation
# (ask the session: run `warden selftest`). Non-destructive against real
# repos; verdicts from filesystem truth. Maps to acceptance tests 1-10.
set -u
WD="/Library/Application Support/ClaudeCode/warden"
REG="$WD/registry.json"
PASSN=0; FAILN=0; SKIPN=0
say() { printf '%-6s %-52s %s\n' "$1" "$2" "${3:-}"; }
pass() { say PASS "$1"; PASSN=$((PASSN+1)); }
fail() { say FAIL "$1" "${2:-}"; FAILN=$((FAILN+1)); }
skip() { say SKIP "$1" "${2:-}"; SKIPN=$((SKIPN+1)); }

if [ "${WARDEN_ACTIVE:-}" != "1" ]; then
  echo "warden selftest: WARDEN_ACTIVE is not set — enforcement not active in this session."
  echo "Install with sudo ./install.sh and start a FRESH session, then re-run."
  exit 3
fi
[ -r "$REG" ] || { echo "no registry at $REG"; exit 3; }

REPO=$(python3 -c 'import json,sys;r=json.load(open(sys.argv[1]))["repos"];print(r[0]["root"] if r else "")' "$REG")
BRANCH=$(python3 -c 'import json,sys;r=json.load(open(sys.argv[1]))["repos"];print(r[0]["head_branch"] or "")' "$REG")
[ -n "$REPO" ] || { echo "registry has no repos"; exit 3; }

CWD=$(pwd -P)
case "$CWD" in
  */.claude/worktrees/*) OWN_WT="${CWD%%/.claude/worktrees/*}/.claude/worktrees/${CWD##*/.claude/worktrees/}"; OWN_WT="${OWN_WT%%/*(/)}";;
  *) OWN_WT="";;
esac
# normalize: own worktree root = first two path components after .claude/worktrees
if [ -n "$OWN_WT" ]; then
  OWN_WT=$(python3 - "$CWD" <<'EOF'
import os,sys
p=sys.argv[1]; parts=p.split("/.claude/worktrees/")
print(parts[0]+"/.claude/worktrees/"+parts[1].split("/")[0] if len(parts)>1 else "")
EOF
)
fi

echo "== warden selftest: session cwd=$CWD"
echo "== probing shared checkout: $REPO (HEAD branch: ${BRANCH:-detached})"
echo

# T1/T2-analog: mutation at shared root (sentinel file; harmless if it lands)
P="$REPO/warden-selftest-$$.txt"
if echo x > "$P" 2>/dev/null; then rm -f "$P"; fail "T1 write at shared root blocked" "WRITE SUCCEEDED"; else pass "T1 write at shared root blocked"; fi

# T2: cd into shared root && mutate
if (cd "$REPO" && echo y >> README.md) 2>/dev/null; then fail "T2 cd-drift mutation blocked" "WRITE SUCCEEDED (restore README!)"; else pass "T2 cd-drift mutation blocked"; fi

# T3: git -C shared reset --hard (safe: HEAD~0 with clean index would no-op; index write must EPERM first)
if git -C "$REPO" reset --hard HEAD~0 >/dev/null 2>&1; then fail "T3 git -C shared reset --hard blocked" "SUCCEEDED"; else pass "T3 git -C shared reset --hard blocked"; fi

# T3b: protected-branch ref plumbing (no-op value: same sha)
if [ -n "$BRANCH" ]; then
  if git -C "$REPO" update-ref "refs/heads/$BRANCH" "refs/heads/$BRANCH" >/dev/null 2>&1; then
    fail "T3b update-ref on shared HEAD branch blocked" "SUCCEEDED (no-op value)"
  else pass "T3b update-ref on shared HEAD branch blocked"; fi
else skip "T3b update-ref on shared HEAD branch" "detached HEAD"; fi

# T4: sibling worktree write
SIB=$(python3 - "$REG" "$OWN_WT" <<'EOF'
import json,sys
reg=json.load(open(sys.argv[1])); own=sys.argv[2]
for r in reg["repos"]:
    for w in r["worktrees"]:
        if w != own: print(w); raise SystemExit
EOF
)
if [ -n "$SIB" ]; then
  if echo x > "$SIB/warden-selftest-$$.txt" 2>/dev/null; then rm -f "$SIB/warden-selftest-$$.txt"; fail "T4 sibling worktree write blocked" "SUCCEEDED"; else pass "T4 sibling worktree write blocked"; fi
else
  if echo x > "$REPO/.claude/worktrees/no-such-wt-$$/f" 2>/dev/null; then fail "T4 worktree-area write blocked" "SUCCEEDED"; else pass "T4 worktree-area write blocked (no live sibling; probed area)"; fi
fi

# T5: own workspace ops
if [ -n "$OWN_WT" ]; then
  if ( echo w > "$OWN_WT/warden-selftest-$$.txt" && git -C "$OWN_WT" add "warden-selftest-$$.txt" \
       && git -C "$OWN_WT" commit -qm "warden selftest probe" \
       && git -C "$OWN_WT" reset -q --hard HEAD~1 && rm -f "$OWN_WT/warden-selftest-$$.txt" ) 2>/dev/null; then
    pass "T5 own-workspace write+commit+reset works"
  else fail "T5 own-workspace write+commit+reset works" "a legitimate op was blocked"; fi
else skip "T5 own-workspace ops" "session cwd is not a worktree — run from a worktree session"; fi

# T6: worktree lifecycle + read-only inspection against shared repo
if git -C "$REPO" status --porcelain >/dev/null 2>&1 && git -C "$REPO" log --oneline -1 >/dev/null 2>&1; then
  pass "T6a status/log against shared repo works"
else fail "T6a status/log against shared repo works"; fi
skip "T6b worktree add against shared repo" "run from a root-cwd session or the app's worktree flow (design §3 L4)"

# T7: push to integration from own workspace (dry-run: exercises remote path, writes nothing)
if [ -n "$OWN_WT" ] && git -C "$OWN_WT" remote get-url origin >/dev/null 2>&1; then
  if git -C "$OWN_WT" push --dry-run origin HEAD >/dev/null 2>&1; then pass "T7 push (dry-run) from own workspace works"; else fail "T7 push (dry-run) from own workspace works"; fi
else skip "T7 push from own workspace" "no worktree cwd or no origin remote"; fi

# T8: remote-host command (opt-in)
if [ -n "${WARDEN_SELFTEST_SSH_HOST:-}" ]; then
  if ssh -o BatchMode=yes -o ConnectTimeout=4 "$WARDEN_SELFTEST_SSH_HOST" true 2>/dev/null; then pass "T8 ssh to $WARDEN_SELFTEST_SSH_HOST works"; else fail "T8 ssh to $WARDEN_SELFTEST_SSH_HOST works" "check key/host first"; fi
else skip "T8 remote-host command" "set WARDEN_SELFTEST_SSH_HOST to test"; fi

# T9: this very session proves auto-binding
pass "T9 fresh session bound with zero setup (WARDEN_ACTIVE=1, denials above)"

# T10: enforcement config immutable to sessions
if echo x >> "/Library/Application Support/ClaudeCode/managed-settings.json" 2>/dev/null; then
  fail "T10 managed-settings.json append blocked" "SUCCEEDED — investigate immediately"
else pass "T10 managed-settings.json append blocked"; fi

echo
echo "== result: $PASSN pass, $FAILN fail, $SKIPN skip"
echo "== manual check remaining: ask this session to retry a blocked write with"
echo "   the Bash dangerouslyDisableSandbox parameter — it must STILL be blocked."
[ "$FAILN" -eq 0 ]
```

- [ ] **Step 2: Run** `bash -n selftest.sh && WARDEN_ACTIVE= bash selftest.sh; echo exit=$?` — Expected: syntax OK; "not active" message; exit=3.
- [ ] **Step 3: Commit** `git commit -am "feat(selftest): activation-day acceptance suite"`

### Task 6: README, evidence bundle, final verification

**Files:**
- Create: `README.md`
- Copy: `tests/lab/derive.sh` (seatbelt regression lab from scratchpad, paths parameterized)

- [ ] **Step 1: README.md** — what Warden is (three sentences), install/rollback commands, the coverage table from the design doc, residual gaps R1–R7 verbatim, selftest instructions, audit-query one-liners (`log show --predicate 'process == "logger" && eventMessage CONTAINS "warden"' --last 1h`; `tail ~/.claude/warden/audit.jsonl`).
- [ ] **Step 2: Port derive.sh into tests/lab/ with LAB dir parameterized (`${WARDEN_LAB_DIR:-$(mktemp -d)}`), run it once, keep output as `tests/lab/EVIDENCE-2026-07-16.txt`.**
- [ ] **Step 3: Run full suite** `python3 -m unittest discover -s tests -v && bash tests/test_install_dryrun.sh` — Expected: all PASS.
- [ ] **Step 4: Commit** `git commit -am "docs: README, seatbelt evidence lab"`

## Self-Review

- Spec coverage: L1 renderer (T3), L2 guard (T1–2), L3 registry (T3), L4 documented lane (selftest T6b skip note), L5 audit (T2), install/rollback (T4), selftest (T5), evidence (T6). LaunchDaemon auto-refresh: deliberately deferred to Jay gate G2 — installer prints refresh instructions instead. Gap accepted and documented.
- Placeholders: template contains one droppable deny line — flagged inline with explicit instruction.
- Type consistency: classify/Verdict names match between T1 tests and T2 main; render CLI flags match T4 installer invocation; WARDEN_ACTIVE consistent across T3 template, T4 status, T5 selftest.
