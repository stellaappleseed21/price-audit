"""
Collection entry point.

    python run.py test   --zip 94704 --device desktop --n 2      # step 1c: single test load
    python run.py check  --zips 10001 10301 --n 5                # step 0: manual comparison of two ZIPs
    python run.py pilot                                          # step 3: 24-hour pilot
    python run.py exp0   [--now]                                 # step 4: --now runs one slot immediately
    python run.py exp1   [--now]                                 # step 5

Without --now, the script schedules runs from slots_local and days in the config; the computer must stay on
(or use cron to call `python run.py exp0 --now` once per slot).
"""
from __future__ import annotations

import argparse
import asyncio
import random
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from core import Session, launch, load_config, loads_today, open_db, polite_sleep


def is_search(cfg) -> bool:
    return cfg["site"].get("kind") == "search"


def targets(cfg) -> list[dict]:
    """Return items in product-page mode; queries in search mode (Kayak)."""
    return cfg["search"]["queries"] if is_search(cfg) else cfg["items"]


async def load_one(s: Session, t: dict, rnd: int):
    """Return (price summary, blocked or not) for printing."""
    if is_search(s.cfg):
        rs = await s.load_search(t, round_no=rnd)
        ok = [r.price for r in rs if r.price]
        return (f"{len(ok)} results, lowest {min(ok) if ok else None}", rs[0].block_reason, rs[0].error)
    r = await s.load_item(t, round_no=rnd)
    return (f"{r.price} src={r.source}", r.block_reason, r.error)


async def new_session(cfg, con, browser, pw, *, mode, zip_code, device, slot_id="", order=0,
                      persona=""):
    s = Session(browser, pw, cfg, con, mode=mode, zip_code=zip_code, device=device,
                slot_id=slot_id, session_order=order, persona=persona)
    await s.start()
    if persona and is_search(cfg):
        urls = cfg["search"].get("personas", {}).get(persona, [])
        await s.cultivate(urls)
    return s


def cap_reached(cfg, con) -> bool:
    if loads_today(con) >= cfg["politeness"]["daily_page_cap"]:
        print("!! Daily load cap reached; stopping.")
        return True
    return False


# ---------------------------------------------------------------- test / check
async def cmd_test(cfg, con, zip_code, device, n, mode="test", persona=""):
    pw, browser = await launch(cfg)
    s = await new_session(cfg, con, browser, pw, mode=mode, zip_code=zip_code, device=device,
                          slot_id=datetime.now().strftime("%Y-%m-%d_%H:%M"), persona=persona)
    print(f"session {s.session_id} zip={zip_code} device={device} persona={persona or '-'} "
          f"zip_confirmed={s.zip_confirmed}")
    for t in targets(cfg)[:n]:
        if is_search(cfg):
            rs = await s.load_search(t)
            print(f"  {t['id']}: {len(rs)} rows  blocked={rs[0].block_reason} err={rs[0].error}")
            for r in rs[:8]:
                print(f"      ${r.price}  {r.raw.get('provider','')!r:<18} {r.raw.get('text','')[:90]}")
        else:
            r = await s.load_item(t)
            print(f"  {t['id']:<22} price={r.price} src={r.source} status={r.status} "
                  f"blocked={r.block_reason} {r.latency_ms}ms err={r.error}")
            print(f"      candidates={r.raw}")
        await polite_sleep(cfg)
    await s.close(); await browser.close(); await pw.stop()


async def cmd_check(cfg, con, zips, n):
    for z in zips:
        await cmd_test(cfg, con, z, "desktop", n, mode="check")
    rows = con.execute(
        "SELECT item_id, zip, price FROM loads WHERE mode='check' ORDER BY ts_utc DESC LIMIT ?",
        (len(zips) * n,)).fetchall()
    table: dict = {}
    for item, z, p in rows:
        table.setdefault(item, {})[z] = p
    print("\n=== Price comparison across two ZIPs ===")
    diff = 0
    for item, d in table.items():
        vals = [d.get(z) for z in zips]
        flag = "  <-- different" if len(set(v for v in vals if v)) > 1 else ""
        diff += bool(flag)
        print(f"  {item:<22}", "  ".join(f"{z}:{d.get(z)}" for z in zips), flag)
    print(f"\n{diff}/{len(table)} products differ in price." +
          ("  -> the platform varies prices by ZIP; usable." if diff else "  -> no difference seen; try more ZIPs or another platform."))


# ---------------------------------------------------------------- pilot
async def cmd_pilot(cfg, con):
    pc = cfg["pilot"]
    items = targets(cfg)[:pc["n_items"]]
    zip_code = cfg["zips"]["pilot_zip"]
    end = datetime.now() + timedelta(hours=pc["hours"])
    pw, browser = await launch(cfg)
    sess = None
    tick = 0
    while datetime.now() < end and not cap_reached(cfg, con):
        t_start = datetime.now()
        if sess is None or sess.dead or not pc["persistent_session"]:
            if sess:
                await sess.close()
                if sess.dead:
                    print("Session blocked; cooling down 10 minutes before a new session")
                    await asyncio.sleep(600)
            sess = await new_session(cfg, con, browser, pw, mode="pilot", zip_code=zip_code,
                                     device=pc["device"], slot_id="pilot",
                                     persona=pc.get("persona", ""))
        for item in items:
            summ, blk, err = await load_one(sess, item, tick)
            print(f"[{t_start:%H:%M:%S}] {item['id']} {summ} {blk or ''}")
        tick += 1
        wait = pc["interval_min"] * 60 - (datetime.now() - t_start).total_seconds()
        await asyncio.sleep(max(0, wait))
    if sess:
        await sess.close()
    await browser.close(); await pw.stop()


# ---------------------------------------------------------------- slot (exp0 / exp1)
async def run_session(cfg, con, browser, pw, *, mode, slot_id, order, zip_code, device,
                      delay_s, rounds, gap_min, persona=""):
    await asyncio.sleep(delay_s)
    s = None
    try:
        s = await new_session(cfg, con, browser, pw, mode=mode, zip_code=zip_code, device=device,
                              slot_id=slot_id, order=order, persona=persona)
        t0 = asyncio.get_event_loop().time()
        for rnd in range(rounds):
            if rnd > 0 and is_search(cfg) and cfg["search"].get("fresh_context_per_round", True):
                await s.restart()
            items = targets(cfg)[:]
            random.shuffle(items)
            for item in items:
                if s.dead or cap_reached(cfg, con):
                    return
                await load_one(s, item, rnd)
                await polite_sleep(cfg)
            next_start = t0 + (rnd + 1) * gap_min * 60
            if rnd < rounds - 1:
                await asyncio.sleep(max(0, next_start - asyncio.get_event_loop().time()))
    finally:
        if s:
            await s.close()
            print(f"  done {s.session_id} zip={zip_code} dev={device} persona={persona or '-'} dead={s.dead}")


async def run_slot(cfg, con, mode):
    ec = cfg[mode]
    slot_id = datetime.now().strftime("%Y-%m-%d_%H:%M")
    if mode == "exp0":
        specs = [(cfg["zips"]["exp0_zip"], ec["device"], ec.get("persona", ""))] * ec["sessions_per_slot"]
    elif ec.get("personas"):            # Kayak: persona x device x reps per cell
        specs = [(cfg["zips"]["exp0_zip"], d, p) for p in ec["personas"] for d in ec["devices"]
                 for _ in range(ec.get("reps_per_cell", 1))]
    else:
        zips = cfg["zips"]["exp1"]["low"] + cfg["zips"]["exp1"]["high"]
        specs = [(z, d, "") for z in zips for d in ec["devices"]]
    random.shuffle(specs)                                   # random start order
    n = len(specs)
    offsets = sorted(random.uniform(0, ec["launch_window_s"]) for _ in range(n))
    print(f"[{mode}] slot {slot_id}: {n} sessions")
    pw, browser = await launch(cfg)
    try:
        await asyncio.gather(*[
            run_session(cfg, con, browser, pw, mode=mode, slot_id=slot_id, order=i,
                        zip_code=z, device=d, delay_s=offsets[i], persona=p,
                        rounds=ec["rounds_per_session"], gap_min=ec["round_gap_min"])
            for i, (z, d, p) in enumerate(specs)])
    finally:
        await browser.close(); await pw.stop()


async def schedule(cfg, con, mode):
    ec = cfg[mode]
    tz = ZoneInfo(cfg["browser"]["timezone_id"])
    now = datetime.now(tz)
    times = []
    for d in range(ec["days"] + 1):
        for hhmm in ec["slots_local"]:
            h, m = map(int, hhmm.split(":"))
            t = (now + timedelta(days=d)).replace(hour=h, minute=m, second=0, microsecond=0)
            if t > now:
                times.append(t)
    times = sorted(times)[: ec["days"] * len(ec["slots_local"])]
    print(f"[{mode}] {len(times)} slots in total, first {times[0]}, last {times[-1]}")
    for t in times:
        wait = (t - datetime.now(tz)).total_seconds()
        if wait > 0:
            print(f"  waiting until {t} ...")
            await asyncio.sleep(wait)
        if cap_reached(cfg, con):
            break
        await run_slot(cfg, con, mode)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["test", "check", "pilot", "exp0", "exp1"])
    ap.add_argument("--zip", default=None)
    ap.add_argument("--zips", nargs="+", default=None)
    ap.add_argument("--device", default="desktop")
    ap.add_argument("--persona", default="")
    ap.add_argument("--n", type=int, default=2)
    ap.add_argument("--now", action="store_true", help="exp0/exp1: run one slot immediately")
    ap.add_argument("--config", default=None)
    a = ap.parse_args()
    cfg = load_config(a.config) if a.config else load_config()
    con = open_db(cfg)
    if a.cmd == "test":
        asyncio.run(cmd_test(cfg, con, a.zip or cfg["zips"]["pilot_zip"], a.device, a.n,
                             persona=a.persona))
    elif a.cmd == "check":
        if not a.zips or len(a.zips) < 2:
            sys.exit("check needs at least two --zips")
        asyncio.run(cmd_check(cfg, con, a.zips, a.n))
    elif a.cmd == "pilot":
        asyncio.run(cmd_pilot(cfg, con))
    else:
        asyncio.run(run_slot(cfg, con, a.cmd) if a.now else schedule(cfg, con, a.cmd))


if __name__ == "__main__":
    main()
