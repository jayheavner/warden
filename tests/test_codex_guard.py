import json, os, subprocess, tempfile, unittest

GUARD = os.path.join(os.path.dirname(__file__), "..", "codex_guard.py")


def sh(*a):
    subprocess.run(a, check=True, capture_output=True)


def run_hook(payload, audit_file):
    env = dict(os.environ, WARDEN_AUDIT_FILE=audit_file, WARDEN_NO_SYSLOG="1")
    p = subprocess.run(["python3", GUARD], input=json.dumps(payload),
                       capture_output=True, text=True, env=env, timeout=30)
    return p


def pre_tool_use(tool_name, tool_input, cwd):
    return {"hook_event_name": "PreToolUse", "session_id": "s1",
            "turn_id": "t1", "tool_use_id": "u1", "model": "m",
            "permission_mode": "default", "transcript_path": None,
            "cwd": cwd, "tool_name": tool_name, "tool_input": tool_input}


class TestCodexGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.audit = os.path.join(self.tmp.name, "audit.jsonl")
        self.repo = os.path.join(self.tmp.name, "claude", "alpha")
        sh("git", "init", self.repo)
        sh("git", "-C", self.repo, "config", "user.email", "t@t")
        sh("git", "-C", self.repo, "config", "user.name", "t")
        open(os.path.join(self.repo, "README.md"), "w").write("x")
        sh("git", "-C", self.repo, "add", "-A")
        sh("git", "-C", self.repo, "commit", "-m", "init")
        self.wt1 = os.path.join(self.repo, ".claude", "worktrees", "w1")
        self.wt2 = os.path.join(self.repo, ".claude", "worktrees", "w2")
        sh("git", "-C", self.repo, "worktree", "add", self.wt1, "-b", "b1")
        sh("git", "-C", self.repo, "worktree", "add", self.wt2, "-b", "b2")

    def tearDown(self):
        self.tmp.cleanup()

    def deny_reason(self, p):
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        h = out["hookSpecificOutput"]
        self.assertEqual(h["hookEventName"], "PreToolUse")
        self.assertEqual(h["permissionDecision"], "deny")
        self.assertTrue(h["permissionDecisionReason"])
        return h["permissionDecisionReason"]

    def test_apply_patch_sibling_worktree_denied(self):
        target = os.path.join(self.wt2, "f.txt")
        p = run_hook(pre_tool_use("apply_patch",
                                  {"changes": {target: {"add": {"content": "x"}}}},
                                  self.wt1), self.audit)
        self.assertIn("I3", self.deny_reason(p))

    def test_apply_patch_own_worktree_allowed_silent(self):
        target = os.path.join(self.wt1, "f.txt")
        p = run_hook(pre_tool_use("apply_patch",
                                  {"changes": {target: {"add": {"content": "x"}}}},
                                  self.wt1), self.audit)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "")

    def test_shared_root_write_denied_I2(self):
        target = os.path.join(self.repo, "README.md")
        p = run_hook(pre_tool_use("write_file", {"file_path": target},
                                  self.wt1), self.audit)
        self.assertIn("I2", self.deny_reason(p))

    def test_shell_tool_audit_only(self):
        p = run_hook(pre_tool_use("shell",
                                  {"command": ["rm", "-rf", self.repo]},
                                  self.wt1), self.audit)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "")
        rec = [json.loads(l) for l in open(self.audit)][-1]
        self.assertEqual(rec["verdict"], "audit")
        self.assertEqual(rec["harness"], "codex")

    def test_escalation_request_denied(self):
        p = run_hook(pre_tool_use("shell",
                                  {"command": ["true"],
                                   "with_additional_permissions": {"x": 1}},
                                  self.wt1), self.audit)
        self.assertIn("E3", self.deny_reason(p))
        p = run_hook(pre_tool_use("shell",
                                  {"command": ["true"],
                                   "require_escalated": True},
                                  self.wt1), self.audit)
        self.assertIn("E3", self.deny_reason(p))

    def test_session_start_additional_context(self):
        p = run_hook({"hook_event_name": "SessionStart", "session_id": "s1",
                      "cwd": self.wt1, "model": "m", "source": "startup",
                      "permission_mode": "default", "transcript_path": None},
                     self.audit)
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        h = out["hookSpecificOutput"]
        self.assertEqual(h["hookEventName"], "SessionStart")
        self.assertIn("warden enforcement is active", h["additionalContext"])
        self.assertIn(os.path.realpath(self.wt1), h["additionalContext"])

    def test_bad_stdin_fails_open(self):
        env = dict(os.environ, WARDEN_AUDIT_FILE=self.audit,
                   WARDEN_NO_SYSLOG="1")
        p = subprocess.run(["python3", GUARD], input="not json",
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout.strip(), "")

    def test_shared_root_write_allowed_when_disabled(self):
        sentinel = os.path.join(self.tmp.name, "DISABLED")
        json.dump({"disabled_at": "2026-01-01T00:00:00", "by_uid": 0},
                  open(sentinel, "w"))
        target = os.path.join(self.repo, "README.md")
        env = dict(os.environ, WARDEN_AUDIT_FILE=self.audit,
                   WARDEN_NO_SYSLOG="1", WARDEN_SENTINEL=sentinel)
        p = subprocess.run(["python3", GUARD],
                           input=json.dumps(pre_tool_use(
                               "write_file", {"file_path": target}, self.wt1)),
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "")
        rec = [json.loads(l) for l in open(self.audit)][-1]
        self.assertEqual(rec["verdict"], "disabled-allow")
        self.assertEqual(rec["harness"], "codex")

    def test_deny_audited_with_harness(self):
        target = os.path.join(self.wt2, "f.txt")
        run_hook(pre_tool_use("edit_file", {"path": target}, self.wt1),
                 self.audit)
        rec = [json.loads(l) for l in open(self.audit)][-1]
        self.assertEqual(rec["harness"], "codex")
        self.assertEqual(rec["verdict"], "deny")
        self.assertEqual(rec["rule"], "I3")


if __name__ == "__main__":
    unittest.main()
