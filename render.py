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



def render_settings(base, repos, managed_root):
    """Claude Code managed settings: hooks + env only. The filesystem wall
    is NOT delivered here — Claude Code's native sandbox cannot be made
    filesystem-only (it forces a network proxy that breaks gh/Node TLS and
    denies keychain writes), so warden ships `sandbox.enabled: false` and
    the wall is warden's own Seatbelt profile (render_seatbelt), applied
    by the launcher shim. This function only carries the base through and
    guarantees the native sandbox stays off."""
    out = json.loads(json.dumps(base))
    out.setdefault("sandbox", {})["enabled"] = False
    out["sandbox"].pop("filesystem", None)
    out["sandbox"].pop("failIfUnavailable", None)
    out["sandbox"].pop("allowUnsandboxedCommands", None)
    return out


# shared-.git paths a worktree session must write for normal git work
# (lab-proven carve-out set; also fixes upstream codex worktree-commit bug)
CODEX_GIT_CARVEOUTS = ["objects/**", "refs/**", "logs/**", "worktrees/**",
                       "packed-refs", "packed-refs.lock", "FETCH_HEAD"]


def render_seatbelt(repos, managed_root, home=None):
    """Warden's own Seatbelt profile — the wall Claude Code's sandbox
    could not be: filesystem-only. Sessions launch wrapped in this profile
    (sandbox-exec -f, via the claude shim); every child process inherits
    it. No network, credential, or process rules exist here, so nothing
    but the listed writes is touched — Warden blocks zero networking and
    zero commands. The profile is loaded from a file, so rule count has
    no ARG_MAX ceiling.

    Rule order is load-bearing: Seatbelt is last-match-wins (proven in
    tests/lab, EVIDENCE-2026-07-16). Per repo: deny the whole checkout,
    re-open the worktree container and the shared-.git write set inside
    it, then re-close the protected HEAD-branch refs inside those allows.
    Trunk's .git/index, HEAD, config, and hooks stay denied by the root
    deny — the hook-neutralization hole is closed by prevention."""
    lines = ["(version 1)", "(allow default)",
             '(deny file-write* (subpath "%s"))' % managed_root]
    for r in repos:
        root = r["root"]
        lines.append('(deny file-write* (subpath "%s"))' % root)
        lines.append('(allow file-write* (subpath "%s/.claude/worktrees"))'
                     % root)
        for sub in ["objects", "refs", "logs", "worktrees"]:
            lines.append('(allow file-write* (subpath "%s/.git/%s"))'
                         % (root, sub))
        for lit in ["packed-refs", "packed-refs.lock", "FETCH_HEAD"]:
            lines.append('(allow file-write* (literal "%s/.git/%s"))'
                         % (root, lit))
        if r["head_branch"]:
            ref = root + "/.git/refs/heads/" + r["head_branch"]
            for lit in [ref, ref + ".lock",
                        root + "/.git/logs/refs/heads/" + r["head_branch"]]:
                lines.append('(deny file-write* (literal "%s"))' % lit)
    if home:
        home = home.rstrip("/")
        for lit in [".claude/settings.json", ".claude/settings.local.json"]:
            lines.append('(deny file-write* (literal "%s/%s"))'
                         % (home, lit))
    return "\n".join(lines) + "\n"

# Write scope is deny-only: warden never enumerates the directories tools
# may write — any such list is stale the day a new tool appears (proven
# 2026-07-17: a curated carve-out list silently blocked the Azure CLI and
# global agent memory). The home directory gets one blanket write grant;
# the only paths held back are governance surfaces, which are warden's to
# know about. Repo protection rules are more specific than the home grant,
# so they still win (codex precedence is more-specific-wins, lab-proven).
CODEX_HOME_TAMPER_SURFACES = {
    ".codex/config.toml": "read",
    ".claude/settings.json": "read",
    ".claude/settings.local.json": "read",
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
        rules[home + "/**"] = "write"
        for sub, access in CODEX_HOME_TAMPER_SURFACES.items():
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
    ap.add_argument("--write-seatbelt")
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
    # sandbox is already off in both states; on disable, the guard hook
    # reads the DISABLED sentinel and the shim skips the seatbelt, so the
    # settings shape does not change between enabled and disabled here.
    gitconfig = render_gitconfig(repos, a.managed_root)
    if a.check:
        print(json.dumps({"settings": settings, "registry": registry,
                          "gitconfig": gitconfig},
                         indent=2, sort_keys=True))
        return 0
    if a.write_gitconfig:
        _atomic_write_text(a.write_gitconfig, gitconfig)
    if a.write_seatbelt:
        # disabled render still writes a valid allow-everything profile:
        # the shim also checks the sentinel, but a stale profile must
        # never be the thing that re-imposes walls after a disable
        sb = ("(version 1)\n(allow default)\n" if a.disabled else
              render_seatbelt(repos, a.managed_root,
                              home=scan_owner_home(a.scan)))
        _atomic_write_text(a.write_seatbelt, sb)
    _atomic_write(a.write_settings, settings)
    _atomic_write(a.write_registry, registry)
    sb_rules = (render_seatbelt(repos, a.managed_root).count("file-write*")
                if a.write_seatbelt and not a.disabled else 0)
    print("wrote %s (%d repos, %d seatbelt filesystem rules, native sandbox off)"
          % (a.write_settings, len(repos), sb_rules))
    return 0


if __name__ == "__main__":
    sys.exit(main())
