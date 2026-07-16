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

DISABLED_BANNER = ("⚠ Warden enforcement is DISABLED (since %s). Session "
                   "isolation is off. Re-enable with: sudo warden enable")
DISABLED_ADDENDUM = ("Bash writes in sessions started before the disable "
                     "are still sandboxed until those sessions restart.")


def sentinel_path(managed_root=MANAGED_ROOT_DEFAULT):
    return (os.environ.get("WARDEN_SENTINEL")
            or os.path.join(managed_root, "warden", "DISABLED"))


def disabled_since(managed_root=MANAGED_ROOT_DEFAULT):
    """ISO timestamp if warden is disabled, else None. Any anomaly —
    directory, unreadable, bad JSON — reads as enabled (fail safe)."""
    p = sentinel_path(managed_root)
    try:
        if not os.path.isfile(p):
            return None
        return str(json.load(open(p))["disabled_at"])
    except (OSError, ValueError, KeyError):
        return None


def _notify_once(sid, since):
    """Emit the disabled banner at most once per session."""
    d = (os.environ.get("WARDEN_NOTIFY_DIR")
         or os.path.expanduser("~/.claude/warden/notified"))
    mark = os.path.join(d, sid or "no-session")
    try:
        if os.path.exists(mark):
            return
        os.makedirs(d, exist_ok=True)
        open(mark, "w").write(since + "\n")
    except OSError:
        pass  # can't remember we notified; fail loud, print anyway
    print(json.dumps({"systemMessage":
                      DISABLED_BANNER % since + " " + DISABLED_ADDENDUM}))


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


FILE_TOOLS = {"Edit": "file_path", "Write": "file_path",
              "NotebookEdit": "notebook_path"}


def _audit(record):
    record["ts"] = datetime.datetime.now().astimezone().isoformat()
    line = json.dumps(record, ensure_ascii=False)
    path = (os.environ.get("WARDEN_AUDIT_FILE")
            or os.path.expanduser("~/.claude/warden/audit.jsonl"))
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if not os.environ.get("WARDEN_NO_SYSLOG"):
        try:
            subprocess.run(["logger", "-t", "warden", line[:900]],
                           timeout=5, check=False)
        except Exception:
            pass


def _deny(event, reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": event,
        "permissionDecision": "deny",
        "permissionDecisionReason": reason}}))


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        _audit({"verdict": "guard-error", "rule": "bad-stdin",
                "session_id": "", "cwd": "", "tool": "", "target": ""})
        return 0
    event = data.get("hook_event_name", "")
    sid = data.get("session_id", "")
    cwd = data.get("cwd", "") or os.getcwd()
    tool = data.get("tool_name", "")
    tin = data.get("tool_input") or {}
    base = {"session_id": sid, "cwd": cwd, "tool": tool}
    since = disabled_since()
    try:
        if event == "PreToolUse" and tool in FILE_TOOLS:
            target = tin.get(FILE_TOOLS[tool], "")
            if since:
                _audit(dict(base, target=target, verdict="disabled-allow",
                            rule=""))
                _notify_once(sid, since)
            else:
                v = classify(target, cwd) if target else Verdict("none", "", "")
                _audit(dict(base, target=target, verdict=v.decision or "none",
                            rule=v.rule))
                if v.decision == "deny":
                    _deny(event, v.reason)
        elif event == "PreToolUse" and tool == "Bash":
            if since:
                _audit(dict(base, target=(tin.get("command") or "")[:500],
                            verdict="disabled-audit", rule=""))
                _notify_once(sid, since)
            else:
                _audit(dict(base, target=(tin.get("command") or "")[:500],
                            verdict="audit", rule=""))
        elif event == "SessionStart":
            rcwd = _resolve(cwd)
            wt = worktree_container(rcwd)
            root = None if wt else shared_root(rcwd)
            scope = wt or cwd
            if since:
                _audit(dict(base, target=scope, verdict="session-start-disabled",
                            rule=""))
                print(json.dumps({"hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": DISABLED_BANNER % since}}))
            elif root:
                _audit(dict(base, target=root,
                            verdict="session-start-shared-root", rule="I2"))
                print(json.dumps({"hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext":
                        "⚠ warden: this session started in the shared checkout "
                        "%s, which is read-only to every session — writes, "
                        "commits, and branch moves here are denied. Create or "
                        "enter a worktree before doing any work (the app "
                        "creates one per session)." % root}}))
            else:
                _audit(dict(base, target=scope, verdict="session-start", rule=""))
                print(json.dumps({"hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext":
                        "warden enforcement is active: writes are limited to your "
                        "workspace (%s); shared checkouts and other sessions' "
                        "worktrees are read-only. To integrate finished work, run "
                        "`warden land <branch>` — a root daemon fast-forwards the "
                        "shared HEAD branch; never ask the human to merge." % scope}}))
        elif event in ("WorktreeCreate", "WorktreeRemove"):
            _audit(dict(base, target=json.dumps(tin)[:300], verdict=event,
                        rule=""))
            flag = os.path.expanduser("~/.claude/warden/refresh-requested")
            os.makedirs(os.path.dirname(flag), exist_ok=True)
            with open(flag, "w") as f:
                f.write(sid + "\n")
        else:
            _audit(dict(base, target="", verdict="ignored-event", rule=event))
    except Exception as exc:  # fail open; the sandbox remains the wall
        _audit(dict(base, target="", verdict="guard-error",
                    rule=repr(exc)[:200]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
