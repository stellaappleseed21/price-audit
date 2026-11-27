"""
Step 4 analysis: Exp 0 noise baseline.

Six groups of output:
  1. Multi-price rate: share of product x slot cells with more than one price (within a round vs. across the whole slot)
  2. Number of prices and price spread
  3. Within-session stability: whether the 3 visits of one session to one product show the same price
  4. Cross-product consistency: if a session is high on one product, is it also high on others (ICC)
  5. Noise figures for power calculations: dev standard deviation, block rate, completion rate, rough MDE
  6. Correlation ceiling for calibration
Results are also saved to data/exp0_summary.json

    python exp0_analysis.py [--db data/loads.sqlite] [--reps 1000]
"""
import argparse
import json

import numpy as np
import pandas as pd

from analysis_common import ROOT, header, load_mode
from core import load_config

CONFIG = None


def icc_oneway(df: pd.DataFrame, group: str, val: str) -> float:
    g = df.groupby(group)[val]
    k = g.size().mean()
    grand = df[val].mean()
    n_groups = g.ngroups
    ssb = (g.size() * (g.mean() - grand) ** 2).sum()
    ssw = ((df[val] - g.transform("mean")) ** 2).sum()
    msb = ssb / max(n_groups - 1, 1)
    msw = ssw / max(len(df) - n_groups, 1)
    denom = msb + (k - 1) * msw
    return float((msb - msw) / denom) if denom > 0 else float("nan")


def corr_ceiling(sess_item: pd.DataFrame, reps: int, rng: np.random.Generator) -> tuple[float, float]:
    """For each product x slot, randomly draw two different sessions, correlate their deviations across cells, repeat reps times and average.

    Deviation uses dev_item = log price - the product's mean across all slots (removes only the price level).
    Note: subtracting the "product x slot mean" as in the original design mechanically forces the two sessions in a cell into
    a negative correlation of -1/(n-1) (-0.2 with 6 sessions), so nothing can be measured.
    With dev_item, the part shared by two same-slot sessions = slot-level price changes (an agent looking at the same time would match),
    and the part unique to each = random grouping (an agent cannot predict it). So the correlation ~ share of variance that is predictable.
    """
    cells = [g["dev_item"].values for _, g in sess_item.groupby(["item_id", "slot_id"]) if len(g) >= 2]
    if not cells:
        return float("nan"), float("nan")
    rs = []
    for _ in range(reps):
        x = np.empty(len(cells)); y = np.empty(len(cells))
        for j, c in enumerate(cells):
            a, b = rng.choice(len(c), 2, replace=False)
            x[j], y[j] = c[a], c[b]
        if x.std() == 0 or y.std() == 0:
            rs.append(0.0)
        else:
            rs.append(np.corrcoef(x, y)[0, 1])
    rs = np.array(rs)
    return float(rs.mean()), float(rs.std())


def main(db, reps):
    cfg = load_config(CONFIG) if CONFIG else load_config()
    df = load_mode("exp0", db, cfg)
    rng = np.random.default_rng(0)
    v = df[df["valid"]].copy()
    search = cfg["site"].get("kind") == "search"
    n_items = df["category"].nunique() if search else df["item_id"].nunique()
    planned = cfg["exp0"]["rounds_per_session"] * n_items          # planned page loads per session
    # page-load level (one load writes many rows in search mode)
    L = df.groupby(["session_id", "ts_utc"]).agg(blocked=("blocked", "max"), ok=("valid", "any")).reset_index()

    header("Overview")
    print(f"{df['slot_id'].nunique()} slots, {df['session_id'].nunique()} sessions, "
          f"{len(L)} page loads, {len(v)} price observations")
    if search:
        seen = v.groupby(["item_id", "slot_id"])["session_id"].nunique()
        print(f"Search mode: {v['item_id'].nunique()} \"flight x seller\" items in total; "
              f"{(seen >= 2).mean():.0%} were seen by >=2 sessions in the same slot (only these can be compared)")

    # 1. Multi-price rate
    cell = v.groupby(["item_id", "slot_id"])["price_c"].nunique()
    multi_slot = (cell > 1).mean()
    cell_r = v.groupby(["item_id", "slot_id", "round"])["price_c"].nunique()
    multi_round = (cell_r > 1).mean()
    header("1. Multi-price rate")
    print(f"Across the whole slot (including changes within 1 hour): {multi_slot:.1%}")
    print(f"Within a round (minutes apart, across sessions): {multi_round:.1%}   <- the main figure")

    # 2. Spread
    g = v.groupby(["item_id", "slot_id", "round"])["price_c"]
    spread = ((g.max() - g.min()) / g.min())[cell_r > 1]
    header("2. Number of prices and spread (multi-price cells only)")
    if len(spread):
        print(f"Prices per cell: mean {cell_r[cell_r > 1].mean():.2f}, max {cell_r.max()}")
        print(f"(max-min)/min: mean {spread.mean():.1%}, median {spread.median():.1%}, max {spread.max():.1%}")
        print("Multi-price rate by product:")
        print((cell_r > 1).groupby("item_id").mean().sort_values(ascending=False).round(3).to_string())
    else:
        print("No multi-price cells.")

    # 3. Within-session stability
    si_n = v.groupby(["session_id", "item_id"])["price_c"].nunique()
    stable = (si_n == 1).mean()
    header("3. Within-session stability")
    print(f"Repeated visits by one session to one product all at the same price: {stable:.1%}")

    # dev: session x product mean log price, minus the product x slot mean
    si = (v.groupby(["slot_id", "session_id", "item_id"])["log_price"].mean()
          .rename("lp").reset_index())
    si["dev"] = si["lp"] - si.groupby(["item_id", "slot_id"])["lp"].transform("mean")
    si["dev_item"] = si["lp"] - si.groupby("item_id")["lp"].transform("mean")

    # 4. Cross-product consistency
    icc = icc_oneway(si, "session_id", "dev")
    header("4. Cross-product consistency (ICC, session level)")
    print(f"ICC = {icc:.3f}  (near 0 = products grouped independently; clearly > 0 = whole session assigned to a high/low price group)")

    # 5. Noise / power
    sd_dev = float(si["dev"].std())
    blocked_rate = float(L["blocked"].mean())
    per_sess = L.groupby("session_id")["ok"].sum()
    completion = float((per_sess >= planned).mean())
    m = max(1, int(si.groupby("session_id")["item_id"].nunique().median()))
    icc0 = 0 if np.isnan(icc) else max(icc, 0)
    var_sess = sd_dev ** 2 * (icc0 + (1 - icc0) / m)
    n_arm = 120   # about 120 sessions per comparison group in Exp 1
    mde = 2.8 * np.sqrt(var_sess * 2 / n_arm * (1 - blocked_rate) ** -1)
    header("5. Figures for power calculations")
    print(f"dev standard deviation (log price): {sd_dev:.4f}  ~ {sd_dev * 100:.2f}%")
    print(f"Block rate (per load): {blocked_rate:.1%}")
    print(f"Session completion rate (all {planned} loads valid): {completion:.1%}")
    print(f"Rough MDE (Exp 1, {n_arm} sessions per group, 80% power, 5% significance): {mde * 100:.2f}%")
    print("  ^ rough estimate using session-level variance only; use simulation for formal power")

    # 6. Correlation ceiling
    rho, rho_sd = corr_ceiling(si, reps, rng)
    header("6. Correlation ceiling for calibration")
    print(f"Correlation of two same-slot sessions (after removing product price level): {rho:.3f} (SD over {reps} reps {rho_sd:.3f})")
    if np.isnan(rho):
        print("Fewer than 2 sessions per cell; cannot compute.")
    elif rho >= 0.5:
        print(f">= 0.5: variance can be reduced by about {rho**2:.0%}; calibration can be a main contribution.")
    elif rho >= 0.3:
        print(f"0.3-0.5: variance reduced by about {rho**2:.0%}; do calibration at small scale.")
    else:
        print(f"< 0.3: variance reduced by less than {max(rho, 0)**2:.0%}; use calibration only as a robustness check (test whether the human-agent mean difference is 0).")

    header("Decision")
    if multi_round < 0.02:
        print("Multi-price rate < 2%: price tests may have stopped. Run Exp 1 as planned and report a null result with confidence intervals.")
    elif multi_round <= 0.10:
        print("Multi-price rate 2-10%: run Exp 1 as originally planned.")
    else:
        print("Multi-price rate > 10%: run Exp 1, and lead the paper with the structure of price grouping, which is itself a finding.")

    summary = dict(multi_rate_round=multi_round, multi_rate_slot=multi_slot,
                   mean_spread=float(spread.mean()) if len(spread) else 0.0,
                   session_stability=stable, icc=icc, sd_dev=sd_dev,
                   blocked_rate=blocked_rate, completion=completion, rough_mde=float(mde),
                   corr_ceiling=rho)
    out = ROOT / "data" / "exp0_summary.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2))
    print(f"\nSaved {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--reps", type=int, default=1000)
    ap.add_argument("--config", default=None)
    a = ap.parse_args()
    CONFIG = a.config
    main(a.db, a.reps)
