import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import lanes  # noqa: E402


def run(cwd, *cmd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


def make_repo(base, name):
    root = os.path.join(base, name)
    os.makedirs(root)
    run(root, "git", "init", "-q", "-b", "main")
    run(root, "git", "-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "-q", "--allow-empty", "-m", "init")
    return root


def add_origin(base, root):
    bare = os.path.join(base, os.path.basename(root) + ".git")
    run(base, "git", "init", "-q", "--bare", "-b", "main", bare)
    run(root, "git", "remote", "add", "origin", bare)
    run(root, "git", "push", "-q", "origin", "main")
    run(root, "git", "remote", "set-head", "origin", "main")
    return bare


class TestResolve(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.learned = os.path.join(self.base, "learned.json")

    def test_no_remote_infers_local(self):
        root = make_repo(self.base, "solo")
        res = lanes.resolve(root, learned_path=self.learned)
        self.assertEqual(res["lane"], "local")
        self.assertEqual(res["provenance"], "inferred: no remote")
        self.assertIsNone(res["remote"])

    def test_remoted_infers_push(self):
        root = make_repo(self.base, "mine")
        add_origin(self.base, root)
        res = lanes.resolve(root, learned_path=self.learned)
        self.assertEqual(res["lane"], "push")
        self.assertEqual(res["provenance"], "inferred: remoted")
        self.assertEqual(res["remote"], "origin")
        self.assertEqual(res["default_branch"], "main")

    def test_declared_beats_everything(self):
        root = make_repo(self.base, "conv")
        add_origin(self.base, root)
        with open(os.path.join(root, ".warden.json"), "w") as f:
            json.dump({"version": 1, "lane": "pr"}, f)
        run(root, "git", "add", ".warden.json")
        run(root, "git", "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-q", "-m", "declare")
        res = lanes.resolve(root, learned_path=self.learned)
        self.assertEqual((res["lane"], res["provenance"]), ("pr", "declared"))

    def test_declaration_reads_committed_content_not_working_tree(self):
        root = make_repo(self.base, "tamper")
        add_origin(self.base, root)
        with open(os.path.join(root, ".warden.json"), "w") as f:
            json.dump({"version": 1, "lane": "pr"}, f)  # uncommitted
        res = lanes.resolve(root, learned_path=self.learned)
        self.assertEqual(res["lane"], "push")  # stray file must not steer

    def test_invalid_declaration_falls_through_with_note(self):
        root = make_repo(self.base, "bad")
        add_origin(self.base, root)
        with open(os.path.join(root, ".warden.json"), "w") as f:
            f.write('{"lane": "yolo"}')
        run(root, "git", "add", ".warden.json")
        run(root, "git", "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-q", "-m", "bad")
        res = lanes.resolve(root, learned_path=self.learned)
        self.assertEqual(res["lane"], "push")
        self.assertIn("yolo", res["note"])

    def test_learned_lane_used_when_remote_url_matches(self):
        root = make_repo(self.base, "work")
        bare = add_origin(self.base, root)
        with open(self.learned, "w") as f:
            json.dump({"version": 1, "repos": {os.path.realpath(root): {
                "lane": "pr", "remote_url": bare,
                "learned_from": "GH006", "ts": "2026-07-16T00:00:00Z"}}}, f)
        res = lanes.resolve(root, learned_path=self.learned)
        self.assertEqual((res["lane"], res["provenance"]), ("pr", "learned"))

    def test_learned_lane_dropped_when_remote_url_changed(self):
        root = make_repo(self.base, "moved")
        add_origin(self.base, root)
        with open(self.learned, "w") as f:
            json.dump({"version": 1, "repos": {os.path.realpath(root): {
                "lane": "pr", "remote_url": "git@github.com:old/gone.git",
                "learned_from": "GH006", "ts": "2026-07-16T00:00:00Z"}}}, f)
        res = lanes.resolve(root, learned_path=self.learned)
        self.assertEqual(res["lane"], "push")  # stale lesson ignored


class TestGhAccounts(unittest.TestCase):
    def _write_hosts(self, home, text):
        d = os.path.join(home, ".config", "gh")
        os.makedirs(d)
        with open(os.path.join(d, "hosts.yml"), "w") as f:
            f.write(text)

    def test_multi_account_multi_host_active_first(self):
        home = tempfile.mkdtemp()
        self._write_hosts(home, (
            "github.com:\n"
            "    git_protocol: ssh\n"
            "    users:\n"
            "        jayheavner:\n"
            "            oauth_token: REDACTED1\n"
            "        jay-at-work:\n"
            "            oauth_token: REDACTED2\n"
            "    user: jay-at-work\n"
            "ghe.example.com:\n"
            "    users:\n"
            "        jheavner:\n"
            "            oauth_token: REDACTED3\n"
            "    user: jheavner\n"))
        self.assertEqual(lanes.gh_accounts("github.com", home),
                         ["jay-at-work", "jayheavner"])
        self.assertEqual(lanes.gh_accounts("ghe.example.com", home),
                         ["jheavner"])
        self.assertEqual(lanes.gh_accounts("gitlab.com", home), [])

    def test_hosts_file_absent(self):
        self.assertEqual(lanes.gh_accounts("github.com", tempfile.mkdtemp()),
                         [])


if __name__ == "__main__":
    unittest.main()
