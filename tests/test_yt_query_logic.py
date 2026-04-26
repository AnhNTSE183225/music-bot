import unittest

from yt_query_logic import is_probable_url, is_youtube_link, normalize_yt_search_term


class TestYtQueryLogic(unittest.TestCase):
    def test_is_youtube_link_accepts_common_hosts(self):
        self.assertTrue(is_youtube_link("https://www.youtube.com/watch?v=abc"))
        self.assertTrue(is_youtube_link("https://m.youtube.com/watch?v=abc"))
        self.assertTrue(is_youtube_link("https://music.youtube.com/watch?v=abc"))
        self.assertTrue(is_youtube_link("https://youtu.be/abc"))

    def test_is_youtube_link_rejects_non_youtube(self):
        self.assertFalse(is_youtube_link("https://vimeo.com/123"))
        self.assertFalse(is_youtube_link("into the unknown"))

    def test_is_probable_url(self):
        self.assertTrue(is_probable_url("https://example.com/test"))
        self.assertTrue(is_probable_url("www.example.com/test"))
        self.assertFalse(is_probable_url("just a search query"))

    def test_normalize_yt_search_term_adds_lyrics_when_missing(self):
        query, added = normalize_yt_search_term("into the unknown")
        self.assertEqual(query, "into the unknown lyrics")
        self.assertTrue(added)

    def test_normalize_yt_search_term_keeps_existing_lyrics(self):
        query, added = normalize_yt_search_term("into the unknown Lyrics")
        self.assertEqual(query, "into the unknown Lyrics")
        self.assertFalse(added)


if __name__ == "__main__":
    unittest.main()
