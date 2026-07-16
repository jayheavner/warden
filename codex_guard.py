#!/usr/bin/env python3
"""warden Codex hook adapter: session-isolation judgment for Codex sessions.

Reuses guard.py's classifier verbatim; this file only speaks Codex's hook
wire format (deny-only PreToolUse in build 0.144.0-alpha.4) and maps Codex
tool shapes to target paths. The rendered permission profile is the wall;
this hook is the judgment for path-addressed tools, the escalation
kill-switch, and the audit trail. It never parses command text.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import guard  # noqa: E402

# tool_input keys whose string values are file targets
PATH_KEYS = {"file_path", "path", "notebook_path"}
# tools whose filesystem effects the sandbox owns; hook audits only
SHELL_HINT_KEYS = {"command", "cmd", "argv"}
# per-command escalation request fields (structural, not command text)
ESCALATION_KEYS = {"require_escalated", "with_additional_permissions",
                   "with_escalated_permissions"}


def extract_paths(tool_input):
    """Collect candidate target paths structurally from any tool_input."""
    paths = []

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "changes" and isinstance(v, dict):
                    paths.extend(p for p in v if isinstance(p, str))
                    for sub in v.values():
                        walk(sub)
                elif k in PATH_KEYS and isinstance(v, str):
                    paths.append(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(tool_input)
    return paths


def escalation_requested(tool_input):
    if not isinstance(tool_input, dict):
        return False
    return any(tool_input.get(k) for k in ESCALATION_KEYS)


def is_shell_tool(tool_name, tool_input):
    if isinstance(tool_name, str) and ("shell" in tool_name.lower()
                                       or "exec" in tool_name.lower()):
        return True
    return isinstance(tool_input, dict) and bool(SHELL_HINT_KEYS & set(tool_input))


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        guard._audit({"harness": "codex", "verdict": "guard-error",
                      "rule": "bad-stdin", "session_id": "", "cwd": "",
                      "tool": "", "target": ""})
        return 0
    event = data.get("hook_event_name", "")
    sid = data.get("session_id", "")
    cwd = data.get("cwd", "") or os.getcwd()
    tool = data.get("tool_name", "")
    tin = data.get("tool_input") or {}
    base = {"harness": "codex", "session_id": sid, "cwd": cwd, "tool": tool}
    try:
        if event == "PreToolUse":
            if escalation_requested(tin):
                guard._audit(dict(base, target="<escalation-request>",
                                  verdict="deny", rule="E3"))
                guard._deny(event,
                            "warden E3: per-command permission escalation "
                            "(require_escalated/with_additional_permissions) "
                            "is disabled on this machine. Work inside your "
                            "own worktree's writable scope.")
                return 0
            if is_shell_tool(tool, tin):
                guard._audit(dict(base, target=json.dumps(tin)[:500],
                                  verdict="audit", rule=""))
                return 0
            for target in extract_paths(tin):
                v = guard.classify(target, cwd)
                guard._audit(dict(base, target=target,
                                  verdict=v.decision or "none", rule=v.rule))
                if v.decision == "deny":
                    guard._deny(event, v.reason)
                    return 0
            if not extract_paths(tin):
                guard._audit(dict(base, target="", verdict="none", rule=""))
        elif event == "SessionStart":
            scope = guard.worktree_container(guard._resolve(cwd)) or cwd
            guard._audit(dict(base, target=scope, verdict="session-start",
                              rule=""))
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext":
                    "warden enforcement is active: writes are limited to "
                    "your workspace (%s); shared checkouts and other "
                    "sessions' worktrees are read-only." % scope}}))
        else:
            guard._audit(dict(base, target="", verdict="ignored-event",
                              rule=event))
    except Exception as exc:  # fail open; the sandbox remains the wall
        guard._audit(dict(base, target="", verdict="guard-error",
                          rule=repr(exc)[:200]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
