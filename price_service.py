# price_service.py
# -*- coding: utf-8 -*-

import argparse
import sqlite3
import time
from pathlib import Path
from typing import List, Optional, Tuple

from msu_dynamic_pricing_scraper import (
    run_batch, CUBE_PRESETS
)

def _open_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    return conn

def _fetch_stats_for_items(conn: sqlite3.Connection, items: List[str], names_mode: bool) -> List[Tuple]:
    """
    回傳：[(item_id,item_name,upgrade_type,upgrade_subtype,from_star,to_star,timeframe,last_ts_utc,last_close,all_time_low,all_time_high,samples)]
    若 names_mode=True，會先用 item_name 比對；否則以 item_id 比對。
    """
    cur = conn.cursor()
    if names_mode:
        q = "SELECT item_id,item_name,upgrade_type,upgrade_subtype,from_star,to_star,timeframe,last_ts_utc,last_close,all_time_low,all_time_high,samples FROM price_stats WHERE item_name IN ({}) ORDER BY upgrade_type, from_star, to_star, upgrade_subtype".format(
            ",".join("?"*len(items))
        )
        cur.execute(q, items)
    else:
        q = "SELECT item_id,item_name,upgrade_type,upgrade_subtype,from_star,to_star,timeframe,last_ts_utc,last_close,all_time_low,all_time_high,samples FROM price_stats WHERE item_id IN ({}) ORDER BY upgrade_type, from_star, to_star, upgrade_subtype".format(
            ",".join("?"*len(items))
        )
        cur.execute(q, items)
    return cur.fetchall()

def _pretty_print_stats(title: str, rows: List[Tuple]):
    print("\n=== {} ===".format(title))
    if not rows:
        print("(no data)")
        return
    # 精簡易讀的表頭
    print("{:<10}  {:<28}  {:<4} {:<6} {:<5} {:<5}  {:<8} {:<10} {:<10} {:<10} {:<7}".format(
        "ItemID", "Name", "Type", "SubTp", "From", "To", "Timeframe", "Last", "Low", "High", "N"
    ))
    for (item_id, item_name, utype, usub, f, t, tf, ts, last, low, high, n) in rows:
        print("{:<10}  {:<28}  {:<4} {:<6} {:<5} {:<5}  {:<8} {:<10} {:<10} {:<10} {:<7}".format(
            item_id, (item_name or "")[:28], utype, (usub or "")[-6:], f if f is not None else -1,
            t if t is not None else -1, tf, 
            f"{last:.0f}" if last is not None else "-", 
            f"{low:.0f}" if low is not None else "-", 
            f"{high:.0f}" if high is not None else "-", 
            n if n is not None else 0
        ))

def scrape_once(index_path: str, db_path: str, timeframe: str = "20m",
                headless: bool = True, delay: float = 0.6):
    """
    針對 index 中所有 item：同時抓 Star Force + Cubes（red/black/bonus），自動星數，更新統計。
    """
    # 直接呼叫 run_batch：names_mode=True（用名稱解析索引），mode='both'
    run_batch(
        item_ids=["__ALL__"],  # 這個值只是一個 placeholder，實際不會用到（names_mode=True 下我們仍從索引取）
        upgrade_type=0,
        cube_subtypes=list(CUBE_PRESETS.values()),
        star_range=(0, 0),  # 會被 auto_star 覆蓋
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
        names_mode=True,
        index_path=index_path,
        auto_star=True,
    )

def scrape_loop(index_path: str, db_path: str, timeframe: str = "20m",
                headless: bool = True, delay: float = 0.6, interval_mins: int = 120):
    """
    常駐模式：每 interval_mins 分鐘執行一次 scrape_once。
    """
    print(f"[LOOP] start. interval={interval_mins} min")
    while True:
        print("\n[LOOP] scrape_once begin")
        try:
            scrape_once(index_path, db_path, timeframe, headless, delay)
            print("[LOOP] scrape_once done")
        except Exception as e:
            print(f"[LOOP] ERROR: {e}")
        time.sleep(interval_mins * 60)

def query(items: List[str], db_path: str, index_path: Optional[str] = None,
          names_mode: bool = True, timeframe: str = "20m",
          refresh: bool = True, headless: bool = True):
    """
    查詢流程：
    1) 先讀統計表（快速估價：上次 last / all-time low / high）
    2) 若 refresh=True，再立即抓一次現價並更新統計
    3) 再讀一次統計表，顯示更新後數值
    """
    # BEFORE
    with _open_conn(db_path) as conn:
        snap = _fetch_stats_for_items(conn, items, names_mode)
    _pretty_print_stats("BEFORE (快取快照)", snap)

    if refresh:
        # 立即刷新：只抓目標 items
        run_batch(
            item_ids=items,
            upgrade_type=0,
            cube_subtypes=list(CUBE_PRESETS.values()),
            star_range=(0, 0),              # 會被 auto_star 覆蓋
            timeframe=timeframe,
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
            names_mode=names_mode,          # 支援用名字查
            index_path=index_path,
            auto_star=True,
        )

        with _open_conn(db_path) as conn:
            snap2 = _fetch_stats_for_items(conn, items, names_mode)
        _pretty_print_stats("AFTER (立即刷新)", snap2)

# ===================== CLI =====================

def main():
    ap = argparse.ArgumentParser(description="Price Service (scheduler & query)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("scrape-once", help="立即抓 index 中所有物品（Star+Cube）並更新統計")
    p1.add_argument("--index", default="items_index.json")
    p1.add_argument("--db", default="msu_dynamic_pricing.sqlite")
    p1.add_argument("--timeframe", default="20m")
    p1.add_argument("--headless", action="store_true", default=True)
    p1.add_argument("--delay", type=float, default=0.6)

    p2 = sub.add_parser("scrape-loop", help="每 N 分鐘抓一次（預設 120）")
    p2.add_argument("--index", default="items_index.json")
    p2.add_argument("--db", default="msu_dynamic_pricing.sqlite")
    p2.add_argument("--timeframe", default="20m")
    p2.add_argument("--headless", action="store_true", default=True)
    p2.add_argument("--delay", type=float, default=0.6)
    p2.add_argument("--interval-mins", type=int, default=120)

    p3 = sub.add_parser("query", help="查詢指定物品（先顯示舊值，再刷新後顯示新值）")
    p3.add_argument("--items", required=True, help="以逗號分隔的名稱或ID")
    p3.add_argument("--names-mode", action="store_true", default=True, help="將輸入視為『名稱』")
    p3.add_argument("--index", default="items_index.json")
    p3.add_argument("--db", default="msu_dynamic_pricing.sqlite")
    p3.add_argument("--timeframe", default="20m")
    p3.add_argument("--no-refresh", action="store_true", help="只看快照，不立即刷新")
    p3.add_argument("--headless", action="store_true", default=True)

    args = ap.parse_args()

    if args.cmd == "scrape-once":
        scrape_once(args.index, args.db, args.timeframe, args.headless, args.delay)
    elif args.cmd == "scrape-loop":
        scrape_loop(args.index, args.db, args.timeframe, args.headless, args.delay, args.interval_mins)
    elif args.cmd == "query":
        items = [x.strip() for x in args.items.split(",") if x.strip()]
        query(items, args.db, args.index, names_mode=args.names_mode,
              timeframe=args.timeframe, refresh=(not args.no_refresh), headless=args.headless)

if __name__ == "__main__":
    main()
