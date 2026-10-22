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
from datetime import datetime
from PySide6.QtCore import QThread, Signal

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
    if os.path.exists(_DATA_FILE):
        try:
            with open(_DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[SongSorter] Failed to load taiko_arcade_data.json: {e}")
    return {"dates": {}, "ac16_orders": {}, "pinyin_map": {}}

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
        return "2010-01-01"
    d_str = str(d_str).replace('/', '-')
    parts = d_str.split('-')
    try:
        if len(parts) == 3:
            return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
        elif len(parts) == 2:
            return f"{int(parts[0]):04d}-{int(parts[1]):02d}-01"
        elif len(parts) == 1 and len(parts[0]) == 4:
            return f"{int(parts[0]):04d}-01-01"
    except Exception:
        pass
    return "2010-01-01"

def extract_tja_titles(song_dir):
    """解析資料夾內的 .tja 取得 TITLE 與 TITLEJA"""
    titles = []
    tjas = [f for f in os.listdir(song_dir) if f.lower().endswith('.tja')]
    if not tjas:
        return titles
    tja_path = os.path.join(song_dir, tjas[0])
    for enc in ['cp932', 'shift_jis', 'utf-8', 'utf-8-sig']:
        try:
            with open(tja_path, 'r', encoding=enc) as f:
                for line in f:
                    if line.startswith('#START'):
                        break
                    if ':' in line:
                        fld, val = line.split(':', 1)
                        if fld.strip().upper() in ['TITLE', 'TITLEJA']:
                            val_clean = val.strip()
                            if val_clean and val_clean not in titles:
                                titles.append(val_clean)
            if titles:
                break
        except Exception:
            continue
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

    norm_d = normalize_date(resolved_date) if resolved_date else "2010-01-01"
    clean_name = re.sub(r'^\d{2}\s+', '', dirname)
    
    return {
        'clean_name': clean_name,
        'date': norm_d,
        'ac16_order': ac16_order if ac16_order is not None else 99999,
        'has_known_date': bool(resolved_date)
    }

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
        self.move_mode = self.options.get("move_mode", False)         # True = 移動, False = 複製
        self.sync_boxdef = self.options.get("sync_boxdef", True)      # 同步 box.def

    def cancel(self):
        self.is_cancelled = True

    def run(self):
        start_time = time.time()
        self.sig_log.emit(f"🚀 開始曲目整理作業...")
        self.sig_log.emit(f"📂 來源路徑: {self.src_dir}")
        self.sig_log.emit(f"📁 目的路徑: {self.dst_dir}")
        self.sig_log.emit(f"⚙️ 模式: {'街機新曲置頂' if self.reverse_order else '老歌正序'} | "
                          f"分頁: {'每 ' + str(self.batch_size) + ' 首' if self.use_pagination else '不分頁'} | "
                          f"操作: {'移動' if self.move_mode else '複製'}")

        if not os.path.exists(self.src_dir):
            self.sig_finished.emit(False, f"來源路徑不存在: {self.src_dir}", {})
            return

        os.makedirs(self.dst_dir, exist_ok=True)

        # 1. 偵測曲目分類結構
        # 判斷是「分類目錄」（包含多個分類如 01-J-POP、02 Anime）或「單一曲庫目錄」
        entries = [d for d in os.listdir(self.src_dir) if os.path.isdir(os.path.join(self.src_dir, d))]
        
        # 檢測各子目錄中是否包含 .tja（若直接就是歌曲資料夾）
        has_direct_songs = False
        for e in entries[:10]:
            ep = os.path.join(self.src_dir, e)
            if any(f.lower().endswith('.tja') for f in os.listdir(ep)):
                has_direct_songs = True
                break

        categories = []
        if has_direct_songs:
            # 來源目錄本身就是單一分類或扁平曲目
            categories = [("", self.src_dir)]
        else:
            # 來源目錄包含多個分類資料夾（或包含舊分頁目錄）
            for e in sorted(entries):
                ep = os.path.join(self.src_dir, e)
                # 排除系統資料夾
                if e.startswith('.') or e in ['__pycache__', 'System', 'GameData']:
                    continue
                categories.append((e, ep))

        self.sig_log.emit(f"🔍 偵測到 {len(categories)} 個分類群組，開始掃描曲目...")

        # 2. 收集各分類的全部歌曲
        all_categorized_songs = {}
        total_songs_found = 0

        for cat_name, cat_path in categories:
            cat_songs = []
            for root, dirs, files in os.walk(cat_path):
                if any(f.lower().endswith('.tja') for f in files):
                    dirname = os.path.basename(root)
                    meta = resolve_song_metadata(dirname, root)
                    cat_songs.append({
                        'dirname': dirname,
                        'clean_name': meta['clean_name'],
                        'src_path': root,
                        'date': meta['date'],
                        'ac16_order': meta['ac16_order'],
                        'has_known_date': meta['has_known_date']
                    })
            if cat_songs:
                all_categorized_songs[cat_name] = cat_songs
                total_songs_found += len(cat_songs)
                self.sig_log.emit(f"  • [{cat_name or '根目錄'}] 找到 {len(cat_songs)} 首曲目")

        if total_songs_found == 0:
            self.sig_finished.emit(False, "未在來源目錄中找到任何含有 .tja 的曲目資料夾！", {})
            return

        self.sig_log.emit(f"📊 總計發現 {total_songs_found} 首曲目，準備開始排序與寫入...")

        processed_count = 0

        # 3. 對每個分類執行排序與輸出
        for cat_name, songs in all_categorized_songs.items():
            if self.is_cancelled:
                self.sig_log.emit("⚠️ 使用者已取消作業。")
                self.sig_finished.emit(False, "作業已被使用者中止", {"processed": processed_count, "total": total_songs_found})
                return

            # 目標分類目錄
            cat_target_dir = os.path.join(self.dst_dir, cat_name) if cat_name else self.dst_dir
            os.makedirs(cat_target_dir, exist_ok=True)

            # 同步或複製 box.def
            if self.sync_boxdef and cat_name:
                box_src = os.path.join(self.src_dir, cat_name, 'box.def')
                box_dst = os.path.join(cat_target_dir, 'box.def')
                if os.path.exists(box_src) and not os.path.exists(box_dst):
                    try:
                        shutil.copy2(box_src, box_dst)
                    except Exception:
                        pass

            # 排序曲目：
            # 若 reverse_order 為 True（街機順序）：date 降序（最新在上），ac16_order 升序，clean_name
            # 若為 False：date 升序（最舊在上）
            if self.reverse_order:
                songs.sort(key=lambda x: (x['date'], -x['ac16_order'], x['clean_name']), reverse=True)
            else:
                songs.sort(key=lambda x: (x['date'], x['ac16_order'], x['clean_name']))

            # 分頁處理
            total_in_cat = len(songs)
            if self.use_pagination and self.batch_size > 0:
                num_batches = (total_in_cat + self.batch_size - 1) // self.batch_size
            else:
                num_batches = 1

            for b in range(num_batches):
                if self.is_cancelled:
                    break

                if self.use_pagination and self.batch_size > 0:
                    start_idx = b * self.batch_size
                    end_idx = min((b + 1) * self.batch_size, total_in_cat)
                    batch_songs = songs[start_idx:end_idx]
                    start_song_num = start_idx + 1

                    if b == num_batches - 1:
                        batch_folder_name = f"{b+1:02d} {start_song_num:03d}~"
                    else:
                        batch_folder_name = f"{b+1:02d} {start_song_num:03d}~{(b+1)*self.batch_size:03d}"

                    batch_target_dir = os.path.join(cat_target_dir, batch_folder_name)
                else:
                    batch_songs = songs
                    batch_target_dir = cat_target_dir

                os.makedirs(batch_target_dir, exist_ok=True)

                for idx_in_batch, s in enumerate(batch_songs, start=1):
                    if self.is_cancelled:
                        break

                    new_folder_name = f"{idx_in_batch:02d} {s['clean_name']}"
                    dest_song_dir = os.path.join(batch_target_dir, new_folder_name)

                    try:
                        if self.move_mode and s['src_path'] != dest_song_dir:
                            if os.path.exists(dest_song_dir):
                                shutil.rmtree(dest_song_dir)
                            shutil.move(s['src_path'], dest_song_dir)
                        elif s['src_path'] != dest_song_dir:
                            if os.path.exists(dest_song_dir):
                                shutil.rmtree(dest_song_dir)
                            shutil.copytree(s['src_path'], dest_song_dir)
                    except Exception as ex:
                        self.sig_log.emit(f"  ❌ 處理失敗 [{s['clean_name']}]: {ex}")

                    processed_count += 1
                    self.sig_progress.emit(processed_count, total_songs_found, s['clean_name'])

        elapsed = time.time() - start_time
        msg = f"曲目整理完成！共處理 {processed_count} 首曲目，耗時 {elapsed:.2f} 秒。"
        self.sig_log.emit(f"🎉 {msg}")
        self.sig_finished.emit(True, msg, {
            "processed": processed_count,
            "total": total_songs_found,
            "elapsed": elapsed
        })
