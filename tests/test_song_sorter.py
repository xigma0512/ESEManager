import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import song_sorter as sorter


def contents(root):
    return {str(p.relative_to(root)): p.read_bytes()
            for p in root.rglob("*") if p.is_file()}


class SongSorterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.root = Path(self.temp.name)
        self.source = self.root / "Songs"
        self.source.mkdir()
        self.backup = self.root / "song_backup"

    def tearDown(self):
        self.temp.cleanup()

    def song(self, name, title, category="01 Pop", encoding="utf-8"):
        folder = self.source / category / name
        folder.mkdir(parents=True)
        (folder / "chart.tja").write_bytes(
            f"TITLE:Sample\nTITLEJA:{title}\nWAVE:audio.ogg\n#START\n0,\n#END\n".encode(encoding))
        (folder / "audio.ogg").write_bytes(b"OggS-test-audio")
        return folder

    def run_worker(self, **options):
        worker = sorter.SongSorterWorker(str(self.source), str(self.backup), options)
        results = []
        worker.sig_finished.connect(lambda *args: results.append(args))
        worker.run()
        self.assertEqual(len(results), 1)
        return results[0]

    def test_utf8_title_matches_existing_arcade_date(self):
        folder = self.song("06 LOVE Machine", "LOVEマシーン")
        meta = sorter.resolve_song_metadata(folder.name, str(folder))
        self.assertTrue(meta["has_known_date"])
        self.assertEqual(meta["date"], "2001-02-21")

    def test_shift_jis_and_utf8_bom_titles(self):
        for encoding in ("shift_jis", "utf-8-sig"):
            with self.subTest(encoding=encoding):
                folder = self.song(encoding, "ワタリドリ", encoding=encoding)
                self.assertEqual(sorter.resolve_song_metadata(folder.name, str(folder))["date"],
                                 "2016-12-20")

    def test_checks_more_than_first_chart(self):
        folder = self.song("unmatched", "Unknown song")
        (folder / "second.tja").write_text("TITLEJA:ワタリドリ\n#START\n", encoding="utf-8")
        self.assertTrue(sorter.resolve_song_metadata(folder.name, str(folder))["has_known_date"])

    def test_unknown_date_does_not_use_file_time(self):
        folder = self.song("Unknown", "Unknown song")
        with patch.object(sorter.os.path, "getmtime", side_effect=AssertionError("mtime used")):
            meta = sorter.resolve_song_metadata(folder.name, str(folder))
        self.assertFalse(meta["has_known_date"])
        self.assertFalse(meta["date"])

    def test_invalid_dates_are_unknown(self):
        for value in ("bad", "2026-99-99", "2025-02-29"):
            self.assertFalse(sorter.normalize_date(value), value)

    def test_numeric_song_title_is_not_mistaken_for_sort_prefix(self):
        folder = self.song("142 Toki no Wataridori", "142 -Toki no Wataridori-")
        self.assertEqual(sorter.resolve_song_metadata(folder.name, str(folder))["clean_name"],
                         folder.name)

    def prepare_library(self):
        self.song("Old", "夏祭り")
        self.song("New", "ワタリドリ")
        self.song("Unknown", "Unknown song")
        (self.source / "box.def").write_bytes(b"ROOT BOX")
        (self.source / "01 Pop" / "box.def").write_bytes(b"POP BOX")
        extras = self.source / "Courses" / "sample"
        extras.mkdir(parents=True)
        (extras / "Dan.json").write_bytes(b"{}")
        return contents(self.source)

    def test_replaces_songs_only_after_complete_copy_and_preserves_backup(self):
        before = self.prepare_library()
        ok, message, stats = self.run_worker()
        self.assertTrue(ok, message)
        self.assertEqual(contents(self.backup), before)
        page = self.source / "01 Pop" / "01 001~"
        self.assertEqual(sorted(p.name for p in page.iterdir()), ["01 New", "02 Old", "03 Unknown"])
        self.assertEqual((self.source / "box.def").read_bytes(), b"ROOT BOX")
        self.assertEqual((self.source / "01 Pop" / "box.def").read_bytes(), b"POP BOX")
        self.assertEqual((self.source / "Courses" / "sample" / "Dan.json").read_bytes(), b"{}")
        self.assertEqual(Path(stats["backup_path"]), self.backup)
        self.assertEqual(stats["unknown_dates"], ["Unknown"])
        self.assertEqual({p.name for p in self.root.iterdir()}, {"Songs", "song_backup"})

    def test_unknown_dates_stay_last_in_oldest_first_mode(self):
        self.prepare_library()
        ok, message, _ = self.run_worker(reverse_order=False)
        self.assertTrue(ok, message)
        page = self.source / "01 Pop" / "01 001~"
        self.assertEqual(sorted(p.name for p in page.iterdir()), ["01 Old", "02 New", "03 Unknown"])

    def test_repeated_sort_preserves_existing_backup(self):
        before = self.prepare_library()
        self.assertTrue(self.run_worker()[0])
        sorted_once = contents(self.source)
        ok, message, stats = self.run_worker()
        self.assertTrue(ok, message)
        self.assertEqual(contents(self.backup), before)
        self.assertEqual(contents(Path(stats["backup_path"])), sorted_once)
        self.assertNotEqual(Path(stats["backup_path"]), self.backup)
        self.assertEqual(contents(self.source), sorted_once)

    def test_copy_failure_leaves_original_untouched(self):
        before = self.prepare_library()
        with patch.object(sorter.shutil, "copy2", side_effect=OSError("disk full")):
            ok, message, _ = self.run_worker()
        self.assertFalse(ok)
        self.assertIn("disk full", message)
        self.assertEqual(contents(self.source), before)
        self.assertEqual({p.name for p in self.root.iterdir()}, {"Songs"})

    def test_cancel_during_copy_leaves_original_untouched(self):
        before = self.prepare_library()
        worker = sorter.SongSorterWorker(str(self.source), str(self.backup))
        results = []
        worker.sig_finished.connect(lambda *args: results.append(args))
        original_copy = sorter.shutil.copy2

        def copy_and_cancel(*args, **kwargs):
            result = original_copy(*args, **kwargs)
            worker.cancel()
            return result

        with patch.object(sorter.shutil, "copy2", side_effect=copy_and_cancel):
            worker.run()
        self.assertFalse(results[0][0])
        self.assertEqual(contents(self.source), before)
        self.assertEqual({p.name for p in self.root.iterdir()}, {"Songs"})

    def test_install_failure_restores_original(self):
        before = self.prepare_library()
        original_rename = os.rename
        backed_up = False

        def fail_install(src, dst, *args, **kwargs):
            nonlocal backed_up
            if Path(src) == self.source:
                backed_up = True
            elif Path(dst) == self.source and backed_up and Path(src) != self.backup:
                raise PermissionError("install blocked")
            return original_rename(src, dst, *args, **kwargs)

        with patch.object(sorter.os, "rename", side_effect=fail_install):
            ok, message, _ = self.run_worker()
        self.assertFalse(ok)
        self.assertIn("install blocked", message)
        self.assertEqual(contents(self.source), before)
        self.assertEqual({p.name for p in self.root.iterdir()}, {"Songs"})

    def test_failed_rollback_keeps_full_backup_and_reports_its_path(self):
        before = self.prepare_library()
        original_rename = os.rename

        def fail_after_backup(src, dst, *args, **kwargs):
            if Path(dst) == self.source:
                raise PermissionError("library is locked")
            return original_rename(src, dst, *args, **kwargs)

        with patch.object(sorter.os, "rename", side_effect=fail_after_backup):
            ok, message, _ = self.run_worker()
        self.assertFalse(ok)
        self.assertIn(str(self.backup), message)
        self.assertEqual(contents(self.backup), before)

    def test_source_changed_during_copy_is_not_replaced(self):
        before = self.prepare_library()
        original_copy = sorter.shutil.copy2

        def change_source(*args, **kwargs):
            result = original_copy(*args, **kwargs)
            (self.source / "new-download.ogg").write_bytes(b"new audio")
            return result

        with patch.object(sorter.shutil, "copy2", side_effect=change_source):
            ok, _, _ = self.run_worker()
        self.assertFalse(ok)
        self.assertFalse(self.backup.exists())
        self.assertEqual(contents(self.source), dict(before, **{"new-download.ogg": b"new audio"}))

    def test_backup_inside_source_is_rejected_without_changes(self):
        before = self.prepare_library()
        self.backup = self.source / "backup"
        ok, _, _ = self.run_worker()
        self.assertFalse(ok)
        self.assertEqual(contents(self.source), before)
        self.assertFalse(self.backup.exists())

    def test_paginated_single_category_can_be_sorted_again(self):
        self.song("01 Old", "夏祭り", category="01 001~")
        self.song("02 New", "ワタリドリ", category="01 001~")
        ok, message, _ = self.run_worker()
        self.assertTrue(ok, message)
        self.assertTrue((self.source / "01 001~" / "01 New" / "chart.tja").exists())
        self.assertFalse((self.source / "01 001~" / "01 001~").exists())


class SongSorterDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        import ese_qt
        cls.gui = ese_qt
        cls.app = QApplication.instance() or QApplication([])

    def test_backup_default_and_open_button_use_correct_paths(self):
        dialog = self.gui.SongSorterDialog(None, lambda key: key, str(Path.cwd() / "Songs"))
        self.assertEqual(Path(dialog.dst_edit.text()), Path.cwd() / "song_backup")
        output = str(Path.cwd())
        with patch.object(self.gui.QMessageBox, "information"):
            dialog._on_finished(True, "ok", {"output_path": output})
        with patch.object(self.gui.os, "startfile") as startfile:
            dialog.open_dst_folder()
        startfile.assert_called_once_with(output)
        dialog.close()

    def test_cannot_close_while_sorting(self):
        dialog = self.gui.SongSorterDialog(None, lambda key: key, str(Path.cwd() / "Songs"))
        dialog._thread = Mock()
        dialog._thread.isRunning.return_value = True
        with patch.object(self.gui.QDialog, "reject") as reject:
            dialog.reject()
        dialog._thread.cancel.assert_called_once_with()
        reject.assert_not_called()
        dialog._thread = None
        dialog.close()

    def test_active_download_blocks_opening_sorter(self):
        window = Mock()
        window.dir_edit.text.return_value = str(Path.cwd())
        window.dl_thread.isRunning.return_value = True
        with patch.object(self.gui, "SongSorterDialog") as dialog, \
                patch.object(self.gui.QMessageBox, "warning") as warning:
            self.gui.MainWindow.open_song_sorter(window)
        dialog.assert_not_called()
        warning.assert_called_once()


if __name__ == "__main__":
    unittest.main()
