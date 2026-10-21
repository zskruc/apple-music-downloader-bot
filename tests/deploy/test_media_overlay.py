from pathlib import Path
import unittest

import yaml

from deploy.dependencies import DEFAULT_IMAGES


ROOT = Path(__file__).resolve().parents[2]


class MediaOverlayTests(unittest.TestCase):
    def setUp(self):
        self.overlay = yaml.safe_load((ROOT / "services/media/compose.yaml").read_text())

    def test_management_ports_are_loopback_and_profiles_independent(self):
        for name in ("navidrome", "emby", "lrcapi"):
            service = self.overlay["services"][name]
            self.assertEqual(service["profiles"], [name])
            self.assertTrue(all(port.startswith("127.0.0.1:") for port in service["ports"]))
            self.assertNotIn("network_mode", service)
            self.assertNotIn("privileged", service)
            self.assertNotIn("env_file", service)
            self.assertIn(DEFAULT_IMAGES[{"navidrome": "NAVIDROME_IMAGE", "emby": "EMBY_IMAGE", "lrcapi": "LRCAPI_IMAGE"}[name]], service["image"])

    def test_media_is_shared_with_bot_and_read_only(self):
        for name in ("navidrome", "emby", "lrcapi"):
            service = self.overlay["services"][name]
            media = [v for v in service["volumes"] if v["target"].startswith(("/music/", "/video/"))]
            self.assertEqual(len(media), 5 if name == "emby" else 3)
            for volume in media:
                self.assertTrue(volume["read_only"])
                self.assertFalse(volume["bind"]["create_host_path"])
                self.assertNotIn("PERSONAL", volume["source"])

    def test_private_service_state_has_separate_paths(self):
        state_paths = set()
        for name in ("navidrome", "emby", "lrcapi"):
            state = self.overlay["services"][name]["volumes"][0]
            self.assertFalse(state.get("read_only", False))
            state_paths.add(state["source"])
        self.assertEqual(len(state_paths), 3)

    def test_external_player_settings_are_not_forced_to_internal_services(self):
        env = self.overlay["services"]["bot"]["environment"]
        self.assertTrue(all(value.endswith(":-}") for value in env.values()))


if __name__ == "__main__":
    unittest.main()
