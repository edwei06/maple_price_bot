# pricing_engine.py
# -*- coding: utf-8 -*-
"""
Pricing Engine for MSU Dynamic Pricing data
- Star Force expected mesos cost (uses dynamic pricing per-star step & your probability table)
- Potential expected cost:
    * Main potential: Red/Black pick the cheaper EV per step
    * Bonus potential: Bonus-only
    * NEW: dual commands to set different targets for main & bonus separately
- Works with your SQLite DB (tables: dynamic_pricing, price_stats) and items_index.json

Python 3.9 compatible. No third-party deps.
"""

from __future__ import annotations
import argparse
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# -----------------------------
# Probabilities (from your table)
# Index i means step i -> i+1 (0-based stars). Values are in DECIMALS.
# Keys: succ (success), keep (stay same), drop (to i-1), boom (to 10)
# -----------------------------

def _p(v: Optional[float]) -> float:
    return 0.0 if v is None else float(v)

STAR_PROBS: List[Dict[str, float]] = [
    # 0->1  .. 24->25
    {"succ":0.9975, "keep":0.0025, "drop":0.0,    "boom":0.0},   # 0->1
    {"succ":0.9450, "keep":0.0550, "drop":0.0,    "boom":0.0},   # 1->2
    {"succ":0.8925, "keep":0.1075, "drop":0.0,    "boom":0.0},   # 2->3
    {"succ":0.8925, "keep":0.1075, "drop":0.0,    "boom":0.0},   # 3->4
    {"succ":0.8400, "keep":0.1600, "drop":0.0,    "boom":0.0},   # 4->5
    {"succ":0.7875, "keep":0.2125, "drop":0.0,    "boom":0.0},   # 5->6
    {"succ":0.7350, "keep":0.2650, "drop":0.0,    "boom":0.0},   # 6->7
    {"succ":0.6825, "keep":0.3175, "drop":0.0,    "boom":0.0},   # 7->8
    {"succ":0.6300, "keep":0.3700, "drop":0.0,    "boom":0.0},   # 8->9
    {"succ":0.5775, "keep":0.4225, "drop":0.0,    "boom":0.0},   # 9->10
    {"succ":0.5250, "keep":0.4750, "drop":0.0,    "boom":0.0},   # 10->11
    {"succ":0.4725, "keep":0.0,    "drop":0.5275, "boom":0.0},   # 11->12
    {"succ":0.4200, "keep":0.0,    "drop":0.5742, "boom":0.0058},# 12->13
    {"succ":0.3675, "keep":0.0,    "drop":0.6198, "boom":0.0126},# 13->14
    {"succ":0.3150, "keep":0.0,    "drop":0.6713, "boom":0.0137},# 14->15
    {"succ":0.3150, "keep":0.6644, "drop":0.0,    "boom":0.0206},# 15->16
    {"succ":0.3150, "keep":0.0,    "drop":0.6644, "boom":0.0206},# 16->17
    {"succ":0.3150, "keep":0.0,    "drop":0.6644, "boom":0.0206},# 17->18
    {"succ":0.3150, "keep":0.0,    "drop":0.6576, "boom":0.0274},# 18->19
    {"succ":0.3150, "keep":0.0,    "drop":0.6576, "boom":0.0274},# 19->20
    {"succ":0.3150, "keep":0.6165, "drop":0.0,    "boom":0.0685},# 20->21
    {"succ":0.3150, "keep":0.0,    "drop":0.6165, "boom":0.0685},# 21->22
    {"succ":0.0315, "keep":0.0,    "drop":0.7748, "boom":0.1937},# 22->23
    {"succ":0.0210, "keep":0.0,    "drop":0.6853, "boom":0.2937},# 23->24
    {"succ":0.0105, "keep":0.0,    "drop":0.5937, "boom":0.3958},# 24->25
]

# Potential tier-up odds (per step)
POTENTIAL_ODDS = {
    "red":   {"Epic": 0.06,   "Unique": 0.018,  "Legendary": 0.003},
    "black": {"Epic": 0.15,   "Unique": 0.035,  "Legendary": 0.01 },
    "bonus": {"Epic": 0.0476, "Unique": 0.0196, "Legendary": 0.005},
}
CUBE_SUBTYPE = {
    "red":   "5062009",
    "black": "5062010",
    "bonus": "5062500",
}
TIERS_ORDER = ["Rare", "Epic", "Unique", "Legendary"]  # 0..3

# -----------------------------
# Data access helpers
# -----------------------------

@dataclass
class StarPriceRow:
    from_star: int
    to_star: int
    last_close: Optional[float]

def _load_index(index_path: Optional[str]) -> Dict[str, Dict[str, object]]:
    """
    Returns: dict name_lower -> {"id": str, "name": str, "max_star": Optional[int]}
    """
    out: Dict[str, Dict[str, object]] = {}
    if not index_path:
        return out
    p = Path(index_path)
    if not p.exists():
        return out
    data = json.loads(p.read_text(encoding="utf-8"))
    for it in data.get("items", []):
        name = (it.get("name") or "").strip()
        iid  = str(it.get("id") or "").strip()
        ms   = it.get("max_star")
        if name and iid:
            out[name.lower()] = {"id": iid, "name": name, "max_star": ms}
    return out

def _resolve_item_id(token: str, names_mode: bool, index_path: Optional[str]) -> Tuple[str, Optional[str]]:
    if not names_mode:
        return token, None
    idx = _load_index(index_path)
    rec = idx.get(token.strip().lower())
    if not rec:
        raise ValueError(f"名稱不在索引：{token}")
    return str(rec["id"]), str(rec["name"])

def _open_conn(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(db_path)

def _fallback_latest_any_tf_for_star(conn: sqlite3.Connection, item_id: str) -> Dict[int, float]:
    cur = conn.cursor()
    cur.execute("""
        SELECT from_star, to_star, close_price, ts_utc
        FROM dynamic_pricing
        WHERE item_id=? AND upgrade_type=0 AND close_price IS NOT NULL
        ORDER BY ts_utc DESC
    """, (item_id,))
    best: Dict[int, Tuple[float, str]] = {}
    for fs, ts, close, t in cur.fetchall():
        if fs is None:
            continue
        if (fs not in best) or (t > best[fs][1]):
            best[fs] = (float(close), t)
    return {fs: v[0] for fs, v in best.items()}

def load_star_prices(conn: sqlite3.Connection, item_id: str, timeframe: str) -> Dict[int, float]:
    cur = conn.cursor()
    cur.execute("""
        SELECT from_star, to_star, last_close
        FROM price_stats
        WHERE item_id=? AND upgrade_type=0 AND timeframe=? 
    """, (item_id, timeframe))
    rows = cur.fetchall()
    out: Dict[int, float] = {}
    for fs, ts, last in rows:
        if fs is not None and last is not None:
            out[int(fs)] = float(last)
    if out:
        return out
    return _fallback_latest_any_tf_for_star(conn, item_id)

def load_cube_price(conn: sqlite3.Connection, item_id: str, subtype: str, timeframe: str) -> Optional[float]:
    cur = conn.cursor()
    cur.execute("""
        SELECT last_close 
        FROM price_stats
        WHERE item_id=? AND upgrade_type=1 AND timeframe=? AND upgrade_subtype=?
        LIMIT 1
    """, (item_id, timeframe, subtype))
    r = cur.fetchone()
    if r and r[0] is not None:
        return float(r[0])
    # fallback to latest any timeframe
    cur.execute("""
        SELECT close_price 
        FROM dynamic_pricing
        WHERE item_id=? AND upgrade_type=1 AND upgrade_subtype=? AND close_price IS NOT NULL
        ORDER BY ts_utc DESC LIMIT 1
    """, (item_id, subtype))
    r = cur.fetchone()
    return float(r[0]) if r else None

# -----------------------------
# Linear solver (small N)
# -----------------------------

def solve_linear_system(A: List[List[float]], b: List[float]) -> List[float]:
    n = len(A)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-12:
            raise ValueError("Matrix is singular or ill-conditioned.")
        if piv != col:
            M[col], M[piv] = M[piv], M[col]
        fac = M[col][col]
        for j in range(col, n+1):
            M[col][j] /= fac
        for i in range(n):
            if i == col:
                continue
            fac2 = M[i][col]
            if fac2 != 0.0:
                for j in range(col, n+1):
                    M[i][j] -= fac2 * M[col][j]
    return [M[i][n] for i in range(n)]

# -----------------------------
# Star Force expected cost
# -----------------------------

@dataclass
class StarCostResult:
    item_id: str
    start_star: int
    target_star: int
    timeframe: str
    step_costs: Dict[int, float]      # from_star -> price used
    expected_cost_from_0: float
    expected_cost_from_start: float

def expected_star_cost(
    conn: sqlite3.Connection,
    item_id: str,
    target_star: int,
    timeframe: str,
    start_star: int = 0,
) -> StarCostResult:
    T = int(target_star)
    if T < 1 or T > 25:
        raise ValueError("target_star must be within 1..25")
    prices = load_star_prices(conn, item_id, timeframe)
    missing = [s for s in range(0, T) if s not in prices]
    if missing:
        raise ValueError(f"缺少 Star Force 價格（timeframe={timeframe}）: steps {missing}")

    n = T  # states 0..T-1
    A = [[0.0 for _ in range(n)] for _ in range(n)]
    b = [0.0 for _ in range(n)]

    def P(i: int) -> Dict[str, float]:
        if i < 0 or i >= len(STAR_PROBS):
            return {"succ":0.0, "keep":0.0, "drop":0.0, "boom":0.0}
        return STAR_PROBS[i]

    for s in range(0, T):
        prob = P(s)
        succ = _p(prob.get("succ"))
        keep = _p(prob.get("keep"))
        drop = _p(prob.get("drop"))
        boom = _p(prob.get("boom"))

        A[s][s] = 1.0 - keep
        if s+1 < T:
            A[s][s+1] = -succ
        if s-1 >= 0:
            A[s][s-1] -= drop
        if 10 < T:
            A[s][10] -= boom
        b[s] = float(prices[s])

    E = solve_linear_system(A, b)
    e0 = float(E[0])
    estart = float(E[start_star]) if 0 <= start_star < T else (0.0 if start_star >= T else float("inf"))

    return StarCostResult(
        item_id=item_id,
        start_star=start_star,
        target_star=T,
        timeframe=timeframe,
        step_costs=prices,
        expected_cost_from_0=e0,
        expected_cost_from_start=estart
    )

# -----------------------------
# Potential expected cost
# -----------------------------

@dataclass
class PotentialCostResult:
    item_id: str
    timeframe: str
    target_tier: str                  # 'Epic'|'Unique'|'Legendary'
    main_cost: Optional[float]        # choose cheaper EV between red/black per step
    main_breakdown: Optional[List[Tuple[str, float, float]]]  # [(step, chosen_price, p_up)]
    bonus_cost: Optional[float]       # bonus-only EV
    bonus_breakdown: Optional[List[Tuple[str, float, float]]]

def _steps_from(start_tier: str, target_tier: str) -> List[str]:
    s = TIERS_ORDER.index(start_tier)
    t = TIERS_ORDER.index(target_tier)
    if t <= s:
        return []
    names = []
    for i in range(s, t):
        names.append(f"{TIERS_ORDER[i]}->{TIERS_ORDER[i+1]}")
    return names

def expected_potential_cost(
    conn: sqlite3.Connection,
    item_id: str,
    timeframe: str,
    target_tier: str,
    start_tier: str = "Rare",
) -> PotentialCostResult:
    target_tier = target_tier.capitalize()
    if target_tier not in ("Epic","Unique","Legendary"):
        raise ValueError("target_tier 必須是 Epic / Unique / Legendary")

    price_red   = load_cube_price(conn, item_id, CUBE_SUBTYPE["red"],   timeframe)
    price_black = load_cube_price(conn, item_id, CUBE_SUBTYPE["black"], timeframe)
    price_bonus = load_cube_price(conn, item_id, CUBE_SUBTYPE["bonus"], timeframe)

    steps = _steps_from(start_tier, target_tier)

    main_cost = None
    main_breakdown: Optional[List[Tuple[str, float, float]]] = None
    if price_red is not None and price_black is not None:
        main_cost = 0.0
        main_breakdown = []
        for step in steps:
            tier_to = step.split("->")[1]
            p_r = POTENTIAL_ODDS["red"][tier_to]
            p_b = POTENTIAL_ODDS["black"][tier_to]
            ev_r = price_red / p_r
            ev_b = price_black / p_b
            if ev_r <= ev_b:
                main_cost += ev_r
                main_breakdown.append((step, price_red, p_r))
            else:
                main_cost += ev_b
                main_breakdown.append((step, price_black, p_b))

    bonus_cost = None
    bonus_breakdown: Optional[List[Tuple[str, float, float]]] = None
    if price_bonus is not None:
        bonus_cost = 0.0
        bonus_breakdown = []
        for step in steps:
            tier_to = step.split("->")[1]
            p = POTENTIAL_ODDS["bonus"][tier_to]
            ev = price_bonus / p
            bonus_cost += ev
            bonus_breakdown.append((step, price_bonus, p))

    return PotentialCostResult(
        item_id=item_id,
        timeframe=timeframe,
        target_tier=target_tier,
        main_cost=main_cost,
        main_breakdown=main_breakdown,
        bonus_cost=bonus_cost,
        bonus_breakdown=bonus_breakdown,
    )

# NEW: dual-target potential -----------------------------------------------

@dataclass
class PotentialDualResult:
    item_id: str
    timeframe: str
    main_start_tier: str
    main_target_tier: str
    main_cost: Optional[float]
    main_breakdown: Optional[List[Tuple[str, float, float]]]
    bonus_start_tier: Optional[str]
    bonus_target_tier: Optional[str]
    bonus_cost: Optional[float]
    bonus_breakdown: Optional[List[Tuple[str, float, float]]]

def expected_potential_cost_dual(
    conn: sqlite3.Connection,
    item_id: str,
    timeframe: str,
    main_target_tier: str,
    main_start_tier: str = "Rare",
    bonus_target_tier: Optional[str] = None,
    bonus_start_tier: str = "Rare",
) -> PotentialDualResult:
    """
    Compute main & bonus potential costs with separate start/target tiers.
    - main: choose cheaper EV per step between red/black
    - bonus: bonus-only
    If bonus_target_tier is None, bonus part is skipped.
    """
    main_target_tier = main_target_tier.capitalize()
    if main_target_tier not in ("Epic","Unique","Legendary"):
        raise ValueError("main_target_tier 必須是 Epic / Unique / Legendary")

    # Prices (shared)
    price_red   = load_cube_price(conn, item_id, CUBE_SUBTYPE["red"],   timeframe)
    price_black = load_cube_price(conn, item_id, CUBE_SUBTYPE["black"], timeframe)
    price_bonus = load_cube_price(conn, item_id, CUBE_SUBTYPE["bonus"], timeframe)

    # main
    main_steps = _steps_from(main_start_tier, main_target_tier)
    main_cost = None
    main_breakdown: Optional[List[Tuple[str, float, float]]] = None
    if price_red is not None and price_black is not None:
        main_cost = 0.0
        main_breakdown = []
        for step in main_steps:
            tier_to = step.split("->")[1]
            p_r = POTENTIAL_ODDS["red"][tier_to]
            p_b = POTENTIAL_ODDS["black"][tier_to]
            ev_r = price_red / p_r
            ev_b = price_black / p_b
            if ev_r <= ev_b:
                main_cost += ev_r
                main_breakdown.append((step, price_red, p_r))
            else:
                main_cost += ev_b
                main_breakdown.append((step, price_black, p_b))

    # bonus
    bonus_cost = None
    bonus_breakdown: Optional[List[Tuple[str, float, float]]] = None
    bonus_target_norm: Optional[str] = None
    if bonus_target_tier:
        bonus_target_norm = bonus_target_tier.capitalize()
        if bonus_target_norm not in ("Epic","Unique","Legendary"):
            raise ValueError("bonus_target_tier 必須是 Epic / Unique / Legendary")
        if price_bonus is not None:
            bonus_steps = _steps_from(bonus_start_tier, bonus_target_norm)
            bonus_cost = 0.0
            bonus_breakdown = []
            for step in bonus_steps:
                tier_to = step.split("->")[1]
                p = POTENTIAL_ODDS["bonus"][tier_to]
                ev = price_bonus / p
                bonus_cost += ev
                bonus_breakdown.append((step, price_bonus, p))

    return PotentialDualResult(
        item_id=item_id,
        timeframe=timeframe,
        main_start_tier=main_start_tier,
        main_target_tier=main_target_tier,
        main_cost=main_cost,
        main_breakdown=main_breakdown,
        bonus_start_tier=bonus_start_tier if bonus_target_tier else None,
        bonus_target_tier=bonus_target_norm,
        bonus_cost=bonus_cost,
        bonus_breakdown=bonus_breakdown,
    )

# -----------------------------
# CLI
# -----------------------------

def cmd_star(args):
    item_id, item_name = _resolve_item_id(args.item, args.names_mode, args.index)
    with _open_conn(args.db) as conn:
        res = expected_star_cost(
            conn, item_id=item_id, target_star=args.target, timeframe=args.timeframe, start_star=args.start
        )
    print(f"\n[STAR] item={item_id} ({item_name or ''}) timeframe={args.timeframe}")
    print(f"target={res.target_star} start={res.start_star}")
    print(f"Expected cost from 0★ -> {res.target_star}★ : {res.expected_cost_from_0:,.0f}")
    if res.start_star > 0:
        print(f"Expected cost from {res.start_star}★ -> {res.target_star}★ : {res.expected_cost_from_start:,.0f}")
    missing_price_steps = [s for s in range(0, args.target) if s not in res.step_costs]
    if missing_price_steps:
        print(f"(WARN) missing step prices: {missing_price_steps}")

def cmd_potential(args):
    item_id, item_name = _resolve_item_id(args.item, args.names_mode, args.index)
    with _open_conn(args.db) as conn:
        res = expected_potential_cost(
            conn, item_id=item_id, timeframe=args.timeframe,
            target_tier=args.target_tier, start_tier=args.start_tier
        )
    print(f"\n[POTENTIAL] item={item_id} ({item_name or ''}) timeframe={args.timeframe}")
    print(f"from {args.start_tier} -> {args.target_tier}")
    if res.main_cost is not None:
        print(f"Main potential (Red/Black best-per-step): {res.main_cost:,.0f}")
        for (step, price, p) in (res.main_breakdown or []):
            print(f"  - {step}: price={price:.0f}, p={p*100:.2f}%  EV/step={price/p:,.0f}")
    else:
        print("Main potential: (no price for red/black)")
    if res.bonus_cost is not None:
        print(f"Bonus potential (Bonus-only): {res.bonus_cost:,.0f}")
        for (step, price, p) in (res.bonus_breakdown or []):
            print(f"  - {step}: price={price:.0f}, p={p*100:.2f}%  EV/step={price/p:,.0f}")
    else:
        print("Bonus potential: (no price for bonus)")

def cmd_potential_dual(args):
    item_id, item_name = _resolve_item_id(args.item, args.names_mode, args.index)
    with _open_conn(args.db) as conn:
        res = expected_potential_cost_dual(
            conn,
            item_id=item_id,
            timeframe=args.timeframe,
            main_target_tier=args.main_target_tier,
            main_start_tier=args.main_start_tier,
            bonus_target_tier=None if args.bonus_target_tier in (None, "none", "None") else args.bonus_target_tier,
            bonus_start_tier=args.bonus_start_tier,
        )
    print(f"\n[POTENTIAL-DUAL] item={item_id} ({item_name or ''}) timeframe={args.timeframe}")
    # main
    print(f"Main: {res.main_start_tier} -> {res.main_target_tier}")
    if res.main_cost is not None:
        print(f"  Cost: {res.main_cost:,.0f}")
        for (step, price, p) in (res.main_breakdown or []):
            print(f"    - {step}: price={price:.0f}, p={p*100:.2f}%  EV={price/p:,.0f}")
    else:
        print("  (no price for red/black)")
    # bonus
    if res.bonus_target_tier:
        print(f"Bonus: {res.bonus_start_tier} -> {res.bonus_target_tier}")
        if res.bonus_cost is not None:
            print(f"  Cost: {res.bonus_cost:,.0f}")
            for (step, price, p) in (res.bonus_breakdown or []):
                print(f"    - {step}: price={price:.0f}, p={p*100:.2f}%  EV={price/p:,.0f}")
        else:
            print("  (no price for bonus cube)")
    else:
        print("Bonus: (skipped)")

def cmd_bundle(args):
    item_id, item_name = _resolve_item_id(args.item, args.names_mode, args.index)
    with _open_conn(args.db) as conn:
        star = expected_star_cost(conn, item_id=item_id, target_star=args.target_star,
                                  timeframe=args.timeframe, start_star=args.start_star)
        pot  = expected_potential_cost(conn, item_id=item_id, timeframe=args.timeframe,
                                       target_tier=args.target_tier, start_tier=args.start_tier)
    print(f"\n[BUNDLE] item={item_id} ({item_name or ''}) timeframe={args.timeframe}")
    print(f"Star: {star.start_star}★ -> {star.target_star}★ = {star.expected_cost_from_start:,.0f} mesos")
    if pot.main_cost is not None:
        print(f"Main potential -> {args.target_tier}: {pot.main_cost:,.0f} mesos")
    if pot.bonus_cost is not None:
        print(f"Bonus potential -> {args.target_tier}: {pot.bonus_cost:,.0f} mesos")

def cmd_bundle_dual(args):
    item_id, item_name = _resolve_item_id(args.item, args.names_mode, args.index)
    with _open_conn(args.db) as conn:
        star = expected_star_cost(conn, item_id=item_id, target_star=args.target_star,
                                  timeframe=args.timeframe, start_star=args.start_star)
        potd = expected_potential_cost_dual(
            conn,
            item_id=item_id,
            timeframe=args.timeframe,
            main_target_tier=args.main_target_tier,
            main_start_tier=args.main_start_tier,
            bonus_target_tier=None if args.bonus_target_tier in (None, "none", "None") else args.bonus_target_tier,
            bonus_start_tier=args.bonus_start_tier,
        )
    print(f"\n[BUNDLE-DUAL] item={item_id} ({item_name or ''}) timeframe={args.timeframe}")
    print(f"Star: {star.start_star}★ -> {star.target_star}★ = {star.expected_cost_from_start:,.0f} mesos")
    # main
    print(f"Main: {potd.main_start_tier} -> {potd.main_target_tier}")
    if potd.main_cost is not None:
        print(f"  Cost: {potd.main_cost:,.0f}")
    else:
        print("  (no price for red/black)")
    # bonus
    if potd.bonus_target_tier:
        print(f"Bonus: {potd.bonus_start_tier} -> {potd.bonus_target_tier}")
        if potd.bonus_cost is not None:
            print(f"  Cost: {potd.bonus_cost:,.0f}")
        else:
            print("  (no price for bonus cube)")
    else:
        print("Bonus: (skipped)")

def main():
    ap = argparse.ArgumentParser(description="Pricing Engine (Star Force & Potential)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("star", help="Compute expected star cost")
    p1.add_argument("--item", required=True, help="item id or name (use --names-mode for name)")
    p1.add_argument("--names-mode", action="store_true", default=False)
    p1.add_argument("--index", default="items_index.json")
    p1.add_argument("--db", default="msu_dynamic_pricing.sqlite")
    p1.add_argument("--timeframe", default="20m", choices=["20m","1H","1D","1W","1M"])
    p1.add_argument("--target", type=int, required=True, help="target star (1..25)")
    p1.add_argument("--start", type=int, default=0, help="start star (default 0)")
    p1.set_defaults(func=cmd_star)

    p2 = sub.add_parser("potential", help="Compute expected potential tier-up cost (same target for main & bonus)")
    p2.add_argument("--item", required=True)
    p2.add_argument("--names-mode", action="store_true", default=False)
    p2.add_argument("--index", default="items_index.json")
    p2.add_argument("--db", default="msu_dynamic_pricing.sqlite")
    p2.add_argument("--timeframe", default="20m", choices=["20m","1H","1D","1W","1M"])
    p2.add_argument("--start-tier", default="Rare", choices=["Rare","Epic","Unique"])
    p2.add_argument("--target-tier", required=True, choices=["Epic","Unique","Legendary"])
    p2.set_defaults(func=cmd_potential)

    # NEW: dual potential
    p2d = sub.add_parser("potential-dual", help="Compute potential with separate targets for main & bonus")
    p2d.add_argument("--item", required=True)
    p2d.add_argument("--names-mode", action="store_true", default=False)
    p2d.add_argument("--index", default="items_index.json")
    p2d.add_argument("--db", default="msu_dynamic_pricing.sqlite")
    p2d.add_argument("--timeframe", default="20m", choices=["20m","1H","1D","1W","1M"])
    p2d.add_argument("--main-start-tier", default="Rare", choices=["Rare","Epic","Unique"])
    p2d.add_argument("--main-target-tier", required=True, choices=["Epic","Unique","Legendary"])
    p2d.add_argument("--bonus-start-tier", default="Rare", choices=["Rare","Epic","Unique"])
    p2d.add_argument("--bonus-target-tier", default="none",
                     help="Epic/Unique/Legendary or 'none' to skip bonus")
    p2d.set_defaults(func=cmd_potential_dual)

    p3 = sub.add_parser("bundle", help="Star + Potential together (single target for main/bonus)")
    p3.add_argument("--item", required=True)
    p3.add_argument("--names-mode", action="store_true", default=False)
    p3.add_argument("--index", default="items_index.json")
    p3.add_argument("--db", default="msu_dynamic_pricing.sqlite")
    p3.add_argument("--timeframe", default="20m", choices=["20m","1H","1D","1W","1M"])
    p3.add_argument("--start-star", type=int, default=0)
    p3.add_argument("--target-star", type=int, required=True)
    p3.add_argument("--start-tier", default="Rare", choices=["Rare","Epic","Unique"])
    p3.add_argument("--target-tier", required=True, choices=["Epic","Unique","Legendary"])
    p3.set_defaults(func=cmd_bundle)

    # NEW: dual bundle
    p3d = sub.add_parser("bundle-dual", help="Star + Potential (separate targets for main & bonus)")
    p3d.add_argument("--item", required=True)
    p3d.add_argument("--names-mode", action="store_true", default=False)
    p3d.add_argument("--index", default="items_index.json")
    p3d.add_argument("--db", default="msu_dynamic_pricing.sqlite")
    p3d.add_argument("--timeframe", default="20m", choices=["20m","1H","1D","1W","1M"])
    p3d.add_argument("--start-star", type=int, default=0)
    p3d.add_argument("--target-star", type=int, required=True)
    p3d.add_argument("--main-start-tier", default="Rare", choices=["Rare","Epic","Unique"])
    p3d.add_argument("--main-target-tier", required=True, choices=["Epic","Unique","Legendary"])
    p3d.add_argument("--bonus-start-tier", default="Rare", choices=["Rare","Epic","Unique"])
    p3d.add_argument("--bonus-target-tier", default="none",
                     help="Epic/Unique/Legendary or 'none' to skip bonus")
    p3d.set_defaults(func=cmd_bundle_dual)

    args = ap.parse_args()
    args.func(args)

if __name__ == "__main__":
    main()
