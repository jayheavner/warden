#!/usr/bin/env python3
"""warden renderer: disk truth -> registry.json + managed-settings.json.

Scans parent dirs for shared checkouts (dirs whose .git is a directory),
derives per-repo protected paths, and renders the managed settings from the
base template. Never consumes session input. Run via sudo for real writes.
"""
import argparse
import datetime
import json
import os
import subprocess
import sys


def _git(root, *args):
    p = subprocess.run(["git", "-C", root] + list(args),
                       capture_output=True, text=True, timeout=30)
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
            worktrees = ([l.split(" ", 1)[1] for l in wt.splitlines()
                          if l.startswith("worktree ")][1:] if rc == 0 else [])
            rc, _ = _git(root, "config", "--local", "--get",
                         "core.hooksPath")
            repos.append({"root": root, "head_branch": head_branch,
                          "top_entries": top_entries, "worktrees": worktrees,
                          "hookspath_override": rc == 0})
    return repos


# shared-.git subpaths a worktree session legitimately writes (commits,
# fetches, ref updates, packing). Everything else under a repo root is
# frozen by the root-level deny; sandbox filesystem rules resolve by
# most-specific-path-wins, so these allows re-open only these subtrees
# and the branch-trio denies re-close the shared HEAD branch inside them.
GIT_WRITE_SUBPATHS = ["objects", "refs", "logs", "worktrees",
                      "packed-refs", "packed-refs.lock", "FETCH_HEAD"]

# ceiling on rendered filesystem rules: the generated sandbox profile is
# passed as a single exec argument, and past roughly this many rules it
# exceeds the OS ARG_MAX limit and every Bash spawn fails with E2BIG
MAX_FS_RULES_DEFAULT = 250


def render_settings(base, repos, managed_root):
    out = json.loads(json.dumps(base))
    fs = out.setdefault("sandbox", {}).setdefault("filesystem", {})
    deny = fs.setdefault("denyWrite", [])
    allow = fs.setdefault("allowWrite", [])
    deny.append(managed_root)
    for r in repos:
        root = r["root"]
        # one deny per repo root freezes the whole shared checkout —
        # tracked tree, .git identity, .claude/settings.json. A session's
        # own worktree stays writable because its cwd is more specific.
        # No allow for .claude/worktrees: sibling worktrees must stay
        # read-only, and worktree creation is app-side (unsandboxed).
        deny.append(root)
        allow += [root + "/.git/" + p for p in GIT_WRITE_SUBPATHS]
        if r["head_branch"]:
            ref = root + "/.git/refs/heads/" + r["head_branch"]
            deny += [ref, ref + ".lock",
                     root + "/.git/logs/refs/heads/" + r["head_branch"]]
    fs["denyWrite"] = sorted(set(deny))
    fs["allowWrite"] = sorted(set(allow))
    limit = int(os.environ.get("WARDEN_MAX_FS_RULES", MAX_FS_RULES_DEFAULT))
    total = len(fs["denyWrite"]) + len(fs["allowWrite"])
    if total > limit:
        raise SystemExit(
            "render: %d filesystem rules exceed the safe limit of %d — a "
            "profile this large fails every Bash spawn with E2BIG. Reduce "
            "the repos under the scan dir(s) or raise WARDEN_MAX_FS_RULES "
            "only if a live session has proven the larger profile execs."
            % (total, limit))
    return out


# shared-.git paths a worktree session must write for normal git work
# (lab-proven carve-out set; also fixes upstream codex worktree-commit bug)
CODEX_GIT_CARVEOUTS = ["objects/**", "refs/**", "logs/**", "worktrees/**",
                       "packed-refs", "packed-refs.lock", "FETCH_HEAD"]


def codex_fs_rules(repos, managed_root, disabled=False):
    rules = {managed_root.rstrip("/") + "/**": "deny"}
    if disabled:
        return rules
    for r in repos:
        root = r["root"]
        for c in CODEX_GIT_CARVEOUTS:
            rules[root + "/.git/" + c] = "write"
        for f in ["index", "HEAD", "config", "hooks/**", "info/**"]:
            rules[root + "/.git/" + f] = "deny"
        if r["head_branch"]:
            ref = root + "/.git/refs/heads/" + r["head_branch"]
            for f in [ref, ref + ".lock",
                      root + "/.git/logs/refs/heads/" + r["head_branch"]]:
                rules[f] = "deny"
        for entry in r["top_entries"]:
            rules[root + "/" + entry] = "deny"
        rules[root + "/.claude/settings.json"] = "deny"
        rules[root + "/.codex/**"] = "deny"
    return rules


def render_codex_requirements(base_text, repos, managed_root, disabled=False):
    lines = [base_text.rstrip("\n"), "", "[permissions.warden.filesystem]"]
    for path, access in sorted(
            codex_fs_rules(repos, managed_root, disabled=disabled).items()):
        lines.append("%s = %s" % (json.dumps(path), json.dumps(access)))
    return "\n".join(lines) + "\n"


def render_gitconfig(repos, managed_root):
    """One includeIf stanza per adopted repo; trailing / on the gitdir
    pattern matches the repo's .git and every linked worktree's gitdir."""
    hookpath = managed_root + "/warden/hookpath.gitconfig"
    lines = ["# rendered by warden render.py -- do not edit; sudo warden refresh"]
    for r in repos:
        lines += ['[includeIf "gitdir:%s/"]' % r["root"],
                  "\tpath = %s" % hookpath]
    return "\n".join(lines) + "\n"


def _atomic_write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")
    json.load(open(tmp))          # refuse to swap in unparseable output
    os.replace(tmp, path)


def _atomic_write_text(path, text, validate=None):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    if validate:
        validate(open(tmp).read())  # refuse to swap in unparseable output
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scan", action="append", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--write-settings", required=True)
    ap.add_argument("--write-registry", required=True)
    ap.add_argument("--managed-root", default=None)
    ap.add_argument("--format", choices=["claude", "codex"], default="claude")
    ap.add_argument("--write-gitconfig")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--disabled", action="store_true")
    a = ap.parse_args(argv)
    if a.managed_root is None:
        a.managed_root = ("/etc/codex" if a.format == "codex"
                          else "/Library/Application Support/ClaudeCode")
    repos = scan_repos(a.scan)
    registry = {
        "generated_at": datetime.datetime.now().astimezone().isoformat(),
        "scanned": a.scan,
        "repos": repos,
    }
    if a.format == "codex":
        import tomllib
        text = render_codex_requirements(open(a.base).read(), repos,
                                         a.managed_root, disabled=a.disabled)
        tomllib.loads(text)
        if a.check:
            print(text)
            return 0
        _atomic_write_text(a.write_settings, text, tomllib.loads)
        _atomic_write(a.write_registry, registry)
        rules = codex_fs_rules(repos, a.managed_root, disabled=a.disabled)
        print("wrote %s (%d repos, %d filesystem rules)" % (
            a.write_settings, len(repos), len(rules)))
        return 0
    base = json.load(open(a.base))
    settings = render_settings(base, repos, a.managed_root)
    if a.disabled:
        settings["sandbox"]["enabled"] = False
        settings["sandbox"]["failIfUnavailable"] = False
    gitconfig = render_gitconfig(repos, a.managed_root)
    if a.check:
        print(json.dumps({"settings": settings, "registry": registry,
                          "gitconfig": gitconfig},
                         indent=2, sort_keys=True))
        return 0
    if a.write_gitconfig:
        _atomic_write_text(a.write_gitconfig, gitconfig)
    _atomic_write(a.write_settings, settings)
    _atomic_write(a.write_registry, registry)
    print("wrote %s (%d repos, %d denyWrite entries)" % (
        a.write_settings, len(repos),
        len(settings["sandbox"]["filesystem"]["denyWrite"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
