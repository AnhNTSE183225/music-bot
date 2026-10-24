import sys
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

# Ensure music-bot is in path
sys.path.insert(0, '.')

import yt_query_logic
import api

class TestPlaylistAPI(unittest.TestCase):
    def test_parse_youtube_url_detection(self):
        # Standard playlist
        res = yt_query_logic.parse_youtube_url("https://www.youtube.com/playlist?list=PL123456789")
        self.assertTrue(res['is_playlist'])
        self.assertEqual(res['list_id'], 'PL123456789')
        self.assertEqual(res['playlist_url'], 'https://www.youtube.com/playlist?list=PL123456789')

        # Video in playlist
        res_v = yt_query_logic.parse_youtube_url("https://www.youtube.com/watch?v=abc12345&list=PL123456789")
        self.assertTrue(res_v['is_playlist'])
        self.assertEqual(res_v['video_id'], 'abc12345')
        self.assertEqual(res_v['list_id'], 'PL123456789')

        # Private playlist prefixes
        res_ll = yt_query_logic.parse_youtube_url("https://www.youtube.com/playlist?list=LL")
        self.assertFalse(res_ll['is_playlist'])

        res_wl = yt_query_logic.parse_youtube_url("https://www.youtube.com/playlist?list=WL")
        self.assertFalse(res_wl['is_playlist'])

        # Regular single video
        res_single = yt_query_logic.parse_youtube_url("https://www.youtube.com/watch?v=abc12345")
        self.assertFalse(res_single['is_playlist'])

    def test_request_models(self):
        req_prev = api.PlaylistPreviewRequest(url="https://www.youtube.com/playlist?list=PL123")
        self.assertEqual(req_prev.url, "https://www.youtube.com/playlist?list=PL123")

        req_add = api.AddPlaylistRequest(
            title="My Playlist",
            start_index=2,
            end_index=5,
            tracks=[
                api.PlaylistTrackItem(url="https://youtube.com/watch?v=1", title="Track 1"),
                api.PlaylistTrackItem(url="https://youtube.com/watch?v=2", title="Track 2"),
            ]
        )
        self.assertEqual(req_add.start_index, 2)
        self.assertEqual(req_add.end_index, 5)
        self.assertEqual(len(req_add.tracks), 2)

if __name__ == '__main__':
    unittest.main()
