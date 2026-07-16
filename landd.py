#!/usr/bin/env python3
"""warden landing daemon: zero-tax integration to the shared HEAD branch.

Sessions are sandboxed to their own worktrees and can never write the
shared checkout — by design. Integration therefore runs OUT of session:
a session drops a request into the queue (via `warden land`), and this
root-owned daemon fast-forwards the shared checkout's HEAD branch to the
named branch, running git as the repo's owner so no root-owned files land
in the working tree. ff-only: history is never rewritten and diverged
requests are rejected with the fix (merge the HEAD branch into your
branch in your own worktree, then re-land).
"""
import json
import os
import pwd
import subprocess
import sys

QUEUE_DEFAULT = "/tmp/claude/warden-land"
REGISTRIES = ["/Library/Application Support/ClaudeCode/warden/registry.json",
              "/etc/codex/warden/registry.json"]


def _git(root, *args, demote=True):
    kw = dict(capture_output=True, text=True, timeout=60)
    if demote and os.geteuid() == 0:
        st = os.stat(root)
        pw = pwd.getpwuid(st.st_uid)

        def demote_fn():
            os.setgid(st.st_gid)
            os.setuid(st.st_uid)
        kw["preexec_fn"] = demote_fn
        kw["env"] = dict(os.environ, HOME=pw.pw_dir,
                         USER=pw.pw_name, LOGNAME=pw.pw_name)
    p = subprocess.run(["git", "-C", root] + list(args), **kw)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def process_request(req, registry, demote=True):
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
    rc, _, err = _git(repo, "merge", "--ff-only", sha, demote=demote)
    if rc != 0:
        return {"status": "rejected",
                "reason": "%s does not fast-forward %s (%s). In your own "
                          "worktree run: git merge %s, resolve, then land "
                          "again." % (branch, target, err[:200], target)}
    return {"status": "landed", "repo": repo, "branch": branch,
            "target": target, "sha": sha}


def load_registry():
    repos = []
    for path in REGISTRIES:
        try:
            repos += json.load(open(path)).get("repos", [])
        except OSError:
            continue
        except ValueError:
            continue
    return {"repos": repos}


def scan_queue(queue, registry, demote=True):
    if not os.path.isdir(queue):
        return
    for name in sorted(os.listdir(queue)):
        if not (name.startswith("land-") and name.endswith(".json")):
            continue
        req_path = os.path.join(queue, name)
        try:
            req = json.load(open(req_path))
            res = process_request(req, registry, demote=demote)
        except ValueError:
            res = {"status": "rejected", "reason": "request is not JSON"}
        except Exception as exc:
            res = {"status": "rejected", "reason": repr(exc)[:200]}
        tmp = req_path + ".result.tmp"
        with open(tmp, "w") as f:
            json.dump(res, f)
        os.chmod(tmp, 0o644)
        os.replace(tmp, req_path + ".result")
        os.unlink(req_path)
        try:
            subprocess.run(["logger", "-t", "warden",
                            json.dumps(dict(res, event="land"))[:900]],
                           timeout=5, check=False)
        except Exception:
            pass


def main():
    queue = sys.argv[1] if len(sys.argv) > 1 else QUEUE_DEFAULT
    scan_queue(queue, load_registry())
    return 0


if __name__ == "__main__":
    sys.exit(main())
