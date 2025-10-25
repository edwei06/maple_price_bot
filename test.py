# msu_dynamic_pricing_scraper.py
# -*- coding: utf-8 -*-

import os
import re
import csv
import time
import math
import argparse
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import List, Tuple, Optional

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

# ===================== Data Model =====================

@dataclass
class DPRecord:
    ts_utc: str
    item_id: str
    upgrade_type: int          # 0 = Star Force, 1 = Potential/Cube
    upgrade_subtype: str       # e.g., 5062010 for Black Cube
    from_star: int
    to_star: int
    close_price: Optional[float]
    lowest_price: Optional[float]
    highest_price: Optional[float]
    enhancement_count: Optional[float]
    timeframe: str             # 20m / 1H / 1D / 1W / 1M
    url: str

# ===================== Utils =====================

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

def build_url(item_id: str, upgrade_type: int, upgrade_subtype: str, from_star: int) -> str:
    return (
        f"https://msu.io/navigator/item/{item_id}"
        f"?itemUpgrade={from_star}"
        f"&itemUpgradeSubType={upgrade_subtype or ''}"
        f"&itemUpgradeType={upgrade_type}"
    )

# ===================== DP Scoping & Extraction =====================

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
    # 向上找包含卡片的容器
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
    # 先 exact，再 non-exact
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
    # 保證捲到視窗內，避免尚未渲染
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

# ===================== Robust single-interval scrape =====================

@retry(
    reraise=True,
    retry=retry_if_exception_type((PWTimeoutError,)),
    wait=wait_exponential(multiplier=0.6, min=0.6, max=6),
    stop=stop_after_attempt(4),
)
def scrape_one_interval(page, url: str, timeframe: str,
                        max_read_tries: int = 6,
                        reload_on_try: int = 4,
                        settle_ms: int = 450,
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

    # 多次輪詢，必要時重載一次
    last_vals = (None, None, None, None)
    for attempt in range(1, max_read_tries + 1):
        try:
            # 確保 DP 區塊在視窗內
            page.get_by_text("Dynamic Pricing", exact=False).first.scroll_into_view_if_needed()
        except Exception:
            pass

        page.wait_for_timeout(settle_ms)
        vals = extract_four_tiles(page)

        # 簡單品質門檻：若四個都 None，或（close=None 且 low/high 皆 None），視為未就緒
        ready = not (
            (vals[0] is None and vals[1] is None and vals[2] is None and vals[3] is None) or
            (vals[0] is None and vals[1] is None and vals[2] is None)
        )

        # 若你想避免「低/高=0.0」視為未就緒，可在此加入條件；目前保留 0.0（站上短期視窗有可能為 0）
        if ready:
            last_vals = vals
            break

        last_vals = vals

        # 到指定嘗試次數，重載一次同 URL
        if attempt == reload_on_try:
            try:
                page.reload(wait_until="domcontentloaded", timeout=60_000)
                wait_dp_ready(page)
            except PWTimeoutError:
                pass

        # 需要的話拍除錯圖
        if debug_screens and debug_dir:
            os.makedirs(debug_dir, exist_ok=True)
            page.screenshot(path=os.path.join(debug_dir, f"retry_{int(time.time()*1000)}.png"))

    return last_vals

# ===================== Storage =====================

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
        # 檔案可能被 Excel/其他程式鎖定：寫到備援檔
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        alt = os.path.splitext(csv_path)[0] + f"_{ts}-fallback.csv"
        with open(alt, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=header)
            w.writeheader()
            for r in rows:
                w.writerow(asdict(r))
        print(f"[CSV] Permission denied on '{csv_path}'. Wrote to fallback '{alt}' instead.")

# ===================== Main Flow =====================

def run_scrape(
    item_id: str = "1032136",
    upgrade_type: int = 0,
    upgrade_subtype: str = "",
    star_range: Tuple[int, int] = (0, 19),
    timeframe: str = "20m",
    db_path: str = "msu_dynamic_pricing.sqlite",
    csv_path: Optional[str] = "msu_dynamic_pricing.csv",
    headless: bool = False,          # 先開視窗觀察，穩定後可改 True
    delay_sec: float = 0.7,
    block_trackers: bool = True,
    debug_screens: bool = False,
    debug_dir: str = "screenshots",
    max_read_tries: int = 6,
    reload_on_try: int = 4,
    settle_ms: int = 450,
    warmup: bool = True              # 新增：先暖機一次，降低第 1 段 miss 的機率
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

        # ---- Warmup：先打首段 URL 減少冷啟 miss ----
        if warmup:
            warm_url = build_url(item_id, upgrade_type, upgrade_subtype, star_range[0])
            try:
                page.goto(warm_url, wait_until="domcontentloaded", timeout=90_000)
                wait_dp_ready(page)
                try:
                    page.get_by_text("Dynamic Pricing", exact=False).first.scroll_into_view_if_needed()
                except Exception:
                    pass
                page.wait_for_timeout(700)
            except Exception:
                pass

        for fs in range(star_range[0], star_range[1] + 1):
            url = build_url(item_id, upgrade_type, upgrade_subtype, fs)

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
                upgrade_type=upgrade_type,
                upgrade_subtype=upgrade_subtype,
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

            print(f"[{fs:02d}->{fs+1:02d}] close={close_v} low={low_v} high={high_v} count={enh_v}")

            # 前幾段與某些段（如 6->7）常較慢，略增等待時間
            extra = 300 if fs in (star_range[0], star_range[0]+1, 6) else 0
            page.wait_for_timeout(delay_sec*1000 + extra)

        context.close()
        browser.close()

    if results:
        save_sqlite(db_path, results)
        if csv_path:
            save_csv(csv_path, results)
    return results

# ===================== CLI =====================

def main():
    ap = argparse.ArgumentParser(description="MSU Dynamic Pricing scraper")
    ap.add_argument("--item-id", default="1032136")
    ap.add_argument("--upgrade-type", type=int, default=0, help="0=Star Force, 1=Potential/Cube")
    ap.add_argument("--upgrade-subtype", default="")
    ap.add_argument("--from-star", type=int, default=0)
    ap.add_argument("--to-star", type=int, default=19)
    ap.add_argument("--timeframe", default="20m", choices=["20m", "1H", "1D", "1W", "1M"])
    ap.add_argument("--db", default="msu_dynamic_pricing.sqlite")
    ap.add_argument("--csv", default="msu_dynamic_pricing.csv")
    ap.add_argument("--headless", action="store_true", default=False)
    ap.add_argument("--delay", type=float, default=0.7)
    ap.add_argument("--no-block-trackers", action="store_true")
    ap.add_argument("--debug-shots", action="store_true")
    ap.add_argument("--debug-dir", default="screenshots")
    ap.add_argument("--max-read-tries", type=int, default=6)
    ap.add_argument("--reload-on-try", type=int, default=4)
    ap.add_argument("--settle-ms", type=int, default=450)
    ap.add_argument("--no-warmup", action="store_true")

    args = ap.parse_args()

    run_scrape(
        item_id=args.item_id,
        upgrade_type=args.upgrade_type,
        upgrade_subtype=args.upgrade_subtype,
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
