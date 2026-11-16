import copy
import hashlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import ese_qt
from download_songs import SongDownloader

RAW_URL = "https://ese.tjadataba.se/ESE/ESE/raw/branch/master/03%20Vocaloid/Sample/Sample.ogg"
MEDIA_URL = RAW_URL.replace("/raw/", "/media/")
AUDIO = b"OggS" + bytes(range(256))
POINTER = ("version https://git-lfs.github.com/spec/v1\n"
           f"oid sha256:{hashlib.sha256(AUDIO).hexdigest()}\n"
           f"size {len(AUDIO)}\n").encode()


def response(body):
    result = Mock()
    result.headers = {"content-length": str(len(body))}
    result.iter_content.return_value = iter([body])
    return result


class LFSAudioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.root = Path(self.temp.name)
        self.folder = self.root / "custom category" / "Sample"
        self.folder.mkdir(parents=True)
        self.chart = self.folder / "Sample.tja"
        self.chart.write_bytes(b"TITLE:Sample\nWAVE:Sample.ogg\n")
        self.audio = self.folder / "Sample.ogg"
        self.audio.write_bytes(POINTER)
        self.file = {"filename": "Sample.ogg", "file_path": "03 Vocaloid/Sample/Sample.ogg",
                     "file_type": ".ogg", "size": 0, "download_url": RAW_URL}
        self.song = {"song_name": "Sample", "category": "03 Vocaloid",
                     "files": [dict(self.file, filename="Sample.tja", file_type=".tja",
                                    file_path="03 Vocaloid/Sample/Sample.tja"), self.file]}

    def tearDown(self):
        self.temp.cleanup()

    def test_both_downloaders_replace_pointer_using_media_url(self):
        for gui in (False, True):
            with self.subTest(gui=gui):
                self.audio.write_bytes(POINTER)
                fake_response = response(AUDIO)
                if gui:
                    worker = ese_qt.DownloadThread(
                        [(self.song, dict(self.file, local_path=str(self.audio)))], str(self.root), 1)
                    session = Mock(headers={})
                    session.get.return_value = fake_response
                    with patch.object(ese_qt.requests, "Session", return_value=session), \
                            patch.object(worker, "_fetch_boxdefs"):
                        worker.run()
                    self.assertEqual((worker.dl, worker.skip, worker.fail), (1, 0, 0))
                    self.assertEqual(session.get.call_args.args[0], MEDIA_URL)
                else:
                    downloader = SongDownloader()
                    with patch("download_songs.requests.get", return_value=fake_response) as get:
                        self.assertTrue(downloader.download_file(RAW_URL, str(self.audio)))
                    self.assertIsNotNone(get.call_args)
                    self.assertEqual(get.call_args.args[0], MEDIA_URL)
                    self.assertEqual(downloader.stats["downloaded"], 1)
                self.assertEqual(self.audio.read_bytes(), AUDIO)

    def test_both_downloaders_reject_pointer_response(self):
        for gui in (False, True):
            with self.subTest(gui=gui):
                self.audio.unlink(missing_ok=True)
                fake_response = response(POINTER)
                if gui:
                    worker = ese_qt.DownloadThread(
                        [(self.song, dict(self.file, local_path=str(self.audio)))], str(self.root), 1)
                    session = Mock(headers={})
                    session.get.return_value = fake_response
                    with patch.object(ese_qt.requests, "Session", return_value=session), \
                            patch.object(worker, "_fetch_boxdefs"):
                        worker.run()
                    self.assertEqual((worker.dl, worker.fail), (0, 1))
                else:
                    with patch("download_songs.requests.get", return_value=fake_response):
                        self.assertFalse(SongDownloader().download_file(RAW_URL, str(self.audio)))
                self.assertFalse(self.audio.exists())

    def test_existing_real_audio_is_skipped(self):
        self.audio.write_bytes(AUDIO)
        worker = ese_qt.DownloadThread(
            [(self.song, dict(self.file, local_path=str(self.audio)))], str(self.root), 1)
        session = Mock(headers={})
        with patch.object(ese_qt.requests, "Session", return_value=session), \
                patch.object(worker, "_fetch_boxdefs"):
            worker.run()
        session.get.assert_not_called()
        self.assertEqual((worker.dl, worker.skip, worker.fail), (0, 1, 0))

    def run_scan(self, updates=False):
        if updates:
            database = self.root / "songs.db"
            conn = sqlite3.connect(database)
            try:
                conn.execute("CREATE TABLE song_files (file_path TEXT, blob_sha TEXT)")
                blob_sha = hashlib.sha1(b"blob %d\0" % len(POINTER) + POINTER).hexdigest()
                conn.execute("INSERT INTO song_files VALUES (?, ?)",
                             (self.file["file_path"], blob_sha))
                conn.commit()
            finally:
                conn.close()
            worker = ese_qt.CheckUpdatesThread(str(self.root))
        else:
            database = self.root / "unused.db"
            worker = ese_qt.CheckMissingThread(str(self.root))
        results, errors = [], []
        worker.done.connect(results.append)
        worker.fail.connect(errors.append)
        with patch.object(ese_qt, "SongDownloader") as downloader, \
                patch.object(ese_qt, "DB_PATH", str(database)):
            downloader.return_value.search_songs.return_value = [copy.deepcopy(self.song)]
            worker.run()
        self.assertEqual(errors, [])
        return results[0]

    def test_missing_scan_queues_pointer_at_existing_custom_path(self):
        songs = self.run_scan()
        self.assertEqual(len(songs), 1)
        self.assertEqual(len(songs[0]["files"]), 1)
        self.assertEqual(songs[0]["files"][0]["local_path"], str(self.audio))

    def test_update_scan_queues_pointer_even_when_git_blob_matches(self):
        songs = self.run_scan(updates=True)
        self.assertEqual(len(songs), 1)
        self.assertEqual(songs[0]["files"][0]["local_path"], str(self.audio))

    def test_update_scan_accepts_downloaded_lfs_content(self):
        self.audio.write_bytes(AUDIO)
        self.assertEqual(self.run_scan(updates=True), [])

    def test_update_scan_detects_changed_audio(self):
        self.audio.write_bytes(AUDIO + b"changed")
        self.assertEqual(len(self.run_scan(updates=True)), 1)

    def test_url_conversion_keeps_other_hosts_unchanged(self):
        self.assertEqual(ese_qt._live_url("https://example.com/ESE/ESE/raw/branch/master/a.ogg"),
                         "https://example.com/ESE/ESE/raw/branch/master/a.ogg")
        self.assertEqual(ese_qt._live_url(RAW_URL.replace("ese.tjadataba.se", "git.vanillaaaa.org")),
                         MEDIA_URL)


if __name__ == "__main__":
    unittest.main()
