import json, os, subprocess, tempfile, unittest

GUARD = os.path.join(os.path.dirname(__file__), "..", "guard.py")


def run_hook(payload, audit):
    env = dict(os.environ, WARDEN_AUDIT_FILE=audit, WARDEN_NO_SYSLOG="1")
    return subprocess.run(["python3", GUARD], input=json.dumps(payload),
                          capture_output=True, text=True, env=env)


def payload(tool, tinput, cwd, event="PreToolUse"):
    return {"session_id": "sess-test-1", "cwd": cwd, "hook_event_name": event,
            "tool_name": tool, "tool_input": tinput}


class TestMain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.audit = os.path.join(self.tmp.name, "audit.jsonl")
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(os.path.join(self.repo, ".git"))
        self.wt = os.path.join(self.repo, ".claude", "worktrees", "w1")
        os.makedirs(self.wt)
        open(os.path.join(self.wt, ".git"), "w").write("gitdir: x\n")

    def tearDown(self):
        self.tmp.cleanup()

    def last_audit(self):
        return [json.loads(l) for l in open(self.audit)][-1]

    def test_edit_shared_denied_with_reason(self):
        p = run_hook(payload("Edit", {"file_path": os.path.join(self.repo, "a.md")},
                             self.wt), self.audit)
        self.assertEqual(p.returncode, 0)
        hso = json.loads(p.stdout)["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PreToolUse")
        self.assertEqual(hso["permissionDecision"], "deny")
        self.assertIn("I2", hso["permissionDecisionReason"])
        rec = self.last_audit()
        self.assertEqual((rec["verdict"], rec["session_id"]), ("deny", "sess-test-1"))
        self.assertTrue(rec["ts"])

    def test_write_own_worktree_silent_allow(self):
        p = run_hook(payload("Write", {"file_path": os.path.join(self.wt, "n.py")},
                             self.wt), self.audit)
        self.assertEqual((p.returncode, p.stdout.strip()), (0, ""))
        self.assertEqual(self.last_audit()["verdict"], "allow")

    def test_notebook_path_field(self):
        p = run_hook(payload("NotebookEdit",
                             {"notebook_path": os.path.join(self.repo, "n.ipynb")},
                             self.wt), self.audit)
        out = json.loads(p.stdout)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_bash_audit_only_never_denies(self):
        p = run_hook(payload("Bash", {"command": "rm -rf %s" % self.repo}, self.wt),
                     self.audit)
        self.assertEqual((p.returncode, p.stdout.strip()), (0, ""))
        rec = self.last_audit()
        self.assertEqual((rec["tool"], rec["verdict"]), ("Bash", "audit"))
        self.assertIn("rm -rf", rec["target"])

    def test_sessionstart_announces(self):
        p = run_hook(payload("", {}, self.wt, event="SessionStart"), self.audit)
        out = json.loads(p.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "SessionStart")
        self.assertIn("warden", out["additionalContext"])
        self.assertIn(self.wt, out["additionalContext"])
        self.assertIn("warden land", out["additionalContext"])

    def test_worktree_create_touches_refresh_flag(self):
        home = os.path.join(self.tmp.name, "home")
        env_home = dict(os.environ, WARDEN_AUDIT_FILE=self.audit,
                        WARDEN_NO_SYSLOG="1", HOME=home)
        p = subprocess.run(["python3", GUARD],
                           input=json.dumps(payload("", {"path": self.wt}, self.wt,
                                                    event="WorktreeCreate")),
                           capture_output=True, text=True, env=env_home)
        self.assertEqual(p.returncode, 0)
        self.assertTrue(os.path.exists(
            os.path.join(home, ".claude", "warden", "refresh-requested")))

    def test_garbage_stdin_fails_open(self):
        env = dict(os.environ, WARDEN_AUDIT_FILE=self.audit, WARDEN_NO_SYSLOG="1")
        p = subprocess.run(["python3", GUARD], input="not json",
                           capture_output=True, text=True, env=env)
        self.assertEqual((p.returncode, p.stdout.strip()), (0, ""))
        self.assertEqual(self.last_audit()["verdict"], "guard-error")

    def test_missing_file_path_is_none_not_crash(self):
        p = run_hook(payload("Edit", {}, self.wt), self.audit)
        self.assertEqual((p.returncode, p.stdout.strip()), (0, ""))


if __name__ == "__main__":
    unittest.main()
