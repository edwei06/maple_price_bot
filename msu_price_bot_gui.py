# msu_price_bot_gui.py
# -*- coding: utf-8 -*-

import sys
import threading
import queue
import io
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from msu_dynamic_pricing_scraper import run_batch, CUBE_PRESETS

APP_TITLE = "MSU Dynamic Pricing Bot (GUI)"
DEFAULT_DB = "msu_dynamic_pricing.sqlite"

# stdout 導向 GUI
class GuiLogger(io.TextIOBase):
    def __init__(self, text_widget: tk.Text, queue_obj: queue.Queue):
        self.text = text_widget
        self.queue = queue_obj
    def write(self, s):
        if s:
            self.queue.put(s)
    def flush(self): pass

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("980x760")
        self.minsize(920, 620)
        self.worker_thread = None
        self.log_queue = queue.Queue()
        self._build_ui()
        self._poll_log_queue()

    def _build_ui(self):
        notebook = ttk.Notebook(self); notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.tab_run = ttk.Frame(notebook); notebook.add(self.tab_run, text="Run")
        self.tab_settings = ttk.Frame(notebook); notebook.add(self.tab_settings, text="Settings")
        self.tab_future = ttk.Frame(notebook); notebook.add(self.tab_future, text="(預留) Navigator 搜尋 / 精簡輸出")

        self._build_run_tab(self.tab_run)
        self._build_settings_tab(self.tab_settings)
        self._build_future_tab(self.tab_future)
        self._build_bottom_bar()

    def _build_run_tab(self, parent):
        # 行1：模式 / timeframe / headless
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
        ttk.Checkbutton(row1, text="Headless（穩定後再勾）", variable=self.headless_var).pack(side=tk.LEFT)

        # 行2：Star Force 範圍 / Cubes 選擇 / 自動星數
        row2 = ttk.Frame(parent); row2.pack(fill=tk.X, padx=6, pady=6)

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

        self.cube_frame = ttk.Frame(row2)
        self.cube_red = tk.BooleanVar(value=True); self.cube_black = tk.BooleanVar(value=True); self.cube_bonus = tk.BooleanVar(value=True)
        ttk.Label(self.cube_frame, text="Cubes:").pack(side=tk.LEFT)
        ttk.Checkbutton(self.cube_frame, text="Red", variable=self.cube_red).pack(side=tk.LEFT, padx=6)
        ttk.Checkbutton(self.cube_frame, text="Black", variable=self.cube_black).pack(side=tk.LEFT, padx=6)
        ttk.Checkbutton(self.cube_frame, text="Bonus", variable=self.cube_bonus).pack(side=tk.LEFT, padx=6)

        # Both 模式：兩塊都顯示；Star/Cube 單獨顯示
        self._mode_changed()

        # 行3：輸入（支援「名稱或ID」）
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
        self.index_path_var = tk.StringVar(value=str(Path("items_index.json")))
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

    def _build_settings_tab(self, parent):
        frm = ttk.Frame(parent); frm.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
        ttk.Label(frm, text="其它設定").grid(row=0, column=0, sticky="w")
        self.block_trackers_var = tk.BooleanVar(value=True)
        self.debug_shots_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="封鎖常見追蹤腳本（穩定）", variable=self.block_trackers_var).grid(row=1, column=0, sticky="w", pady=4)
        ttk.Checkbutton(frm, text="截圖除錯（screenshots/）", variable=self.debug_shots_var).grid(row=2, column=0, sticky="w", pady=4)

        sep = ttk.Separator(frm, orient="horizontal"); sep.grid(row=3, column=0, columnspan=3, sticky="we", pady=10)
        ttk.Label(frm, text="進階重試參數：").grid(row=4, column=0, sticky="w", pady=(0,4))
        self.max_read_var = tk.IntVar(value=8)
        self.reload_on_var = tk.IntVar(value=4)
        self.settle_ms_var = tk.IntVar(value=600)
        self.warmup_var = tk.BooleanVar(value=True)

        row = 5
        ttk.Label(frm, text="max_read_tries").grid(row=row, column=0, sticky="w"); ttk.Entry(frm, textvariable=self.max_read_var, width=6).grid(row=row, column=1, sticky="w"); row+=1
        ttk.Label(frm, text="reload_on_try").grid(row=row, column=0, sticky="w"); ttk.Entry(frm, textvariable=self.reload_on_var, width=6).grid(row=row, column=1, sticky="w"); row+=1
        ttk.Label(frm, text="settle_ms").grid(row=row, column=0, sticky="w"); ttk.Entry(frm, textvariable=self.settle_ms_var, width=6).grid(row=row, column=1, sticky="w"); row+=1
        ttk.Checkbutton(frm, text="首段暖機（建議開）", variable=self.warmup_var).grid(row=row, column=0, sticky="w"); row+=1
        frm.columnconfigure(2, weight=1)

    def _build_future_tab(self, parent):
        info = tk.Text(parent, wrap="word", height=12); info.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
        info.insert("end",
            "預留兩項：\n"
            "• Navigator 搜尋（名稱→ID fallback）\n"
            "• 精簡輸出與分門別類（只輸出 star/close 等）\n"
        )
        info.configure(state="disabled")

    def _build_bottom_bar(self):
        bar = ttk.Frame(self); bar.pack(fill=tk.X, padx=8, pady=(0,8))
        self.btn_start = ttk.Button(bar, text="開始抓取", command=self._on_start); self.btn_start.pack(side=tk.LEFT)
        self.btn_stop = ttk.Button(bar, text="停止（目前為批次，無即時中斷）", command=self._on_stop, state="disabled"); self.btn_stop.pack(side=tk.LEFT, padx=8)
        self.status_var = tk.StringVar(value="Ready."); ttk.Label(bar, textvariable=self.status_var).pack(side=tk.RIGHT)

    def _mode_changed(self):
        # Both：兩塊都顯示；Star 只顯示 Star；Cube 只顯示 Cube
        for w in (self.sf_frame, self.cube_frame):
            w.pack_forget()
        if self.mode_var.get() == "star":
            self.sf_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)
        elif self.mode_var.get() == "cube":
            self.cube_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)
        else:
            self.sf_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)
            self.cube_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)

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

    def _on_start(self):
        # 收集輸入
        tokens: List[str] = []
        if self.item_ids_var.get().strip():
            tokens += [x.strip() for x in self.item_ids_var.get().split(",") if x.strip()]
        if self.item_ids_file_var.get().strip():
            p = Path(self.item_ids_file_var.get().strip())
            if p.exists():
                for line in p.read_text(encoding="utf-8").splitlines():
                    t = line.strip()
                    if t:
                        tokens.append(t)
        # 去重保序
        seen = set(); uniq: List[str] = []
        for x in tokens:
            if x not in seen:
                uniq.append(x); seen.add(x)
        if not uniq:
            messagebox.showwarning("輸入不足", "請至少提供一個 名稱或ID。"); return

        # 基本參數
        mode = self.mode_var.get()
        timeframe = self.tf_var.get()
        headless = self.headless_var.get()
        delay = max(0.2, float(self.delay_var.get()))
        out_dir = Path(self.out_dir_var.get()); out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_name = f"{self.csv_base_var.get().strip() or 'msu_dynamic_pricing'}_{mode}_{ts}.csv"
        csv_path = str(out_dir / csv_name)
        db_path = str(out_dir / (self.db_name_var.get().strip() or 'msu_dynamic_pricing.sqlite'))

        # 高級參數
        block_trackers = self.block_trackers_var.get()
        debug_shots = self.debug_shots_var.get()
        debug_dir = str(out_dir / "screenshots")
        max_read = int(self.max_read_var.get())
        reload_on = int(self.reload_on_var.get())
        settle_ms = int(self.settle_ms_var.get())
        warmup = self.warmup_var.get()

        # Star/Cube 參數
        if self.auto_star_var.get():
            from_star, to_star = 0, 0  # 會被 auto_star 覆蓋
            auto_star = True
        else:
            from_star = int(self.sf_from.get())
            to_star = int(self.sf_to.get())
            auto_star = False

        if mode in ("cube","both"):
            chosen: List[str] = []
            if self.cube_red.get():   chosen.append(CUBE_PRESETS["red"])
            if self.cube_black.get(): chosen.append(CUBE_PRESETS["black"])
            if self.cube_bonus.get(): chosen.append(CUBE_PRESETS["bonus"])
            if not chosen:
                messagebox.showwarning("輸入不足", "請至少勾選一種 Cube。"); return
            cube_subtypes = chosen
        else:
            cube_subtypes = None

        # 執行
        self._toggle_running(True); self._clear_log()
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
            debug_screens=debug_shots,
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

    def _on_stop(self):
        messagebox.showinfo("停止說明", "目前為批次抓取，不支援立即中斷；請等本輪完成或關閉視窗。")

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
            self._toggle_running(False)

    def _toggle_running(self, running: bool):
        self.btn_start.config(state=("disabled" if running else "normal"))
        self.btn_stop.config(state=("normal" if running else "disabled"))
        self.status_var = tk.StringVar(value=("Running…" if running else "Ready."))

    # Log
    def _clear_log(self):
        self.log_text.configure(state="normal"); self.log_text.delete("1.0", "end"); self.log_text.configure(state="disabled")
    def _log(self, s: str):
        self.log_text.configure(state="normal"); self.log_text.insert("end", s); self.log_text.see("end"); self.log_text.configure(state="disabled")
    def _poll_log_queue(self):
        try:
            while True:
                s = self.log_queue.get_nowait()
                self._log(s)
        except queue.Empty:
            pass
        self.after(80, self._poll_log_queue)

if __name__ == "__main__":
    app = App(); app.mainloop()
