# msu_dynamic_pricing_scraper.py
# -*- coding: utf-8 -*-
"""
MSU Navigator Dynamic Pricing 抓取器
升級內容：
- mode: 'star' / 'cube' / 'both'（同時抓 Star Force 與 Cubes）
- 名稱↔ID↔最大星數 本地索引（items_index.json）
- 自動星數：索引優先，缺少時從物品頁解析 Max Starforce（.MaxStarforce_container__Knt02）
- 多裝備批次，單一 Playwright Context，提高穩定性/效率
"""

import os
import re
import csv
import json
import time
import argparse
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

# ===================== 常量 =====================

CUBE_PRESETS = {
    "red":   "5062009",
    "black": "5062010",
    "bonus": "5062500",
}

# ===================== 資料模型 =====================

@dataclass
class DPRecord:
    ts_utc: str
    item_id: str
    item_name: Optional[str]          # 新增：方便輸出/分類
    upgrade_type: int                 # 0 = Star Force, 1 = Potential/Cube
    upgrade_subtype: str              # 5062009/5062010/5062500 ...（Star Force 為空字串）
    from_star: Optional[int]          # Cubes 可為 None
    to_star: Optional[int]            # Cubes 可為 None
    close_price: Optional[float]
    lowest_price: Optional[float]
    highest_price: Optional[float]
    enhancement_count: Optional[float]
    timeframe: str                    # 20m / 1H / 1D / 1W / 1M
    url: str

# ===================== 名稱/ID/星數 索引 =====================

class ItemIndex:
    """
    索引 JSON 結構（建議）：
    {
      "items": [
        {"id":"1032136","name":"Will o’ the Wisps","max_star":22},
        {"id":"1234567","name":"Some Hat","max_star":20}
      ]
    }
    """
    def __init__(self, path: Optional[Path]):
        self.path = path
        self.items: Dict[str, Dict[str, Any]] = {}  # key by lower name
        self.by_id: Dict[str, Dict[str, Any]] = {}
        if path and path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                for it in data.get("items", []):
                    name = (it.get("name") or "").strip()
                    iid  = (it.get("id") or "").strip()
                    ms   = it.get("max_star")
                    if not name or not iid:
                        continue
                    rec = {"id": iid, "name": name, "max_star": ms}
                    self.items[name.lower()] = rec
                    self.by_id[iid] = rec
            except Exception:
                pass

    def resolve_name(self, name: str) -> Optional[Dict[str, Any]]:
        if not name:
            return None
        return self.items.get(name.strip().lower())

    def get_by_id(self, iid: str) -> Optional[Dict[str, Any]]:
        return self.by_id.get(iid)

    def upsert(self, iid: str, name: Optional[str], max_star: Optional[int]):
        # 以 id 為主鍵進行更新
        rec = self.by_id.get(iid, {"id": iid, "name": name or "", "max_star": None})
        if name:
            rec["name"] = name
        if max_star is not None:
            rec["max_star"] = max_star
        self.by_id[iid] = rec
        if rec["name"]:
            self.items[rec["name"].lower()] = rec

    def dump(self):
        out = {"items": sorted(self.by_id.values(), key=lambda r: (r.get("name") or "").lower())}
        if self.path:
            self.path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

# ===================== 工具函式 =====================

NUM_RE = re.compile(r"[-+]?\d{1,3}(?:,\d{3})*(?:\.\d+)?|[-+]?\d+(?:\.\d+)?")

def parse_float(text: str) -> Optional[float]:
    if not text:
        return None
    t = text.strip().replace("\u202f", "").replace("\xa0", " ")
    if t == "---":
        return None
    m = NUM_RE.search(t)
    return float(m.group(0).replace(",", "")) if m else None

def is_int_like(x: float) -> bool:
    return x is not None and abs(x - round(x)) < 1e-9

def build_url(item_id: str, upgrade_type: int, upgrade_subtype: str, from_star: Optional[int]) -> str:
    if from_star is None:
        from_star = 0
    return (
        f"https://msu.io/navigator/item/{item_id}"
        f"?itemUpgrade={from_star}"
        f"&itemUpgradeSubType={upgrade_subtype or ''}"
        f"&itemUpgradeType={upgrade_type}"
    )

# ===================== DP 定位/取值（沿用你穩定版） =====================

def wait_dp_ready(page):
    for s in ["text=Dynamic Pricing", "text=動態定價", "text=Close Price"]:
        try:
            page.locator(s).first.wait_for(state="visible", timeout=20_000)
            return
        except PWTimeoutError:
            continue
    page.wait_for_timeout(800)

def get_dp_scope(page):
    h = page.get_by_text("Dynamic Pricing", exact=False).first
    h.wait_for(timeout=15_000)
    for hop in ["..", "../..", "../../..", "../../../..", "../../../../.."]:
        scope = h.locator(f"xpath={hop}").first
        try:
            scope.get_by_text("Close Price", exact=False).first.wait_for(timeout=800)
            return scope
        except PWTimeoutError:
            continue
    return h

def collect_numbers_in_card(card) -> List[float]:
    vals: List[float] = []
    try:
        texts = card.locator(
            "xpath=.//*[not(self::script) and not(self::style) and normalize-space(text())!='']"
        ).all_text_contents()
    except Exception:
        texts = []
    for t in texts:
        v = parse_float(t)
        if v is not None:
            vals.append(v)
    return vals

def get_card_by_label(dp_scope, labels: List[str]):
    for exact in (True, False):
        for label in labels:
            try:
                node = dp_scope.get_by_text(label, exact=exact).first
                node.wait_for(timeout=1500)
                for hop in ["..", "../..", "../../.."]:
                    card = node.locator(f"xpath={hop}").first
                    try:
                        card.get_by_text(label, exact=False).first.wait_for(timeout=400)
                        return card
                    except PWTimeoutError:
                        continue
            except PWTimeoutError:
                continue
    return None

def pick_price(card_vals: List[float]) -> Optional[float]:
    return max(card_vals) if card_vals else None

def pick_count(card_vals: List[float]) -> Optional[float]:
    cands = [v for v in card_vals if v >= 0 and is_int_like(v)]
    if cands:
        return float(min(cands))
    if len(card_vals) == 1:
        return card_vals[0]
    return None

def extract_four_tiles(page):
    dp_scope = get_dp_scope(page)
    try:
        page.get_by_text("Dynamic Pricing", exact=False).first.scroll_into_view_if_needed()
        page.wait_for_timeout(150)
    except Exception:
        pass

    lbl_close = ["Close Price", "收盤價", "收盘价"]
    lbl_low   = ["Lowest Price", "最低價", "最低价"]
    lbl_high  = ["Highest Price", "最高價", "最高价"]
    lbl_cnt   = ["Enhancement Count", "強化次數", "强化次数"]

    card = get_card_by_label(dp_scope, lbl_close)
    close_v = pick_price(collect_numbers_in_card(card)) if card else None

    card = get_card_by_label(dp_scope, lbl_low)
    low_v = pick_price(collect_numbers_in_card(card)) if card else None

    card = get_card_by_label(dp_scope, lbl_high)
    high_v = pick_price(collect_numbers_in_card(card)) if card else None

    card = get_card_by_label(dp_scope, lbl_cnt)
    enh_v = pick_count(collect_numbers_in_card(card)) if card else None

    return close_v, low_v, high_v, enh_v

# ===================== 解析：單段抓取 & 最大星數偵測 =====================

@retry(
    reraise=True,
    retry=retry_if_exception_type((PWTimeoutError,)),
    wait=wait_exponential(multiplier=0.6, min=0.6, max=6),
    stop=stop_after_attempt(4),
)
def scrape_one_interval(page, url: str, timeframe: str,
                        max_read_tries: int = 8,
                        reload_on_try: int = 4,
                        settle_ms: int = 600):
    page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    wait_dp_ready(page)

    if timeframe.upper() != "20M":
        try:
            page.get_by_text(timeframe).first.click(timeout=6_000)
            page.wait_for_timeout(300)
        except PWTimeoutError:
            pass

    last_vals = (None, None, None, None)
    for attempt in range(1, max_read_tries + 1):
        try:
            page.get_by_text("Dynamic Pricing", exact=False).first.scroll_into_view_if_needed()
        except Exception:
            pass

        page.wait_for_timeout(settle_ms)
        vals = extract_four_tiles(page)
        ready = not (vals[0] is None and vals[1] is None and vals[2] is None and vals[3] is None)
        if ready:
            last_vals = vals
            break

        last_vals = vals
        if attempt == reload_on_try:
            try:
                page.reload(wait_until="domcontentloaded", timeout=60_000)
                wait_dp_ready(page)
            except PWTimeoutError:
                pass

    return last_vals

def detect_max_star_from_page(page, item_id: str) -> Optional[int]:
    """
    從物品頁解析最大星數。
    你提供的 class: "Bubble_container__pW9y1 MaxStarforce_container__Knt02"
    我們先找 '.MaxStarforce_container__' 前綴（避免哈希變化），或 'Max Starforce' 文字鄰近數字。
    """
    url = f"https://msu.io/navigator/item/{item_id}"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    except Exception:
        return None

    # 方案 A：class 前綴
    try:
        node = page.locator("[class*='MaxStarforce_container__']").first
        node.wait_for(state="visible", timeout=5000)
        txt = node.inner_text(timeout=1000)
        m = re.search(r"\d{1,2}", txt)
        if m:
            return int(m.group(0))
    except Exception:
        pass

    # 方案 B：文字錨點
    for label in ["Max Starforce", "Max Star Force", "星力上限", "最大星數", "最大星数"]:
        try:
            n = page.get_by_text(label, exact=False).first
            n.wait_for(state="visible", timeout=3000)
            t = n.evaluate("n => (n.parentElement && n.parentElement.innerText) ? n.parentElement.innerText : n.innerText")
            m = re.search(r"\d{1,2}", t or "")
            if m:
                return int(m.group(0))
        except Exception:
            continue

    return None

# ===================== 儲存 =====================
def _ensure_schema(conn: sqlite3.Connection):
    """
    檢查/建立 dynamic_pricing 資料表結構。
    若表已存在且缺欄位，動態以 ALTER TABLE 方式補欄位（不破壞舊資料）。
    """
    cur = conn.cursor()
    # 表是否存在
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='dynamic_pricing'")
    row = cur.fetchone()

    full_schema_sql = """
    CREATE TABLE IF NOT EXISTS dynamic_pricing (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_utc TEXT,
        item_id TEXT,
        item_name TEXT,
        upgrade_type INTEGER,
        upgrade_subtype TEXT,
        from_star INTEGER,
        to_star INTEGER,
        close_price REAL,
        lowest_price REAL,
        highest_price REAL,
        enhancement_count REAL,
        timeframe TEXT,
        url TEXT
    )
    """

    if not row:
        # 全新建立
        cur.execute(full_schema_sql)
        conn.commit()
        return

    # 已存在：檢查欄位，缺的就補
    cur.execute("PRAGMA table_info(dynamic_pricing)")
    existing_cols = {r[1] for r in cur.fetchall()}  # r[1] 是欄位名稱

    # 目標欄位與型別（有缺就補）
    desired_cols = [
        ("ts_utc", "TEXT"),
        ("item_id", "TEXT"),
        ("item_name", "TEXT"),
        ("upgrade_type", "INTEGER"),
        ("upgrade_subtype", "TEXT"),
        ("from_star", "INTEGER"),
        ("to_star", "INTEGER"),
        ("close_price", "REAL"),
        ("lowest_price", "REAL"),
        ("highest_price", "REAL"),
        ("enhancement_count", "REAL"),
        ("timeframe", "TEXT"),
        ("url", "TEXT"),
    ]

    for col, coltype in desired_cols:
        if col not in existing_cols:
            cur.execute(f"ALTER TABLE dynamic_pricing ADD COLUMN {col} {coltype}")

    conn.commit()

def save_sqlite(db_path: str, rows):
    """
    先確保/遷移 schema，再批次寫入資料。
    """
    if not rows:
        return
    conn = sqlite3.connect(db_path)
    try:
        _ensure_schema(conn)
        cur = conn.cursor()
        cur.executemany("""
        INSERT INTO dynamic_pricing (
            ts_utc,item_id,item_name,upgrade_type,upgrade_subtype,from_star,to_star,
            close_price,lowest_price,highest_price,enhancement_count,timeframe,url
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, [(
            r.ts_utc, r.item_id, getattr(r, "item_name", None), r.upgrade_type, r.upgrade_subtype,
            r.from_star, r.to_star, r.close_price, r.lowest_price, r.highest_price,
            r.enhancement_count, r.timeframe, r.url
        ) for r in rows])
        conn.commit()
    finally:
        conn.close()

def save_csv(csv_path: str, rows: List[DPRecord]):
    if not rows:
        return
    header = list(asdict(rows[0]).keys())
    try:
        new_file = not os.path.exists(csv_path)
        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=header)
            if new_file:
                w.writeheader()
            for r in rows:
                w.writerow(asdict(r))
        print(f"[CSV] appended to {csv_path}")
    except PermissionError:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        alt = os.path.splitext(csv_path)[0] + f"_{ts}-fallback.csv"
        with open(alt, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=header)
            w.writeheader()
            for r in rows:
                w.writerow(asdict(r))
        print(f"[CSV] Permission denied on '{csv_path}'. Wrote to fallback '{alt}' instead.")

# ===================== 主流程（支援 BOTH + 名稱/索引 + 自動星數） =====================

def run_batch(
    item_ids: List[str],                     # 這裡可傳「IDs」或「Names」（names_mode=True 時）
    upgrade_type: int,                       # 保留舊參數（0/1），若使用 mode='both' 此值會被忽略
    cube_subtypes: Optional[List[str]],
    star_range: Tuple[int, int],
    timeframe: str,
    db_path: str,
    csv_path: Optional[str],
    headless: bool,
    delay_sec: float,
    block_trackers: bool,
    debug_screens: bool,
    debug_dir: str,
    max_read_tries: int,
    reload_on_try: int,
    settle_ms: int,
    warmup: bool,
    # 新增：
    mode: str = "star",                      # 'star' | 'cube' | 'both'
    names_mode: bool = False,                # True 表示 item_ids 是名稱
    index_path: Optional[str] = None,        # items_index.json
    auto_star: bool = False,                 # True 則每件裝備用索引/頁面自動取最大星數
) -> List[DPRecord]:

    # 索引初始化
    index = ItemIndex(Path(index_path) if index_path else None)

    results: List[DPRecord] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            locale="en-US",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"),
            viewport={"width": 1440, "height": 900},
        )
        context.set_default_timeout(30_000)
        context.set_default_navigation_timeout(60_000)

        if block_trackers:
            block_list = ["googletagmanager", "google-analytics", "doubleclick", "hotjar", "segment.io"]
            def _should_block(url: str) -> bool:
                return any(b in url for b in block_list)
            context.route("**/*", lambda route: route.abort()
                          if _should_block(route.request.url) else route.continue_())

        page = context.new_page()

        # 將輸入轉成 [(id, name)]，names_mode=True 時透過索引解析
        resolved_items: List[Tuple[str, Optional[str]]] = []
        for token in item_ids:
            token = token.strip()
            if not token:
                continue
            if names_mode:
                rec = index.resolve_name(token)
                if rec:
                    resolved_items.append((rec["id"], rec.get("name")))
                else:
                    print(f"[WARN] 名稱未在索引中：{token}（可補到 items_index.json）")
            else:
                # 直接當作 ID；若索引中有名稱，帶上
                rec = index.get_by_id(token)
                resolved_items.append((token, rec.get("name") if rec else None))

        if not resolved_items:
            print("[ERROR] 沒有可處理的項目。")
            context.close(); browser.close()
            return []

        # Cubes 子型態（用於 cube / both）
        if cube_subtypes is None:
            cube_subtypes = list(CUBE_PRESETS.values())

        # 逐件裝備處理
        for item_id, item_name in resolved_items:
            print(f"\n===== ITEM {item_id} ({item_name or 'n/a'}) =====")

            # 取得最大星數（auto_star=True）
            if auto_star:
                max_star = None
                # 1) 索引有就用
                rec = index.get_by_id(item_id)
                if rec and isinstance(rec.get("max_star"), int):
                    max_star = rec["max_star"]
                # 2) 索引沒有 → 物品頁解析
                if max_star is None:
                    ms = detect_max_star_from_page(page, item_id)
                    if ms:
                        max_star = ms
                        # 回寫索引（名稱若未知，保持現狀）
                        index.upsert(item_id, item_name, max_star)
                        print(f"[AutoStar] 解析到最大星數 {max_star} 並寫入索引。")
                    else:
                        print("[AutoStar] 未能解析最大星數，改用使用者提供的 star_range。")

                # 套用 star_range
                if max_star:
                    sf_from, sf_to = 0, max_star - 1  # e.g. 22 星 → 0→21 段
                else:
                    sf_from, sf_to = star_range
            else:
                sf_from, sf_to = star_range

            # 暖機一次
            if warmup:
                warm_url = build_url(item_id, 0, "", sf_from)
                try:
                    page.goto(warm_url, wait_until="domcontentloaded", timeout=90_000)
                    wait_dp_ready(page)
                    page.get_by_text("Dynamic Pricing", exact=False).first.scroll_into_view_if_needed()
                    page.wait_for_timeout(700)
                except Exception:
                    pass

            # === Star Force ===
            if mode in ("star", "both"):
                for fs in range(sf_from, sf_to + 1):
                    url = build_url(item_id, 0, "", fs)
                    try:
                        close_v, low_v, high_v, enh_v = scrape_one_interval(
                            page, url, timeframe,
                            max_read_tries=max_read_tries,
                            reload_on_try=reload_on_try,
                            settle_ms=settle_ms,
                        )
                    except Exception:
                        close_v = low_v = high_v = enh_v = None

                    rec = DPRecord(
                        ts_utc=datetime.now(timezone.utc).isoformat(),
                        item_id=item_id,
                        item_name=item_name,
                        upgrade_type=0,
                        upgrade_subtype="",
                        from_star=fs,
                        to_star=fs + 1,
                        close_price=close_v,
                        lowest_price=low_v,
                        highest_price=high_v,
                        enhancement_count=enh_v,
                        timeframe=timeframe,
                        url=url,
                    )
                    results.append(rec)
                    print(f"[STAR] [{fs:02d}->{fs+1:02d}] close={close_v} low={low_v} high={high_v} count={enh_v}")
                    extra = 300 if fs in (sf_from, sf_from+1, 6) else 0
                    page.wait_for_timeout(int(delay_sec*1000) + extra)

            # === Cubes ===
            if mode in ("cube", "both"):
                # 暖機一個 cube
                if warmup:
                    warm_url = build_url(item_id, 1, cube_subtypes[0], None)
                    try:
                        page.goto(warm_url, wait_until="domcontentloaded", timeout=90_000)
                        wait_dp_ready(page)
                        page.get_by_text("Dynamic Pricing", exact=False).first.scroll_into_view_if_needed()
                        page.wait_for_timeout(700)
                    except Exception:
                        pass

                for st in cube_subtypes:
                    url = build_url(item_id, 1, st, None)
                    try:
                        close_v, low_v, high_v, enh_v = scrape_one_interval(
                            page, url, timeframe,
                            max_read_tries=max_read_tries,
                            reload_on_try=reload_on_try,
                            settle_ms=settle_ms,
                        )
                    except Exception:
                        close_v = low_v = high_v = enh_v = None

                    rec = DPRecord(
                        ts_utc=datetime.now(timezone.utc).isoformat(),
                        item_id=item_id,
                        item_name=item_name,
                        upgrade_type=1,
                        upgrade_subtype=st,
                        from_star=None,
                        to_star=None,
                        close_price=close_v,
                        lowest_price=low_v,
                        highest_price=high_v,
                        enhancement_count=enh_v,
                        timeframe=timeframe,
                        url=url,
                    )
                    results.append(rec)
                    name = next((k for k,v in CUBE_PRESETS.items() if v == st), st)
                    print(f"[CUBE] [{name}] close={close_v} low={low_v} high={high_v} count={enh_v}")
                    page.wait_for_timeout(int(delay_sec*1000))

        context.close()
        browser.close()

    # 寫檔
    if results:
        # 若有索引檔路徑，回存（可能新增了 max_star 或名稱）
        if index.path:
            try:
                index.dump()
                print(f"[INDEX] 已更新索引：{index.path}")
            except Exception as e:
                print(f"[INDEX] 索引寫入失敗：{e}")

        save_sqlite(db_path, results)
        if csv_path:
            save_csv(csv_path, results)
    return results

# ===================== CLI =====================

def parse_ids_or_names(args) -> List[str]:
    vals: List[str] = []
    if args.item_ids:
        vals += [x.strip() for x in args.item_ids.split(",") if x.strip()]
    if args.item_ids_file and Path(args.item_ids_file).exists():
        for line in Path(args.item_ids_file).read_text(encoding="utf-8").splitlines():
            t = line.strip()
            if t:
                vals.append(t)
    if args.item_id:
        vals.append(args.item_id.strip())
    # 去重保序
    seen = set(); out = []
    for v in vals:
        if v not in seen:
            out.append(v); seen.add(v)
    return out

def parse_cube_subtypes(args) -> List[str]:
    if args.cube_subtypes:
        return [x.strip() for x in args.cube_subtypes.split(",") if x.strip()]
    if args.cube_presets:
        out = []
        for name in [x.strip().lower() for x in args.cube_presets.split(",") if x.strip()]:
            if name == "all":
                return list(CUBE_PRESETS.values())
            if name in CUBE_PRESETS:
                out.append(CUBE_PRESETS[name])
        if out:
            return out
    return list(CUBE_PRESETS.values())

def main():
    ap = argparse.ArgumentParser(description="MSU Dynamic Pricing scraper (Star/Cube/Both + Index + AutoStar)")
    # 輸入（可用名稱或ID）
    ap.add_argument("--item-id", default=None)
    ap.add_argument("--item-ids", default=None, help="逗號分隔；可填名稱或ID")
    ap.add_argument("--item-ids-file", default=None, help="每行一個 名稱或ID")
    ap.add_argument("--names-mode", action="store_true", help="把輸入視為『名稱』而非 ID")
    ap.add_argument("--index", default="items_index.json", help="名稱/ID/星數 索引檔路徑")

    # 模式
    ap.add_argument("--mode", default="star", choices=["star","cube","both"])

    # Star Force 範圍（若 --auto-star 則忽略此參數）
    ap.add_argument("--from-star", type=int, default=0)
    ap.add_argument("--to-star", type=int, default=19)
    ap.add_argument("--auto-star", action="store_true", help="自動從索引或頁面偵測最大星數")

    # Cubes 子型態
    ap.add_argument("--cube-presets", default=None, help="red,black,bonus 或 all")
    ap.add_argument("--cube-subtypes", default=None, help="自訂代碼，逗號分隔，如 5062009,5062010")

    # 其它
    ap.add_argument("--timeframe", default="20m", choices=["20m", "1H", "1D", "1W", "1M"])
    ap.add_argument("--db", default="msu_dynamic_pricing.sqlite")
    ap.add_argument("--csv", default="msu_dynamic_pricing.csv")
    ap.add_argument("--headless", action="store_true", default=False)
    ap.add_argument("--delay", type=float, default=0.7)
    ap.add_argument("--no-block-trackers", action="store_true")
    ap.add_argument("--debug-shots", action="store_true")
    ap.add_argument("--debug-dir", default="screenshots")
    ap.add_argument("--max-read-tries", type=int, default=8)
    ap.add_argument("--reload-on-try", type=int, default=4)
    ap.add_argument("--settle-ms", type=int, default=600)
    ap.add_argument("--no-warmup", action="store_true")

    args = ap.parse_args()

    ids_or_names = parse_ids_or_names(args)
    if not ids_or_names:
        raise SystemExit("請提供 item（名稱或ID）。")

    cube_subtypes = parse_cube_subtypes(args) if args.mode in ("cube","both") else None

    run_batch(
        item_ids=ids_or_names,
        upgrade_type=0 if args.mode=="star" else (1 if args.mode=="cube" else 0),
        cube_subtypes=cube_subtypes,
        star_range=(args.from_star, args.to_star),
        timeframe=args.timeframe,
        db_path=args.db,
        csv_path=args.csv,
        headless=args.headless,
        delay_sec=args.delay,
        block_trackers=(not args.no_block_trackers),
        debug_screens=args.debug_shots,
        debug_dir=args.debug_dir,
        max_read_tries=args.max_read_tries,
        reload_on_try=args.reload_on_try,
        settle_ms=args.settle_ms,
        warmup=(not args.no_warmup),
        mode=args.mode,
        names_mode=args.names_mode,
        index_path=args.index,
        auto_star=args.auto_star,
    )

if __name__ == "__main__":
    main()
