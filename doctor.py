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

# The Claude Code version the sandbox constraints in docs/limitations.md
# and docs/upstream-asks.md were last proven on. When the installed
# version moves, those constraints are unverified claims until the lab
# probes re-prove them (or show an upstream fix landed and warden's
# design should upgrade).
PROVEN_ON_CLAUDE = "2.1.212"


def version_drift(current, pinned=PROVEN_ON_CLAUDE):
    """Advisory text when the harness version left the proven pin, else
    None. `current` is `claude --version` output, e.g. '2.1.212 (...)'."""
    cur = (current or "").split()[0] if (current or "").strip() else ""
    if not cur:
        return ("claude CLI not found — the version the sandbox "
                "constraints were proven on (%s) cannot be checked."
                % pinned)
    if cur == pinned:
        return None
    return ("Claude Code is %s but the documented sandbox constraints "
            "were proven on %s. Re-verify before trusting them: run "
            "tests/lab/probe-write-precedence.sh from a plain terminal — "
            "if it reports RETIRED, an upstream fix landed and warden "
            "should upgrade to byte-level tree freeze (see "
            "docs/upstream-asks.md); if STANDS, update PROVEN_ON_CLAUDE "
            "in doctor.py to %s." % (cur, pinned, cur))


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


def dirty_shared_checkouts():
    """Uncommitted changes sitting at shared checkout roots. Sessions can
    never commit there, so unexplained bytes are either a human's
    in-progress work or a session's stray shell write — surface them
    instead of letting them sit invisible until a land fails. Worktree
    plumbing under .claude/ is not a signal and is excluded."""
    import subprocess
    reg = (os.environ.get("WARDEN_REGISTRY")
           or os.path.join(MANAGED, "warden", "registry.json"))
    try:
        repos = json.load(open(reg))["repos"]
    except (OSError, ValueError, KeyError):
        return
    dirty = []
    for r in repos:
        try:
            p = subprocess.run(
                ["git", "-C", r["root"], "status", "--porcelain"],
                capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            continue
        hits = [ln for ln in p.stdout.splitlines()
                if ln[3:] and not ln[3:].startswith(".claude/")]
        if p.returncode == 0 and hits:
            dirty.append((r["root"], hits))
    if not dirty:
        print("shared checkouts: clean (no uncommitted bytes at any root)")
        return
    print("shared checkouts with uncommitted bytes (%d):" % len(dirty))
    for root, hits in dirty:
        print("  %s (%d path%s, e.g. %s)"
              % (root, len(hits), "" if len(hits) == 1 else "s",
                 hits[0][3:]))
    print("  If you didn't put them there, a session's shell did — the "
          "audit trail (~/.claude/warden/audit.jsonl) records every "
          "session command. Stray tracked-file bytes are recoverable "
          "with git checkout; history and refs were never movable.")


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
    import subprocess
    try:
        cur = subprocess.run(["claude", "--version"], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        cur = ""
    drift = version_drift(cur)
    if drift:
        print("version drift: " + drift)
    dirty_shared_checkouts()
    recent_denials()
    return 0


if __name__ == "__main__":
    sys.exit(main())
