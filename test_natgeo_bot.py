import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import natGeo_bot


class TestGeoSereneUpgrades(unittest.TestCase):
    def test_clean_text_for_tts_preserves_natural_commas(self):
        raw = "Wait, did you know #orca can hunt? Visit https://example.com @user!"
        cleaned = natGeo_bot.clean_text_for_tts(raw)
        self.assertIn(",", cleaned)
        self.assertNotIn("https://", cleaned)
        self.assertNotIn("#orca", cleaned)
        self.assertNotIn("@user", cleaned)

    def test_generate_ass_subtitles_creates_valid_dialogue_lines(self):
        events = [
            {"text": "This", "start": 0.1, "end": 0.3},
            {"text": "tiny", "start": 0.3, "end": 0.6},
            {"text": "cold-blooded", "start": 0.65, "end": 1.1},
            {"text": "killer", "start": 1.12, "end": 1.4},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            ass_path = os.path.join(tmpdir, "test.ass")
            out = natGeo_bot.generate_ass_subtitles(events, ass_path, words_per_chunk=2)
            self.assertEqual(out, ass_path)
            with open(ass_path, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn("WrapStyle: 0", content)
            self.assertIn("THIS TINY", content)
            self.assertIn("COLD-BLOODED\n", content)
            self.assertIn("KILLER\n", content)

    def test_ensure_complete_sentences_strips_ellipses_and_fragments(self):
        raw = "Why is it warm inside? Three hundred teeth wait to strike, proving that predators need..."
        cleaned = natGeo_bot._ensure_complete_sentences(raw)
        self.assertNotIn("...", cleaned)
        self.assertTrue(cleaned.endswith("."))

    @patch("natGeo_bot.requests.get")
    def test_fetch_wildlife_fact_success(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "title": "Mantis shrimp",
            "description": "Order of marine crustaceans",
            "extract": "Mantis shrimp strike with the acceleration of a .22 caliber bullet.",
        }
        mock_get.return_value = mock_resp

        fact = natGeo_bot.fetch_wildlife_fact("mantis shrimp")
        self.assertEqual(fact["title"], "Mantis shrimp")
        self.assertIn("bullet", fact["extract"])

    @patch("natGeo_bot.requests.get")
    def test_fetch_nasa_videos_parses_mp4(self, mock_get):
        search_resp = MagicMock(status_code=200)
        search_resp.json.return_value = {
            "collection": {"items": [{"data": [{"nasa_id": "GSFC_123", "title": "Aurora"}]}]}
        }
        asset_resp = MagicMock(status_code=200)
        asset_resp.json.return_value = {
            "collection": {"items": [{"href": "https://images-assets.nasa.gov/video/GSFC_123/orig.mp4"}]}
        }
        mock_get.side_effect = [search_resp, asset_resp]

        with patch("natGeo_bot.is_duplicate_video", return_value=False):
            results = natGeo_bot.fetch_nasa_videos("aurora", max_results=1)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0][0].endswith(".mp4"))
        self.assertEqual(results[0][1]["source"], "nasa")

    @patch("natGeo_bot.requests.get")
    def test_fetch_wikimedia_videos_parses_file_url(self, mock_get):
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "query": {
                "pages": {
                    "1": {
                        "title": "File:Orca_breach.webm",
                        "imageinfo": [{"url": "https://upload.wikimedia.org/orca.webm", "size": 5000000}],
                    }
                }
            }
        }
        mock_get.return_value = mock_resp

        with patch("natGeo_bot.is_duplicate_video", return_value=False):
            results = natGeo_bot.fetch_wikimedia_videos("orca", max_results=1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][1]["source"], "wikimedia")

    def test_generate_tweet_from_video_respects_280_chars(self):
        long_poem = "A" * 400
        tweet = natGeo_bot.generate_tweet_from_video(
            long_poem, "https://youtu.be/abc", "https://www.instagram.com/p/xyz/"
        )
        self.assertLessEqual(len(tweet), 280)


if __name__ == "__main__":
    unittest.main()
