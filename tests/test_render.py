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

    def test_denywrite_entries(self):
        repos = render.scan_repos([self.parent])
        base = {"sandbox": {"filesystem": {"denyWrite": []}}}
        out = render.render_settings(base, repos,
                                     "/Library/Application Support/ClaudeCode")
        deny = out["sandbox"]["filesystem"]["denyWrite"]
        root = os.path.realpath(self.repo)
        b = repos[0]["head_branch"]
        for want in [root + "/.git/index", root + "/.git/HEAD",
                     root + "/.git/config", root + "/.git/hooks",
                     root + "/.git/info",
                     root + "/.git/refs/heads/" + b,
                     root + "/.git/refs/heads/" + b + ".lock",
                     root + "/.git/logs/refs/heads/" + b,
                     root + "/README.md", root + "/docs",
                     root + "/.claude/settings.json",
                     "/Library/Application Support/ClaudeCode"]:
            self.assertIn(want, deny)
        self.assertNotIn(root + "/.claude/worktrees", deny)
        self.assertNotIn(root, deny)

    def test_allowwrite_carveouts_preserved(self):
        # the base template's home carve-outs (agent CLIs, global memory,
        # caches) must pass through the render untouched
        repos = render.scan_repos([self.parent])
        tpl = os.path.join(os.path.dirname(__file__), "..",
                           "templates", "managed-settings.base.json")
        base = json.load(open(tpl))
        out = render.render_settings(base, repos,
                                     "/Library/Application Support/ClaudeCode")
        allow = out["sandbox"]["filesystem"]["allowWrite"]
        for want in ["~/.claude", "~/.azure", "~/.config", "~/.cache"]:
            self.assertIn(want, allow)
        deny = out["sandbox"]["filesystem"]["denyWrite"]
        self.assertIn("~/.claude/settings.json", deny)
        self.assertIn("~/.claude/settings.local.json", deny)

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
        self.assertTrue(s["sandbox"]["enabled"])
        self.assertTrue(s["sandbox"]["failIfUnavailable"])
        self.assertFalse(s["sandbox"]["allowUnsandboxedCommands"])
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

    def test_disabled_render_flips_sandbox_only(self):
        def render_check(extra_args=None):
            p = subprocess.run(["python3", render.__file__, "--scan",
                                self.parent, "--base", TEMPLATE,
                                "--write-settings", os.path.join(
                                    self.tmp.name, "ms.json"),
                                "--write-registry", os.path.join(
                                    self.tmp.name, "reg.json"),
                                "--check"] + (extra_args or []),
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            return json.loads(p.stdout)["settings"]

        on = render_check()
        off = render_check(["--disabled"])
        self.assertFalse(off["sandbox"]["enabled"])
        self.assertFalse(off["sandbox"]["failIfUnavailable"])
        off["sandbox"]["enabled"] = on["sandbox"]["enabled"]
        off["sandbox"]["failIfUnavailable"] = on["sandbox"]["failIfUnavailable"]
        self.assertEqual(on, off)

    def test_hookspath_override_detected(self):
        repos = render.scan_repos([self.parent])
        self.assertFalse(repos[0]["hookspath_override"])
        subprocess.run(["git", "-C", repos[0]["root"], "config",
                        "core.hooksPath", "/tmp/x"], check=True)
        repos = render.scan_repos([self.parent])
        self.assertTrue(repos[0]["hookspath_override"])

if __name__ == "__main__":
    unittest.main()
