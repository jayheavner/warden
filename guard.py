#!/usr/bin/env python3
"""warden guard hook: session-isolation judgment for Claude Code file tools.

Enforcement model: the native sandbox (delivered by managed settings) is the
wall for Bash at the syscall layer; this hook is the judgment for
path-addressed file tools plus the audit trail for everything. It never
parses command text.
"""
import collections
import datetime
import json
import os
import subprocess
import sys

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
    """Nearest ancestor that is a linked-worktree root (its .git is a file).

    Stops at the first .git of either kind: a worktree nested inside a repo
    hits its own .git file before the repo's .git directory.
    """
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
        return Verdict(
            "deny", "E3",
            "warden E3: %s is enforcement configuration; sessions may not "
            "modify it." % t)
    wt_t = worktree_container(t)
    wt_c = worktree_container(cwd)
    if wt_t:
        if wt_c == wt_t:
            return Verdict("allow", "I1", "inside this session's own workspace")
        return Verdict(
            "deny", "I3",
            "warden I3: %s is inside workspace %s, which is not this "
            "session's workspace (%s). Write only inside your own worktree."
            % (t, wt_t, wt_c or cwd))
    repo_t = shared_root(t)
    if repo_t is None:
        return Verdict("none", "", "")
    return Verdict(
        "deny", "I2",
        "warden I2: %s is inside the shared checkout %s, which is read-only "
        "to every session. Do this work inside your own worktree (the app "
        "creates one per session)." % (t, repo_t))
