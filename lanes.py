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
    token values never enter any returned or logged structure.
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
