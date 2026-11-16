import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import ese_qt


HTML = b'<!DOCTYPE html><html><script>location.replace("/")</script></html>'


def database_bytes(path, tables):
    with sqlite3.connect(path) as conn:
        for table in tables:
            conn.execute(f"CREATE TABLE {table} (id INTEGER)")
    conn.close()
    return path.read_bytes()


def response(data):
    result = Mock()
    result.text = data.decode("utf-8", errors="replace")
    result.iter_content.return_value = iter([data])
    return result


class DatabaseUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.previous_cwd = os.getcwd()
        os.chdir(self.temp.name)
        self.songs = database_bytes(Path("ese_songs.db"),
                                    ("songs", "categories", "song_files"))
        self.local = database_bytes(Path("ese_local.db"), ("local_songs",))
        Path("version.txt").write_text("old", encoding="utf-8")
        self.before = {name: Path(name).read_bytes() for name in
                       ("ese_songs.db", "ese_local.db", "version.txt")}

    def tearDown(self):
        os.chdir(self.previous_cwd)
        self.temp.cleanup()

    def run_update(self, responses, version="new"):
        worker = ese_qt.DBUpdateThread(version)
        results = []
        worker.done.connect(lambda ok, message: results.append((ok, message)))
        with patch.object(ese_qt.requests, "get", side_effect=responses):
            worker.run()
        return results[0]

    def assert_originals_preserved(self):
        for name, contents in self.before.items():
            self.assertEqual(Path(name).read_bytes(), contents, name)
        self.assertEqual(set(os.listdir()), set(self.before))

    def test_html_response_does_not_overwrite_databases(self):
        ok, message = self.run_update([response(HTML), response(HTML)])
        self.assertFalse(ok)
        self.assertIn("ese_songs.db", message)
        self.assert_originals_preserved()

    def test_invalid_second_database_preserves_both(self):
        ok, _ = self.run_update([response(self.songs), response(HTML)])
        self.assertFalse(ok)
        self.assert_originals_preserved()

    def test_interrupted_download_preserves_both(self):
        interrupted = response(b"")

        def chunks():
            yield self.local[:100]
            raise ese_qt.requests.ConnectionError("connection lost")

        interrupted.iter_content.side_effect = chunks
        ok, _ = self.run_update([response(self.songs), interrupted])
        self.assertFalse(ok)
        self.assert_originals_preserved()

    def test_wrong_sqlite_schema_is_rejected(self):
        ok, _ = self.run_update([response(self.local), response(self.local)])
        self.assertFalse(ok)
        self.assert_originals_preserved()

    def test_valid_pair_updates_version(self):
        Path("ese_songs.db").write_bytes(HTML)
        Path("ese_local.db").write_bytes(HTML)
        self.assertEqual(self.run_update([response(self.songs), response(self.local)]),
                         (True, "ok"))
        self.assertEqual(Path("ese_songs.db").read_bytes(), self.songs)
        self.assertEqual(Path("ese_local.db").read_bytes(), self.local)
        self.assertEqual(Path("version.txt").read_text(encoding="utf-8"), "new")
        self.assertEqual(set(os.listdir()), set(self.before))

    def test_html_version_does_not_trigger_update(self):
        worker = ese_qt.VersionThread()
        results = []
        worker.result.connect(lambda remote, local: results.append((remote, local)))
        with patch.object(ese_qt.requests, "get", return_value=response(HTML)):
            worker.run()
        self.assertEqual(results, [("", "old")])

    def test_plain_version_is_accepted(self):
        worker = ese_qt.VersionThread()
        results = []
        worker.result.connect(lambda remote, local: results.append((remote, local)))
        with patch.object(ese_qt.requests, "get", return_value=response(b"20261116\n")):
            worker.run()
        self.assertEqual(results, [("20261116", "old")])

    def test_invalid_version_preserves_databases(self):
        ok, _ = self.run_update([response(self.songs), response(self.local)],
                                version=HTML.decode())
        self.assertFalse(ok)
        self.assert_originals_preserved()


if __name__ == "__main__":
    unittest.main()
