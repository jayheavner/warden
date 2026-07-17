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


# ceiling on rendered filesystem rules: the generated sandbox profile is
# passed as a single exec argument, and past roughly 400 rules it exceeds
# the OS ARG_MAX limit and every Bash spawn fails with E2BIG
MAX_FS_RULES_DEFAULT = 250


def render_settings(base, repos, managed_root):
    out = json.loads(json.dumps(base))
    fs = out.setdefault("sandbox", {}).setdefault("filesystem", {})
    deny = fs.setdefault("denyWrite", [])
    deny.append(managed_root)
    for r in repos:
        root = r["root"]
        # Freeze the shared checkout's git identity and enforcement config
        # only. The tracked tree is deliberately NOT enumerated: per-file
        # denies blew the profile past ARG_MAX (E2BIG bricked every Bash
        # spawn at 18 repos), and a root-level deny is unusable because a
        # write deny always beats any allow beneath it — it would freeze
        # the repo's worktrees and the shared-.git writes their git ops
        # need. Tree protection comes from the sandbox's cwd-scoped write
        # allowance plus the guard's shared-root Bash denial (rule I4);
        # commits and ref moves against the shared checkout stay
        # impossible everywhere via the denies below.
        deny += [root + "/.git/index", root + "/.git/HEAD",
                 root + "/.git/config", root + "/.git/hooks",
                 root + "/.git/info", root + "/.claude/settings.json"]
        if r["head_branch"]:
            ref = root + "/.git/refs/heads/" + r["head_branch"]
            deny += [ref, ref + ".lock",
                     root + "/.git/logs/refs/heads/" + r["head_branch"]]
    fs["denyWrite"] = sorted(set(deny))
    limit = int(os.environ.get("WARDEN_MAX_FS_RULES", MAX_FS_RULES_DEFAULT))
    total = len(fs["denyWrite"]) + len(fs.get("allowWrite", []))
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

# home directories agent tooling legitimately writes (mirrors the Claude
# template's allowWrite carve-outs): Codex's workspace-write sandbox
# otherwise confines writes to cwd + temp, blocking CLI token caches,
# session state, and caches. ~/.codex stays writable for session state,
# but its config is a tamper surface and stays denied.
CODEX_HOME_CARVEOUTS = {
    ".codex/**": "write", ".codex/config.toml": "read",
    ".azure/**": "write", ".aws/**": "write", ".config/**": "write",
    ".cache/**": "write", ".local/**": "write",
    "Library/Caches/**": "write", "Library/Logs/**": "write",
}


def codex_fs_rules(repos, managed_root, home=None, disabled=False):
    # "read" everywhere warden means read-only-not-writable: in the codex
    # access enum (read | write | deny) "deny" is TOTAL no-access — it
    # blocked reading the shared checkout's files, .git refs, and even
    # warden's own selftest under /etc/codex (proven live 2026-07-17,
    # exit 126 / Operation not permitted from a governed session).
    # Precedence is more-specific-wins, so a "read" on a protected path
    # still overrides a broader "write" carve-out.
    rules = {managed_root.rstrip("/") + "/**": "read"}
    if disabled:
        return rules
    if home:
        home = home.rstrip("/")
        for sub, access in CODEX_HOME_CARVEOUTS.items():
            rules[home + "/" + sub] = access
    for r in repos:
        root = r["root"]
        for c in CODEX_GIT_CARVEOUTS:
            rules[root + "/.git/" + c] = "write"
        for f in ["index", "HEAD", "config", "hooks/**", "info/**"]:
            rules[root + "/.git/" + f] = "read"
        if r["head_branch"]:
            ref = root + "/.git/refs/heads/" + r["head_branch"]
            for f in [ref, ref + ".lock",
                      root + "/.git/logs/refs/heads/" + r["head_branch"]]:
                rules[f] = "read"
        for entry in r["top_entries"]:
            rules[root + "/" + entry] = "read"
        rules[root + "/.claude/settings.json"] = "read"
        rules[root + "/.codex/**"] = "read"
    return rules


def render_codex_requirements(base_text, repos, managed_root, home=None,
                              disabled=False):
    lines = [base_text.rstrip("\n"), "", "[permissions.warden.filesystem]"]
    for path, access in sorted(
            codex_fs_rules(repos, managed_root, home=home,
                           disabled=disabled).items()):
        lines.append("%s = %s" % (json.dumps(path), json.dumps(access)))
    return "\n".join(lines) + "\n"


def scan_owner_home(parents):
    """Home directory of the scan dir's owner — the governed user. The
    renderer runs as root, so os.path.expanduser would give root's home."""
    import pwd
    for parent in parents:
        parent = os.path.realpath(os.path.expanduser(parent))
        try:
            return pwd.getpwuid(os.stat(parent).st_uid).pw_dir
        except (OSError, KeyError):
            continue
    return None


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
        home = scan_owner_home(a.scan)
        text = render_codex_requirements(open(a.base).read(), repos,
                                         a.managed_root, home=home,
                                         disabled=a.disabled)
        tomllib.loads(text)
        if a.check:
            print(text)
            return 0
        _atomic_write_text(a.write_settings, text, tomllib.loads)
        _atomic_write(a.write_registry, registry)
        rules = codex_fs_rules(repos, a.managed_root, home=home,
                               disabled=a.disabled)
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
