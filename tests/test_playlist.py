"""Run with: python -m unittest discover -s tests -v (server on PYTHONPATH)."""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from potbridge.playlist import _active_saved_playlist, discover_playlist


class PlaylistTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def media(self, name):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return path.resolve()

    def test_numbered_dpl_preserves_playlist_order_not_filename_order(self):
        ep10, ep2 = self.media("ep10.mkv"), self.media("ep2.mkv")
        playlist = self.root / "saved.dpl"
        playlist.write_text("DAUMPLAYLIST\n2*file=ep2.mkv\n1*file=ep10.mkv\n", encoding="utf-8-sig")
        selection = discover_playlist(ep2, playlist)
        self.assertEqual(selection.paths, [ep10, ep2])
        self.assertEqual(selection.source, "saved-playlist")
        self.assertIn("сохраненный", selection.warning)

    def test_cp1251_relative_m3u_omits_missing_files_and_urls(self):
        first, second = self.media("медиа/серия 2.mkv"), self.media("медиа/серия 1.mkv")
        playlist = self.root / "список.m3u"
        playlist.write_bytes("#EXTM3U\nмедиа/серия 2.mkv\nhttps://example.invalid/video.mkv\nmissing.mkv\nмедиа/серия 1.mkv\n".encode("cp1251"))
        selection = discover_playlist(first, playlist)
        self.assertEqual(selection.paths, [first, second])
        self.assertIn("2.", selection.warning)

    def test_utf8_bom_m3u8_and_current_file_inclusion(self):
        first, current = self.media("один.mkv"), self.media("текущий.mkv")
        playlist = self.root / "saved.m3u8"
        playlist.write_text("#EXTM3U\nодин.mkv\n", encoding="utf-8-sig")
        selection = discover_playlist(current, playlist)
        self.assertEqual(selection.paths, [first, current])
        self.assertIn("добавлен в конец", selection.warning)

    def test_folder_navigator_natural_sort_and_media_filter(self):
        ep10, ep2 = self.media("season/ep10.mkv"), self.media("season/ep2.mkv")
        self.media("season/notes.txt")
        self.media("season/subtitles.srt")
        self.media("season/subfolder/ep1.mkv")
        playlist = self.root / "navigator.dpl"
        playlist.write_text("DAUMPLAYLIST\nworkfolder=season\nplayname=season/ep2.mkv\n", encoding="utf-8")
        selection = discover_playlist(ep2, playlist)
        self.assertEqual(selection.paths, [ep2, ep10])
        self.assertEqual(selection.source, "directory")
        self.assertIn("навигацию папки", selection.warning)
        self.assertIn("может отличаться", selection.warning)

    def test_automatic_stale_album_does_not_become_current_queue(self):
        current = self.media("new/ep1.mkv")
        old = self.media("old/film.mkv")
        playlist = self.root / "old.dpl"
        playlist.write_text(f"DAUMPLAYLIST\n1*file={old}\n", encoding="utf-8")
        with patch("potbridge.playlist._active_saved_playlist", return_value=playlist):
            selection = discover_playlist(current)
        self.assertEqual(selection.paths, [current])
        self.assertEqual(selection.source, "directory")
        self.assertIn("другим файлам", selection.warning)

    def test_history_is_never_a_queue_when_no_saved_album_exists(self):
        current = self.media("film.mkv")
        registry = MagicMock()
        registry.HKEY_CURRENT_USER = object()
        registry.OpenKey.side_effect = FileNotFoundError("No Settings key")
        with patch.dict(sys.modules, {"winreg": registry}):
            selection = discover_playlist(current)
        self.assertEqual(selection.paths, [current])
        self.assertEqual(selection.source, "directory")
        registry.OpenKey.assert_called_once_with(
            registry.HKEY_CURRENT_USER, r"Software\DAUM\PotPlayerMini64\Settings"
        )
        registry.QueryValueEx.assert_not_called()

    def test_registry_reads_only_active_saved_album_settings(self):
        registry = MagicMock()
        registry.HKEY_CURRENT_USER = object()
        registry.QueryValueEx.side_effect = [("my.dpl", 1), (str(self.root), 1)]
        with patch.dict(sys.modules, {"winreg": registry}):
            result = _active_saved_playlist()
        self.assertEqual(result, self.root / "my.dpl")
        self.assertEqual(
            [call.args[1] for call in registry.QueryValueEx.call_args_list],
            ["LastPlayListName", "PlayListFolder"],
        )

    def test_repeated_playlist_entries_are_retained(self):
        first, second = self.media("a.mkv"), self.media("b.mkv")
        playlist = self.root / "repeat.m3u8"
        playlist.write_text("a.mkv\nb.mkv\na.mkv\n", encoding="utf-8")
        selection = discover_playlist(first, playlist)
        self.assertEqual(selection.paths, [first, second, first])

    def test_missing_current_media_is_an_error(self):
        with self.assertRaises(FileNotFoundError):
            discover_playlist(self.root / "missing.mkv")


if __name__ == "__main__":
    unittest.main()
