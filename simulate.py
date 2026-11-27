"""
Generate simulated data with known true effects, to check the analysis code before real collection.

    python simulate.py                 # writes data/sim.sqlite and data/sim_zipmeta.csv
    python pilot_analysis.py --db data/sim.sqlite
    python exp0_analysis.py  --db data/sim.sqlite
    python exp1_analysis.py  --db data/sim.sqlite --zipmeta data/sim_zipmeta.csv

True settings: low income +3%, rival within 20 miles -2%, mobile 0;
in Exp 0, 30% of products run random price tests (two price levels, +/-5%).
"""
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from core import COLUMNS, ROOT, SCHEMA, load_config

rng = np.random.default_rng(42)
cfg = load_config()
ITEMS = [(f"item{i:02d}", ["office", "paper", "ink"][i % 3], 5 + 40 * rng.random()) for i in range(18)]


def row(ts, mode, slot, sess, order, rnd, zip_, dev, item, cat, price, blocked=0):
    return (uuid.uuid4().hex, ts.isoformat(timespec="milliseconds"), mode, slot, sess, order, rnd,
            zip_, 1, dev, item, cat, "sim://" + item, 200 if not blocked else 403,
            None if blocked else round(price, 2), None, "sim", json.dumps({}), blocked,
            "http_403" if blocked else None, 800, None, None, "", None)


def main():
    db = ROOT / "data" / "sim.sqlite"
    db.parent.mkdir(exist_ok=True)
    db.unlink(missing_ok=True)
    con = sqlite3.connect(db); con.executescript(SCHEMA)
    rows = []
    t0 = datetime(2026, 12, 1, 9, tzinfo=timezone.utc)

    # ---- pilot: 3 products, every 2 minutes, 24 hours; occasional transient changes that revert after 1-3 readings
    for item, cat, base in ITEMS[:3]:
        p = base
        transient_left, saved = 0, None
        for k in range(720):
            if transient_left == 0 and saved is not None:
                p, saved = saved, None
            if saved is None and rng.random() < 0.004:         # transient
                saved, p, transient_left = p, p * 1.03, rng.integers(1, 4)
            elif saved is None and rng.random() < 0.01:        # real repricing
                p = p * (1 + rng.choice([-0.05, 0.05]))
            transient_left = max(0, transient_left - 1)
            rows.append(row(t0 + timedelta(minutes=2 * k), "pilot", "pilot", "pilot-s", 0, k,
                            cfg["zips"]["pilot_zip"], "desktop", item, cat, p))

    # ---- exp0: 20 slots x 6 sessions x 18 products x 3 rounds
    tested = {it[0] for it in ITEMS if rng.random() < 0.3}
    for s in range(20):
        slot = f"slot{s:02d}"
        ts_slot = t0 + timedelta(days=1 + s // 4, hours=4 * (s % 4))
        for j in range(6):
            sess = f"exp0-{s}-{j}"
            sess_eff = 0.0
            arm = {it: rng.choice([-0.05, 0.05]) for it in tested}
            for rnd in range(3):
                for item, cat, base in ITEMS:
                    lp = np.log(base) + sess_eff + arm.get(item, 0)
                    blk = int(rng.random() < 0.02)
                    rows.append(row(ts_slot + timedelta(minutes=20 * rnd), "exp0", slot, sess, j, rnd,
                                    cfg["zips"]["exp0_zip"], "desktop", item, cat, np.exp(lp), blk))

    # ---- exp1: 20 slots x (6 ZIPs x 2 devices) x 18 products x 3 rounds
    zips = cfg["zips"]["exp1"]["low"] + cfg["zips"]["exp1"]["high"]
    rival_mi = {z: float(rng.choice([8, 30])) for z in zips}
    pd.DataFrame({"zip": zips, "rival_mi": [rival_mi[z] for z in zips]}).to_csv(
        ROOT / "data" / "sim_zipmeta.csv", index=False)
    for s in range(20):
        slot = f"slot{s:02d}"
        ts_slot = t0 + timedelta(days=10 + s // 4, hours=4 * (s % 4))
        for j, (z, dev) in enumerate([(z, d) for z in zips for d in ("desktop", "mobile")]):
            sess = f"exp1-{s}-{j}"
            low = z in cfg["zips"]["exp1"]["low"]
            eff = 0.03 * low - 0.02 * (rival_mi[z] <= 20) + rng.normal(0, 0.01)
            for rnd in range(3):
                for item, cat, base in ITEMS:
                    lp = np.log(base) + eff + rng.normal(0, 0.01)
                    blk = int(rng.random() < 0.02)
                    rows.append(row(ts_slot + timedelta(minutes=20 * rnd), "exp1", slot, sess, j, rnd,
                                    z, dev, item, cat, np.exp(lp), blk))

    con.executemany(f"INSERT INTO loads ({','.join(COLUMNS)}) VALUES ({','.join('?' * len(COLUMNS))})", rows)
    con.commit(); con.close()
    print(f"Wrote {len(rows)} rows to {db}; products with price tests: {sorted(tested)}")


if __name__ == "__main__":
    main()
