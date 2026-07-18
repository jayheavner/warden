import json, os, subprocess, tempfile, unittest, importlib.util

spec = importlib.util.spec_from_file_location(
    "render", os.path.join(os.path.dirname(__file__), "..", "render.py"))
render = importlib.util.module_from_spec(spec); spec.loader.exec_module(render)

TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "templates",
                        "managed-settings.base.json")


def sh(*a):
    subprocess.run(a, check=True, capture_output=True)


class TestRender(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.parent = os.path.join(self.tmp.name, "claude")
        os.makedirs(self.parent)
        self.repo = os.path.join(self.parent, "alpha")
        sh("git", "init", self.repo)
        sh("git", "-C", self.repo, "config", "user.email", "t@t")
        sh("git", "-C", self.repo, "config", "user.name", "t")
        os.makedirs(os.path.join(self.repo, "docs"))
        open(os.path.join(self.repo, "README.md"), "w").write("x")
        open(os.path.join(self.repo, "docs", "d.md"), "w").write("x")
        sh("git", "-C", self.repo, "add", "-A")
        sh("git", "-C", self.repo, "commit", "-m", "init")
        self.wt = os.path.join(self.repo, ".claude", "worktrees", "w1")
        sh("git", "-C", self.repo, "worktree", "add", self.wt, "-b", "worktree-w1")

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_finds_repo_with_metadata(self):
        repos = render.scan_repos([self.parent])
        self.assertEqual(len(repos), 1)
        r = repos[0]
        self.assertEqual(r["root"], os.path.realpath(self.repo))
        self.assertIn(r["head_branch"], ("master", "main"))
        self.assertEqual(sorted(r["top_entries"]), ["README.md", "docs"])
        self.assertEqual(len(r["worktrees"]), 1)
        self.assertTrue(r["worktrees"][0].endswith("w1"))

    def test_scan_skips_nonrepos(self):
        os.makedirs(os.path.join(self.parent, "not-a-repo"))
        repos = render.scan_repos([self.parent])
        self.assertEqual([os.path.basename(r["root"]) for r in repos], ["alpha"])

    def test_scan_missing_parent_ok(self):
        self.assertEqual(render.scan_repos(["/no/such/dir"]), [])

    def test_claude_native_sandbox_is_off(self):
        # Warden's own seatbelt profile is the wall; Claude Code's native
        # sandbox must be OFF, because it cannot be filesystem-only — it
        # forces a network proxy that breaks gh/Node TLS and denies
        # keychain writes. Warden blocks zero networking, zero commands.
        tpl = os.path.join(os.path.dirname(__file__), "..",
                           "templates", "managed-settings.base.json")
        base = json.load(open(tpl))
        self.assertIs(base["sandbox"]["enabled"], False)
        self.assertNotIn("filesystem", base["sandbox"])

    def test_seatbelt_freezes_trunk_AND_all_worktrees(self):
        # the sibling-worktree fix: the profile must NOT blanket-re-open
        # the worktrees container — no worktree is writable except via the
        # per-session parameter. Only the shared-.git write set is re-opened.
        repos = render.scan_repos([self.parent])
        root = os.path.realpath(self.repo)
        b = repos[0]["head_branch"]
        sb = render.render_seatbelt(
            repos, "/Library/Application Support/ClaudeCode")
        self.assertIn('(deny file-write* (subpath "%s"))' % root, sb)
        # NO blanket worktrees-container allow (that let siblings write)
        self.assertNotIn('(allow file-write* (subpath "%s/.claude/worktrees"))'
                         % root, sb)
        self.assertIn('(allow file-write* (subpath "%s/.git/objects"))'
                      % root, sb)
        self.assertIn('(deny file-write* (literal "%s/.git/refs/heads/%s"))'
                      % (root, b), sb)
        self.assertIn('(deny file-write* (subpath "/Library/Application '
                      'Support/ClaudeCode"))', sb)
        self.assertTrue(sb.startswith("(version 1)\n(allow default)"))

    def test_seatbelt_own_worktree_param_is_last(self):
        # the per-session allow must be the FINAL rule so last-match-wins
        # re-opens exactly one worktree over the repo denies above it
        repos = render.scan_repos([self.parent])
        sb = render.render_seatbelt(
            repos, "/Library/Application Support/ClaudeCode")
        param_line = ('(allow file-write* (subpath (param "%s")))'
                      % render.OWN_WT_PARAM)
        self.assertIn(param_line, sb)
        self.assertEqual(sb.strip().splitlines()[-1], param_line)

    def test_seatbelt_rule_order_deny_before_git_reopen(self):
        # the repo deny must precede its shared-.git allows, and the ref
        # denies must come after those allows (re-close inside re-open)
        repos = render.scan_repos([self.parent])
        root = os.path.realpath(self.repo)
        b = repos[0]["head_branch"]
        sb = render.render_seatbelt(
            repos, "/Library/Application Support/ClaudeCode")
        i_deny_repo = sb.index('(deny file-write* (subpath "%s"))' % root)
        i_allow_git = sb.index('(subpath "%s/.git/objects"))' % root)
        i_deny_ref = sb.index('(literal "%s/.git/refs/heads/%s"))' % (root, b))
        self.assertLess(i_deny_repo, i_allow_git)
        self.assertLess(i_allow_git, i_deny_ref)

    def test_check_mode_writes_nothing(self):
        settings = os.path.join(self.tmp.name, "ms.json")
        registry = os.path.join(self.tmp.name, "reg.json")
        p = subprocess.run(["python3", render.__file__, "--scan", self.parent,
                            "--base", TEMPLATE, "--write-settings", settings,
                            "--write-registry", registry, "--check"],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        json.loads(p.stdout)
        self.assertFalse(os.path.exists(settings) or os.path.exists(registry))

    def test_write_mode_atomic_and_valid(self):
        settings = os.path.join(self.tmp.name, "ms.json")
        registry = os.path.join(self.tmp.name, "reg.json")
        p = subprocess.run(["python3", render.__file__, "--scan", self.parent,
                            "--base", TEMPLATE, "--write-settings", settings,
                            "--write-registry", registry],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        s = json.load(open(settings))
        r = json.load(open(registry))
        # claude-native sandbox off — warden's seatbelt is the wall
        self.assertIs(s["sandbox"]["enabled"], False)
        self.assertTrue(s["hooks"]["PreToolUse"])
        self.assertTrue(s["hooks"]["SessionStart"])
        self.assertEqual(s["env"]["WARDEN_ACTIVE"], "1")
        self.assertEqual(len(r["repos"]), 1)
        self.assertTrue(r["generated_at"])
        self.assertFalse(os.path.exists(settings + ".tmp"))


    def test_gitconfig_rendered_per_repo(self):
        repos = render.scan_repos([self.parent])
        text = render.render_gitconfig(
            repos, "/Library/Application Support/ClaudeCode")
        self.assertIn('[includeIf "gitdir:%s/"]' % repos[0]["root"], text)
        self.assertIn("\tpath = /Library/Application Support/ClaudeCode"
                      "/warden/hookpath.gitconfig", text)
        self.assertTrue(text.startswith("#"))  # do-not-edit header

    def test_write_gitconfig_atomic(self):
        settings = os.path.join(self.tmp.name, "ms.json")
        registry = os.path.join(self.tmp.name, "reg.json")
        gc = os.path.join(self.tmp.name, "warden.gitconfig")
        p = subprocess.run(["python3", render.__file__, "--scan", self.parent,
                            "--base", TEMPLATE, "--write-settings", settings,
                            "--write-registry", registry,
                            "--write-gitconfig", gc],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("includeIf", open(gc).read())
        self.assertFalse(os.path.exists(gc + ".tmp"))

    def test_disabled_seatbelt_is_allow_everything(self):
        # the disable failsafe must ship a profile that imposes NO walls —
        # a stale seatbelt must never re-impose isolation after a disable
        sb_on = os.path.join(self.tmp.name, "on.sb")
        sb_off = os.path.join(self.tmp.name, "off.sb")
        for out, extra in [(sb_on, []), (sb_off, ["--disabled"])]:
            p = subprocess.run(["python3", render.__file__, "--scan",
                                self.parent, "--base", TEMPLATE,
                                "--write-settings", os.path.join(
                                    self.tmp.name, "ms.json"),
                                "--write-registry", os.path.join(
                                    self.tmp.name, "reg.json"),
                                "--write-seatbelt", out] + extra,
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("deny file-write*", open(sb_on).read())
        off = open(sb_off).read()
        self.assertNotIn("deny", off)
        self.assertIn("(allow default)", off)

    def test_hookspath_override_detected(self):
        repos = render.scan_repos([self.parent])
        self.assertFalse(repos[0]["hookspath_override"])
        subprocess.run(["git", "-C", repos[0]["root"], "config",
                        "core.hooksPath", "/tmp/x"], check=True)
        repos = render.scan_repos([self.parent])
        self.assertTrue(repos[0]["hookspath_override"])

if __name__ == "__main__":
    unittest.main()
