import os, subprocess, tempfile, unittest

HERE = os.path.dirname(__file__)
SCRIPT = os.path.join(HERE, "..", "session_worktree.py")


def make_repo(base, worktrees=("wt1",)):
    root = os.path.join(base, "repo")
    os.makedirs(os.path.join(root, ".git"))            # shared checkout
    for wt in worktrees:
        w = os.path.join(root, ".claude", "worktrees", wt)
        os.makedirs(w)
        open(os.path.join(w, ".git"), "w").write(
            "gitdir: %s/.git/worktrees/%s\n" % (root, wt))
        os.makedirs(os.path.join(w, "src"))
    return root


class TestSessionWorktree(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = make_repo(self.tmp.name, worktrees=("mine", "theirs"))
        self.mine = os.path.join(self.repo, ".claude", "worktrees", "mine")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, cwd):
        p = subprocess.run(["python3", SCRIPT, cwd],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def test_prints_own_worktree_from_inside_it(self):
        out = self._run(os.path.join(self.mine, "src"))
        self.assertEqual(out, os.path.realpath(self.mine))

    def test_prints_own_worktree_at_its_root(self):
        self.assertEqual(self._run(self.mine), os.path.realpath(self.mine))

    def test_empty_for_shared_root(self):
        self.assertEqual(self._run(self.repo), "")

    def test_empty_outside_any_repo(self):
        self.assertEqual(self._run(self.tmp.name), "")

    def test_does_not_leak_sibling(self):
        # from inside 'mine', it must never print 'theirs'
        out = self._run(os.path.join(self.mine, "src"))
        self.assertNotIn("theirs", out)


if __name__ == "__main__":
    unittest.main()
