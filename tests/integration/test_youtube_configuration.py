"""Cross-file YouTube persistence and opt-in administration contracts."""
from pathlib import Path, PurePosixPath
import unittest

import yaml

from test_optional_music_configuration import example_environment, expand, mounts


ROOT = Path(__file__).resolve().parents[2]


class YouTubeConfiguration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.services = yaml.safe_load((ROOT / 'compose.yaml').read_text())['services']
        cls.environment = example_environment()

    def test_cookie_history_and_browser_session_survive_container_recreation(self):
        bot = mounts(self.services['bot'], self.environment)
        browser = mounts(self.services['youtube-auth-browser'], self.environment)
        self.assertEqual(bot['/state']['source'], self.environment['STATE_PATH'])
        self.assertTrue(PurePosixPath(self.environment['YT_COOKIES_PATH']).is_relative_to('/state'))
        self.assertTrue(PurePosixPath(self.environment['YT_STATE_DIR']).is_relative_to('/state'))
        self.assertEqual(bot['/youtube-auth-browser']['source'], browser['/config']['source'])
        self.assertTrue(bot['/youtube-auth-browser']['read_only'])
        self.assertFalse(browser['/config']['read_only'])
        self.assertTrue(PurePosixPath(self.environment['YT_AUTH_BROWSER_PROFILE_PATH']).is_relative_to('/youtube-auth-browser'))

    def test_browser_is_explicit_and_only_exposes_loopback(self):
        browser = self.services['youtube-auth-browser']
        self.assertEqual(browser['profiles'], ['auth'])
        self.assertEqual(browser['restart'], 'no')
        self.assertTrue(all(expand(port, self.environment).startswith('127.0.0.1:')
                            for port in browser['ports']))
        self.assertNotIn('youtube-auth-browser', self.services['bot'].get('depends_on', {}))

    def test_automatic_refresh_requires_both_private_opt_in_and_socket_overlay(self):
        self.assertEqual(self.environment['ONLINE_AUTH_AUTO_REFRESH_ENABLED'], 'false')
        bot_mounts = mounts(self.services['bot'], self.environment)
        self.assertNotIn('/var/run/docker.sock', bot_mounts)
        overlay = yaml.safe_load((ROOT / 'compose.auth-refresh.yaml').read_text())['services']
        self.assertEqual(set(overlay), {'bot'})
        self.assertEqual(set(mounts(overlay['bot'], self.environment)), {'/var/run/docker.sock'})
        self.assertEqual(expand(overlay['bot']['environment']['ONLINE_AUTH_AUTO_REFRESH_ENABLED'],
                                self.environment), 'false')

    def test_cookie_and_po_helpers_reach_the_same_runtime(self):
        bot = self.services['bot']
        provider = self.services['youtube-pot']
        self.assertIn('youtube-pot', bot['depends_on'])
        self.assertNotIn('ports', provider)
        self.assertEqual(self.environment['YT_PO_TOKEN_PROVIDER_URL'], 'http://youtube-pot:4416')
        dockerfile = (ROOT / 'Dockerfile').read_text()
        self.assertIn('COPY bot/ /app/', dockerfile)
        for filename in ('nas_youtube_auth.py', 'yt_dlp_readonly_cookies.py'):
            self.assertTrue((ROOT / 'bot' / filename).is_file())

    def test_sabr_build_and_runtime_require_separate_explicit_opt_ins(self):
        build = self.services['bot']['build']
        self.assertEqual(self.environment['INSTALL_YT_SABR'], 'false')
        self.assertEqual(self.environment['YT_SABR_FALLBACK_ENABLED'], 'false')
        self.assertEqual(expand(build['args']['INSTALL_YT_SABR'], self.environment), 'false')
        installation = dict(self.environment, INSTALL_YT_SABR='true')
        self.assertEqual(expand(build['args']['INSTALL_YT_SABR'], installation), 'true')
        self.assertEqual(installation['YT_SABR_FALLBACK_ENABLED'], 'false')
        self.assertEqual(self.environment['YT_SABR_TOOL_PATH'], '/app/vendor/yt-dlp-sabr/yt-dlp')


if __name__ == '__main__':
    unittest.main()
