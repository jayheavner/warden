import os, subprocess, tempfile, tomllib, unittest, importlib.util

spec = importlib.util.spec_from_file_location(
    "render", os.path.join(os.path.dirname(__file__), "..", "render.py"))
render = importlib.util.module_from_spec(spec); spec.loader.exec_module(render)

BASE = os.path.join(os.path.dirname(__file__), "..", "templates",
                    "requirements.base.toml")


def sh(*a):
    subprocess.run(a, check=True, capture_output=True)


class TestRenderCodex(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.parent = os.path.join(self.tmp.name, "claude")
        os.makedirs(self.parent)
        self.repo = os.path.join(self.parent, "alpha")
        sh("git", "init", self.repo)
        sh("git", "-C", self.repo, "config", "user.email", "t@t")
        sh("git", "-C", self.repo, "config", "user.name", "t")
        open(os.path.join(self.repo, "README.md"), "w").write("x")
        sh("git", "-C", self.repo, "add", "-A")
        sh("git", "-C", self.repo, "commit", "-m", "init")

    def tearDown(self):
        self.tmp.cleanup()

    def test_profile_rules_from_scan(self):
        repos = render.scan_repos([self.parent])
        base = open(BASE).read()
        out = render.render_codex_requirements(base, repos, "/etc/codex")
        doc = tomllib.loads(out)
        fs = doc["permissions"]["warden"]["filesystem"]
        root = os.path.realpath(self.repo)
        b = repos[0]["head_branch"]
        # frozen zone read-only (codex "deny" is total no-access)
        for frozen in [root + "/.git/index", root + "/.git/HEAD",
                       root + "/.git/config", root + "/.git/hooks/**",
                       root + "/.git/info/**",
                       root + "/.git/refs/heads/" + b,
                       root + "/.git/refs/heads/" + b + ".lock",
                       root + "/.git/logs/refs/heads/" + b,
                       root + "/README.md",
                       root + "/.claude/settings.json",
                       root + "/.codex/**",
                       "/etc/codex/**"]:
            self.assertEqual(fs[frozen], "read", frozen)
        # shared-.git carve-outs writable (upstream worktree-commit bug fix)
        for carve in [root + "/.git/objects/**", root + "/.git/refs/**",
                      root + "/.git/logs/**", root + "/.git/worktrees/**",
                      root + "/.git/packed-refs",
                      root + "/.git/packed-refs.lock",
                      root + "/.git/FETCH_HEAD"]:
            self.assertEqual(fs[carve], "write", carve)
        # worktree area is never frozen
        self.assertNotIn(root + "/.claude/worktrees", fs)
        self.assertNotIn(root, fs)

    def test_cli_codex_format_writes_toml_and_registry(self):
        req = os.path.join(self.tmp.name, "requirements.toml")
        reg = os.path.join(self.tmp.name, "reg.json")
        p = subprocess.run(["python3", render.__file__, "--format", "codex",
                            "--scan", self.parent, "--base", BASE,
                            "--write-settings", req, "--write-registry", reg,
                            "--managed-root", "/etc/codex"],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        doc = tomllib.loads(open(req).read())
        self.assertEqual(doc["default_permissions"], "warden")
        # map, not array: BTreeMap<String, bool> in this codex build
        self.assertEqual(doc["allowed_permission_profiles"], {"warden": True})
        self.assertEqual(doc["allowed_sandbox_modes"],
                         ["read-only", "workspace-write"])
        self.assertEqual(doc["hooks"]["managed_dir"], "/etc/codex/warden")
        self.assertTrue(doc["hooks"]["PreToolUse"][0]["hooks"][0]["command"])
        self.assertTrue(doc["permissions"]["warden"]["filesystem"])
        import json as _json
        self.assertEqual(len(_json.load(open(reg))["repos"]), 1)
        self.assertFalse(os.path.exists(req + ".tmp"))

    def test_disabled_codex_keeps_only_managed_root_rule(self):
        req = os.path.join(self.tmp.name, "requirements.toml")
        reg = os.path.join(self.tmp.name, "reg.json")
        p = subprocess.run(["python3", render.__file__, "--format", "codex",
                            "--scan", self.parent, "--base", BASE,
                            "--write-settings", req, "--write-registry", reg,
                            "--managed-root", "/etc/codex",
                            "--check", "--disabled"],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        rules = tomllib.loads(p.stdout)["permissions"]["warden"]["filesystem"]
        self.assertEqual(list(rules.values()), ["read"])
        (path,) = rules.keys()
        self.assertTrue(path.endswith("/**"))

    def test_cli_codex_check_writes_nothing(self):
        req = os.path.join(self.tmp.name, "requirements.toml")
        reg = os.path.join(self.tmp.name, "reg.json")
        p = subprocess.run(["python3", render.__file__, "--format", "codex",
                            "--scan", self.parent, "--base", BASE,
                            "--write-settings", req, "--write-registry", reg,
                            "--check"], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        tomllib.loads(p.stdout)
        self.assertFalse(os.path.exists(req) or os.path.exists(reg))


if __name__ == "__main__":
    unittest.main()


class TestCodexHomeCarveouts(unittest.TestCase):
    def test_home_carveouts_rendered(self):
        rules = render.codex_fs_rules([], "/etc/codex", home="/Users/u")
        self.assertEqual(rules["/Users/u/.azure/**"], "write")
        self.assertEqual(rules["/Users/u/.config/**"], "write")
        self.assertEqual(rules["/Users/u/.cache/**"], "write")
        self.assertEqual(rules["/Users/u/.codex/**"], "write")
        # tamper surface stays closed inside the ~/.codex grant
        self.assertEqual(rules["/Users/u/.codex/config.toml"], "read")

    def test_no_home_no_carveouts(self):
        rules = render.codex_fs_rules([], "/etc/codex")
        self.assertEqual(list(rules), ["/etc/codex/**"])

    def test_disabled_render_has_no_carveouts(self):
        rules = render.codex_fs_rules([], "/etc/codex", home="/Users/u",
                                      disabled=True)
        self.assertEqual(list(rules), ["/etc/codex/**"])

    def test_scan_owner_home_of_tempdir(self):
        with tempfile.TemporaryDirectory() as d:
            home = render.scan_owner_home([d])
            self.assertTrue(home and os.path.isabs(home))

    def test_scan_owner_home_missing_dir(self):
        self.assertIsNone(render.scan_owner_home(["/no/such/dir"]))
