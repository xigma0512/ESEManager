"""
Song Sorter (曲目街機排序整理器) for ESEManager
- 依街機官方（新筐體 / 虹色版）及曲目上架年代由新到舊排序
- 自動切分為 99 首分頁目錄（01 001~099, 02 100~198...）
- 自動規範曲目資料夾序號（01 曲名, 02 曲名...）
- 自動複製或生成 box.def 設定檔
- 提供 QThread 背景工作線程，支援即時進度與取消
"""

import os
import sys
import shutil
import time
import json
import re
import html
import unicodedata
import stat
import tempfile
from datetime import datetime
from PySide6.QtCore import QThread, Signal
from tja_parser import TJAParser

# 載入內建太鼓年代資料庫
def get_data_file():
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(base, "taiko_arcade_data.json")
    if os.path.exists(p):
        return p
    p_exe = os.path.join(os.path.dirname(sys.executable), "taiko_arcade_data.json")
    if os.path.exists(p_exe):
        return p_exe
    return p

_DATA_FILE = get_data_file()

def load_taiko_data():
    data = {"dates": {}, "ac16_orders": {}, "pinyin_map": {}}
    if os.path.exists(_DATA_FILE):
        try:
            with open(_DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[SongSorter] Failed to load taiko_arcade_data.json: {e}")
    # 支援外部覆寫/補丁檔 custom_arcade_data.json
    custom_p = os.path.join(os.path.dirname(_DATA_FILE), "custom_arcade_data.json")
    if os.path.exists(custom_p):
        try:
            with open(custom_p, "r", encoding="utf-8") as f:
                cdata = json.load(f)
                data.setdefault("dates", {}).update(cdata.get("dates", {}))
                data.setdefault("ac16_orders", {}).update(cdata.get("ac16_orders", {}))
                data.setdefault("pinyin_map", {}).update(cdata.get("pinyin_map", {}))
        except Exception as e:
            print(f"[SongSorter] Failed to load custom_arcade_data.json: {e}")
    return data

_TAIKO_DATA = load_taiko_data()
_PINYIN_MAP = _TAIKO_DATA.get("pinyin_map", {})
_DATES_MAP = _TAIKO_DATA.get("dates", {})
_AC16_ORDERS = _TAIKO_DATA.get("ac16_orders", {})

def clean_key(s):
    """將曲名正規化為比對用 key"""
    if not s:
        return ""
    s = html.unescape(str(s))
    s = unicodedata.normalize('NFKC', s)
    # 移除開頭序號，如 "01 ", "12. "
    m = re.match(r'^[\d\.]+\s+(.+)$', s)
    if m:
        s = m.group(1)
    s = re.sub(r'[\r\n\t]', ' ', s)
    # 移除曲名後贅字
    s = re.sub(r'\s+「[^」]*」より.*$', '', s)
    s = re.sub(r'\s*--.*$', '', s)
    s = re.sub(r'本家譜面|表譜面|裏譜面|【双打】|\(RT版\)', '', s)
    s = s.lower()
    s = re.sub(r'[\s\-_~～〜・:：!！?？/\(\)（）「」『』★☆♪♫♩♭♥♡◆◇※・,，\.。\+\*\'\"‐―ー–—−]+', '', s)
    return s.strip()

def normalize_date(d_str):
    """將各種格式日期轉為標準 YYYY-MM-DD"""
    if not d_str:
        return ""
    d_str = str(d_str).replace('/', '-')
    parts = d_str.split('-')
    try:
        if 1 <= len(parts) <= 3 and len(parts[0]) == 4:
            year, month, day = [int(p) for p in parts] + [1] * (3 - len(parts))
            return datetime(year, month, day).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        pass
    return ""

def extract_tja_titles(song_dir):
    """解析資料夾內的 .tja 取得 TITLE 與 TITLEJA"""
    titles = []
    parser = TJAParser()
    for filename in sorted(os.listdir(song_dir)):
        if not filename.lower().endswith('.tja'):
            continue
        with open(os.path.join(song_dir, filename), 'rb') as f:
            text = parser.decode_bytes(f.read())
        metadata = parser.parse_text(text or "")
        for field in ('title', 'titleja'):
            title = metadata.get(field, '').strip()
            if title and title not in titles:
                titles.append(title)
    return titles

def resolve_song_metadata(dirname, song_dir):
    """根據曲目資料夾與 tja 檔解析上架日期與街機順位"""
    tja_titles = extract_tja_titles(song_dir)
    d_clean = re.sub(r'^\d+\s*', '', dirname)
    raw_keys = [dirname, d_clean] + tja_titles
    
    keys = []
    for rk in raw_keys:
        k = clean_key(rk)
        if k and k not in keys:
            keys.append(k)
            if k in _PINYIN_MAP:
                pk = clean_key(_PINYIN_MAP[k])
                if pk and pk not in keys:
                    keys.append(pk)
    
    # 查詢上架日期
    resolved_date = None
    for k in keys:
        if k in _DATES_MAP:
            resolved_date = _DATES_MAP[k]
            break
            
    # 查詢 AC16 順位
    ac16_order = None
    for k in keys:
        if k in _AC16_ORDERS:
            ac16_order = _AC16_ORDERS[k]
            break

    norm_d = normalize_date(resolved_date)
    # 加上文字前綴比對，讓 clean_key 不會將真正的數字曲名當成排序序號。
    title_matches = any(clean_key('title ' + dirname) == clean_key('title ' + title)
                        for title in tja_titles)
    clean_name = dirname if title_matches else re.sub(r'^\d{2,}\s+', '', dirname)
    
    return {
        'clean_name': clean_name,
        'date': norm_d,
        'ac16_order': ac16_order if ac16_order is not None else 99999,
        'has_known_date': bool(norm_d)
    }

def default_backup_dir(source):
    source = os.path.abspath(source)
    name = os.path.basename(source)
    return os.path.join(os.path.dirname(source),
                        "song_backup" if name.lower() == "songs" else name + "_backup")


class _SortCancelled(Exception):
    pass


class SongSorterWorker(QThread):
    """Qt 背景執行緒，執行曲目整理與排序"""
    sig_progress = Signal(int, int, str)       # (current, total, current_song_name)
    sig_log = Signal(str)                     # log message
    sig_finished = Signal(bool, str, dict)    # (success, message, stats)
    
    def __init__(self, src_dir, dst_dir, options=None):
        super().__init__()
        self.src_dir = os.path.abspath(src_dir)
        self.dst_dir = os.path.abspath(dst_dir)
        self.options = options or {}
        self.is_cancelled = False
        
        # 選項設定
        self.reverse_order = self.options.get("reverse_order", True)  # True = 新曲置頂（街機順序）
        self.batch_size = self.options.get("batch_size", 99)          # 每資料夾曲數（預設 99）
        self.use_pagination = self.options.get("use_pagination", True)# 是否使用 01 001~099 分頁

    def cancel(self):
        self.is_cancelled = True

    def _check_cancel(self):
        if self.is_cancelled:
            raise _SortCancelled("作業已取消，原曲庫未變更。")

    @staticmethod
    def _raise_walk_error(error):
        raise error

    def _validate_paths(self):
        if not os.path.isdir(self.src_dir):
            raise ValueError(f"來源目錄不存在: {self.src_dir}")
        parent = os.path.dirname(self.src_dir)
        if self.src_dir == parent:
            raise ValueError("請選擇曲庫目錄，不能使用磁碟根目錄。")
        backup_parent = os.path.normcase(os.path.realpath(os.path.dirname(self.dst_dir)))
        if backup_parent != os.path.normcase(os.path.realpath(parent)) or (
                os.path.normcase(self.src_dir) == os.path.normcase(self.dst_dir)):
            raise ValueError("備份目錄必須與來源目錄放在同一層，且名稱不同。")
        # 禁止複製連結到曲庫外的資料，再將該連結誤當成已備份的檔案。
        snapshot = {}
        for root, dirs, files in os.walk(self.src_dir, onerror=self._raise_walk_error):
            self._check_cancel()
            for path in [root] + [os.path.join(root, n) for n in dirs + files]:
                info = os.lstat(path)
                if (stat.S_ISLNK(info.st_mode) or
                        getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                    raise ValueError(f"曲庫含有符號連結或接合點，請先處理: {path}")
                snapshot[os.path.relpath(path, self.src_dir)] = (info.st_size, info.st_mtime_ns)
        return snapshot

    def _collect_songs(self):
        entries = sorted(d for d in os.listdir(self.src_dir)
                         if os.path.isdir(os.path.join(self.src_dir, d))
                         and not d.startswith('.') and d not in ('__pycache__', 'System', 'GameData'))
        has_direct_songs = any(
            any(f.lower().endswith('.tja') for f in os.listdir(os.path.join(self.src_dir, d)))
            for d in entries)
        has_pages = any(re.fullmatch(r'\d+\s+\d+~\d*', d) for d in entries)
        if any(f.lower().endswith('.tja') for f in os.listdir(self.src_dir)):
            raise ValueError("請選擇曲庫或分類目錄，而非單首歌曲資料夾。")
        categories = [("", self.src_dir)] if has_direct_songs or has_pages else [
            (d, os.path.join(self.src_dir, d)) for d in entries]
        result = {}
        for category, path in categories:
            songs = []
            for root, dirs, files in os.walk(path, onerror=self._raise_walk_error):
                self._check_cancel()
                if any(f.lower().endswith('.tja') for f in files):
                    meta = resolve_song_metadata(os.path.basename(root), root)
                    meta['relative_path'] = os.path.relpath(root, self.src_dir)
                    songs.append(meta)
                    dirs[:] = []  # 歌曲內的子目錄屬於同一首曲目的資產。
            if songs:
                result[category] = songs
                self.sig_log.emit(f"  • [{category or '根目錄'}] 找到 {len(songs)} 首曲目")
        return result

    def _arrange_songs(self, library, holding, categories, stats):
        # 先移出所有歌曲，避免舊序號與新序號互相覆蓋。
        os.mkdir(holding)
        count = 0
        for songs in categories.values():
            for song in songs:
                self._check_cancel()
                temporary_path = os.path.join(holding, str(count))
                os.rename(os.path.join(library, song['relative_path']), temporary_path)
                song['staged_path'] = temporary_path
                count += 1
        for root, dirs, files in os.walk(library, topdown=False):
            if root != library and not os.listdir(root):
                os.rmdir(root)
        for category, songs in categories.items():
            # 穩定排序：同日順位與曲名升序；未知日期在兩種模式都排最後。
            songs.sort(key=lambda s: (s['ac16_order'], s['clean_name'].casefold(), s['relative_path']))
            songs.sort(key=lambda s: s['date'], reverse=self.reverse_order)
            songs.sort(key=lambda s: not s['has_known_date'])
            size = self.batch_size if self.use_pagination and self.batch_size > 0 else len(songs)
            for index, song in enumerate(songs):
                self._check_cancel()
                target = os.path.join(library, category)
                if self.use_pagination and self.batch_size > 0:
                    page = index // size
                    end = str((page + 1) * size).zfill(3) if (page + 1) * size < len(songs) else ""
                    target = os.path.join(target, f"{page + 1:02d} {page * size + 1:03d}~{end}")
                os.makedirs(target, exist_ok=True)
                target = os.path.join(target, f"{index % size + 1:02d} {song['clean_name']}")
                if os.path.lexists(target):
                    raise FileExistsError(f"排序目標已存在，已停止以避免覆寫: {target}")
                os.rename(song['staged_path'], target)
                if not song['has_known_date']:
                    stats['unknown_dates'].append(song['clean_name'])
                    self.sig_log.emit(f"⚠ 日期未知，排至分類末尾: {song['clean_name']}")
                stats['processed'] += 1
                self.sig_progress.emit(stats['processed'], stats['total'], song['clean_name'])

    def _install_library(self, library):
        backup = self.dst_dir
        suffix = 2
        while os.path.lexists(backup):
            backup = f"{self.dst_dir}_{suffix}"
            suffix += 1
        self._check_cancel()
        os.rename(self.src_dir, backup)
        try:
            os.rename(library, self.src_dir)
        except Exception as install_error:
            try:
                os.rename(backup, self.src_dir)
            except Exception as restore_error:
                raise OSError(f"替換失敗: {install_error}；自動還原失敗: {restore_error}。"
                              f"完整原曲庫保留在: {backup}") from install_error
            raise OSError(f"替換失敗，已還原原曲庫: {install_error}") from install_error
        return backup

    def run(self):
        start_time = time.time()
        work_dir = None
        stats = {'processed': 0, 'total': 0, 'unknown_dates': []}
        ok = False
        self.sig_log.emit(f"📂 整理後替換原曲庫: {self.src_dir}")
        try:
            self._check_cancel()
            original_snapshot = self._validate_paths()
            categories = self._collect_songs()
            stats['total'] = sum(len(songs) for songs in categories.values())
            if not stats['total']:
                raise ValueError("未在來源目錄中找到任何含有 .tja 的曲目資料夾。")
            work_dir = tempfile.mkdtemp(prefix=f".{os.path.basename(self.src_dir)}-sort-",
                                        dir=os.path.dirname(self.src_dir))
            library = os.path.join(work_dir, 'library')
            copied = 0

            def copy_file(src, dst):
                nonlocal copied
                self._check_cancel()
                shutil.copy2(src, dst)
                copied += 1
                if copied % 100 == 0:
                    self.sig_log.emit(f"📋 已複製 {copied} 個檔案至暫存目錄")
                self._check_cancel()

            self.sig_log.emit("📋 複製完整曲庫至暫存目錄，保留 box.def、段位及其他資產…")
            shutil.copytree(self.src_dir, library, copy_function=copy_file)
            self._arrange_songs(library, os.path.join(work_dir, 'holding'), categories, stats)
            if self._validate_paths() != original_snapshot:
                raise OSError("來源曲庫在整理期間有變更，已停止替換。請先停止下載或編輯後重試。")
            self.sig_log.emit("📦 排序完成，正在備份並替換原曲庫…")
            stats['backup_path'] = self._install_library(library)
            stats['output_path'] = self.src_dir
            stats['elapsed'] = time.time() - start_time
            message = (f"已整理 {stats['processed']} 首並替換原曲庫。\n"
                       f"完整備份: {stats['backup_path']}\n"
                       f"日期未知: {len(stats['unknown_dates'])} 首（已排至各分類末尾）。")
            ok = True
        except Exception as error:
            message = str(error)
        finally:
            if work_dir:
                try:
                    # 僅清除此輪建立、且位於來源同層的暫存目錄。
                    if os.path.dirname(os.path.realpath(work_dir)) != os.path.realpath(os.path.dirname(self.src_dir)):
                        raise ValueError("暫存目錄位置異常，已保留。")
                    shutil.rmtree(work_dir)
                except (OSError, ValueError) as error:
                    self.sig_log.emit(f"⚠ 暫存目錄清理失敗: {work_dir}: {error}")
        self.sig_log.emit(message)
        self.sig_finished.emit(ok, message, stats)
