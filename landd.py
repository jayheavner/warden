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
import datetime
import json
import os
import pwd
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lanes  # noqa: E402

QUEUE_DEFAULT = "/tmp/claude/warden-land"

# Curated policy-denial patterns. Extend ONLY by adding a captured real
# transcript to tests/test_landd.py TestClassifier — never from memory.
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


def _write_learned(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
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
    if demote and os.geteuid() == 0:
        home = pwd.getpwuid(os.stat(repo).st_uid).pw_dir
    else:
        home = os.path.expanduser("~")
    accounts = lanes.gh_accounts(_host_of(res["remote_url"]), home)
    return accounts[0] if accounts else None


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
    # ff-only merge is a silent no-op when local is AHEAD of origin, so the
    # invariant (local never holds commits origin lacks) needs its own check.
    rc, ahead, _ = _git(repo, "rev-list", "--count",
                        "refs/remotes/%s/%s..HEAD" % (remote, db),
                        demote=demote)
    if rc != 0 or ahead != "0":
        return {"status": "rejected",
                "reason": "shared checkout holds %s commit(s) that %s/%s "
                          "does not — local history diverged outside warden; "
                          "a human must look before anything lands here" % (
                              ahead or "?", remote, db)}
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


REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# https or ssh git URLs only: no file://, ext::, local paths, options, or
# whitespace — the daemon writes remote.<name>.url into a root-protected
# .git/config, so anything a git transport could execute is rejected.
REMOTE_URL_RE = re.compile(
    r"^(https://[A-Za-z0-9.-]+(:\d+)?/[A-Za-z0-9._~/-]+"
    r"|ssh://([A-Za-z0-9._-]+@)?[A-Za-z0-9.-]+(:\d+)?/[A-Za-z0-9._~/-]+"
    r"|[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[A-Za-z0-9._~/-]+)\Z")


def process_remote_request(req, registry, demote=True):
    """Mediated `git remote add`: sessions can't write the shared checkout's
    .git/config (denyWrite blocks hook/credential-helper injection), so this
    daemon applies remote.<name>.url — and nothing else — after validating
    the repo is adopted, the name is a plain remote name, and the URL is a
    plausible https/ssh git URL."""
    repo = os.path.realpath(str(req.get("repo", "")))
    name = str(req.get("name", ""))
    url = str(req.get("url", ""))
    roots = {r["root"] for r in registry.get("repos", [])}
    if repo not in roots:
        return {"status": "rejected",
                "reason": "%s is not an adopted repo in the registry" % repo}
    if not REMOTE_NAME_RE.match(name):
        return {"status": "rejected", "reason": "invalid remote name"}
    if not REMOTE_URL_RE.match(url):
        return {"status": "rejected",
                "reason": "invalid remote URL (https:// or ssh git URLs only)"}
    rc, current, _ = _git(repo, "remote", "get-url", name, demote=demote)
    if rc == 0 and current == url:
        return {"status": "unchanged", "name": name, "url": url}
    if rc == 0:
        rc, _, err = _git(repo, "remote", "set-url", name, url, demote=demote)
        if rc != 0:
            return {"status": "rejected",
                    "reason": "git remote set-url failed (%s)" % err[:200]}
        return {"status": "remote-updated", "name": name, "url": url,
                "previous": current}
    rc, _, err = _git(repo, "remote", "add", name, url, demote=demote)
    if rc != 0:
        return {"status": "rejected",
                "reason": "git remote add failed (%s)" % err[:200]}
    return {"status": "remote-added", "name": name, "url": url}


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


def sweep_results(queue, max_age_days=7):
    """Results are left for crashed pollers to find; sweep only stale ones."""
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
    """Fetch+ff every remoted repo's shared HEAD branch (refresh-time)."""
    results = []
    for r in registry.get("repos", []):
        root = r["root"]
        res = lanes.resolve(root)
        if not res["remote"]:
            continue
        err = _sync_from_origin(root, res, demote)
        results.append({"repo": root,
                        "status": "synced" if err is None else "rejected",
                        "reason": None if err is None else err["reason"]})
    return results


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


if __name__ == "__main__":
    sys.exit(main())
