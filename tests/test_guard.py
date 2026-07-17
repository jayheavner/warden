import os, tempfile, unittest, importlib.util

spec = importlib.util.spec_from_file_location(
    "guard", os.path.join(os.path.dirname(__file__), "..", "guard.py"))
guard = importlib.util.module_from_spec(spec); spec.loader.exec_module(guard)

MANAGED = "/Library/Application Support/ClaudeCode"


def make_repo(base, name="repo", worktrees=("wt1", "wt2")):
    root = os.path.join(base, name)
    os.makedirs(os.path.join(root, ".git"))            # shared checkout
    os.makedirs(os.path.join(root, "docs"))
    open(os.path.join(root, "README.md"), "w").write("x")
    for wt in worktrees:
        w = os.path.join(root, ".claude", "worktrees", wt)
        os.makedirs(w)
        open(os.path.join(w, ".git"), "w").write(
            "gitdir: %s/.git/worktrees/%s\n" % (root, wt))
        os.makedirs(os.path.join(w, "src"))
    return root


class TestClassify(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = make_repo(self.tmp.name)
        self.wt1 = os.path.join(self.repo, ".claude", "worktrees", "wt1")
        self.wt2 = os.path.join(self.repo, ".claude", "worktrees", "wt2")

    def tearDown(self):
        self.tmp.cleanup()

    def c(self, target, cwd):
        return guard.classify(target, cwd, MANAGED)

    def test_own_worktree_allow(self):
        v = self.c(os.path.join(self.wt1, "src", "new.py"), self.wt1)
        self.assertEqual(v.decision, "allow")

    def test_own_worktree_allow_from_subdir_cwd(self):
        v = self.c(os.path.join(self.wt1, "f.txt"), os.path.join(self.wt1, "src"))
        self.assertEqual(v.decision, "allow")

    def test_sibling_worktree_deny(self):
        v = self.c(os.path.join(self.wt2, "src", "hack.py"), self.wt1)
        self.assertEqual((v.decision, v.rule), ("deny", "I3"))

    def test_shared_root_deny_from_worktree_session(self):
        v = self.c(os.path.join(self.repo, "README.md"), self.wt1)
        self.assertEqual((v.decision, v.rule), ("deny", "I2"))

    def test_shared_root_deny_from_root_session(self):
        v = self.c(os.path.join(self.repo, "docs", "x.md"), self.repo)
        self.assertEqual((v.decision, v.rule), ("deny", "I2"))

    def test_other_repo_deny_from_unrelated_session(self):
        other = make_repo(self.tmp.name, "other", worktrees=())
        v = self.c(os.path.join(other, "README.md"), self.wt1)
        self.assertEqual((v.decision, v.rule), ("deny", "I2"))

    def test_dotgit_of_shared_root_deny(self):
        v = self.c(os.path.join(self.repo, ".git", "config"), self.wt1)
        self.assertEqual((v.decision, v.rule), ("deny", "I2"))

    def test_relative_traversal_resolved(self):
        sneaky = os.path.join(self.wt1, "src", "..", "..", "..", "..", "README.md")
        v = self.c(sneaky, self.wt1)
        self.assertEqual((v.decision, v.rule), ("deny", "I2"))

    def test_symlink_into_shared_resolved(self):
        link = os.path.join(self.wt1, "sneaky")
        os.symlink(os.path.join(self.repo, "docs"), link)
        v = self.c(os.path.join(link, "x.md"), self.wt1)
        self.assertEqual((v.decision, v.rule), ("deny", "I2"))

    def test_managed_root_deny(self):
        v = self.c(os.path.join(MANAGED, "managed-settings.json"), self.wt1)
        self.assertEqual((v.decision, v.rule), ("deny", "E3"))

    def test_outside_repos_none(self):
        v = self.c(os.path.join(self.tmp.name, "scratch.txt"), self.wt1)
        self.assertEqual(v.decision, "none")

    def test_worktree_file_tool_write_from_root_session_denied(self):
        v = self.c(os.path.join(self.wt2, "f.txt"), self.repo)
        self.assertEqual((v.decision, v.rule), ("deny", "I3"))

    def test_deny_reasons_name_rule_and_fix(self):
        v = self.c(os.path.join(self.repo, "README.md"), self.wt1)
        self.assertIn("I2", v.reason)
        self.assertIn("worktree", v.reason)


if __name__ == "__main__":
    unittest.main()


class TestClassifyBash(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = make_repo(self.tmp.name)
        self.wt1 = os.path.join(self.repo, ".claude", "worktrees", "wt1")
        self.reg = os.path.join(self.tmp.name, "registry.json")
        import json
        json.dump({"repos": [{"root": os.path.realpath(self.repo)}]},
                  open(self.reg, "w"))
        os.environ["WARDEN_REGISTRY"] = self.reg

    def tearDown(self):
        del os.environ["WARDEN_REGISTRY"]
        self.tmp.cleanup()

    def test_bash_denied_at_adopted_shared_root(self):
        v = guard.classify_bash(self.repo)
        self.assertEqual((v.decision, v.rule), ("deny", "I4"))
        self.assertIn("worktree", v.reason)

    def test_bash_denied_in_subdir_of_shared_root(self):
        v = guard.classify_bash(os.path.join(self.repo, "docs"))
        self.assertEqual(v.decision, "deny")

    def test_bash_allowed_in_worktree(self):
        self.assertEqual(guard.classify_bash(self.wt1).decision, "none")

    def test_bash_allowed_outside_repos(self):
        self.assertEqual(guard.classify_bash(self.tmp.name).decision, "none")

    def test_bash_allowed_at_unadopted_repo(self):
        other = make_repo(self.tmp.name, name="unadopted")
        self.assertEqual(guard.classify_bash(other).decision, "none")

    def test_missing_registry_fails_open(self):
        os.environ["WARDEN_REGISTRY"] = "/nonexistent/registry.json"
        self.assertEqual(guard.classify_bash(self.repo).decision, "none")
