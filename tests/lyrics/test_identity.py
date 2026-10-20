from __future__ import annotations

import unittest

from word_lyrics_automation.identity import artist_matches, artist_search_names, title_matches


class IdentityTests(unittest.TestCase):
    def test_soundtrack_qualifier_does_not_hide_exact_title(self) -> None:
        self.assertTrue(
            title_matches("相思 (电视剧《西游记后传》片尾曲)", "相思")
        )

    def test_artist_component_match(self) -> None:
        self.assertTrue(artist_matches("毛阿敏", "毛阿敏 / 群星"))
        self.assertFalse(artist_matches("毛阿敏", "那英"))

    def test_label_verified_localized_artist_name(self) -> None:
        self.assertTrue(artist_matches("The Red Birds", "赤い鳥"))
        self.assertTrue(artist_matches("赤い鳥", "The Red Birds"))
        self.assertIn("赤い鳥", artist_search_names("The Red Birds"))
        self.assertNotIn("赤い鳥", artist_search_names("The Red Birds Tribute"))
        self.assertFalse(artist_matches("The Red Birds", "赤い公園"))


if __name__ == "__main__":
    unittest.main()
