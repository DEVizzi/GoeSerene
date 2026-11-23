import json
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
            self.assertTrue(os.path.exists(out))
            with open(out, "r", encoding="utf-8") as f:
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
    def test_fetch_wikimedia_videos_parses_file_url_and_filters_nc_license(self, mock_get):
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "query": {
                "pages": {
                    "1": {
                        "title": "File:Orca_breach.webm",
                        "imageinfo": [{
                            "url": "https://upload.wikimedia.org/orca.webm",
                            "size": 5000000,
                            "extmetadata": {"LicenseShortName": {"value": "CC BY 4.0"}},
                        }],
                    },
                    "2": {
                        "title": "File:Orca_noncommercial.webm",
                        "imageinfo": [{
                            "url": "https://upload.wikimedia.org/orca_nc.webm",
                            "size": 5000000,
                            "extmetadata": {"LicenseShortName": {"value": "CC BY-NC-ND 4.0"}},
                        }],
                    },
                }
            }
        }
        mock_get.return_value = mock_resp

        with patch("natGeo_bot.is_duplicate_video", return_value=False):
            results = natGeo_bot.fetch_wikimedia_videos("orca", max_results=5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], "https://upload.wikimedia.org/orca.webm")
        self.assertEqual(results[0][1]["source"], "wikimedia")

    def test_generate_tweet_from_video_respects_280_chars(self):
        long_poem = "A" * 400
        tweet = natGeo_bot.generate_tweet_from_video(
            long_poem, "https://youtu.be/abc", "https://www.instagram.com/p/xyz/"
        )
        self.assertLessEqual(len(tweet), 280)

    def test_video_matches_topic_rejects_mismatched_animals(self):
        duck_meta = {"title": "ducks swimming in pond", "tags": ["duck", "bird", "water"], "original_url": ""}
        self.assertFalse(natGeo_bot._video_matches_topic("platypus", duck_meta))

        brown_bear_meta = {"title": "brown bear in forest", "tags": ["bear", "wildlife"], "original_url": ""}
        self.assertFalse(natGeo_bot._video_matches_topic("polar bear", brown_bear_meta))

        polar_bear_meta = {"title": "two polar bears walking on ice", "tags": ["arctic"], "original_url": ""}
        self.assertTrue(natGeo_bot._video_matches_topic("polar bear", polar_bear_meta))

    def test_free_commercial_license_allow_and_deny_rules(self):
        # Allowed commercial-safe free licenses
        self.assertTrue(natGeo_bot.is_free_commercial_license("Public Domain"))
        self.assertTrue(natGeo_bot.is_free_commercial_license("https://creativecommons.org/publicdomain/zero/1.0/"))
        self.assertTrue(natGeo_bot.is_free_commercial_license("CC BY 4.0"))
        self.assertTrue(natGeo_bot.is_free_commercial_license("CC-BY-SA 3.0"))
        self.assertTrue(natGeo_bot.is_free_commercial_license("NASA Public Domain"))
        self.assertTrue(natGeo_bot.is_free_commercial_license("Pexels Free License"))
        self.assertTrue(natGeo_bot.is_free_commercial_license("Coverr Free Commercial License"))

        # Rejected restricted / unknown licenses
        self.assertFalse(natGeo_bot.is_free_commercial_license("CC BY-NC 4.0"))
        self.assertFalse(natGeo_bot.is_free_commercial_license("CC BY-ND 4.0"))
        self.assertFalse(natGeo_bot.is_free_commercial_license("All Rights Reserved"))
        self.assertFalse(natGeo_bot.is_free_commercial_license("Fair Use"))
        self.assertFalse(natGeo_bot.is_free_commercial_license(""))

    def test_verify_long_horizontal_video_quality_enforces_orientation_duration_and_motion(self):
        frame_a = bytes((i * 17) % 220 + 20 for i in range(160 * 90))
        frame_b = bytes((i * 31 + 40) % 220 + 20 for i in range(160 * 90))
        frame_c = bytes((i * 53 + 80) % 220 + 20 for i in range(160 * 90))

        # 1) Reject vertical video even if >8 mins
        with patch("natGeo_bot.probe_video_file", return_value={
            "width": 1080, "height": 1920, "duration": 620.0, "fps": 30.0, "bit_rate": 4_000_000,
        }):
            ok, reason, _ = natGeo_bot.verify_long_horizontal_video_quality(os.path.join(tempfile.gettempdir(), "v.mp4"))
            self.assertFalse(ok)
            self.assertIn("vertical", reason)

        # 2) Reject horizontal video <= 8 mins (e.g. 420s = 7 mins)
        with patch("natGeo_bot.probe_video_file", return_value={
            "width": 1920, "height": 1080, "duration": 420.0, "fps": 30.0, "bit_rate": 4_000_000,
        }):
            ok, reason, _ = natGeo_bot.verify_long_horizontal_video_quality(os.path.join(tempfile.gettempdir(), "v.mp4"))
            self.assertFalse(ok)
            self.assertIn("8 minutes", reason)

        # 3) Reject static/frozen horizontal >8m video
        with patch("natGeo_bot.probe_video_file", return_value={
            "width": 1920, "height": 1080, "duration": 600.0, "fps": 30.0, "bit_rate": 3_000_000,
        }), patch("natGeo_bot._sample_frame_stats", side_effect=[
            (frame_a, 110.0, 42.0),
            (frame_a, 110.0, 42.0),
            (frame_a, 110.0, 42.0),
        ]):
            ok, reason, _ = natGeo_bot.verify_long_horizontal_video_quality(os.path.join(tempfile.gettempdir(), "v.mp4"))
            self.assertFalse(ok)
            self.assertIn("static", reason)

        # 4) Accept crisp 1920x1080 horizontal >8m film with good visual detail and motion
        with patch("natGeo_bot.probe_video_file", return_value={
            "width": 1920, "height": 1080, "duration": 640.0, "fps": 30.0, "bit_rate": 4_500_000,
        }), patch("natGeo_bot._sample_frame_stats", side_effect=[
            (frame_a, 115.0, 44.0),
            (frame_b, 122.0, 48.0),
            (frame_c, 108.0, 41.0),
        ]):
            ok, reason, _ = natGeo_bot.verify_long_horizontal_video_quality(os.path.join(tempfile.gettempdir(), "v.mp4"))
            self.assertTrue(ok)
            self.assertEqual(reason, "ok")

    @patch("natGeo_bot.requests.get")
    def test_fetch_internet_archive_horizontal_long_filters_over_8_mins(self, mock_get):
        search_resp = MagicMock(status_code=200)
        search_resp.json.return_value = {
            "response": {
                "docs": [{
                    "identifier": "usfws_yellowstone_hd",
                    "title": "Yellowstone Wildlife Documentary",
                    "licenseurl": "https://creativecommons.org/publicdomain/mark/1.0/",
                }]
            }
        }
        meta_resp = MagicMock(status_code=200)
        meta_resp.json.return_value = {
            "files": [
                {"name": "short_clip.mp4", "size": "15000000", "length": "45", "width": "1920", "height": "1080"},
                {"name": "vertical_long.mp4", "size": "85000000", "length": "540", "width": "1080", "height": "1920"},
                {"name": "yellowstone_full_1080p.mp4", "size": "240000000", "length": "12:30", "width": "1920", "height": "1080"},
            ]
        }
        mock_get.side_effect = [search_resp, meta_resp]

        with patch("natGeo_bot.is_duplicate_video", return_value=False):
            out = natGeo_bot.fetch_internet_archive_videos(
                "yellowstone wildlife",
                max_results=2,
                min_duration=natGeo_bot.LONG_VIDEO_MIN_SECONDS,
                horizontal_only=True,
            )
        self.assertEqual(len(out), 1)
        self.assertIn("yellowstone_full_1080p.mp4", out[0][0])
        self.assertEqual(out[0][1]["duration"], 750.0)
        self.assertEqual(out[0][1]["width"], 1920)
        self.assertEqual(out[0][1]["height"], 1080)

    @patch("natGeo_bot.requests.get")
    def test_fetch_loc_public_domain_videos_parses_open_mp4(self, mock_get):
        loc_resp = MagicMock(status_code=200)
        loc_resp.json.return_value = {
            "results": [{
                "title": "Glacier National Park Wildlife Film",
                "access_restricted": False,
                "id": "https://www.loc.gov/item/glacier123/",
                "resources": [{"files": [[{"url": "https://tile.loc.gov/storage-services/glacier.mp4"}]]}],
            }]
        }
        mock_get.return_value = loc_resp
        with patch("natGeo_bot.is_duplicate_video", return_value=False):
            out = natGeo_bot.fetch_loc_public_domain_videos("glacier national park", max_results=1, horizontal_only=True)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][1]["source"], "library_of_congress")

    def test_main_fails_loudly_and_queues_backfill_when_youtube_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "state.json")
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump({"pending_youtube_reels": [], "recent_reels": [], "long_videos": []}, f)

            fake_reel = {
                "topic": "orca",
                "title": "Why Orcas Rule the Ocean",
                "instagram_ok": True,
                "instagram_url": "https://www.instagram.com/reel/abc/",
                "youtube_short_ok": False,
                "youtube_short_url": "",
                "youtube_error": "invalid_grant: Token has been expired or revoked.",
                "instagram_error": "",
            }
            with patch("natGeo_bot.load_publish_state", return_value={"pending_youtube_reels": [], "recent_reels": [], "long_videos": []}), \
                 patch("natGeo_bot.run_reel_pipeline", return_value=fake_reel):
                rc = natGeo_bot.main(["--mode", "reel"])
            self.assertEqual(rc, 2)

    def test_validate_remote_media_url_blocks_non_https_and_loopback(self):
        with self.assertRaises(ValueError):
            natGeo_bot._validate_remote_media_url("http://example.com/v.mp4")
        with self.assertRaises(ValueError):
            natGeo_bot._validate_remote_media_url("file:///etc/passwd")
        with self.assertRaises(ValueError):
            natGeo_bot._validate_remote_media_url("https://127.0.0.1/v.mp4")
        self.assertTrue(natGeo_bot._validate_remote_media_url("https://archive.org/download/a/b.mp4"))
        self.assertTrue(natGeo_bot.is_free_commercial_license("Pixabay Content License"))

    def test_youtube_reauth_pkce_and_csrf_state_validation(self):
        import importlib.util
        reauth_path = os.path.join(natGeo_bot.BASE_DIR, "scripts", "youtube_reauth.py")
        spec = importlib.util.spec_from_file_location("youtube_reauth", reauth_path)
        reauth = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reauth)

        verifier, challenge = reauth.pkce_pair()
        self.assertGreaterEqual(len(verifier), 43)
        url = reauth.build_auth_url("123.apps.googleusercontent.com", "http://127.0.0.1:8080/", "st_ok", challenge)
        self.assertIn("code_challenge_method=S256", url)
        self.assertEqual(reauth.parse_callback("/?code=authcode123&state=st_ok", "st_ok"), "authcode123")
        with self.assertRaises(ValueError):
            reauth.parse_callback("/?code=authcode123&state=wrong_state", "st_ok")


if __name__ == "__main__":
    unittest.main()
