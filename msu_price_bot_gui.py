# msu_price_bot_gui.py
# -*- coding: utf-8 -*-

import sys
import os
import threading
import queue
import io
from datetime import datetime
from pathlib import Path
from typing import List, Optional   # ✅ 3.9 相容

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# 依賴你的爬蟲主程式
from msu_dynamic_pricing_scraper import run_batch, CUBE_PRESETS

APP_TITLE = "MSU Dynamic Pricing Bot (GUI)"
DEFAULT_DB = "msu_dynamic_pricing.sqlite"

# --- 未來擴充：名稱→ID 解析骨架（目前佔位） ---
class ItemResolver:
    """
    預留：之後接 2/3 項需求
    - 本地 items_index.json 做 name->id 對照
    - 或呼叫 Navigator 搜尋取得 id 與星數上限
    目前僅回傳空結果，讓 GUI 結構完整。
    """
    def __init__(self, index_json_path: Optional[Path] = None):  # ✅ 用 Optional[Path]
        self.index_path = index_json_path

    def resolve_names(self, names: List[str]) -> List[str]:      # ✅ 用 List[str]
        # TODO: 未來實作 名稱→ID
        return []

    def detect_star_range(self, item_id: str) -> Optional[tuple]:
        # TODO: 未來實作 自動偵測星數
        return None

# --- 將 stdout 導向 GUI 的 logger ---
class GuiLogger(io.TextIOBase):
    def __init__(self, text_widget: tk.Text, queue_obj: queue.Queue):
        self.text = text_widget
        self.queue = queue_obj

    def write(self, s):
        if s:
            self.queue.put(s)

    def flush(self):
        pass

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("920x720")
        self.minsize(880, 600)

        self.stop_flag = False
        self.worker_thread = None
        self.log_queue = queue.Queue()

        self._build_ui()
        self._poll_log_queue()

    def _build_ui(self):
        notebook = ttk.Notebook(self)
        notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        self.tab_run = ttk.Frame(notebook)
        self.tab_settings = ttk.Frame(notebook)
        self.tab_future = ttk.Frame(notebook)

        notebook.add(self.tab_run, text="Run")
        notebook.add(self.tab_settings, text="Settings")
        notebook.add(self.tab_future, text="(預留) Name→ID / 自動星數 / 輸出分類")

        self._build_run_tab(self.tab_run)
        self._build_settings_tab(self.tab_settings)
        self._build_future_tab(self.tab_future)
        self._build_bottom_bar()

    def _build_run_tab(self, parent):
        row1 = ttk.Frame(parent)
        row1.pack(fill=tk.X, padx=6, pady=6)

        self.mode_var = tk.StringVar(value="star")
        ttk.Label(row1, text="Mode:").pack(side=tk.LEFT)
        ttk.Radiobutton(row1, text="Star Force", variable=self.mode_var, value="star", command=self._mode_changed).pack(side=tk.LEFT, padx=(4,8))
        ttk.Radiobutton(row1, text="Potential (Cubes)", variable=self.mode_var, value="cube", command=self._mode_changed).pack(side=tk.LEFT)

        ttk.Label(row1, text="  Timeframe:").pack(side=tk.LEFT, padx=(12,4))
        self.tf_var = tk.StringVar(value="20m")
        ttk.Combobox(row1, textvariable=self.tf_var, values=["20m", "1H", "1D", "1W", "1M"], width=6, state="readonly").pack(side=tk.LEFT)

        self.headless_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row1, text="Headless（穩定後再勾）", variable=self.headless_var).pack(side=tk.LEFT, padx=(12,0))

        row2 = ttk.Frame(parent)
        row2.pack(fill=tk.X, padx=6, pady=6)

        # Star Force 區塊
        self.sf_frame = ttk.Frame(row2)
        self.sf_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(self.sf_frame, text="Star Range (from → to):").pack(side=tk.LEFT)
        self.sf_from = tk.IntVar(value=0)
        self.sf_to = tk.IntVar(value=19)
        ttk.Entry(self.sf_frame, textvariable=self.sf_from, width=6).pack(side=tk.LEFT, padx=(6,2))
        ttk.Label(self.sf_frame, text="→").pack(side=tk.LEFT)
        ttk.Entry(self.sf_frame, textvariable=self.sf_to, width=6).pack(side=tk.LEFT, padx=(2,10))
        ttk.Label(self.sf_frame, text="延遲(秒):").pack(side=tk.LEFT)
        self.delay_var = tk.DoubleVar(value=0.7)
        ttk.Entry(self.sf_frame, textvariable=self.delay_var, width=6).pack(side=tk.LEFT, padx=(4,0))

        # Cubes 區塊
        self.cube_frame = ttk.Frame(row2)
        self.cube_red = tk.BooleanVar(value=True)
        self.cube_black = tk.BooleanVar(value=True)
        self.cube_bonus = tk.BooleanVar(value=True)
        ttk.Label(self.cube_frame, text="Cubes:").pack(side=tk.LEFT)
        ttk.Checkbutton(self.cube_frame, text="Red (5062009)", variable=self.cube_red).pack(side=tk.LEFT, padx=6)
        ttk.Checkbutton(self.cube_frame, text="Black (5062010)", variable=self.cube_black).pack(side=tk.LEFT, padx=6)
        ttk.Checkbutton(self.cube_frame, text="Bonus (5062500)", variable=self.cube_bonus).pack(side=tk.LEFT, padx=6)

        self.cube_frame.pack_forget()  # 預設顯示 Star Force

        row3 = ttk.LabelFrame(parent, text="Items")
        row3.pack(fill=tk.X, padx=6, pady=6)

        ttk.Label(row3, text="Item IDs（逗號分隔）:").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.item_ids_var = tk.StringVar(value="1032136")
        ttk.Entry(row3, textvariable=self.item_ids_var, width=60).grid(row=0, column=1, sticky="we", padx=6, pady=4, columnspan=3)

        ttk.Label(row3, text="或 Item IDs 檔:").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        self.item_ids_file_var = tk.StringVar()
        ttk.Entry(row3, textvariable=self.item_ids_file_var, width=50).grid(row=1, column=1, sticky="we", padx=6, pady=4)
        ttk.Button(row3, text="選擇檔案…", command=self._choose_item_ids_file).grid(row=1, column=2, sticky="w", padx=6, pady=4)

        row3.columnconfigure(1, weight=1)

        row4 = ttk.LabelFrame(parent, text="Output")
        row4.pack(fill=tk.X, padx=6, pady=6)

        ttk.Label(row4, text="輸出目錄:").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.out_dir_var = tk.StringVar(value=str(Path.cwd()))
        ttk.Entry(row4, textvariable=self.out_dir_var).grid(row=0, column=1, sticky="we", padx=6, pady=4)
        ttk.Button(row4, text="選擇目錄…", command=self._choose_out_dir).grid(row=0, column=2, sticky="w", padx=6, pady=4)

        ttk.Label(row4, text="CSV 檔名（自動加時間戳）:").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        self.csv_base_var = tk.StringVar(value="msu_dynamic_pricing")
        ttk.Entry(row4, textvariable=self.csv_base_var, width=32).grid(row=1, column=1, sticky="w", padx=6, pady=4)

        ttk.Label(row4, text="SQLite 檔名:").grid(row=2, column=0, sticky="w", padx=6, pady=4)
        self.db_name_var = tk.StringVar(value=DEFAULT_DB)
        ttk.Entry(row4, textvariable=self.db_name_var, width=32).grid(row=2, column=1, sticky="w", padx=6, pady=4)

        row5 = ttk.LabelFrame(parent, text="Log")
        row5.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self.log_text = tk.Text(row5, height=16, wrap="word")
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self.log_text.configure(state="disabled")

    def _build_settings_tab(self, parent):
        frm = ttk.Frame(parent)
        frm.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        ttk.Label(frm, text="其它設定").grid(row=0, column=0, sticky="w")
        self.block_trackers_var = tk.BooleanVar(value=True)
        self.debug_shots_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="封鎖常見追蹤腳本（穩定）", variable=self.block_trackers_var).grid(row=1, column=0, sticky="w", pady=4)
        ttk.Checkbutton(frm, text="截圖除錯（screenshots/）", variable=self.debug_shots_var).grid(row=2, column=0, sticky="w", pady=4)

        sep = ttk.Separator(frm, orient="horizontal")
        sep.grid(row=3, column=0, columnspan=3, sticky="we", pady=10)

        ttk.Label(frm, text="進階重試參數（通常不用調）：").grid(row=4, column=0, sticky="w", pady=(0,4))
        self.max_read_var = tk.IntVar(value=8)
        self.reload_on_var = tk.IntVar(value=4)
        self.settle_ms_var = tk.IntVar(value=600)
        self.warmup_var = tk.BooleanVar(value=True)

        row = 5
        ttk.Label(frm, text="max_read_tries").grid(row=row, column=0, sticky="w"); ttk.Entry(frm, textvariable=self.max_read_var, width=6).grid(row=row, column=1, sticky="w"); row += 1
        ttk.Label(frm, text="reload_on_try").grid(row=row, column=0, sticky="w"); ttk.Entry(frm, textvariable=self.reload_on_var, width=6).grid(row=row, column=1, sticky="w"); row += 1
        ttk.Label(frm, text="settle_ms").grid(row=row, column=0, sticky="w"); ttk.Entry(frm, textvariable=self.settle_ms_var, width=6).grid(row=row, column=1, sticky="w"); row += 1
        ttk.Checkbutton(frm, text="首段暖機（建議開啟）", variable=self.warmup_var).grid(row=row, column=0, sticky="w"); row += 1

        frm.columnconfigure(2, weight=1)

    def _build_future_tab(self, parent):
        info = tk.Text(parent, wrap="word", height=12)
        info.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
        info.insert("end",
            "這個分頁先預留三個升級位子：\n"
            "1) Item 名稱 → ID 對照（本地資料庫 / Navigator 搜尋）。\n"
            "2) 自動對應 Star Force 的星數上限（依裝備類型）。\n"
            "3) 分門別類與精簡欄位的 CSV 輸出（例如只存 star 與 close 價）。\n\n"
            "GUI 已預留接口與類別，等你確認就把解析邏輯接上。"
        )
        info.configure(state="disabled")

        frm = ttk.Frame(parent)
        frm.pack(fill=tk.X, padx=12, pady=6)

        ttk.Label(frm, text="(預留) Item Names：").grid(row=0, column=0, sticky="w")
        self.names_var = tk.StringVar()
        self.names_entry = ttk.Entry(frm, textvariable=self.names_var, width=50, state="disabled")
        self.names_entry.grid(row=0, column=1, padx=6)

        self.btn_resolve = ttk.Button(frm, text="解析名稱→ID（未實作）", state="disabled", command=self._resolve_names_stub)
        self.btn_resolve.grid(row=0, column=2, padx=6)

        ttk.Label(frm, text="(預留) 自動星數：").grid(row=1, column=0, sticky="w", pady=(8,0))
        self.auto_star_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="啟用（未實作）", variable=self.auto_star_var, state="disabled").grid(row=1, column=1, sticky="w", pady=(8,0))

    def _build_bottom_bar(self):
        bar = ttk.Frame(self)
        bar.pack(fill=tk.X, padx=8, pady=(0,8))
        self.btn_start = ttk.Button(bar, text="開始抓取", command=self._on_start)
        self.btn_start.pack(side=tk.LEFT)

        self.btn_stop = ttk.Button(bar, text="停止（關閉視窗或等待當前批次結束）", command=self._on_stop, state="disabled")
        self.btn_stop.pack(side=tk.LEFT, padx=8)

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(bar, textvariable=self.status_var).pack(side=tk.RIGHT)

    # -------- 事件處理 --------
    def _mode_changed(self):
        if self.mode_var.get() == "star":
            self.cube_frame.pack_forget()
            self.sf_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)
        else:
            self.sf_frame.pack_forget()
            self.cube_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)

    def _choose_item_ids_file(self):
        path = filedialog.askopenfilename(title="選擇 Item IDs 檔案（每行一個）", filetypes=[("Text", "*.txt;*.csv;*.list"), ("All", "*.*")])
        if path:
            self.item_ids_file_var.set(path)

    def _choose_out_dir(self):
        path = filedialog.askdirectory(title="選擇輸出目錄")
        if path:
            self.out_dir_var.set(path)

    def _resolve_names_stub(self):
        messagebox.showinfo("尚未實作", "名稱→ID 解析與自動星數將在下一步接上。\n目前 GUI 僅預留接口。")

    def _on_start(self):
        item_ids: List[str] = []
        if self.item_ids_var.get().strip():
            item_ids += [x.strip() for x in self.item_ids_var.get().split(",") if x.strip()]
        if self.item_ids_file_var.get().strip():
            p = Path(self.item_ids_file_var.get().strip())
            if p.exists():
                for line in p.read_text(encoding="utf-8").splitlines():
                    t = line.strip()
                    if t:
                        item_ids.append(t)
        seen = set(); uniq: List[str] = []
        for x in item_ids:
            if x not in seen:
                uniq.append(x); seen.add(x)
        if not uniq:
            messagebox.showwarning("輸入不足", "請至少提供一個 Item ID（或 Item IDs 檔）。")
            return

        mode = self.mode_var.get()
        timeframe = self.tf_var.get()
        headless = self.headless_var.get()
        delay = max(0.2, float(self.delay_var.get()))
        out_dir = Path(self.out_dir_var.get())
        out_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_name = f"{self.csv_base_var.get().strip() or 'msu_dynamic_pricing'}_{ts}.csv"
        csv_path = str(out_dir / csv_name)
        db_path = str(out_dir / (self.db_name_var.get().strip() or 'msu_dynamic_pricing.sqlite'))

        block_trackers = self.block_trackers_var.get()
        debug_shots = self.debug_shots_var.get()
        debug_dir = str(out_dir / "screenshots")
        max_read = int(self.max_read_var.get())
        reload_on = int(self.reload_on_var.get())
        settle_ms = int(self.settle_ms_var.get())
        warmup = self.warmup_var.get()

        if mode == "star":
            from_star = int(self.sf_from.get())
            to_star = int(self.sf_to.get())
            cube_subtypes = None
            upgrade_type = 0
        else:
            chosen: List[str] = []
            if self.cube_red.get():   chosen.append(CUBE_PRESETS["red"])
            if self.cube_black.get(): chosen.append(CUBE_PRESETS["black"])
            if self.cube_bonus.get(): chosen.append(CUBE_PRESETS["bonus"])
            if not chosen:
                messagebox.showwarning("輸入不足", "請至少勾選一種 Cube。")
                return
            upgrade_type = 1
            from_star = 0
            to_star = 0
            cube_subtypes = chosen

        self._set_running(True)
        self._clear_log()
        self._log(f"[Start] mode={mode}, timeframe={timeframe}, items={len(uniq)}, csv={csv_path}\n")

        args = dict(
            item_ids=uniq,
            upgrade_type=upgrade_type,
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
        )
        self.worker_thread = threading.Thread(target=self._worker_run, args=(args,), daemon=True)
        self.worker_thread.start()

    def _on_stop(self):
        messagebox.showinfo("停止說明", "目前抓取是批次性，不支援立即中斷。\n請等待當前批次完成，或直接關閉視窗。")

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
            self._set_running(False)

    def _set_running(self, running: bool):
        if running:
            self.btn_start.config(state="disabled")
            self.btn_stop.config(state="normal")
            self.status_var.set("Running…")
        else:
            self.btn_start.config(state="normal")
            self.btn_stop.config(state="disabled")
            self.status_var.set("Ready.")

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _log(self, text: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

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
