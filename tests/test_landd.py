import json, os, subprocess, tempfile, time, unittest, importlib.util

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


class TestClassifier(unittest.TestCase):
    # Captured GitHub transcript (GH006). Fixture text is real, not invented.
    GH006_PORCELAIN = ("To github.com:acme/app.git\n"
                       "!\trefs/heads/main:refs/heads/main\t"
                       "[remote rejected] (protected branch hook declined)\n"
                       "Done")
    GH006_STDERR = ("remote: error: GH006: Protected branch update failed "
                    "for refs/heads/main.\n"
                    "remote: error: Changes must be made through a pull "
                    "request.\n"
                    "error: failed to push some refs to "
                    "'github.com:acme/app.git'")

    def test_gh006_is_policy(self):
        self.assertEqual(
            landd.classify_push_failure(self.GH006_PORCELAIN,
                                        self.GH006_STDERR), "policy")

    def test_non_ff_is_nonff_not_policy(self):
        porcelain = ("To github.com:me/mine.git\n"
                     "!\trefs/heads/main:refs/heads/main\t"
                     "[rejected] (non-fast-forward)\nDone")
        stderr = ("hint: Updates were rejected because the remote contains "
                  "work that you do\nhint: not have locally.")
        self.assertEqual(landd.classify_push_failure(porcelain, stderr),
                         "nonff")

    def test_transport_failure_is_other_even_with_policy_words(self):
        # No "! ..." rejected ref line -> the push was never judged by the
        # remote; text mentioning pull requests must not cause learning.
        stderr = ("ssh: connect to host github.com port 22: Network is "
                  "unreachable\nPlease open a pull request instead? "
                  "fatal: Could not read from remote repository.")
        self.assertEqual(landd.classify_push_failure("", stderr), "other")

    def test_unrecognized_rejection_is_other(self):
        porcelain = ("To git.example.com:x/y.git\n"
                     "!\trefs/heads/main:refs/heads/main\t"
                     "[remote rejected] (pre-receive hook declined)\nDone")
        stderr = "remote: custom policy: deploy freeze until Friday"
        self.assertEqual(landd.classify_push_failure(porcelain, stderr),
                         "other")


def make_remoted_pair(base, name):
    """Shared checkout with a bare origin, one commit pushed, origin/HEAD set."""
    bare = os.path.join(base, name + ".git")
    sh("git", "init", "-q", "--bare", "-b", "main", bare)
    root = os.path.join(base, name)
    sh("git", "clone", "-q", bare, root)
    sh("git", "-C", root, "-c", "user.email=t@t", "-c", "user.name=t",
       "commit", "-q", "--allow-empty", "-m", "init")
    sh("git", "-C", root, "push", "-q", "origin", "main")
    sh("git", "-C", root, "remote", "set-head", "origin", "main")
    return root, bare


def add_branch_commit(root, branch):
    """Create <branch> = main + one empty commit; leave main where it was."""
    sh("git", "-C", root, "-c", "user.email=t@t", "-c", "user.name=t",
       "commit", "-q", "--allow-empty", "-m", "work on " + branch)
    sha = out("git", "-C", root, "rev-parse", "HEAD")
    sh("git", "-C", root, "update-ref", "refs/heads/" + branch, sha)
    sh("git", "-C", root, "reset", "-q", "--hard", "HEAD~1")
    return sha


class TestPushLane(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.learned = os.path.join(self.base, "learned.json")
        self.root, self.bare = make_remoted_pair(self.base, "mine")
        self.registry = {"repos": [{"root": os.path.realpath(self.root),
                                    "head_branch": "main"}]}

    def tearDown(self):
        self.tmp.cleanup()

    def land(self, branch, repo=None):
        return landd.process_request(
            {"repo": repo or self.root, "branch": branch}, self.registry,
            demote=False, learned_path=self.learned)

    def test_push_lane_lands_and_pushes(self):
        sha = add_branch_commit(self.root, "feat")
        res = self.land("feat")
        self.assertEqual(res["status"], "landed", res)
        self.assertEqual(res["lane"], "push")
        self.assertEqual(out("git", "-C", self.bare, "rev-parse", "main"),
                         sha)  # remote advanced
        self.assertEqual(out("git", "-C", self.root, "rev-parse", "main"),
                         sha)  # local followed

    def test_sync_catches_local_up_before_landing(self):
        # someone else pushed to origin; local main is behind
        other = os.path.join(self.base, "other")
        sh("git", "clone", "-q", self.bare, other)
        sh("git", "-C", other, "-c", "user.email=o@o", "-c", "user.name=o",
           "commit", "-q", "--allow-empty", "-m", "upstream")
        sh("git", "-C", other, "push", "-q", "origin", "main")
        add_branch_commit(self.root, "feat")
        res = self.land("feat")
        # feat does not contain the upstream commit -> must be rejected
        # against the TRUE tip, with the merge fix
        self.assertEqual(res["status"], "rejected", res)
        self.assertIn("merge", res["reason"])

    def test_local_ahead_of_origin_rejects_loudly(self):
        # invariant broken outside warden: local main has a commit origin lacks
        sh("git", "-C", self.root, "-c", "user.email=t@t",
           "-c", "user.name=t", "commit", "-q", "--allow-empty",
           "-m", "rogue local")
        add_branch_commit(self.root, "feat")
        res = self.land("feat")
        self.assertEqual(res["status"], "rejected", res)
        self.assertIn("human", res["reason"])

    def test_no_remote_repo_still_lands_local_v1(self):
        solo = os.path.join(self.base, "solo")
        os.makedirs(solo)
        sh("git", "-C", solo, "init", "-q", "-b", "main")
        sh("git", "-C", solo, "-c", "user.email=t@t", "-c", "user.name=t",
           "commit", "-q", "--allow-empty", "-m", "init")
        add_branch_commit(solo, "feat")
        reg = {"repos": [{"root": os.path.realpath(solo),
                          "head_branch": "main"}]}
        res = landd.process_request({"repo": solo, "branch": "feat"}, reg,
                                    demote=False, learned_path=self.learned)
        self.assertEqual((res["status"], res["lane"]), ("landed", "local"))


GH006_HOOK = """#!/bin/sh
# Protect only main, like real branch protection: topic branches push fine.
while read old new ref; do
  if [ "$ref" = "refs/heads/main" ]; then
    echo "error: GH006: Protected branch update failed for refs/heads/main." >&2
    echo "error: Changes must be made through a pull request." >&2
    exit 1
  fi
done
exit 0
"""


def protect_main(bare):
    hook = os.path.join(bare, "hooks", "pre-receive")
    with open(hook, "w") as f:
        f.write(GH006_HOOK)
    os.chmod(hook, 0o755)


class TestPolicyLearning(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.learned = os.path.join(self.base, "learned.json")
        self.root, self.bare = make_remoted_pair(self.base, "work")
        self.registry = {"repos": [{"root": os.path.realpath(self.root),
                                    "head_branch": "main"}]}

    def tearDown(self):
        self.tmp.cleanup()

    def land(self, branch):
        return landd.process_request(
            {"repo": self.root, "branch": branch}, self.registry,
            demote=False, learned_path=self.learned)

    def test_policy_denial_falls_back_and_learns(self):
        protect_main(self.bare)
        add_branch_commit(self.root, "feat")
        res = self.land("feat")
        # falls into pr lane: at minimum the branch reached the remote
        self.assertIn(res["status"],
                      ("branch-pushed", "pr-opened", "pr-exists"), res)
        lesson = json.load(open(self.learned))["repos"][
            os.path.realpath(self.root)]
        self.assertEqual(lesson["lane"], "pr")
        self.assertIn("gh006", lesson["learned_from"].lower())
        # local main did NOT advance to the session sha
        self.assertNotEqual(out("git", "-C", self.root, "rev-parse", "main"),
                            out("git", "-C", self.root, "rev-parse", "feat"))

    def test_second_land_goes_straight_to_pr_lane(self):
        protect_main(self.bare)
        add_branch_commit(self.root, "one")
        self.land("one")
        add_branch_commit(self.root, "two")
        res = self.land("two")
        self.assertEqual(res["provenance"], "learned", res)

    def test_forget_restores_push(self):
        protect_main(self.bare)
        add_branch_commit(self.root, "feat")
        self.land("feat")
        removed = landd.forget(self.root, learned_path=self.learned)
        self.assertEqual(removed["lane"], "pr")
        self.assertEqual(json.load(open(self.learned))["repos"], {})
        os.unlink(os.path.join(self.bare, "hooks", "pre-receive"))
        add_branch_commit(self.root, "next")
        res = self.land("next")
        self.assertEqual((res["status"], res["lane"]), ("landed", "push"),
                         res)

    def test_ambiguous_rejection_writes_no_lesson(self):
        hook = os.path.join(self.bare, "hooks", "pre-receive")
        with open(hook, "w") as f:
            f.write("#!/bin/sh\necho 'deploy freeze until Friday' >&2\n"
                    "exit 1\n")
        os.chmod(hook, 0o755)
        add_branch_commit(self.root, "feat")
        res = self.land("feat")
        self.assertEqual(res["status"], "rejected", res)
        self.assertFalse(os.path.exists(self.learned))


FAKE_GH_OK = """#!/bin/sh
echo "$@" >> "$FAKE_GH_LOG"
echo "https://github.com/acme/app/pull/312"
"""

FAKE_GH_EXISTS = """#!/bin/sh
echo "$@" >> "$FAKE_GH_LOG"
case "$1 $2" in
  "pr create") echo "a pull request for branch feat already exists" >&2; exit 1;;
  "pr view")   echo "https://github.com/acme/app/pull/311";;
esac
"""


class TestPrLane(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.learned = os.path.join(self.base, "learned.json")
        self.root, self.bare = make_remoted_pair(self.base, "acme")
        self.registry = {"repos": [{"root": os.path.realpath(self.root),
                                    "head_branch": "main"}]}
        # declare pr so the lane is entered directly
        with open(os.path.join(self.root, ".warden.json"), "w") as f:
            json.dump({"version": 1, "lane": "pr"}, f)
        sh("git", "-C", self.root, "add", ".warden.json")
        sh("git", "-C", self.root, "-c", "user.email=t@t",
           "-c", "user.name=t", "commit", "-q", "-m", "declare")
        sh("git", "-C", self.root, "push", "-q", "origin", "main")
        self.ghlog = os.path.join(self.base, "gh.log")
        os.environ["FAKE_GH_LOG"] = self.ghlog
        self.addCleanup(os.environ.pop, "FAKE_GH_LOG", None)

    def tearDown(self):
        self.tmp.cleanup()

    def _fake_gh(self, script):
        bindir = os.path.join(self.base, "bin")
        os.makedirs(bindir, exist_ok=True)
        gh = os.path.join(bindir, "gh")
        with open(gh, "w") as f:
            f.write(script)
        os.chmod(gh, 0o755)
        old = os.environ["PATH"]
        os.environ["PATH"] = bindir + os.pathsep + old
        self.addCleanup(os.environ.__setitem__, "PATH", old)

    def _no_gh(self):
        # PATH with git but no gh
        gitdir = os.path.dirname(out("which", "git"))
        shdir = os.path.dirname(out("which", "sh"))
        old = os.environ["PATH"]
        os.environ["PATH"] = gitdir + os.pathsep + shdir
        self.addCleanup(os.environ.__setitem__, "PATH", old)

    def land(self, branch):
        return landd.process_request(
            {"repo": self.root, "branch": branch}, self.registry,
            demote=False, learned_path=self.learned)

    def test_pr_opened(self):
        self._fake_gh(FAKE_GH_OK)
        add_branch_commit(self.root, "feat")
        res = self.land("feat")
        self.assertEqual(res["status"], "pr-opened", res)
        self.assertEqual(res["url"], "https://github.com/acme/app/pull/312")
        argv = open(self.ghlog).read()
        self.assertIn("pr create --base main --head feat --fill", argv)
        # branch reached the remote
        rc = subprocess.run(["git", "-C", self.bare, "rev-parse",
                             "refs/heads/feat"], capture_output=True)
        self.assertEqual(rc.returncode, 0)

    def test_pr_exists(self):
        self._fake_gh(FAKE_GH_EXISTS)
        add_branch_commit(self.root, "feat")
        res = self.land("feat")
        self.assertEqual(res["status"], "pr-exists", res)
        self.assertEqual(res["url"], "https://github.com/acme/app/pull/311")

    def test_gh_absent_degrades_to_branch_pushed(self):
        self._no_gh()
        add_branch_commit(self.root, "feat")
        res = self.land("feat")
        self.assertEqual(res["status"], "branch-pushed", res)
        self.assertIn("gh", res["reason"])


class TestQueueHygiene(unittest.TestCase):
    def test_old_results_swept_fresh_kept(self):
        q = tempfile.mkdtemp()
        old = os.path.join(q, "land-1-1.json.result")
        new = os.path.join(q, "land-2-2.json.result")
        for p in (old, new):
            with open(p, "w") as f:
                f.write("{}")
        past = time.time() - 8 * 86400
        os.utime(old, (past, past))
        landd.sweep_results(q, max_age_days=7)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))

    def test_sync_all_catches_up_remoted_repo(self):
        base = tempfile.mkdtemp()
        root, bare = make_remoted_pair(base, "r")
        other = os.path.join(base, "o")
        sh("git", "clone", "-q", bare, other)
        sh("git", "-C", other, "-c", "user.email=o@o", "-c", "user.name=o",
           "commit", "-q", "--allow-empty", "-m", "up")
        sh("git", "-C", other, "push", "-q", "origin", "main")
        reg = {"repos": [{"root": os.path.realpath(root),
                          "head_branch": "main"}]}
        res = landd.sync_all(reg, demote=False)
        self.assertEqual(res[0]["status"], "synced", res)
        self.assertEqual(out("git", "-C", root, "rev-parse", "main"),
                         out("git", "-C", bare, "rev-parse", "main"))


class TestRemoteAdd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.realpath(os.path.join(self.tmp.name, "alpha"))
        sh("git", "init", "-q", "-b", "main", self.repo)
        sh("git", "-C", self.repo, "-c", "user.email=t@t",
           "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
        self.registry = {"repos": [{"root": self.repo,
                                    "head_branch": "main"}]}

    def tearDown(self):
        self.tmp.cleanup()

    def request(self, **kw):
        req = {"op": "remote-add", "repo": self.repo, "name": "origin",
               "url": "https://github.com/jayheavner/warden.git"}
        req.update(kw)
        return landd.process_remote_request(req, self.registry, demote=False)

    def test_add_creates_remote(self):
        res = self.request()
        self.assertEqual(res["status"], "remote-added", res)
        self.assertEqual(out("git", "-C", self.repo, "remote", "get-url",
                             "origin"),
                         "https://github.com/jayheavner/warden.git")

    def test_same_url_again_is_unchanged(self):
        self.request()
        res = self.request()
        self.assertEqual(res["status"], "unchanged", res)

    def test_different_url_updates_and_reports_previous(self):
        self.request()
        res = self.request(url="https://github.com/jayheavner/other.git")
        self.assertEqual(res["status"], "remote-updated", res)
        self.assertEqual(res["previous"],
                         "https://github.com/jayheavner/warden.git")
        self.assertEqual(out("git", "-C", self.repo, "remote", "get-url",
                             "origin"),
                         "https://github.com/jayheavner/other.git")

    def assert_rejected(self, res, needle):
        self.assertEqual(res["status"], "rejected", res)
        self.assertIn(needle, res["reason"])

    def test_unknown_repo_rejected(self):
        self.assert_rejected(self.request(repo="/no/such"), "registry")

    def test_bad_remote_names_rejected(self):
        for name in ("-evil", "", "a name", "a;b", "../up"):
            self.assert_rejected(self.request(name=name), "remote name")

    def test_non_transport_urls_rejected(self):
        for url in ("file:///etc/passwd",
                    "/Users/jay/claude/other",
                    "ext::sh -c whoami",
                    "--upload-pack=/tmp/x",
                    "https://github.com/a/b.git --upload-pack=/tmp/x",
                    "https://github.com/a/b.git\nhelper = !sh",
                    "git://github.com/a/b.git",
                    "https://github.com/a/b.git\n",
                    ""):
            self.assert_rejected(self.request(url=url), "URL")

    def test_ssh_urls_accepted(self):
        for url in ("git@github.com:jayheavner/warden.git",
                    "ssh://git@github.com/jayheavner/warden.git"):
            res = self.request(name="up-" + str(abs(hash(url)) % 100),
                               url=url)
            self.assertEqual(res["status"], "remote-added", res)

    def test_only_remote_section_changes(self):
        cfg = os.path.join(self.repo, ".git", "config")
        before = open(cfg).read()
        self.request()
        added = [l for l in open(cfg).read().splitlines()
                 if l not in before.splitlines()]
        for line in added:
            self.assertRegex(
                line.strip(),
                r'^(\[remote "origin"\]|url = |fetch = \+refs/heads/)',
                "unexpected config line: %r" % line)


if __name__ == "__main__":
    unittest.main()
