"""
Step 3 analysis: choose the sampling interval from a 24-hour, 2-minute series (Chen, Mislove & Wilson 2016).

Rule: a price change that reverts within 10 minutes counts as "transient" (likely data sync, not a real repricing).
For each candidate interval, subsample at that interval and compute the share of observed changes that are transient;
choose the shortest interval with a share < 5%.

    python pilot_analysis.py [--db data/loads.sqlite]
"""
import argparse

import numpy as np
import pandas as pd

from analysis_common import header, load_mode

REVERT_MIN = 10
THRESH = 0.05


def transient_flags(ts: np.ndarray, p: np.ndarray, idx_changes: np.ndarray, old_vals: np.ndarray):
    """For each change point i (price changed at ts[i]), check whether the full series returns to the old value within 10 minutes."""
    out = []
    for i, old in zip(idx_changes, old_vals):
        win = (ts > ts[i]) & (ts <= ts[i] + np.timedelta64(REVERT_MIN, "m"))
        out.append(bool(np.any(p[win] == old)) or p[i] == old)
    return np.array(out, dtype=bool)


def main(db):
    df = load_mode("pilot", db)
    header("Pilot overview")
    print(f"{len(df)} loads, {df['valid'].sum()} valid, {df['blocked'].sum()} blocked "
          f"({df['blocked'].mean():.1%}), {df['item_id'].nunique()} products")
    v = df[df["valid"]].sort_values("ts")
    base = v.groupby("item_id")["ts"].apply(lambda s: s.diff().median()).median()
    base_min = max(1, round(base.total_seconds() / 60))
    print(f"Actual base interval about {base_min} minutes")

    cands = [m for m in (2, 4, 6, 10, 15, 20, 25, 30, 45, 60) if m >= base_min]
    rows = []
    for m in cands:
        k = max(1, round(m / base_min))
        n_chg = n_tr = 0
        for item, g in v.groupby("item_id"):
            ts = g["ts"].values.astype("datetime64[ns]")
            p = g["price_c"].values
            sub = np.arange(0, len(g), k)
            ps = p[sub]
            ch = np.where(ps[1:] != ps[:-1])[0] + 1          # change positions in the subsample
            if len(ch) == 0:
                continue
            idx_full = sub[ch]
            old = ps[ch - 1]
            # does a "change" in the subsample revert to the old value within 10 minutes in the full series?
            fl = transient_flags(ts, p, idx_full, old)
            n_chg += len(ch); n_tr += fl.sum()
        rows.append({"interval_min": m, "changes_seen": n_chg, "transient": n_tr,
                     "transient_share": (n_tr / n_chg) if n_chg else np.nan})
    tab = pd.DataFrame(rows)
    header("Share of transient changes by sampling interval")
    print(tab.to_string(index=False))

    # full-series overview
    tot_chg = int(sum((g["price_c"].diff() != 0).sum() - 1 for _, g in v.groupby("item_id")))
    print(f"\nFull series: {max(tot_chg, 0)} price changes in total")
    ok = tab[(tab["changes_seen"] > 0) & (tab["transient_share"] < THRESH)]
    header("Recommendation")
    if tab["changes_seen"].sum() == 0:
        print("Prices did not change at all in 24 hours. The interval is not constrained by transient changes; "
              "choose by the daily load cap (a 20-minute round gap in Exp 0 is enough).")
    elif ok.empty:
        print("Every candidate interval has a transient share >= 5%, so prices themselves are very noisy. "
              "Use the longest candidate interval and start the 6 sessions in each Exp 0 slot as close together as possible.")
    else:
        print(f"Choose {int(ok['interval_min'].iloc[0])} minutes: the shortest interval with a transient share < 5%. "
              "Set it as round_gap_min in the config (use it if larger than 20 minutes).")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--db", default=None)
    main(ap.parse_args().db)
