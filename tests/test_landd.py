import json, os, subprocess, tempfile, unittest, importlib.util

spec = importlib.util.spec_from_file_location(
    "landd", os.path.join(os.path.dirname(__file__), "..", "landd.py"))
landd = importlib.util.module_from_spec(spec); spec.loader.exec_module(landd)


def sh(*a):
    return subprocess.run(a, check=True, capture_output=True, text=True)


def out(*a):
    return sh(*a).stdout.strip()


class TestLandd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.realpath(os.path.join(self.tmp.name, "alpha"))
        sh("git", "init", "-b", "main", self.repo)
        sh("git", "-C", self.repo, "config", "user.email", "t@t")
        sh("git", "-C", self.repo, "config", "user.name", "t")
        open(os.path.join(self.repo, "README.md"), "w").write("x\n")
        sh("git", "-C", self.repo, "add", "-A")
        sh("git", "-C", self.repo, "commit", "-m", "init")
        self.wt = os.path.join(self.repo, ".claude", "worktrees", "w1")
        sh("git", "-C", self.repo, "worktree", "add", self.wt, "-b", "feat")
        open(os.path.join(self.wt, "new.txt"), "w").write("y\n")
        sh("git", "-C", self.wt, "add", "-A")
        sh("git", "-C", self.wt, "commit", "-m", "feat work")
        self.registry = {"repos": [{"root": self.repo, "head_branch": "main",
                                    "top_entries": ["README.md"],
                                    "worktrees": [self.wt]}]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_ff_land_advances_main_and_working_tree(self):
        res = landd.process_request({"repo": self.repo, "branch": "feat"},
                                    self.registry, demote=False)
        self.assertEqual(res["status"], "landed", res)
        self.assertEqual(out("git", "-C", self.repo, "rev-parse", "main"),
                         out("git", "-C", self.repo, "rev-parse", "feat"))
        self.assertTrue(os.path.exists(os.path.join(self.repo, "new.txt")))
        self.assertEqual(out("git", "-C", self.repo, "status", "--porcelain",
                             "--untracked-files=no"), "")

    def test_non_ff_rejected_with_guidance(self):
        open(os.path.join(self.repo, "other.txt"), "w").write("z\n")
        sh("git", "-C", self.repo, "add", "-A")
        sh("git", "-C", self.repo, "commit", "-m", "diverge main")
        res = landd.process_request({"repo": self.repo, "branch": "feat"},
                                    self.registry, demote=False)
        self.assertEqual(res["status"], "rejected")
        self.assertIn("fast-forward", res["reason"])
        self.assertIn("merge", res["reason"])  # tells session how to fix

    def test_unknown_repo_rejected(self):
        res = landd.process_request({"repo": "/no/such", "branch": "feat"},
                                    self.registry, demote=False)
        self.assertEqual(res["status"], "rejected")
        self.assertIn("registry", res["reason"])

    def test_unknown_branch_rejected(self):
        res = landd.process_request({"repo": self.repo, "branch": "nope"},
                                    self.registry, demote=False)
        self.assertEqual(res["status"], "rejected")

    def test_dirty_shared_checkout_rejected(self):
        open(os.path.join(self.repo, "README.md"), "a").write("dirty\n")
        res = landd.process_request({"repo": self.repo, "branch": "feat"},
                                    self.registry, demote=False)
        self.assertEqual(res["status"], "rejected")
        self.assertIn("dirty", res["reason"])

    def test_scan_queue_writes_result_files(self):
        q = os.path.join(self.tmp.name, "queue")
        os.makedirs(q)
        req = os.path.join(q, "land-1.json")
        json.dump({"repo": self.repo, "branch": "feat"}, open(req, "w"))
        landd.scan_queue(q, self.registry, demote=False)
        res = json.load(open(req + ".result"))
        self.assertEqual(res["status"], "landed")
        self.assertFalse(os.path.exists(req))  # consumed

    def test_scan_queue_bad_json_gets_result(self):
        q = os.path.join(self.tmp.name, "queue")
        os.makedirs(q)
        req = os.path.join(q, "land-2.json")
        open(req, "w").write("not json")
        landd.scan_queue(q, self.registry, demote=False)
        res = json.load(open(req + ".result"))
        self.assertEqual(res["status"], "rejected")


if __name__ == "__main__":
    unittest.main()
