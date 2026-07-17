#!/usr/bin/env python3
"""warden doctor: explain what warden does to a write at PATH.

`warden doctor <path>` answers "who blocks this and why" from the rendered
policy and the guard's structural rules, in plain sentences. With no path
it reports enforcement state and the most recent denials from the audit
trail. Read-only: it never changes policy.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import guard  # noqa: E402

MANAGED = os.environ.get("WARDEN_DEST",
                         "/Library/Application Support/ClaudeCode")
CODEX_REQ = os.environ.get("WARDEN_CODEX_REQ", "/etc/codex/requirements.toml")


def _home():
    return os.path.expanduser("~")


def _expand(rule):
    return _home() + rule[1:] if rule.startswith("~") else rule


def _covers(rule, target):
    """True when a write at target falls under a path-prefix rule."""
    rule = _expand(rule).rstrip("/") or "/"
    return target == rule or target.startswith(rule + os.sep) or rule == "/"


def sandbox_verdict(target, fs):
    """(verdict, rule) against the rendered Bash-sandbox write rules."""
    denies = [d for d in fs.get("denyWrite", []) if _covers(d, target)]
    if denies:
        return "deny", max(denies, key=len)
    allows = [a for a in fs.get("allowWrite", []) if _covers(a, target)]
    if allows:
        return "allow", max(allows, key=len)
    return "default", ""


def codex_verdict(target, rules):
    """Most-specific matching rule in the codex map (glob, specific wins)."""
    import fnmatch
    best = None
    for pat, access in rules.items():
        if fnmatch.fnmatch(target, pat) or _covers(pat.replace("/**", ""),
                                                   target):
            if best is None or len(pat) > len(best[0]):
                best = (pat, access)
    return best


def explain_path(raw):
    t = guard._resolve(raw)
    print("warden doctor: write target %s" % t)
    try:
        ms = json.load(open(os.path.join(MANAGED, "managed-settings.json")))
    except (OSError, ValueError):
        print("  claude: no rendered policy at %s — warden for Claude Code "
              "is not installed; warden does not restrict this path."
              % MANAGED)
        ms = None
    if ms:
        v, rule = sandbox_verdict(t, ms.get("sandbox", {})
                                       .get("filesystem", {}))
        if v == "deny":
            print("  claude sandbox: DENIED by rendered rule %r — this is a "
                  "warden-protected surface." % rule)
        elif v == "allow":
            print("  claude sandbox: allowed (write scope is deny-only; "
                  "matched %r)." % rule)
        else:
            print("  claude sandbox: outside the rendered write scope — "
                  "only the session's own directory and temp are writable "
                  "by default.")
    # guard structural rules (file tools; Bash cwd rule)
    m = guard._resolve(guard.MANAGED_ROOT_DEFAULT)
    if t == m or t.startswith(m + os.sep):
        print("  guard: DENIED for file tools (E3 — enforcement config).")
    else:
        wt = guard.worktree_container(t)
        root = guard.shared_root(t)
        if wt:
            print("  guard: allowed only from the session that owns "
                  "worktree %s (I1); every other session is denied (I3)."
                  % wt)
        elif root:
            print("  guard: DENIED for file tools in every session (I2 — "
                  "inside shared checkout %s); sessions whose cwd is that "
                  "checkout also lose Bash entirely (I4)." % root)
        else:
            print("  guard: no restriction (not inside any repo).")
    try:
        import tomllib
        rules = (tomllib.loads(open(CODEX_REQ).read())
                 ["permissions"]["warden"]["filesystem"])
        best = codex_verdict(t, rules)
        if best:
            print("  codex: %s (matched %r)." % (
                {"write": "writable", "read": "read-only",
                 "deny": "NO ACCESS"}.get(best[1], best[1]), best[0]))
        else:
            print("  codex: no warden rule — codex's own workspace sandbox "
                  "decides.")
    except (OSError, KeyError, ValueError):
        pass
    print("  If a write still fails where warden allows it, the cause is "
          "outside warden: OS file permissions, or the harness's built-in "
          "protections (Claude Code always denies writes to its own "
          "settings, hooks, and skills files, at every scope).")


def recent_denials(limit=10):
    path = (os.environ.get("WARDEN_AUDIT_FILE")
            or os.path.expanduser("~/.claude/warden/audit.jsonl"))
    try:
        lines = open(path).readlines()
    except OSError:
        print("no audit trail at %s" % path)
        return
    hits = []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("verdict") in ("deny", "guard-error"):
            hits.append(r)
    if not hits:
        print("audit: no denials recorded.")
        return
    print("last %d denial(s):" % min(limit, len(hits)))
    for r in hits[-limit:]:
        print("  %s  %-4s %-8s %s" % (r.get("ts", "?")[:19],
                                      r.get("rule") or "-",
                                      r.get("tool") or "-",
                                      (r.get("target") or "")[:100]))
    print("re-run with the blocked path for a rule-by-rule explanation: "
          "warden doctor <path>")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        explain_path(argv[0])
        return 0
    sentinel = os.path.join(MANAGED, "warden", "DISABLED")
    if os.path.isfile(sentinel):
        try:
            since = json.load(open(sentinel))["disabled_at"]
        except (OSError, ValueError, KeyError):
            since = "?"
        print("state: DISABLED since %s — nothing below is enforced in new "
              "sessions." % since)
    else:
        print("state: enabled")
    recent_denials()
    return 0


if __name__ == "__main__":
    sys.exit(main())
