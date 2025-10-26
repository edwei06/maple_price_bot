# msu_price_bot_gui.py
# -*- coding: utf-8 -*-

import sys
import threading
import queue
import io
import time
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple, Dict

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ---- 估價所需：從 pricing_engine 匯入 ----
try:
    from pricing_engine import expected_star_cost, expected_potential_cost_dual
except Exception as _e:
    expected_star_cost = None
    expected_potential_cost_dual = None

# ---- 既有 scraper 的函式 ----
from msu_dynamic_pricing_scraper import run_batch, CUBE_PRESETS

APP_TITLE = "MSU Dynamic Pricing Bot (GUI)"
DEFAULT_DB = "msu_dynamic_pricing.sqlite"
DEFAULT_INDEX = "items_index.json"

TIERS = ["Rare", "Epic", "Unique", "Legendary"]
POT_TARGETS = ["Epic", "Unique", "Legendary"]
POT_TARGETS_WITH_SKIP = ["Skip", "Epic", "Unique", "Legendary"]

# ------------------ 共用：stdout 導向 GUI ------------------

class GuiLogger(io.TextIOBase):
    def __init__(self, text_widget: tk.Text, queue_obj: queue.Queue):
        self.text = text_widget
        self.queue = queue_obj
    def write(self, s):
        if s:
            self.queue.put(s)
    def flush(self): pass

# ------------------ 共用：DB 修復/快照查詢 ------------------

def ensure_price_stats_table(db_path: str) -> None:
    """
    確保 price_stats 表存在（空 DB 或舊 DB 都能安靜建立）。
    """
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS price_stats (
            item_id TEXT,
            item_name TEXT,
            upgrade_type INTEGER,
            upgrade_subtype TEXT,
            from_star INTEGER DEFAULT -1,
            to_star INTEGER DEFAULT -1,
            timeframe TEXT,
            last_ts_utc TEXT,
            last_close REAL,
            all_time_high REAL,
            all_time_low REAL,
            samples INTEGER DEFAULT 0,
            PRIMARY KEY (item_id, upgrade_type, upgrade_subtype, from_star, to_star, timeframe)
        )
        """)
        conn.commit()
    finally:
        conn.close()

def fetch_stats_for_items(db_path: str, items: List[str], names_mode: bool, timeframe: Optional[str]=None) -> List[Tuple]:
    """
    回傳：
      (item_id,item_name,upgrade_type,upgrade_subtype,from_star,to_star,timeframe,last_ts_utc,last_close,all_time_low,all_time_high,samples)
    names_mode=True → 用 item_name IN (...)
    names_mode=False → 用 item_id IN (...)
    timeframe=None → 不過濾；否則以該 timeframe 過濾（e.g. '20m'）
    """
    if not items:
        return []

    # ✅ 先確保統計表存在（沒有就建空表）
    ensure_price_stats_table(db_path)

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        base = ("SELECT item_id,item_name,upgrade_type,upgrade_subtype,from_star,to_star,timeframe,"
                "last_ts_utc,last_close,all_time_low,all_time_high,samples FROM price_stats WHERE ")
        if names_mode:
            cond = "item_name IN ({})".format(",".join("?"*len(items)))
        else:
            cond = "item_id IN ({})".format(",".join("?"*len(items)))
        params = list(items)
        if timeframe:
            cond += " AND timeframe = ?"
            params.append(timeframe)
        sql = base + cond + " ORDER BY item_id, upgrade_type, from_star, to_star, upgrade_subtype"
        cur.execute(sql, params)
        return cur.fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()

def map_subtype_name(code: str) -> str:
    for k,v in CUBE_PRESETS.items():
        if v == (code or ""):
            return k
    return (code or "")

# ------------------ 索引幫手 ------------------

def load_index(index_path: Path) -> Dict[str, Dict[str, object]]:
    """
    name_lower -> {id, name, max_star}
    """
    out: Dict[str, Dict[str, object]] = {}
    if not index_path.exists():
        return out
    data = json.loads(index_path.read_text(encoding="utf-8"))
    for it in data.get("items", []):
        name = (it.get("name") or "").strip()
        iid  = str(it.get("id") or "").strip()
        ms   = it.get("max_star")
        if name and iid:
            out[name.lower()] = {"id": iid, "name": name, "max_star": ms}
    return out

def resolve_items(tokens: List[str], names_mode: bool, index_path: Path) -> List[Tuple[str, Optional[str]]]:
    """
    將輸入（名稱或 ID）轉為 [(id, name)]。
    名稱模式下，必須在索引中找到；找不到會略過並彈出提醒。
    """
    out: List[Tuple[str, Optional[str]]] = []
    if names_mode:
        idx = load_index(index_path)
        missing = []
        for t in tokens:
            rec = idx.get(t.strip().lower())
            if rec:
                out.append((str(rec["id"]), str(rec["name"])))
            else:
                missing.append(t)
        if missing:
            messagebox.showwarning("名稱未在索引中", "\n".join(missing))
    else:
        # 以 ID 模式，只能顯示 ID；名稱若在索引中也補上
        idx = load_index(index_path)
        for t in tokens:
            t2 = t.strip()
            name = None
            # 逆查名稱（可選）
            for _name_lower, rec in idx.items():
                if str(rec.get("id")) == t2:
                    name = rec.get("name")
                    break
            out.append((t2, name))
    # 去重
    seen=set(); uniq=[]
    for item in out:
        if item[0] not in seen:
            uniq.append(item); seen.add(item[0])
    return uniq

def load_all_from_index(index_path: Path, names_mode: bool) -> List[str]:
    data = json.loads(index_path.read_text(encoding="utf-8"))
    if names_mode:
        vals = [(it.get("name") or "").strip() for it in data.get("items", [])]
    else:
        vals = [str(it.get("id") or "").strip() for it in data.get("items", [])]
    vals = [v for v in vals if v]
    # 去重
    seen=set(); uniq=[]
    for v in vals:
        if v not in seen:
            uniq.append(v); seen.add(v)
    return uniq

# ------------------ 主應用 ------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1180x880")
        self.minsize(1020, 720)

        # 共用狀態
        self.log_queue = queue.Queue()
        self.worker_thread = None

        # 排程狀態
        self.sched_thread = None
        self.sched_stop = threading.Event()

        self._build_ui()
        self._poll_log_queue()

    # ---------------- UI 建置 ----------------

    def _build_ui(self):
        notebook = ttk.Notebook(self); notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        # 分頁
        self.tab_run = ttk.Frame(notebook)
        self.tab_sched = ttk.Frame(notebook)
        self.tab_query = ttk.Frame(notebook)
        self.tab_est = ttk.Frame(notebook)     # NEW

        notebook.add(self.tab_run, text="Run（單次批次）")
        notebook.add(self.tab_sched, text="Schedule（排程）")
        notebook.add(self.tab_query, text="Query（快照 / 刷新）")
        notebook.add(self.tab_est, text="Estimate（估價）")  # NEW

        # 各分頁
        self._build_run_tab(self.tab_run)
        self._build_sched_tab(self.tab_sched)
        self._build_query_tab(self.tab_query)
        self._build_estimate_tab(self.tab_est)  # NEW

        # 底部狀態列
        self._build_bottom_bar()

    # ---------------- Run 分頁（沿用原邏輯 + Both 模式） ----------------

    def _build_run_tab(self, parent):
        row1 = ttk.Frame(parent); row1.pack(fill=tk.X, padx=6, pady=6)

        self.mode_var = tk.StringVar(value="both")
        ttk.Label(row1, text="Mode:").pack(side=tk.LEFT)
        ttk.Radiobutton(row1, text="Star", variable=self.mode_var, value="star", command=self._mode_changed).pack(side=tk.LEFT, padx=(4,6))
        ttk.Radiobutton(row1, text="Cubes", variable=self.mode_var, value="cube", command=self._mode_changed).pack(side=tk.LEFT, padx=(4,6))
        ttk.Radiobutton(row1, text="Both", variable=self.mode_var, value="both", command=self._mode_changed).pack(side=tk.LEFT, padx=(4,12))

        ttk.Label(row1, text="Timeframe:").pack(side=tk.LEFT)
        self.tf_var = tk.StringVar(value="20m")
        ttk.Combobox(row1, textvariable=self.tf_var, values=["20m","1H","1D","1W","1M"], width=6, state="readonly").pack(side=tk.LEFT, padx=(4,10))

        self.headless_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row1, text="Headless（穩定後勾）", variable=self.headless_var).pack(side=tk.LEFT)

        # 行2：Star/Cube 選項
        row2 = ttk.Frame(parent); row2.pack(fill=tk.X, padx=6, pady=6)

        # Star Force 區塊
        self.sf_frame = ttk.Frame(row2); self.sf_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.auto_star_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(self.sf_frame, text="自動最大星數（索引/頁面）", variable=self.auto_star_var).pack(side=tk.LEFT)
        ttk.Label(self.sf_frame, text="  手動 from→to：").pack(side=tk.LEFT, padx=(10,2))
        self.sf_from = tk.IntVar(value=0); self.sf_to = tk.IntVar(value=19)
        ttk.Entry(self.sf_frame, textvariable=self.sf_from, width=5).pack(side=tk.LEFT)
        ttk.Label(self.sf_frame, text="→").pack(side=tk.LEFT)
        ttk.Entry(self.sf_frame, textvariable=self.sf_to, width=5).pack(side=tk.LEFT, padx=(0,10))
        ttk.Label(self.sf_frame, text="延遲(秒)").pack(side=tk.LEFT)
        self.delay_var = tk.DoubleVar(value=0.7)
        ttk.Entry(self.sf_frame, textvariable=self.delay_var, width=6).pack(side=tk.LEFT)

        # Cubes 區塊
        self.cube_frame = ttk.Frame(row2)
        self.cube_red = tk.BooleanVar(value=True); self.cube_black = tk.BooleanVar(value=True); self.cube_bonus = tk.BooleanVar(value=True)
        ttk.Label(self.cube_frame, text="Cubes:").pack(side=tk.LEFT)
        ttk.Checkbutton(self.cube_frame, text="Red", variable=self.cube_red).pack(side=tk.LEFT, padx=6)
        ttk.Checkbutton(self.cube_frame, text="Black", variable=self.cube_black).pack(side=tk.LEFT, padx=6)
        ttk.Checkbutton(self.cube_frame, text="Bonus", variable=self.cube_bonus).pack(side=tk.LEFT, padx=6)

        self._mode_changed()  # 初始顯示

        # 行3：輸入（名稱或 ID）
        row3 = ttk.LabelFrame(parent, text="Items（名稱或ID）")
        row3.pack(fill=tk.X, padx=6, pady=6)
        self.names_mode_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row3, text="將輸入視為『名稱』", variable=self.names_mode_var).grid(row=0, column=0, sticky="w", padx=6, pady=4)

        ttk.Label(row3, text="以逗號分隔：").grid(row=1, column=0, sticky="w", padx=6)
        self.item_ids_var = tk.StringVar(value="Will o’ the Wisps")
        ttk.Entry(row3, textvariable=self.item_ids_var, width=60).grid(row=1, column=1, sticky="we", padx=6, pady=4, columnspan=3)

        ttk.Label(row3, text="或檔案（每行一個）：").grid(row=2, column=0, sticky="w", padx=6)
        self.item_ids_file_var = tk.StringVar()
        ttk.Entry(row3, textvariable=self.item_ids_file_var, width=50).grid(row=2, column=1, sticky="we", padx=6, pady=4)
        ttk.Button(row3, text="選擇檔案…", command=self._choose_item_ids_file).grid(row=2, column=2, sticky="w", padx=6, pady=4)

        ttk.Label(row3, text="索引檔（items_index.json）：").grid(row=3, column=0, sticky="w", padx=6)
        self.index_path_var = tk.StringVar(value=str(Path(DEFAULT_INDEX)))
        ttk.Entry(row3, textvariable=self.index_path_var, width=50).grid(row=3, column=1, sticky="we", padx=6, pady=4)
        ttk.Button(row3, text="選擇索引…", command=self._choose_index_file).grid(row=3, column=2, sticky="w", padx=6, pady=4)

        row3.columnconfigure(1, weight=1)

        # 行4：輸出
        row4 = ttk.LabelFrame(parent, text="Output")
        row4.pack(fill=tk.X, padx=6, pady=6)
        ttk.Label(row4, text="輸出目錄:").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.out_dir_var = tk.StringVar(value=str(Path.cwd()))
        ttk.Entry(row4, textvariable=self.out_dir_var).grid(row=0, column=1, sticky="we", padx=6, pady=4)
        ttk.Button(row4, text="選擇目錄…", command=self._choose_out_dir).grid(row=0, column=2, sticky="w", padx=6, pady=4)

        ttk.Label(row4, text="CSV 基底檔名（自動加時間戳）:").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        self.csv_base_var = tk.StringVar(value="msu_dynamic_pricing")
        ttk.Entry(row4, textvariable=self.csv_base_var, width=32).grid(row=1, column=1, sticky="w", padx=6, pady=4)

        ttk.Label(row4, text="SQLite 檔名:").grid(row=2, column=0, sticky="w", padx=6, pady=4)
        self.db_name_var = tk.StringVar(value=DEFAULT_DB)
        ttk.Entry(row4, textvariable=self.db_name_var, width=32).grid(row=2, column=1, sticky="w", padx=6, pady=4)

        # 行5：Log
        row5 = ttk.LabelFrame(parent, text="Log")
        row5.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self.log_text = tk.Text(row5, height=16, wrap="word")
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self.log_text.configure(state="disabled")

        # 操作按鈕
        bar = ttk.Frame(parent); bar.pack(fill=tk.X, padx=6, pady=(0,8))
        self.btn_start = ttk.Button(bar, text="開始抓取", command=self._on_start)
        self.btn_start.pack(side=tk.LEFT)
        ttk.Button(bar, text="清空 Log", command=self._clear_log).pack(side=tk.LEFT, padx=8)

    def _mode_changed(self):
        for w in (self.sf_frame, self.cube_frame):
            w.pack_forget()
        m = self.mode_var.get()
        if m == "star":
            self.sf_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)
        elif m == "cube":
            self.cube_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)
        else:
            self.sf_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)
            self.cube_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)

    # ---------------- Schedule 分頁 ----------------

    def _build_sched_tab(self, parent):
        row1 = ttk.LabelFrame(parent, text="來源 / 目標")
        row1.pack(fill=tk.X, padx=6, pady=6)

        ttk.Label(row1, text="索引檔：").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.s_index_var = tk.StringVar(value=str(Path(DEFAULT_INDEX)))
        ttk.Entry(row1, textvariable=self.s_index_var, width=50).grid(row=0, column=1, sticky="we", padx=6, pady=4)
        ttk.Button(row1, text="選擇…", command=lambda: self._choose_file_to_var(self.s_index_var, [("JSON","*.json"),("All","*.*")])).grid(row=0, column=2, sticky="w", padx=6, pady=4)

        ttk.Label(row1, text="DB 檔：").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        self.s_db_var = tk.StringVar(value=str(Path(DEFAULT_DB)))
        ttk.Entry(row1, textvariable=self.s_db_var, width=50).grid(row=1, column=1, sticky="we", padx=6, pady=4)
        ttk.Button(row1, text="選擇…", command=lambda: self._choose_file_to_var(self.s_db_var, [("SQLite","*.sqlite;*.db"),("All","*.*")])).grid(row=1, column=2, sticky="w", padx=6, pady=4)

        row1.columnconfigure(1, weight=1)

        row2 = ttk.LabelFrame(parent, text="設定")
        row2.pack(fill=tk.X, padx=6, pady=6)

        ttk.Label(row2, text="Timeframe:").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.s_tf_var = tk.StringVar(value="20m")
        ttk.Combobox(row2, textvariable=self.s_tf_var, values=["20m","1H","1D","1W","1M"], width=6, state="readonly").grid(row=0, column=1, sticky="w", padx=6, pady=4)

        ttk.Label(row2, text="Interval（分鐘）:").grid(row=0, column=2, sticky="w", padx=6, pady=4)
        self.s_interval_var = tk.IntVar(value=120)
        ttk.Entry(row2, textvariable=self.s_interval_var, width=8).grid(row=0, column=3, sticky="w", padx=6, pady=4)

        self.s_headless_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row2, text="Headless", variable=self.s_headless_var).grid(row=0, column=4, sticky="w", padx=10)

        ttk.Label(row2, text="延遲(秒)").grid(row=0, column=5, sticky="w", padx=6)
        self.s_delay_var = tk.DoubleVar(value=0.6)
        ttk.Entry(row2, textvariable=self.s_delay_var, width=6).grid(row=0, column=6, sticky="w")

        # 操作
        bar = ttk.Frame(parent); bar.pack(fill=tk.X, padx=6, pady=6)
        self.btn_sched_start = ttk.Button(bar, text="啟動排程（抓索引中全部物品：Star+Cube）", command=self._on_sched_start)
        self.btn_sched_start.pack(side=tk.LEFT)
        self.btn_sched_stop  = ttk.Button(bar, text="停止排程", command=self._on_sched_stop, state="disabled")
        self.btn_sched_stop.pack(side=tk.LEFT, padx=8)
        ttk.Button(bar, text="立刻執行一次", command=self._on_sched_once).pack(side=tk.LEFT, padx=8)

        # Log
        box = ttk.LabelFrame(parent, text="Schedule Log")
        box.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self.s_log = tk.Text(box, height=14, wrap="word"); self.s_log.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self.s_log.configure(state="disabled")

    def _on_sched_start(self):
        if self.sched_thread and self.sched_thread.is_alive():
            messagebox.showinfo("提示", "排程已在執行。")
            return
        index_path = Path(self.s_index_var.get().strip())
        if not index_path.exists():
            messagebox.showwarning("索引檔不存在", str(index_path)); return
        db_path = self.s_db_var.get().strip()
        interval = max(5, int(self.s_interval_var.get()))
        timeframe = self.s_tf_var.get()
        headless = self.s_headless_var.get()
        delay = max(0.2, float(self.s_delay_var.get()))

        self.sched_stop.clear()
        self.btn_sched_start.config(state="disabled")
        self.btn_sched_stop.config(state="normal")
        self._slog(f"[SCHED] 啟動，間隔 {interval} 分鐘，timeframe={timeframe}\n")

        def loop():
            while not self.sched_stop.is_set():
                self._slog("[SCHED] 執行一次…\n")
                try:
                    ids = self._load_all_ids_from_index(index_path)
                    if not ids:
                        self._slog("[SCHED] 索引沒有 items。\n")
                    else:
                        run_batch(
                            item_ids=ids,
                            upgrade_type=0,
                            cube_subtypes=list(CUBE_PRESETS.values()),
                            star_range=(0, 0),
                            timeframe=timeframe,
                            db_path=db_path,
                            csv_path=None,
                            headless=headless,
                            delay_sec=delay,
                            block_trackers=True,
                            debug_screens=False,
                            debug_dir="screenshots",
                            max_read_tries=8,
                            reload_on_try=4,
                            settle_ms=600,
                            warmup=True,
                            mode="both",
                            names_mode=False,       # 直接用 ID，比名稱穩
                            index_path=str(index_path),
                            auto_star=True,
                        )
                        self._slog("[SCHED] 完成一次抓取並更新統計。\n")
                except Exception as e:
                    self._slog(f"[SCHED] ERROR: {e}\n")

                total = interval * 60
                for _ in range(total):
                    if self.sched_stop.is_set():
                        break
                    time.sleep(1)

            self._slog("[SCHED] 已停止。\n")
            self.btn_sched_start.config(state="normal")
            self.btn_sched_stop.config(state="disabled")

        self.sched_thread = threading.Thread(target=loop, daemon=True)
        self.sched_thread.start()

    def _on_sched_stop(self):
        self.sched_stop.set()

    def _on_sched_once(self):
        index_path = Path(self.s_index_var.get().strip())
        if not index_path.exists():
            messagebox.showwarning("索引檔不存在", str(index_path)); return
        db_path = self.s_db_var.get().strip()
        timeframe = self.s_tf_var.get()
        headless = self.s_headless_var.get()
        delay = max(0.2, float(self.s_delay_var.get()))
        self._slog("[SCHED] 立即執行一次…\n")
        t = threading.Thread(target=lambda: self._sched_once_job(index_path, db_path, timeframe, headless, delay), daemon=True)
        t.start()

    def _sched_once_job(self, index_path: Path, db_path: str, timeframe: str, headless: bool, delay: float):
        try:
            ids = self._load_all_ids_from_index(index_path)
            if not ids:
                self._slog("[SCHED] 索引沒有 items。\n"); return
            run_batch(
                item_ids=ids,
                upgrade_type=0,
                cube_subtypes=list(CUBE_PRESETS.values()),
                star_range=(0, 0),
                timeframe=timeframe,
                db_path=db_path,
                csv_path=None,
                headless=headless,
                delay_sec=delay,
                block_trackers=True,
                debug_screens=False,
                debug_dir="screenshots",
                max_read_tries=8,
                reload_on_try=4,
                settle_ms=600,
                warmup=True,
                mode="both",
                names_mode=False,
                index_path=str(index_path),
                auto_star=True,
            )
            self._slog("[SCHED] 完成一次抓取並更新統計。\n")
        except Exception as e:
            self._slog(f"[SCHED] ERROR: {e}\n")

    def _load_all_ids_from_index(self, index_path: Path) -> List[str]:
        try:
            data = json.loads(index_path.read_text(encoding="utf-8"))
            out = []
            for it in data.get("items", []):
                iid = str(it.get("id", "")).strip()
                if iid:
                    out.append(iid)
            seen = set(); uniq = []
            for x in out:
                if x not in seen:
                    uniq.append(x); seen.add(x)
            return uniq
        except Exception:
            return []

    def _slog(self, s: str):
        self.s_log.configure(state="normal")
        self.s_log.insert("end", s)
        self.s_log.see("end")
        self.s_log.configure(state="disabled")

    def _choose_file_to_var(self, var: tk.StringVar, types):
        path = filedialog.askopenfilename(filetypes=types)
        if path: var.set(path)

    # ---------------- Query 分頁（先快照，再刷新） ----------------

    def _build_query_tab(self, parent):
        row1 = ttk.LabelFrame(parent, text="查詢條件")
        row1.pack(fill=tk.X, padx=6, pady=6)

        ttk.Label(row1, text="DB 檔：").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.q_db_var = tk.StringVar(value=str(Path(DEFAULT_DB)))
        ttk.Entry(row1, textvariable=self.q_db_var, width=50).grid(row=0, column=1, sticky="we", padx=6, pady=4)
        ttk.Button(row1, text="選擇…", command=lambda: self._choose_file_to_var(self.q_db_var, [("SQLite","*.sqlite;*.db"),("All","*.*")])).grid(row=0, column=2, sticky="w", padx=6, pady=4)

        ttk.Label(row1, text="索引檔：").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        self.q_index_var = tk.StringVar(value=str(Path(DEFAULT_INDEX)))
        ttk.Entry(row1, textvariable=self.q_index_var, width=50).grid(row=1, column=1, sticky="we", padx=6, pady=4)
        ttk.Button(row1, text="選擇…", command=lambda: self._choose_file_to_var(self.q_index_var, [("JSON","*.json"),("All","*.*")])).grid(row=1, column=2, sticky="w", padx=6, pady=4)

        ttk.Label(row1, text="Timeframe:").grid(row=0, column=3, sticky="w", padx=8)
        self.q_tf_var = tk.StringVar(value="20m")
        ttk.Combobox(row1, textvariable=self.q_tf_var, values=["20m","1H","1D","1W","1M","(all)"], width=8, state="readonly").grid(row=0, column=4, sticky="w")

        self.q_names_mode = tk.BooleanVar(value=True)
        ttk.Checkbutton(row1, text="名稱模式", variable=self.q_names_mode).grid(row=0, column=5, sticky="w", padx=10)

        ttk.Label(row1, text="物品（逗號分隔）或留空=索引全部：").grid(row=2, column=0, sticky="w", padx=6, pady=4)
        self.q_items_var = tk.StringVar(value="Will o’ the Wisps")
        ttk.Entry(row1, textvariable=self.q_items_var, width=60).grid(row=2, column=1, columnspan=4, sticky="we", padx=6, pady=4)

        row1.columnconfigure(1, weight=1)

        bar = ttk.Frame(parent); bar.pack(fill=tk.X, padx=6, pady=6)
        ttk.Button(bar, text="顯示快照（不刷新）", command=lambda: self._on_query(refresh=False)).pack(side=tk.LEFT)
        ttk.Button(bar, text="查詢並刷新", command=lambda: self._on_query(refresh=True)).pack(side=tk.LEFT, padx=8)
        self.q_headless = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="刷新時 Headless", variable=self.q_headless).pack(side=tk.LEFT, padx=12)

        # 結果表
        table_box = ttk.LabelFrame(parent, text="結果")
        table_box.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        cols = ("item_id","item_name","type","subtype","from","to","timeframe","last","low","high","n","last_ts")
        self.tree = ttk.Treeview(table_box, columns=cols, show="headings", height=16)
        for c, w in zip(cols, (100,180,60,70,60,60,80,90,90,90,60,160)):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, anchor="center")
        self.tree.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        # Log
        qbox = ttk.LabelFrame(parent, text="Query Log")
        qbox.pack(fill=tk.BOTH, expand=False, padx=6, pady=6)
        self.q_log = tk.Text(qbox, height=8, wrap="word")
        self.q_log.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self.q_log.configure(state="disabled")

    def _on_query(self, refresh: bool):
        db_path = self.q_db_var.get().strip()
        if not Path(db_path).exists():
            messagebox.showwarning("DB 不存在", db_path); return

        items = []
        raw = self.q_items_var.get().strip()
        if raw:
            items = [x.strip() for x in raw.split(",") if x.strip()]
        else:
            index_path = Path(self.q_index_var.get().strip())
            if index_path.exists():
                if self.q_names_mode.get():
                    items = self._load_all_names_from_index(index_path)
                else:
                    items = self._load_all_ids_from_index(index_path)
            else:
                messagebox.showwarning("索引檔不存在", str(index_path)); return
        if not items:
            messagebox.showinfo("沒有項目", "請輸入物品或準備索引。")
            return

        timeframe = self.q_tf_var.get()
        tf_filter = None if timeframe == "(all)" else timeframe
        names_mode = self.q_names_mode.get()

        # 先顯示快照
        self._qlog("[QUERY] 讀取快照…\n")
        rows = fetch_stats_for_items(db_path, items, names_mode, timeframe=tf_filter)
        self._fill_table(rows)

        if not refresh:
            return

        # 立即刷新
        self._qlog("[QUERY] 立即刷新中（Both + AutoStar）…\n")
        headless = self.q_headless.get()
        index_path = self.q_index_var.get().strip() or None

        def job():
            try:
                run_batch(
                    item_ids=items,
                    upgrade_type=0,
                    cube_subtypes=list(CUBE_PRESETS.values()),
                    star_range=(0, 0),
                    timeframe=(tf_filter or "20m"),
                    db_path=db_path,
                    csv_path=None,
                    headless=headless,
                    delay_sec=0.6,
                    block_trackers=True,
                    debug_screens=False,
                    debug_dir="screenshots",
                    max_read_tries=8,
                    reload_on_try=4,
                    settle_ms=600,
                    warmup=True,
                    mode="both",
                    names_mode=names_mode,
                    index_path=index_path,
                    auto_star=True,
                )
                self._qlog("[QUERY] 刷新完成，讀回快照…\n")
                rows2 = fetch_stats_for_items(db_path, items, names_mode, timeframe=tf_filter)
                self._fill_table(rows2)
                self._qlog("[QUERY] 完成。\n")
            except Exception as e:
                self._qlog(f"[QUERY] ERROR: {e}\n")

        threading.Thread(target=job, daemon=True).start()

    def _fill_table(self, rows: List[Tuple]):
        self.tree.delete(*self.tree.get_children())
        for (item_id, item_name, utype, usub, f, t, tf, ts, last, low, high, n) in rows:
            typ = "STAR" if int(utype or 0) == 0 else "CUBE"
            sub = map_subtype_name(usub) if typ == "CUBE" else ""
            f2 = f if f is not None and int(f) >= 0 else "-"
            t2 = t if t is not None and int(t) >= 0 else "-"
            last_s = "-" if last is None else f"{float(last):.0f}"
            low_s  = "-" if low  is None else f"{float(low):.0f}"
            high_s = "-" if high is None else f"{float(high):.0f}"
            n_s    = "0" if n is None else str(int(n))
            ts_s   = ts or ""
            self.tree.insert("", "end", values=(item_id, item_name or "", typ, sub, f2, t2, tf, last_s, low_s, high_s, n_s, ts_s))

    def _load_all_names_from_index(self, index_path: Path) -> List[str]:
        try:
            data = json.loads(index_path.read_text(encoding="utf-8"))
            out = []
            for it in data.get("items", []):
                nm = (it.get("name") or "").strip()
                if nm:
                    out.append(nm)
            seen=set(); uniq=[]
            for x in out:
                if x not in seen:
                    uniq.append(x); seen.add(x)
            return uniq
        except Exception:
            return []

    # ---------------- Estimate 分頁（NEW） ----------------

    def _build_estimate_tab(self, parent):
        # 資料來源
        row0 = ttk.LabelFrame(parent, text="來源")
        row0.pack(fill=tk.X, padx=6, pady=6)

        ttk.Label(row0, text="DB 檔：").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.e_db_var = tk.StringVar(value=str(Path(DEFAULT_DB)))
        ttk.Entry(row0, textvariable=self.e_db_var, width=50).grid(row=0, column=1, sticky="we", padx=6, pady=4)
        ttk.Button(row0, text="選擇…", command=lambda: self._choose_file_to_var(self.e_db_var, [("SQLite","*.sqlite;*.db"),("All","*.*")])).grid(row=0, column=2, sticky="w", padx=6, pady=4)

        ttk.Label(row0, text="索引檔：").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        self.e_index_var = tk.StringVar(value=str(Path(DEFAULT_INDEX)))
        ttk.Entry(row0, textvariable=self.e_index_var, width=50).grid(row=1, column=1, sticky="we", padx=6, pady=4)
        ttk.Button(row0, text="選擇…", command=lambda: self._choose_file_to_var(self.e_index_var, [("JSON","*.json"),("All","*.*")])).grid(row=1, column=2, sticky="w", padx=6, pady=4)

        ttk.Label(row0, text="Timeframe:").grid(row=0, column=3, sticky="w", padx=8)
        self.e_tf_var = tk.StringVar(value="20m")
        ttk.Combobox(row0, textvariable=self.e_tf_var, values=["20m","1H","1D","1W","1M"], width=8, state="readonly").grid(row=0, column=4, sticky="w")

        self.e_names_mode = tk.BooleanVar(value=True)
        ttk.Checkbutton(row0, text="名稱模式", variable=self.e_names_mode).grid(row=1, column=3, sticky="w", padx=8)

        row0.columnconfigure(1, weight=1)

        # 物品輸入
        row1 = ttk.LabelFrame(parent, text="Items（留空=索引全部）")
        row1.pack(fill=tk.X, padx=6, pady=6)

        ttk.Label(row1, text="物品（逗號分隔）：").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.e_items_var = tk.StringVar(value="Will o’ the Wisps")
        ttk.Entry(row1, textvariable=self.e_items_var, width=60).grid(row=0, column=1, sticky="we", padx=6, pady=4, columnspan=3)
        row1.columnconfigure(1, weight=1)

        # 星力設定
        row2 = ttk.LabelFrame(parent, text="Star Force")
        row2.pack(fill=tk.X, padx=6, pady=6)
        self.e_enable_star = tk.BooleanVar(value=True)
        ttk.Checkbutton(row2, text="計算星力期望成本", variable=self.e_enable_star).grid(row=0, column=0, sticky="w", padx=6)
        ttk.Label(row2, text="Start★").grid(row=0, column=1, sticky="w")
        self.e_star_start = tk.IntVar(value=0)
        ttk.Entry(row2, textvariable=self.e_star_start, width=6).grid(row=0, column=2, sticky="w", padx=(0,10))
        ttk.Label(row2, text="Target★").grid(row=0, column=3, sticky="w")
        self.e_star_target = tk.IntVar(value=22)
        ttk.Entry(row2, textvariable=self.e_star_target, width=6).grid(row=0, column=4, sticky="w")

        # 潛能設定（主/加分開）
        row3 = ttk.LabelFrame(parent, text="Potential（Dual）")
        row3.pack(fill=tk.X, padx=6, pady=6)

        ttk.Label(row3, text="Main:").grid(row=0, column=0, sticky="e", padx=6)
        self.e_main_start = tk.StringVar(value="Rare")
        self.e_main_target = tk.StringVar(value="Legendary")
        ttk.Combobox(row3, textvariable=self.e_main_start, values=TIERS[:-1], width=10, state="readonly").grid(row=0, column=1, sticky="w")
        ttk.Label(row3, text="→").grid(row=0, column=2, sticky="w")
        ttk.Combobox(row3, textvariable=self.e_main_target, values=POT_TARGETS, width=12, state="readonly").grid(row=0, column=3, sticky="w")

        ttk.Label(row3, text="Bonus:").grid(row=1, column=0, sticky="e", padx=6)
        self.e_bonus_start = tk.StringVar(value="Rare")
        self.e_bonus_target = tk.StringVar(value="Unique")  # 你提到常見案例：Unique
        ttk.Combobox(row3, textvariable=self.e_bonus_start, values=TIERS[:-1], width=10, state="readonly").grid(row=1, column=1, sticky="w")
        ttk.Label(row3, text="→").grid(row=1, column=2, sticky="w")
        ttk.Combobox(row3, textvariable=self.e_bonus_target, values=POT_TARGETS_WITH_SKIP, width=12, state="readonly").grid(row=1, column=3, sticky="w")

        # 按鈕
        bar = ttk.Frame(parent); bar.pack(fill=tk.X, padx=6, pady=6)
        ttk.Button(bar, text="開始估價", command=self._on_estimate).pack(side=tk.LEFT)
        ttk.Button(bar, text="清空結果", command=lambda: self._clear_tree(self.est_tree)).pack(side=tk.LEFT, padx=8)

        # 結果表
        box = ttk.LabelFrame(parent, text="估價結果")
        box.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        cols = ("item_id","item_name","timeframe","star_from","star_to","star_cost","main_from","main_to","main_cost","bonus_from","bonus_to","bonus_cost","total")
        self.est_tree = ttk.Treeview(box, columns=cols, show="headings", height=16)
        widths = (100,180,80,70,70,110,90,110,110,90,110,110,120)
        heads  = ("ItemID","Name","TF","S_from","S_to","S_cost","M_from","M_to","M_cost","B_from","B_to","B_cost","Total")
        for c, w, h in zip(cols, widths, heads):
            self.est_tree.heading(c, text=h)
            self.est_tree.column(c, width=w, anchor="center")
        self.est_tree.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        # Log
        lbox = ttk.LabelFrame(parent, text="Estimate Log")
        lbox.pack(fill=tk.BOTH, expand=False, padx=6, pady=6)
        self.e_log = tk.Text(lbox, height=8, wrap="word")
        self.e_log.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self.e_log.configure(state="disabled")

    def _clear_tree(self, tree: ttk.Treeview):
        tree.delete(*tree.get_children())

    def _on_estimate(self):
        # 檢查匯入
        if expected_star_cost is None or expected_potential_cost_dual is None:
            messagebox.showerror("缺少 pricing_engine", "找不到 pricing_engine.py 或匯入失敗，請確認檔案存在於同資料夾。")
            return

        db_path = self.e_db_var.get().strip()
        if not Path(db_path).exists():
            messagebox.showwarning("DB 不存在", db_path); return
        index_path = Path(self.e_index_var.get().strip())
        if not index_path.exists():
            messagebox.showwarning("索引檔不存在", str(index_path)); return

        timeframe = self.e_tf_var.get()
        names_mode = self.e_names_mode.get()

        # 解析 items
        raw = self.e_items_var.get().strip()
        if raw:
            tokens = [x.strip() for x in raw.split(",") if x.strip()]
        else:
            tokens = load_all_from_index(index_path, names_mode)
        if not tokens:
            messagebox.showinfo("沒有項目", "請輸入物品或準備索引。"); return

        resolved = resolve_items(tokens, names_mode, index_path)
        if not resolved:
            messagebox.showwarning("無可估價的項目", "輸入的名稱不在索引，或為空。"); return

        # 參數
        enable_star = self.e_enable_star.get()
        try:
            start_star = int(self.e_star_start.get())
            target_star = int(self.e_star_target.get())
        except Exception:
            messagebox.showwarning("星力參數錯誤", "Start/Target 星數必須是整數。"); return

        main_start = self.e_main_start.get()
        main_target = self.e_main_target.get()
        bonus_start = self.e_bonus_start.get()
        bonus_target = self.e_bonus_target.get()    # "Skip"/Epic/Unique/Legendary

        # 背景工作
        self._elog("[EST] 開始估價…\n")
        self.btn_text_backup = None
        def job():
            try:
                conn = sqlite3.connect(db_path)
                rows_to_insert = []
                for (iid, name) in resolved:
                    star_cost = None
                    if enable_star:
                        try:
                            res_star = expected_star_cost(conn, item_id=iid,
                                                          target_star=target_star,
                                                          timeframe=timeframe,
                                                          start_star=start_star)
                            star_cost = res_star.expected_cost_from_start
                        except Exception as e:
                            self._elog(f"[STAR] {iid} {name or ''}: {e}\n")

                    # bonus_target 轉換
                    bt = None if bonus_target.lower() == "skip" else bonus_target

                    main_cost = None
                    bonus_cost = None
                    try:
                        res_p = expected_potential_cost_dual(
                            conn, item_id=iid, timeframe=timeframe,
                            main_target_tier=main_target, main_start_tier=main_start,
                            bonus_target_tier=bt, bonus_start_tier=bonus_start
                        )
                        main_cost = res_p.main_cost
                        bonus_cost = res_p.bonus_cost
                    except Exception as e:
                        self._elog(f"[POT] {iid} {name or ''}: {e}\n")

                    total = 0.0
                    parts = []
                    if star_cost is not None: total += star_cost; parts.append("star")
                    if main_cost is not None: total += main_cost; parts.append("main")
                    if (bt is not None) and (bonus_cost is not None): total += bonus_cost; parts.append("bonus")

                    rows_to_insert.append((
                        iid, name or "", timeframe,
                        start_star, target_star,
                        "-" if star_cost is None else f"{float(star_cost):,.0f}",
                        main_start, main_target,
                        "-" if main_cost is None else f"{float(main_cost):,.0f}",
                        bonus_start, (bonus_target if bt is not None else "Skip"),
                        "-" if (bt is None or bonus_cost is None) else f"{float(bonus_cost):,.0f}",
                        "-" if not parts else f"{float(total):,.0f}",
                    ))

                conn.close()

                # 更新 UI
                def ui_update():
                    for r in rows_to_insert:
                        self.est_tree.insert("", "end", values=r)
                    self._elog("[EST] 完成。\n")
                self.after(0, ui_update)
            except Exception as e:
                self._elog(f"[EST] ERROR: {e}\n")

        threading.Thread(target=job, daemon=True).start()

    def _elog(self, s: str):
        self.e_log.configure(state="normal"); self.e_log.insert("end", s); self.e_log.see("end"); self.e_log.configure(state="disabled")

    # ---------------- 底部 / 共用 ----------------

    def _build_bottom_bar(self):
        bar = ttk.Frame(self); bar.pack(fill=tk.X, padx=8, pady=(0,8))
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(bar, textvariable=self.status_var).pack(side=tk.RIGHT)

    def _choose_item_ids_file(self):
        path = filedialog.askopenfilename(title="選擇 名稱/ID 檔案（每行一個）",
                                          filetypes=[("Text", "*.txt;*.csv;*.list"), ("All", "*.*")])
        if path: self.item_ids_file_var.set(path)

    def _choose_index_file(self):
        path = filedialog.askopenfilename(title="選擇 items_index.json",
                                          filetypes=[("JSON", "*.json"), ("All", "*.*")])
        if path: self.index_path_var.set(path)

    def _choose_out_dir(self):
        path = filedialog.askdirectory(title="選擇輸出目錄")
        if path: self.out_dir_var.set(path)

    # Run 分頁開始抓取
    def _on_start(self):
        tokens: List[str] = []
        if self.item_ids_var.get().strip():
            tokens += [x.strip() for x in self.item_ids_var.get().split(",") if x.strip()]
        if self.item_ids_file_var.get().strip():
            p = Path(self.item_ids_file_var.get().strip())
            if p.exists():
                for line in p.read_text(encoding="utf-8").splitlines():
                    t = line.strip()
                    if t: tokens.append(t)
        seen=set(); uniq=[]
        for x in tokens:
            if x not in seen:
                uniq.append(x); seen.add(x)
        if not uniq:
            messagebox.showwarning("輸入不足", "請至少提供一個 名稱或ID。"); return

        mode = self.mode_var.get()
        timeframe = self.tf_var.get()
        headless = self.headless_var.get()
        delay = max(0.2, float(self.delay_var.get()))
        out_dir = Path(self.out_dir_var.get()); out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_name = f"{self.csv_base_var.get().strip() or 'msu_dynamic_pricing'}_{mode}_{ts}.csv"
        csv_path = str(out_dir / csv_name)
        db_path = str(out_dir / (self.db_name_var.get().strip() or 'msu_dynamic_pricing.sqlite'))

        if self.auto_star_var.get():
            from_star, to_star = 0, 0
            auto_star = True
        else:
            from_star, to_star = int(self.sf_from.get()), int(self.sf_to.get())
            auto_star = False

        cube_subtypes = None
        if mode in ("cube","both"):
            chosen: List[str] = []
            if self.cube_red.get():   chosen.append(CUBE_PRESETS["red"])
            if self.cube_black.get(): chosen.append(CUBE_PRESETS["black"])
            if self.cube_bonus.get(): chosen.append(CUBE_PRESETS["bonus"])
            if not chosen:
                messagebox.showwarning("輸入不足", "請至少勾選一種 Cube。"); return
            cube_subtypes = chosen

        block_trackers=True; debug_screens=False; debug_dir=str(out_dir/"screenshots")
        max_read=8; reload_on=4; settle_ms=600; warmup=True

        self._clear_log()
        self._log(f"[Start] mode={mode} timeframe={timeframe} items={len(uniq)} csv={csv_path}\n")
        args = dict(
            item_ids=uniq,
            upgrade_type=0,
            cube_subtypes=cube_subtypes,
            star_range=(from_star, to_star),
            timeframe=timeframe,
            db_path=db_path,
            csv_path=csv_path,
            headless=headless,
            delay_sec=delay,
            block_trackers=block_trackers,
            debug_screens=debug_screens,
            debug_dir=debug_dir,
            max_read_tries=max_read,
            reload_on_try=reload_on,
            settle_ms=settle_ms,
            warmup=warmup,
            mode=mode,
            names_mode=self.names_mode_var.get(),
            index_path=self.index_path_var.get().strip() or None,
            auto_star=auto_star,
        )
        t = threading.Thread(target=self._worker_run, args=(args,), daemon=True)
        t.start()

    def _worker_run(self, args):
        orig_stdout = sys.stdout
        sys.stdout = GuiLogger(self.log_text, self.log_queue)
        try:
            run_batch(**args)
            self._log("\n[Done] 任務完成。\n")
        except Exception as e:
            self._log(f"\n[Error] {e}\n")
        finally:
            sys.stdout = orig_stdout

    # 共用 log
    def _clear_log(self):
        self.log_text.configure(state="normal"); self.log_text.delete("1.0", "end"); self.log_text.configure(state="disabled")
    def _log(self, s: str):
        self.log_text.configure(state="normal"); self.log_text.insert("end", s); self.log_text.see("end"); self.log_text.configure(state="disabled")
    def _qlog(self, s: str):
        self.q_log.configure(state="normal"); self.q_log.insert("end", s); self.q_log.see("end"); self.q_log.configure(state="disabled")

    def _poll_log_queue(self):
        try:
            while True:
                s = self.log_queue.get_nowait()
                self._log(s)
        except queue.Empty:
            pass
        self.after(80, self._poll_log_queue)

if __name__ == "__main__":
    app = App()
    app.mainloop()
