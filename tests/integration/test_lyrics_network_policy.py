from __future__ import annotations

import fcntl
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("lyrics_network_policy", ROOT / "ops" / "lyrics_network_policy.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


def fixture():
    return {"secret": "fixture-private-controller-secret", "mixed-port": 7890,
            "proxy-groups": [{"name": "Existing", "type": "select", "proxies": ["DIRECT", "node"]}],
            "proxies": [{"name": "node", "type": "ss", "server": "example.test", "password": "fixture-node-password"}],
            "listeners": [{"name": "existing", "type": "mixed", "port": 7891}],
            "dns": {"enable": True, "respect-rules": True, "nameserver-policy": {"+.private.test": ["192.0.2.53"]}},
            "rules": ["DOMAIN,telegram.example,Existing", "MATCH,Existing"],
            "external-controller": "127.0.0.1:9090"}


class LyricsNetworkPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="lyrics-network-policy-")
        self.root = Path(self.temporary.name)
        self.config = self.root / "config.yaml"
        self.original = yaml.safe_dump(fixture(), sort_keys=False)
        self.config.write_text(self.original)
        self.config.chmod(0o640)
        self.state = self.root / "state"
        self.calls = []

    def tearDown(self):
        self.temporary.cleanup()

    def validate(self, candidate):
        self.calls.append(("validate", policy.check_policy(candidate.read_text())["passed"]))
        return True

    def reload(self, config):
        self.calls.append(("reload", policy.check_policy(config.read_text())["passed"]))

    def flush(self):
        self.calls.append(("flush",))

    def apply(self, **kwargs):
        callbacks = {"validate": self.validate, "reload_config": self.reload, "flush_dns": self.flush}
        callbacks.update(kwargs)
        return policy.ensure_policy(self.config, self.state, mode="ensure", **callbacks)

    def test_check_is_read_only_and_never_returns_secrets(self):
        result = policy.ensure_policy(self.config, self.state)
        self.assertFalse(result["passed"])
        self.assertFalse(result["changed"])
        self.assertEqual(self.config.read_text(), self.original)
        self.assertFalse(self.state.exists())
        self.assertNotIn("fixture-private", json.dumps(result))
        self.assertNotIn("fixture-node", json.dumps(result))

    def test_subscription_drift_restored_with_all_other_semantics_preserved(self):
        before = fixture()
        result = self.apply()
        self.assertTrue(result["passed"])
        after = yaml.safe_load(self.config.read_text())
        self.assertEqual(policy._unmanaged(after), policy._unmanaged(before))
        self.assertEqual(after["secret"], before["secret"])
        self.assertEqual(after["proxies"], before["proxies"])
        self.assertEqual(after["listeners"], before["listeners"])
        self.assertNotIn("AppleMusicNetwork", [x["name"] for x in after["proxy-groups"]])
        self.assertEqual(self.calls, [("validate", True), ("reload", True), ("flush",)])
        self.assertEqual(Path(result["backup_path"]).read_text(), self.original)
        self.assertEqual(Path(result["backup_path"]).stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o640)
        self.assertEqual(after["rules"][-2:], before["rules"])

    def test_no_change_does_not_validate_reload_flush_or_rewrite(self):
        self.config.write_text(policy.render_policy(self.original))
        before = self.config.stat().st_mtime_ns
        result = self.apply()
        self.assertTrue(result["passed"])
        self.assertEqual(result["action"], "unchanged")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.config.stat().st_mtime_ns, before)

    def test_validator_failure_never_changes_live_config(self):
        def invalid(_):
            raise RuntimeError("fixture-private-controller-secret")
        with self.assertRaises(policy.NetworkPolicyError) as raised:
            self.apply(validate=invalid)
        self.assertEqual(raised.exception.code, "policy_validation_failed")
        self.assertEqual(self.config.read_text(), self.original)
        self.assertNotIn("fixture-private", json.dumps(raised.exception.as_dict()))
        self.assertFalse(list(self.root.glob(".lyrics-policy-*.yaml")))
        self.assertFalse(self.calls)

    def test_reload_failure_rolls_back_file_and_runtime(self):
        def reload(config):
            fixed = policy.check_policy(config.read_text())["passed"]
            self.calls.append(("reload", fixed))
            if fixed:
                raise RuntimeError("unavailable")
        with self.assertRaises(policy.NetworkPolicyError) as raised:
            self.apply(reload_config=reload)
        self.assertEqual(raised.exception.code, "policy_reload_failed")
        self.assertEqual(self.config.read_text(), self.original)
        self.assertEqual(self.calls, [("validate", True), ("reload", True), ("reload", False), ("flush",)])
        self.assertTrue(raised.exception.details["rollback"]["runtime_restored"])

    def test_dns_flush_failure_also_rolls_back(self):
        count = 0
        def flush():
            nonlocal count
            count += 1
            if count == 1:
                raise RuntimeError("controller cache unavailable")
        with self.assertRaises(policy.NetworkPolicyError) as raised:
            self.apply(flush_dns=flush)
        self.assertEqual(raised.exception.code, "policy_dns_flush_failed")
        self.assertEqual(self.config.read_text(), self.original)
        self.assertTrue(raised.exception.details["rollback"]["dns_flushed"])

    def test_external_file_change_during_validation_is_not_overwritten(self):
        changed = self.original + "\n# operator edit\n"
        def validator(_):
            self.config.write_text(changed)
        with self.assertRaises(policy.NetworkPolicyError) as raised:
            self.apply(validate=validator)
        self.assertEqual(raised.exception.code, "policy_concurrent_change")
        self.assertEqual(self.config.read_text(), changed)
        self.assertEqual(self.calls, [])

    def test_candidate_change_by_validator_is_rejected(self):
        def validator(candidate):
            candidate.write_text(candidate.read_text() + "# external edit\n")
        with self.assertRaises(policy.NetworkPolicyError) as raised:
            self.apply(validate=validator)
        self.assertEqual(raised.exception.code, "policy_candidate_changed")
        self.assertEqual(self.config.read_text(), self.original)

    def test_rollback_does_not_clobber_new_operator_configuration(self):
        changed = self.original + "# later operator config\n"
        def reload(_):
            self.config.write_text(changed)
            raise RuntimeError("reload failed after a concurrent update")
        with self.assertRaises(policy.NetworkPolicyError) as raised:
            self.apply(reload_config=reload)
        self.assertEqual(self.config.read_text(), changed)
        self.assertFalse(raised.exception.details["rollback"]["file_restored"])
        self.assertEqual(raised.exception.details["rollback"]["error"], "policy_concurrent_change")

    def test_failed_same_hash_is_cooled_down_then_retried(self):
        def invalid(_):
            raise RuntimeError("temporary unavailable validator")
        with self.assertRaises(policy.NetworkPolicyError):
            self.apply(validate=invalid, clock=lambda: 1000)
        result = self.apply(clock=lambda: 1001)
        self.assertEqual(result["action"], "cooldown")
        self.assertFalse(result["passed"])
        self.assertFalse(self.calls)
        result = self.apply(clock=lambda: 1400)
        self.assertTrue(result["passed"])

    def test_lock_excludes_another_process(self):
        self.state.mkdir()
        code = "import fcntl,sys;f=open(sys.argv[1],'w');fcntl.flock(f,fcntl.LOCK_EX);print('ready',flush=True);sys.stdin.read()"
        child = subprocess.Popen([sys.executable, "-c", code, str(self.state / "policy.lock")],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            with self.assertRaises(policy.NetworkPolicyError) as raised:
                self.apply(lock_timeout_seconds=0)
        finally:
            child.communicate(input="", timeout=5)
        self.assertEqual(raised.exception.code, "policy_lock_busy")
        self.assertEqual(self.config.read_text(), self.original)

    def test_existing_withdrawn_listener_or_group_requires_explicit_operator_action(self):
        for extra in ({"listeners": [{"name": "apple-music-recovery", "port": 7892}]},
                      {"proxy-groups": [{"name": "AppleMusicNetwork", "type": "fallback", "proxies": ["DIRECT", "node"]}]}):
            with self.subTest(extra=extra):
                config = fixture()
                config.update(extra)
                text = yaml.safe_dump(config)
                self.assertFalse(policy.check_policy(text)["passed"])
                with self.assertRaises(policy.NetworkPolicyError) as raised:
                    policy.render_policy(text)
                self.assertEqual(raised.exception.code, "policy_manual_action_required")

    def test_duplicate_yaml_key_is_rejected_without_echoing_source(self):
        with self.assertRaises(policy.NetworkPolicyError) as raised:
            policy.check_policy("secret: private-fixture\nsecret: private-fixture-2\n")
        self.assertEqual(raised.exception.code, "policy_config_invalid")
        self.assertNotIn("private-fixture", str(raised.exception))

    def test_required_rules_after_catchall_are_reordered(self):
        config = fixture()
        config["rules"] += list(policy.REQUIRED_RULES)
        config["dns"]["nameserver-policy"][policy.LYRICS_HOST] = policy.LYRICS_NAMESERVER
        self.assertFalse(policy.check_policy(config)["passed"])
        fixed = yaml.safe_load(policy.render_policy(config))
        self.assertEqual(fixed["rules"][:len(policy.REQUIRED_RULES)], list(policy.REQUIRED_RULES))
        self.assertTrue(policy.check_policy(fixed)["passed"])

    def test_metadata_change_during_validation_is_guarded(self):
        def validator(_):
            self.config.chmod(0o600)
        with self.assertRaises(policy.NetworkPolicyError) as raised:
            self.apply(validate=validator)
        self.assertEqual(raised.exception.code, "policy_concurrent_change")
        self.assertEqual(self.config.read_text(), self.original)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)

    def test_valid_yaml_merge_retains_effective_unowned_semantics(self):
        text = "defaults: &defaults\n  enable: false\ndns:\n  <<: *defaults\n  enable: true\nrules:\n- MATCH,Existing\nsecret: private-fixture\n"
        rendered = policy.render_policy(text)
        self.assertTrue(yaml.safe_load(rendered)["dns"]["enable"])
        self.assertEqual(policy._unmanaged(yaml.safe_load(rendered)), policy._unmanaged(yaml.safe_load(text)))

    @unittest.skipUnless(hasattr(os, "setxattr"), "extended attributes unavailable")
    def test_config_extended_attributes_are_preserved(self):
        attribute = "user.lyrics-policy-test" if os.uname().sysname != "Darwin" else "org.example.lyrics-policy-test"
        try:
            os.setxattr(self.config, attribute, b"fixture-metadata")
        except OSError as error:
            self.skipTest("filesystem does not support test attribute: " + type(error).__name__)
        self.apply()
        self.assertEqual(os.getxattr(self.config, attribute), b"fixture-metadata")


if __name__ == "__main__":
    unittest.main()
