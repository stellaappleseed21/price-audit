"""
Step 5 analysis: Exp 1 (ZIP income tier x device).

  1. Regression: log price ~ low income + mobile | product x slot fixed effects, two-way clustered by product + session
     Also: + rival store within 20 miles; + store fixed effects (if store_id is available)
  2. Permutation test: shuffle session labels within slots (device within slot x ZIP); statistic = sum over cells of the "mean price-rank difference"; 10,000 draws
  3. Estimates by product category, with Benjamini-Hochberg correction
  4. Interpretation: CI within +/-1% and large p -> "no evidence of targeted pricing at this power"

    python exp1_analysis.py [--db data/loads.sqlite] [--zipmeta data/zips/selected.csv] [--perms 10000]

zipmeta needs a zcta (or zip) column; optional columns rival_mi, store_id.
"""
import argparse
import json
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import pyfixest as pf
from statsmodels.stats.multitest import multipletests

from analysis_common import ROOT, header, load_mode
from core import load_config

CONFIG = None

warnings.filterwarnings("ignore")
BENCH = 0.10


def _relative_if_mixed_currency(v):
    """When conditions use different currencies (e.g. US site USD, Argentina site ARS), use within-page relative prices so exchange rates cancel:
    rel = log(seller price) - log(lowest airline-website price for the same flight on the same page).
    log_price in all regressions is then replaced by rel, interpreted as "does the markup over the airline website (%) differ across conditions"."""
    raw = v["price_raw"].map(lambda r: json.loads(r) if r else {})
    v = v.assign(currency=raw.map(lambda d: d.get("currency")),
                 legs=raw.map(lambda d: "|".join(d.get("legs") or [])),
                 is_air=raw.map(lambda d: bool(d.get("is_airline"))))
    cur = v[v["price_source"] == "api"].groupby("tier")["currency"].agg(lambda x: sorted(set(x.dropna())))
    if all(len(c) <= 1 for c in cur) and len({c[0] for c in cur if c}) <= 1:
        return v                                        # same currency: compare absolute prices as usual
    global RELATIVE_MODE
    RELATIVE_MODE = True
    print("!! Conditions use different currencies:", dict(cur), "\n   -> switching to prices relative to the same-page airline website price; exchange rates cancel.")
    a = v[v["price_source"] == "api"].copy()
    page = ["session_id", "ts_utc", "category", "legs"]
    ref = a[a["is_air"]].groupby(page)["log_price"].min().rename("ref_air")
    a = a.join(ref, on=page)
    a = a[a["ref_air"].notna() & ~a["is_air"]]          # keep only third-party offers with an airline-website reference
    a["log_price"] = a["log_price"] - a["ref_air"]
    return a


RELATIVE_MODE = False


def _seller(raw):
    try:
        d = json.loads(raw) if raw else {}
        return d.get("provider_name") or d.get("provider") or ""
    except Exception:
        return ""


def PERSONA_MODE(cfg):
    return bool(cfg.get("exp1", {}).get("personas"))   # external benchmark: WSJ 2012 Staples stapler $14.29 vs $15.79 ~ 10%


def prep(db, zipmeta, cfg):
    df = load_mode("exp1", db, cfg)
    if PERSONA_MODE(cfg):
        df["tier"] = df["persona"]
    else:
        tier = {z: t for t in ("low", "high") for z in cfg["zips"]["exp1"][t]}
        df["tier"] = df["zip"].map(tier)
    v = df[df["valid"] & df["tier"].notna()].copy()
    # session x product: average across rounds (unit of analysis)
    v["price_source"] = v["price_source"].fillna("")
    v["seller"] = v["price_raw"].map(_seller)
    if PERSONA_MODE(cfg):
        v = _relative_if_mixed_currency(v)
    si = (v.groupby(["slot_id", "session_id", "zip", "tier", "device", "item_id", "category",
                     "price_source", "seller"])["log_price"].mean().reset_index())
    si["mobile"] = (si["device"] == "mobile").astype(int)
    if PERSONA_MODE(cfg):
        ref = cfg["exp1"].get("reference_persona", "clean")
        for p in cfg["exp1"]["personas"]:
            if p != ref:
                si[f"p_{p}"] = (si["tier"] == p).astype(int)
        # keep only "flight x seller x slot" items seen by at least two sessions; otherwise nothing is comparable
        n = si.groupby(["item_id", "slot_id"])["session_id"].transform("nunique")
        si = si[n >= 2].copy()
        return df, si
    si["low_income"] = (si["tier"] == "low").astype(int)
    if zipmeta and Path(zipmeta).exists():
        zm = pd.read_csv(zipmeta, dtype=str)
        zcol = "zip" if "zip" in zm.columns else "zcta"
        zm = zm.rename(columns={zcol: "zip"})
        keep = ["zip"] + [c for c in ("rival_mi", "store_id") if c in zm.columns]
        si = si.merge(zm[keep], on="zip", how="left")
        if "rival_mi" in si:
            si["rival_near"] = (pd.to_numeric(si["rival_mi"]) <= 20).astype(int)
    return df, si


def fit(formula, data):
    try:
        m = pf.feols(formula, data=data, vcov={"CRV1": "item_id+session_id"})
        return m.tidy()
    except Exception as e:
        return f"Cannot estimate: {type(e).__name__}: {str(e)[:120]}"


def show(name, tidy, terms):
    print(f"\n-- {name}")
    if isinstance(tidy, str):
        print("  ", tidy); return
    for t in terms:
        if t in tidy.index:
            r = tidy.loc[t]
            print(f"  {t:<12} {r['Estimate']*100:+7.2f}%   95% CI [{r['2.5%']*100:+.2f}%, "
                  f"{r['97.5%']*100:+.2f}%]   p = {r['Pr(>|t|)']:.4f}")


def perm_test(si, label, n_perm, rng, strata=("slot_id",)):
    """Session-level permutation test.

    Within each stratum (income: slot; device: slot x ZIP) shuffle the "session" labels; a session keeps the same label
    across all products (the unit of random assignment is the session, not a single load).
    Statistic = sum over product x slot of (mean price rank of label=1 - mean price rank of label=0).
    Note: the original design shuffled independently within each product x slot, ignoring within-session correlation across products, so p-values were too small.
    """
    obs, null = 0.0, np.zeros(n_perm)
    for slot, g in si.groupby("slot_id"):
        sess = (g.drop_duplicates("session_id").sort_values(list(strata))
                [["session_id", label] + [c for c in strata if c != "slot_id"]])
        if sess[label].nunique() < 2:
            continue
        sid = sess["session_id"].tolist()
        lab = sess[label].values.astype(float)
        g = g.assign(rk=g.groupby("item_id")["log_price"].rank())
        R = g.pivot_table(index="item_id", columns="session_id", values="rk").reindex(columns=sid)
        M = R.notna().values.astype(float); R0 = np.nan_to_num(R.values)
        blocks = [np.flatnonzero((sess[list(c for c in strata if c != "slot_id")] == k).all(1).values)
                  for k in (sess[[c for c in strata if c != "slot_id"]].drop_duplicates().itertuples(index=False))
                  ] if len(strata) > 1 else [np.arange(len(sid))]

        def stat(L):                        # L: (b, n_sess)
            c1 = L @ M.T; c0 = (1 - L) @ M.T
            with np.errstate(invalid="ignore", divide="ignore"):
                d = (L @ R0.T) / c1 - ((1 - L) @ R0.T) / c0
            d[(c1 == 0) | (c0 == 0)] = 0
            return d.sum(1)

        obs += stat(lab[None])[0]
        for start in range(0, n_perm, 1000):
            b = min(1000, n_perm - start)
            Lp = np.tile(lab, (b, 1))
            for blk in blocks:
                idx = np.argsort(rng.random((b, len(blk))), axis=1)
                Lp[:, blk] = lab[blk][idx]
            null[start:start + b] += stat(Lp)
    p = (np.sum(np.abs(null) >= abs(obs) - 1e-9) + 1) / (n_perm + 1)
    return obs, p


def main(db, zipmeta, n_perm):
    cfg = load_config(CONFIG) if CONFIG else load_config()
    df, si = prep(db, zipmeta, cfg)
    rng = np.random.default_rng(0)
    header("Overview")
    print(f"{df['session_id'].nunique()} sessions, {len(df)} loads, block rate {df['blocked'].mean():.1%}")
    print("Sessions per cell (income tier x device):")
    print(si.groupby(["tier", "device"])["session_id"].nunique().unstack().to_string())
    zc = df.groupby("zip")["zip_confirmed"].mean()
    if zc.notna().any() and (zc < 0.95).any():
        print("!! zip_confirmed is below 95% for these ZIPs; check the ZIP setup first:\n", zc[zc < 0.95].to_string())

    if PERSONA_MODE(cfg):
        return main_persona(cfg, df, si, n_perm, rng)

    header("1. Regressions (coefficient x100 ~ percent difference)")
    main_t = fit("log_price ~ low_income + mobile | item_id^slot_id", si)
    show("Main model: product x slot FE, two-way clustered by product + session", main_t, ["low_income", "mobile"])
    show("With interaction", fit("log_price ~ low_income * mobile | item_id^slot_id", si),
         ["low_income", "mobile", "low_income:mobile"])
    if "rival_near" in si:
        show("With rival store distance (competition channel)",
             fit("log_price ~ low_income + mobile + rival_near | item_id^slot_id", si),
             ["low_income", "mobile", "rival_near"])
        print("   -> the change in the low_income coefficient vs. the main model is the part due to the competition channel (rival distance)")
    if "store_id" in si and si["store_id"].notna().any():
        show("With store FE (identified only from differences between ZIPs served by the same store)",
             fit("log_price ~ low_income + mobile | item_id^slot_id + store_id", si),
             ["low_income", "mobile"])
    print("\nNote: with only 6 ZIPs we cannot cluster by ZIP; we cluster two-way by product + session (within-session correlation across products), "
          "and inference on the income effect relies on the permutation test.")

    header(f"2. Permutation test ({n_perm} draws)")
    for lab, strata in (("low_income", ("slot_id",)), ("mobile", ("slot_id", "zip"))):
        obs, p = perm_test(si, lab, n_perm, rng, strata)
        print(f"  {lab:<12} statistic = {obs:+.2f}   permutation p = {p:.4f}")

    header("3. Estimates by category + BH correction")
    rows = []
    for cat, g in si.groupby("category"):
        t = fit("log_price ~ low_income + mobile | item_id^slot_id", g)
        if isinstance(t, str):
            continue
        for term in ("low_income", "mobile"):
            if term in t.index:
                rows.append(dict(category=cat, term=term, est_pct=t.loc[term, "Estimate"] * 100,
                                 lo=t.loc[term, "2.5%"] * 100, hi=t.loc[term, "97.5%"] * 100,
                                 p=t.loc[term, "Pr(>|t|)"]))
    if rows:
        res = pd.DataFrame(rows)
        res["q_BH"] = np.nan
        for term, idx in res.groupby("term").groups.items():
            res.loc[idx, "q_BH"] = multipletests(res.loc[idx, "p"], method="fdr_bh")[1]
        print(res.round(4).to_string(index=False))
        res.to_csv(ROOT / "data" / "exp1_by_category.csv", index=False)

    header("4. Interpretation")
    if not isinstance(main_t, str) and "low_income" in main_t.index:
        r = main_t.loc["low_income"]
        lo, hi, p = r["2.5%"], r["97.5%"], r["Pr(>|t|)"]
        if lo > -0.01 and hi < 0.01 and p > 0.10:
            print("CI within +/-1% and p not significant: write \"no evidence of targeted pricing by ZIP income at this power\", "
                  "not \"there is no targeted pricing\".")
        elif p <= 0.05:
            print(f"Income-tier effect is significant: {r['Estimate']*100:+.2f}%. Check in part 1 whether it shrinks after adding rival distance, "
                  "and compare with the permutation p-value.")
        else:
            print("Not significant, but CI wider than +/-1%: underpowered; report the CI.")
        excl = (hi < BENCH) and (lo > -BENCH)
        print(f"Compared with the ~10% gap in WSJ 2012: an income effect that large {'can be ruled out' if excl else 'cannot be ruled out'}"
              f" (CI [{lo*100:+.2f}%, {hi*100:+.2f}%]).")


def main_persona(cfg, df, si_all, n_perm, rng):
    """Kayak: conditions (persona / language-region) relative to the reference group x device.

    Two comparisons are reported separately:
      provider_list = expanded seller list: same flight, same seller (cleanest; tests "does the same seller price by user")
      card          = lowest price on the result card: same flight (tests "the price consumers actually see", including seller selection)
    """
    srcs = [x for x in ("api", "provider_list", "card") if (si_all["price_source"] == x).any()] or [""]
    for src in srcs:
        si = si_all[si_all["price_source"] == src] if src else si_all
        label = {"api": "same flight x same seller x same fare class (poll API)",
                 "provider_list": "same flight x same seller (expanded seller list)",
                 "card": "lowest card price for the same flight (price consumers see)"}.get(src, "")
        if RELATIVE_MODE:
            label += " (relative price = seller price / same-page airline price; coefficient = difference in markup over the airline)"
        header(f"### {label}  [{len(si)} session x item]")
        _persona_block(cfg, si, n_perm, rng)


def _persona_block(cfg, si, n_perm, rng):
    terms = [c for c in si.columns if c.startswith("p_")] + ["mobile"]
    rhs = " + ".join(terms)
    header("1. Regressions (relative to the clean persona, coefficient x100 ~ percent difference)")
    main_t = fit(f"log_price ~ {rhs} | item_id^slot_id", si)
    show("Main model: flight x seller x slot FE, two-way clustered by flight + session", main_t, terms)
    pt = [t for t in terms if t != "mobile"]
    it = fit(f"log_price ~ ({' + '.join(pt)}) * mobile | item_id^slot_id", si)
    show("With persona x device interaction", it, list(it.index) if not isinstance(it, str) else [])

    header(f"2. Permutation test ({n_perm} draws)")
    ref = cfg["exp1"].get("reference_persona", "clean")
    for t in terms:
        if t == "mobile":
            obs, p = perm_test(si, "mobile", n_perm, rng, ("slot_id", "tier"))
        else:
            sub = si[si["tier"].isin([ref, t[2:]])]
            obs, p = perm_test(sub, t, n_perm, rng, ("slot_id",))
        print(f"  {t:<12} statistic = {obs:+.2f}   permutation p = {p:.4f}")

    header("3. Estimates by route + BH correction")
    rows = []
    for cat, g in si.groupby("category"):
        tt = fit(f"log_price ~ {rhs} | item_id^slot_id", g)
        if isinstance(tt, str):
            continue
        for term in terms:
            if term in tt.index:
                rows.append(dict(route=cat, term=term, est_pct=tt.loc[term, "Estimate"] * 100,
                                 lo=tt.loc[term, "2.5%"] * 100, hi=tt.loc[term, "97.5%"] * 100,
                                 p=tt.loc[term, "Pr(>|t|)"]))
    if rows:
        res = pd.DataFrame(rows)
        res["q_BH"] = np.nan
        for term, idx in res.groupby("term").groups.items():
            res.loc[idx, "q_BH"] = multipletests(res.loc[idx, "p"], method="fdr_bh")[1]
        print(res.round(4).to_string(index=False))
        out = ROOT / "data"; out.mkdir(exist_ok=True)
        res.to_csv(out / "exp1_by_route.csv", index=False)

    if (si["seller"] != "").any() and si["seller"].nunique() > 1:
        header("3b. Estimates by seller + BH correction (which sellers change price by condition)")
        rows = []
        for sel, g in si.groupby("seller"):
            if g["item_id"].nunique() < 3 or g["session_id"].nunique() < 4:
                continue
            tt = fit(f"log_price ~ {rhs} | item_id^slot_id", g)
            if isinstance(tt, str):
                continue
            for term in terms:
                if term in tt.index and not np.isnan(tt.loc[term, "Pr(>|t|)"]):
                    rows.append(dict(seller=sel, term=term, n=len(g),
                                     est_pct=tt.loc[term, "Estimate"] * 100, p=tt.loc[term, "Pr(>|t|)"]))
        if rows:
            res = pd.DataFrame(rows)
            res["q_BH"] = np.nan
            for term, idx in res.groupby("term").groups.items():
                res.loc[idx, "q_BH"] = multipletests(res.loc[idx, "p"], method="fdr_bh")[1]
            res = res.sort_values(["term", "q_BH"])
            print(res.round(4).to_string(index=False))
            out = ROOT / "data"; out.mkdir(exist_ok=True)
            res.to_csv(out / "exp1_by_seller.csv", index=False)
        else:
            print("  (seller prices identical across conditions, or too few observations)")

    header("4. Interpretation")
    if not isinstance(main_t, str):
        for t in terms:
            if t not in main_t.index:
                continue
            r = main_t.loc[t]
            lo, hi, p = r["2.5%"], r["97.5%"], r["Pr(>|t|)"]
            if lo > -0.01 and hi < 0.01 and p > 0.10:
                msg = "CI within +/-1% and not significant: no evidence of differential pricing at this power"
            elif p <= 0.05:
                msg = f"Significant: {r['Estimate']*100:+.2f}% (confirm with the permutation p-value)"
            else:
                msg = "Not significant, CI wider than +/-1%: underpowered"
            print(f"  {t:<12} {msg}")
        print("  Compare Karan et al. 2023: they found the incognito (clean) persona got the lowest prices on average, "
              "i.e. p_budget / p_luxury are expected to be positive.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--zipmeta", default=str(ROOT / "data" / "zips" / "selected.csv"))
    ap.add_argument("--perms", type=int, default=10000)
    ap.add_argument("--config", default=None)
    a = ap.parse_args()
    CONFIG = a.config
    main(a.db, a.zipmeta, a.perms)
