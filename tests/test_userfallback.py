"""userfallback: deliver warden enforcement through the user-settings layer.

On Claude Enterprise machines the org's remote policy replaces the
policySettings layer, discarding the local managed-settings file. These
tests pin the fallback's merge semantics against ~/.claude/settings.json.
"""
import copy
import json
import os
import importlib.util
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    "userfallback",
    os.path.join(os.path.dirname(__file__), "..", "userfallback.py"))
uf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(uf)

MANAGED = {
    "sandbox": {"enabled": False},
    "permissions": {
        "deny": ["Edit(//Library/Application Support/ClaudeCode/**)"],
    },
    "hooks": {
        "PreToolUse": [
            {"matcher": "Edit|Write|NotebookEdit",
             "hooks": [{"type": "command",
                        "command": "python3 '/Library/Application Support/ClaudeCode/warden/guard.py'"}]},
            {"matcher": "Bash",
             "hooks": [{"type": "command",
                        "command": "python3 '/Library/Application Support/ClaudeCode/warden/guard.py'"}]},
        ],
        "SessionStart": [
            {"hooks": [{"type": "command",
                        "command": "python3 '/Library/Application Support/ClaudeCode/warden/guard.py'"}]},
        ],
    },
    "env": {"WARDEN_ACTIVE": "1"},
}

USER = {
    "permissions": {"allow": ["Bash(ls :*)"], "deny": ["WebFetch"]},
    "env": {"FOO": "bar"},
    "hooks": {
        "PreToolUse": [
            {"matcher": "Bash",
             "hooks": [{"type": "command", "command": "/Users/u/mine.sh"}]},
        ],
    },
    "model": "opusplan",
}

SETTINGS_PATH = "/Users/u/.claude/settings.json"
STATE_DIR = "/Users/u/.claude/warden"


class TestMerge(unittest.TestCase):
    def merged(self, user=None, disabled=False):
        return uf.merge(copy.deepcopy(user if user is not None else USER),
                        MANAGED, SETTINGS_PATH, disabled=disabled)

    def test_delivers_env_hooks_sandbox_off(self):
        out = self.merged()
        self.assertEqual(out["env"]["WARDEN_ACTIVE"], "1")
        self.assertEqual(out["env"]["FOO"], "bar")
        cmds = [h["command"] for g in out["hooks"]["PreToolUse"]
                for h in g["hooks"]]
        self.assertEqual(
            sum("warden/guard.py" in c for c in cmds), 2)
        self.assertIn("/Users/u/mine.sh", cmds)
        # native sandbox stays off — the seatbelt profile is the wall, and
        # re-enabling it here would re-break gh/keychain in every session
        self.assertIs(out["sandbox"]["enabled"], False)
        self.assertNotIn("filesystem", out["sandbox"])

    def test_permissions_deny_added_allow_preserved(self):
        out = self.merged()
        self.assertIn("Edit(//Library/Application Support/ClaudeCode/**)",
                      out["permissions"]["deny"])
        self.assertIn("WebFetch", out["permissions"]["deny"])
        self.assertEqual(out["permissions"]["allow"], ["Bash(ls :*)"])
        self.assertEqual(out["model"], "opusplan")

    def test_idempotent(self):
        once = self.merged()
        twice = uf.merge(copy.deepcopy(once), MANAGED, SETTINGS_PATH)
        self.assertEqual(once, twice)

    def test_disabled_keeps_hooks_sandbox_still_off(self):
        out = self.merged(disabled=True)
        self.assertIs(out["sandbox"]["enabled"], False)
        cmds = [h["command"] for g in out["hooks"]["SessionStart"]
                for h in g["hooks"]]
        self.assertTrue(any("warden/guard.py" in c for c in cmds))

    def test_empty_user_settings(self):
        out = self.merged(user={})
        self.assertEqual(out["env"]["WARDEN_ACTIVE"], "1")
        self.assertIn("hooks", out)
        self.assertIn("sandbox", out)


class TestRemove(unittest.TestCase):
    def test_remove_restores_pre_warden_shape(self):
        merged = uf.merge(copy.deepcopy(USER), MANAGED, SETTINGS_PATH)
        state = uf.make_state(USER, MANAGED)
        restored = uf.remove(copy.deepcopy(merged), MANAGED, state)
        self.assertEqual(restored, USER)

    def test_remove_when_user_had_own_sandbox(self):
        user = dict(copy.deepcopy(USER),
                    sandbox={"enabled": False, "note": "mine"})
        merged = uf.merge(copy.deepcopy(user), MANAGED, SETTINGS_PATH)
        state = uf.make_state(user, MANAGED)
        restored = uf.remove(copy.deepcopy(merged), MANAGED, state)
        self.assertEqual(restored, user)

    def test_remove_without_state_still_strips_warden_keys(self):
        merged = uf.merge(copy.deepcopy(USER), MANAGED, SETTINGS_PATH)
        restored = uf.remove(copy.deepcopy(merged), MANAGED, None)
        self.assertNotIn("WARDEN_ACTIVE", restored.get("env", {}))
        self.assertNotIn("sandbox", restored)
        cmds = [h["command"]
                for ev in restored.get("hooks", {}).values()
                for g in ev for h in g["hooks"]]
        self.assertFalse(any("warden/guard.py" in c for c in cmds))
        self.assertIn("/Users/u/mine.sh", cmds)


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.managed = os.path.join(self.tmp.name, "managed-settings.json")
        json.dump(MANAGED, open(self.managed, "w"))
        self.user = os.path.join(self.tmp.name, "settings.json")
        self.state = os.path.join(self.tmp.name, "state", "fallback.json")

    def run_cli(self, *extra):
        return uf.main(["--managed-settings", self.managed,
                        "--user-settings", self.user,
                        "--state", self.state] + list(extra))

    def test_creates_missing_user_settings(self):
        self.assertEqual(self.run_cli(), 0)
        out = json.load(open(self.user))
        self.assertEqual(out["env"]["WARDEN_ACTIVE"], "1")
        self.assertTrue(os.path.isfile(self.state))

    def test_backs_up_pre_warden_settings_once(self):
        json.dump(USER, open(self.user, "w"))
        self.assertEqual(self.run_cli(), 0)
        backup = os.path.join(os.path.dirname(self.state),
                              "settings.json.pre-warden")
        self.assertEqual(json.load(open(backup)), USER)
        # second run must not clobber the original backup
        self.assertEqual(self.run_cli(), 0)
        self.assertEqual(json.load(open(backup)), USER)

    def test_refuses_unparseable_user_settings(self):
        open(self.user, "w").write("{not json")
        self.assertNotEqual(self.run_cli(), 0)
        self.assertEqual(open(self.user).read(), "{not json")

    def test_remove_roundtrip(self):
        json.dump(USER, open(self.user, "w"))
        self.assertEqual(self.run_cli(), 0)
        self.assertEqual(self.run_cli("--remove"), 0)
        self.assertEqual(json.load(open(self.user)), USER)

    def test_check_prints_without_writing(self):
        self.assertEqual(self.run_cli("--check"), 0)
        self.assertFalse(os.path.exists(self.user))


if __name__ == "__main__":
    unittest.main()
