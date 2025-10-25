# msu_dynamic_pricing_scraper.py
# -*- coding: utf-8 -*-
"""
抓取 msu.io Navigator 物品頁「Dynamic Pricing」數據（Star Force 或 Potential/Cube）。
重點功能：
- Star Force：逐段(0→1 ... 19→20)抓 Close/Lowest/Highest/Enhancement Count
- Potential (Cubes)：支援 Red(5062009)/Black(5062010)/Bonus(5062500)
- 多裝備：一次處理多個 item_id（共用同一 Playwright 瀏覽器，省時穩定）
- 強化穩定性：多次輪詢 + 必要時 reload + 自動暖機 + 視窗內滾動
- CSV 檔案被鎖時自動寫入 fallback 檔案；同時寫入 SQLite

CLI 範例：
1) Star Force 0→19（單裝備）
   python msu_dynamic_pricing_scraper.py --upgrade-type 0 --item-id 1032136 --from-star 0 --to-star 19

2) Cubes 三種都抓（單裝備）
   python msu_dynamic_pricing_scraper.py --upgrade-type 1 --item-id 1032136 --cube-presets all

3) 多裝備 + Star Force
   python msu_dynamic_pricing_scraper.py --upgrade-type 0 --item-ids 1032136,1234567 --from-star 0 --to-star 10

4) 多裝備 + Cubes（只抓 Black + Bonus）
   python msu_dynamic_pricing_scraper.py --upgrade-type 1 --item-ids-file item_ids.txt --cube-presets black,bonus
"""
"""
1. 升級一個gui出來
2. 現在這個提供item id 的方法不夠直觀，改成可以用名字對照id來查找
3. 所以還要建立一個item name 對照 id 的資料庫，或是透過在navigator搜尋來取得id，並且要自動對應上star force的數量，而不是以使用者手動輸入的方式。
4. 儲存下來的csv檔案要分門別類，並且提高可讀性，例如不要儲存時間、link、最高最低價等不必要的資訊




"""
import os
import re
import csv
import time
import argparse
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import List, Tuple, Optional

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
    upgrade_type: int                # 0 = Star Force, 1 = Potential/Cube
    upgrade_subtype: str             # 5062009/5062010/5062500 ...（Star Force 為空字串）
    from_star: Optional[int]         # Cubes 可為 None
    to_star: Optional[int]           # Cubes 可為 None
    close_price: Optional[float]
    lowest_price: Optional[float]
    highest_price: Optional[float]
    enhancement_count: Optional[float]
    timeframe: str                   # 20m / 1H / 1D / 1W / 1M
    url: str

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
    # Star Force: upgrade_type=0, upgrade_subtype=""
    # Cubes/Potential: upgrade_type=1, upgrade_subtype in {5062009, 5062010, 5062500}
    # itemUpgrade（from_star）對於 Cubes 不是必要，但傳 0 也不影響頁面。
    if from_star is None:
        from_star = 0
    return (
        f"https://msu.io/navigator/item/{item_id}"
        f"?itemUpgrade={from_star}"
        f"&itemUpgradeSubType={upgrade_subtype or ''}"
        f"&itemUpgradeType={upgrade_type}"
    )

# ===================== DP 區塊定位與取值（強化） =====================

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
    # 先 exact，再 non-exact，找不到就回 None
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
    # Enhancement Count 通常是小整數；取非負且最小的整數值；若只有單一值也接受
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

# ===================== Robust 單段抓取 =====================

@retry(
    reraise=True,
    retry=retry_if_exception_type((PWTimeoutError,)),
    wait=wait_exponential(multiplier=0.6, min=0.6, max=6),
    stop=stop_after_attempt(4),
)
def scrape_one_interval(page, url: str, timeframe: str,
                        max_read_tries: int = 8,
                        reload_on_try: int = 4,
                        settle_ms: int = 600,
                        debug_screens: bool = False,
                        debug_dir: Optional[str] = None):
    page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    wait_dp_ready(page)

    # 切 timeframe（預設 20m；其它按鈕 1H/1D/1W/1M）
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

        # 就緒條件：至少抓到任一張卡的數值
        ready = not (
            (vals[0] is None and vals[1] is None and vals[2] is None and vals[3] is None)
        )

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

        if debug_screens and debug_dir:
            os.makedirs(debug_dir, exist_ok=True)
            page.screenshot(path=os.path.join(debug_dir, f"retry_{int(time.time()*1000)}.png"))

    return last_vals

# ===================== 儲存 =====================

def save_sqlite(db_path: str, rows: List[DPRecord]):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS dynamic_pricing (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_utc TEXT,
        item_id TEXT,
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
    """)
    cur.executemany("""
    INSERT INTO dynamic_pricing (
        ts_utc,item_id,upgrade_type,upgrade_subtype,from_star,to_star,
        close_price,lowest_price,highest_price,enhancement_count,timeframe,url
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, [(
        r.ts_utc, r.item_id, r.upgrade_type, r.upgrade_subtype, r.from_star, r.to_star,
        r.close_price, r.lowest_price, r.highest_price, r.enhancement_count, r.timeframe, r.url
    ) for r in rows])
    conn.commit()
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

# ===================== 主流程（多裝備 / 多子型態） =====================

def run_batch(
    item_ids: List[str],
    upgrade_type: int,
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
):
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

        for item_id in item_ids:
            print(f"\n===== ITEM {item_id} =====")

            if upgrade_type == 0:
                # ----- Star Force -----
                # 暖機一次，降低首段 miss
                if warmup:
                    warm_url = build_url(item_id, upgrade_type, "", star_range[0])
                    try:
                        page.goto(warm_url, wait_until="domcontentloaded", timeout=90_000)
                        wait_dp_ready(page)
                        page.get_by_text("Dynamic Pricing", exact=False).first.scroll_into_view_if_needed()
                        page.wait_for_timeout(700)
                    except Exception:
                        pass

                for fs in range(star_range[0], star_range[1] + 1):
                    url = build_url(item_id, upgrade_type, "", fs)
                    try:
                        close_v, low_v, high_v, enh_v = scrape_one_interval(
                            page, url, timeframe,
                            max_read_tries=max_read_tries,
                            reload_on_try=reload_on_try,
                            settle_ms=settle_ms,
                            debug_screens=debug_screens,
                            debug_dir=debug_dir
                        )
                    except Exception:
                        close_v = low_v = high_v = enh_v = None

                    rec = DPRecord(
                        ts_utc=datetime.now(timezone.utc).isoformat(),
                        item_id=item_id,
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
                    print(f"[{item_id}] [{fs:02d}->{fs+1:02d}] close={close_v} low={low_v} high={high_v} count={enh_v}")

                    extra = 300 if fs in (star_range[0], star_range[0]+1, 6) else 0
                    page.wait_for_timeout(delay_sec*1000 + extra)

            else:
                # ----- Potential / Cubes -----
                # 如果沒指定子型態，預設抓三種
                subtypes = cube_subtypes if cube_subtypes else list(CUBE_PRESETS.values())

                # 暖機一次（任一子型態）
                if warmup:
                    warm_url = build_url(item_id, 1, subtypes[0], None)
                    try:
                        page.goto(warm_url, wait_until="domcontentloaded", timeout=90_000)
                        wait_dp_ready(page)
                        page.get_by_text("Dynamic Pricing", exact=False).first.scroll_into_view_if_needed()
                        page.wait_for_timeout(700)
                    except Exception:
                        pass

                for st in subtypes:
                    url = build_url(item_id, 1, st, None)
                    try:
                        close_v, low_v, high_v, enh_v = scrape_one_interval(
                            page, url, timeframe,
                            max_read_tries=max_read_tries,
                            reload_on_try=reload_on_try,
                            settle_ms=settle_ms,
                            debug_screens=debug_screens,
                            debug_dir=debug_dir
                        )
                    except Exception:
                        close_v = low_v = high_v = enh_v = None

                    rec = DPRecord(
                        ts_utc=datetime.now(timezone.utc).isoformat(),
                        item_id=item_id,
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
                    preset_name = next((k for k,v in CUBE_PRESETS.items() if v == st), st)
                    print(f"[{item_id}] [cube={preset_name}] close={close_v} low={low_v} high={high_v} count={enh_v}")

                    page.wait_for_timeout(delay_sec*1000)

        context.close()
        browser.close()

    if results:
        save_sqlite(db_path, results)
        if csv_path:
            save_csv(csv_path, results)
    return results

# ===================== CLI =====================

def parse_item_ids(args) -> List[str]:
    ids: List[str] = []
    if args.item_ids:
        ids += [x.strip() for x in args.item_ids.split(",") if x.strip()]
    if args.item_ids_file and os.path.exists(args.item_ids_file):
        with open(args.item_ids_file, "r", encoding="utf-8") as f:
            for line in f:
                t = line.strip()
                if t:
                    ids.append(t)
    if not ids and args.item_id:
        ids = [args.item_id.strip()]
    if not ids:
        raise SystemExit("請至少提供一個 item_id（--item-id / --item-ids / --item-ids-file）")
    # 去重保序
    seen = set()
    uniq = []
    for i in ids:
        if i not in seen:
            uniq.append(i); seen.add(i)
    return uniq

def parse_cube_subtypes(args) -> List[str]:
    # 1) --cube-subtypes 直接給代碼（逗號）
    if args.cube_subtypes:
        return [x.strip() for x in args.cube_subtypes.split(",") if x.strip()]

    # 2) --cube-presets 用名稱
    if args.cube_presets:
        out = []
        for name in [x.strip().lower() for x in args.cube_presets.split(",") if x.strip()]:
            if name == "all":
                return list(CUBE_PRESETS.values())
            if name in CUBE_PRESETS:
                out.append(CUBE_PRESETS[name])
            else:
                print(f"[WARN] 未知 cube preset: {name}（可用 red/black/bonus/all）")
        return out

    # 預設：三種都抓
    return list(CUBE_PRESETS.values())

def main():
    ap = argparse.ArgumentParser(description="MSU Dynamic Pricing scraper (Star Force & Potential/Cubes, multi-item)")
    # 裝備 ID
    ap.add_argument("--item-id", default="1032136", help="單一 item_id（若沒提供，請用 --item-ids 或 --item-ids-file）")
    ap.add_argument("--item-ids", default=None, help="多個 item_id，以逗號分隔")
    ap.add_argument("--item-ids-file", default=None, help="多個 item_id 的檔案路徑，每行一個")

    # 模式
    ap.add_argument("--upgrade-type", type=int, default=1, choices=[0,1], help="0=Star Force, 1=Potential/Cube")

    # Star Force 範圍
    ap.add_argument("--from-star", type=int, default=0)
    ap.add_argument("--to-star", type=int, default=19)

    # Cubes 子型態
    ap.add_argument("--cube-presets", default="all", help="red,black,bonus 或 all")
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

    item_ids = parse_item_ids(args)
    cube_subtypes = parse_cube_subtypes(args) if args.upgrade_type == 1 else None

    run_batch(
        item_ids=item_ids,
        upgrade_type=args.upgrade_type,
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
    )

if __name__ == "__main__":
    main()
