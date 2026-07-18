import contextlib, io, json, os, subprocess, tempfile, unittest
import importlib.util

spec = importlib.util.spec_from_file_location(
    "doctor", os.path.join(os.path.dirname(__file__), "..", "doctor.py"))
doctor = importlib.util.module_from_spec(spec); spec.loader.exec_module(doctor)


class TestVerdicts(unittest.TestCase):
    def test_deny_wins_inside_blanket_allow(self):
        fs = {"allowWrite": ["/"],
              "denyWrite": ["/Users/u/claude/alpha/.git/HEAD"]}
        v, rule = doctor.sandbox_verdict(
            "/Users/u/claude/alpha/.git/HEAD", fs)
        self.assertEqual(v, "deny")
        v, rule = doctor.sandbox_verdict(
            "/Users/u/claude/alpha/.git/HEAD/x", fs)
        self.assertEqual(v, "deny")

    def test_blanket_allow_covers_novel_path(self):
        fs = {"allowWrite": ["/"], "denyWrite": []}
        v, rule = doctor.sandbox_verdict("/Users/u/.newtool/cache", fs)
        self.assertEqual((v, rule), ("allow", "/"))

    def test_tilde_rules_expand(self):
        fs = {"allowWrite": ["/"],
              "denyWrite": ["~/.claude/settings.json"]}
        t = os.path.expanduser("~/.claude/settings.json")
        self.assertEqual(doctor.sandbox_verdict(t, fs)[0], "deny")

    def test_default_scope_when_no_allow_matches(self):
        fs = {"allowWrite": [], "denyWrite": []}
        self.assertEqual(doctor.sandbox_verdict("/anywhere", fs)[0],
                         "default")

    def test_codex_most_specific_wins(self):
        rules = {"/Users/u/**": "write",
                 "/Users/u/.codex/config.toml": "read"}
        pat, access = doctor.codex_verdict("/Users/u/.codex/config.toml",
                                           rules)
        self.assertEqual(access, "read")
        pat, access = doctor.codex_verdict("/Users/u/.newtool/db", rules)
        self.assertEqual(access, "write")


class TestLauncherDrift(unittest.TestCase):
    def test_governed_launcher_is_quiet(self):
        self.assertIsNone(doctor.launcher_drift(
            "/Library/Application Support/ClaudeCode/warden/claude-shim",
            "/Users/u/.local/share/claude/versions/2.1.214"))

    def test_missing_launcher_flagged(self):
        self.assertIn("not found", doctor.launcher_drift(None, "/x"))

    def test_repointed_launcher_flagged(self):
        msg = doctor.launcher_drift("/some/other/claude", "/x")
        self.assertIn("does NOT point at warden", msg)

    def test_unresolvable_binary_flagged(self):
        msg = doctor.launcher_drift(
            "/Library/Application Support/ClaudeCode/warden/claude-shim", None)
        self.assertIn("cannot resolve", msg)


class TestStrayBytes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "alpha")
        for cmd in [["git", "init", self.repo],
                    ["git", "-C", self.repo, "config", "user.email", "t@t"],
                    ["git", "-C", self.repo, "config", "user.name", "t"]]:
            subprocess.run(cmd, check=True, capture_output=True)
        with open(os.path.join(self.repo, "README.md"), "w") as f:
            f.write("x")
        subprocess.run(["git", "-C", self.repo, "add", "-A"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-m", "init"],
                       check=True, capture_output=True)
        self.reg = os.path.join(self.tmp.name, "registry.json")
        with open(self.reg, "w") as f:
            json.dump({"repos": [{"root": self.repo}]}, f)
        os.environ["WARDEN_REGISTRY"] = self.reg

    def tearDown(self):
        del os.environ["WARDEN_REGISTRY"]
        self.tmp.cleanup()

    def _report(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            doctor.dirty_shared_checkouts()
        return out.getvalue()

    def test_clean_checkout_reports_clean(self):
        self.assertIn("clean", self._report())

    def test_stray_bytes_surfaced(self):
        with open(os.path.join(self.repo, "README.md"), "a") as f:
            f.write("stray")
        rep = self._report()
        self.assertIn(self.repo, rep)
        self.assertIn("README.md", rep)
        self.assertIn("audit trail", rep)

    def test_worktree_plumbing_not_a_signal(self):
        os.makedirs(os.path.join(self.repo, ".claude", "worktrees", "w1"))
        with open(os.path.join(self.repo, ".claude", "worktrees", "w1",
                               "f"), "w") as f:
            f.write("x")
        self.assertIn("clean", self._report())


class TestSeatbeltVerdict(unittest.TestCase):
    PROFILE = (
        '(version 1)\n(allow default)\n'
        '(deny file-write* (subpath "/Users/u/claude/alpha"))\n'
        '(allow file-write* (subpath "/Users/u/claude/alpha/.claude/worktrees"))\n'
        '(allow file-write* (subpath "/Users/u/claude/alpha/.git/refs"))\n'
        '(deny file-write* (literal "/Users/u/claude/alpha/.git/refs/heads/main"))\n')

    def test_trunk_write_denied(self):
        v, _ = doctor.seatbelt_verdict("/Users/u/claude/alpha/README.md",
                                       self.PROFILE)
        self.assertEqual(v, "deny")

    def test_worktree_reopened(self):
        v, _ = doctor.seatbelt_verdict(
            "/Users/u/claude/alpha/.claude/worktrees/w1/src/f.py",
            self.PROFILE)
        self.assertEqual(v, "allow")

    def test_protected_ref_reclosed_last_match_wins(self):
        # inside the .git/refs allow, but the ref literal deny comes later
        v, _ = doctor.seatbelt_verdict(
            "/Users/u/claude/alpha/.git/refs/heads/main", self.PROFILE)
        self.assertEqual(v, "deny")

    def test_novel_machine_path_allowed(self):
        v, rule = doctor.seatbelt_verdict("/Users/u/.newtool/db", self.PROFILE)
        self.assertEqual(v, "allow")
        self.assertIn("allow default", rule)


class TestCli(unittest.TestCase):
    def test_recent_denials_reads_audit(self):
        with tempfile.TemporaryDirectory() as d:
            aud = os.path.join(d, "audit.jsonl")
            with open(aud, "w") as f:
                f.write(json.dumps({"ts": "2026-07-17T10:00:00", "rule": "I2",
                                    "tool": "Bash", "verdict": "deny",
                                    "target": "/x"}) + "\n")
                f.write(json.dumps({"verdict": "audit"}) + "\n")
            os.environ["WARDEN_AUDIT_FILE"] = aud
            try:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    doctor.recent_denials()
            finally:
                del os.environ["WARDEN_AUDIT_FILE"]
            self.assertIn("I2", out.getvalue())
            self.assertIn("/x", out.getvalue())

    def test_explain_path_runs_without_install(self):
        # must not crash on a machine with no rendered policy
        p = subprocess.run(
            ["python3", doctor.__file__, "/somewhere/new"],
            capture_output=True, text=True,
            env=dict(os.environ, WARDEN_DEST="/nonexistent",
                     WARDEN_CODEX_REQ="/nonexistent/req.toml"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("not installed", p.stdout)

    def test_state_reports_disabled_sentinel(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "warden"))
            with open(os.path.join(d, "warden", "DISABLED"), "w") as f:
                json.dump({"disabled_at": "2026-07-17T12:00:00"}, f)
            p = subprocess.run(
                ["python3", doctor.__file__], capture_output=True, text=True,
                env=dict(os.environ, WARDEN_DEST=d,
                         WARDEN_AUDIT_FILE=os.path.join(d, "none.jsonl")))
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("DISABLED", p.stdout)


if __name__ == "__main__":
    unittest.main()
