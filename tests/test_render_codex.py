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
        # frozen zone denied
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
            self.assertEqual(fs[frozen], "deny", frozen)
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
