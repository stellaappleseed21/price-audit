"""
Step 2: choose Exp 1 ZIPs using ACS income + distance to rival stores.

Example (Bay Area, within 40 miles of Berkeley):
    python acs_zips.py --center 37.87 -122.27 --radius-mi 40 --osm-rivals

Outputs (under data/zips/):
    candidates.csv   all qualifying ZCTAs in the region: income, MOE, population, rival distance, income tier
    selected.csv     the 3 low-income + 3 high-income ZIPs selected
    and prints a zips.exp1 snippet that can be pasted into config.yaml

Data sources:
  - ACS 5-year tables B19013 (median household income) + B01003 (population), ZCTA level, Census API
  - Census Gazetteer ZCTA file (centroid lat/lon)
  - Rival stores: --rivals your own CSV (columns name,lat,lon), or --osm-rivals to pull from OpenStreetMap
    (OSM may miss stores or keep closed ones; verify against the Office Depot store locator)
  - Optional --crosswalk: UDS Mapper ZIP->ZCTA table (columns ZIP_CODE, zcta),
    used to exclude non-residential ZIPs such as PO boxes
Without internet access, use --acs-csv / --gaz-csv to point to manually downloaded files.
"""
from __future__ import annotations

import argparse
import io
import json
import math
import random
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "zips"


# ---------------------------------------------------------------- fetchers
def _get(url: str, timeout=120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "research-zip-selection/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_acs(year: int, key: str | None) -> pd.DataFrame:
    vars_ = "NAME,B19013_001E,B19013_001M,B01003_001E"
    url = (f"https://api.census.gov/data/{year}/acs/acs5?get={vars_}"
           f"&for=zip%20code%20tabulation%20area:*" + (f"&key={key}" if key else ""))
    rows = json.loads(_get(url))
    df = pd.DataFrame(rows[1:], columns=rows[0])
    df = df.rename(columns={"zip code tabulation area": "zcta", "B19013_001E": "income",
                            "B19013_001M": "income_moe", "B01003_001E": "pop"})
    return df[["zcta", "income", "income_moe", "pop"]]


def fetch_gazetteer(year: int) -> pd.DataFrame:
    url = (f"https://www2.census.gov/geo/docs/maps-data/data/gazetteer/{year}_Gazetteer/"
           f"{year}_Gaz_zcta_national.zip")
    z = zipfile.ZipFile(io.BytesIO(_get(url)))
    with z.open(z.namelist()[0]) as f:
        g = pd.read_csv(f, sep="\t", dtype={"GEOID": str})
    return _clean_gaz(g)


def _clean_gaz(g: pd.DataFrame) -> pd.DataFrame:
    g.columns = [c.strip() for c in g.columns]
    return g.rename(columns={"GEOID": "zcta", "INTPTLAT": "lat", "INTPTLONG": "lon"})[["zcta", "lat", "lon"]]


def fetch_osm_stores(brand_regex: str, bbox: tuple[float, float, float, float]) -> pd.DataFrame:
    s, w, n, e = bbox
    q = f"""[out:json][timeout:90];
    ( nwr["shop"]["brand"~"{brand_regex}",i]({s},{w},{n},{e});
      nwr["shop"]["name"~"{brand_regex}",i]({s},{w},{n},{e}); );
    out center;"""
    import time
    data, last = None, None
    for attempt in range(6):
        host = ["https://overpass-api.de", "https://overpass.kumi.systems",
                "https://overpass.private.coffee"][attempt % 3]
        try:
            data = json.loads(_get(host + "/api/interpreter?data=" + urllib.parse.quote(q)))
            break
        except Exception as e:
            last = e
            print(f"  OpenStreetMap attempt {attempt+1} failed ({e}), retrying with another server...")
            time.sleep(5 * (attempt + 1))
    if data is None:
        print(f"  !! Could not fetch store data: {last}")
        return pd.DataFrame(columns=["name", "lat", "lon"])
    rows = []
    for el in data["elements"]:
        lat = el.get("lat") or el.get("center", {}).get("lat")
        lon = el.get("lon") or el.get("center", {}).get("lon")
        if lat and lon:
            rows.append({"name": el.get("tags", {}).get("name", ""), "lat": lat, "lon": lon})
    return pd.DataFrame(rows).drop_duplicates(subset=["lat", "lon"])


# ---------------------------------------------------------------- HUD crosswalk (method B)
B19001_BINS = [  # (variable, lower bound, upper bound)  USD
    ("B19001_002E", 0, 10000), ("B19001_003E", 10000, 15000), ("B19001_004E", 15000, 20000),
    ("B19001_005E", 20000, 25000), ("B19001_006E", 25000, 30000), ("B19001_007E", 30000, 35000),
    ("B19001_008E", 35000, 40000), ("B19001_009E", 40000, 45000), ("B19001_010E", 45000, 50000),
    ("B19001_011E", 50000, 60000), ("B19001_012E", 60000, 75000), ("B19001_013E", 75000, 100000),
    ("B19001_014E", 100000, 125000), ("B19001_015E", 125000, 150000), ("B19001_016E", 150000, 200000),
    ("B19001_017E", 200000, 250000)]  # top bracket is $200k+, approximated as $200k-250k


def fetch_tracts(year: int, states: list[str], key: str | None) -> pd.DataFrame:
    """Fetch tract-level B19013 (median income) + B19001 (households by income bracket) by state."""
    vars_ = ["B19013_001E", "B19001_001E"] + [v for v, _, _ in B19001_BINS]
    out = []
    for st in states:
        url = (f"https://api.census.gov/data/{year}/acs/acs5?get={','.join(vars_)}"
               f"&for=tract:*&in=state:{st}" + (f"&key={key}" if key else ""))
        rows = json.loads(_get(url))
        d = pd.DataFrame(rows[1:], columns=rows[0])
        d["tract"] = d["state"] + d["county"] + d["tract"]
        out.append(d)
        print(f"  Downloaded {len(d)} tracts for state {st}")
    return pd.concat(out, ignore_index=True)


def read_hud(path: str) -> pd.DataFrame:
    hud = pd.read_excel(path, dtype=str) if path.endswith(("xlsx", "xls")) else pd.read_csv(path, dtype=str)
    hud.columns = [c.strip().upper() for c in hud.columns]
    if "TRACT" not in hud.columns:          # newer HUD files call the tract column GEOID
        for alt in ("GEOID", "TRACT_GEOID", "CENSUS_TRACT"):
            if alt in hud.columns:
                hud = hud.rename(columns={alt: "TRACT"})
                break
    need = {"ZIP", "TRACT", "RES_RATIO"}
    if not need <= set(hud.columns):
        raise SystemExit(f"HUD file is missing columns {need - set(hud.columns)}; a ZIP_TRACT file is required")
    hud["ZIP"] = hud["ZIP"].str.zfill(5)
    hud["TRACT"] = hud["TRACT"].str.zfill(11)
    hud["RES_RATIO"] = pd.to_numeric(hud["RES_RATIO"], errors="coerce").fillna(0)
    return hud[["ZIP", "TRACT", "RES_RATIO"]]


def hud_income(hud: pd.DataFrame, tracts: pd.DataFrame) -> pd.DataFrame:
    """Convert tract income to ZIPs using RES_RATIO (share of the ZIP's residential addresses in each tract).
    income_hud_wmean  = weighted average of tract median incomes (approximation)
    income_hud_median = sum tract bracket counts by weight, then linearly interpolate the median within the bracket (more rigorous)"""
    t = tracts.copy()
    num = ["B19013_001E", "B19001_001E"] + [v for v, _, _ in B19001_BINS]
    for c in num:
        t[c] = pd.to_numeric(t[c], errors="coerce")
    t.loc[t["B19013_001E"] < 0, "B19013_001E"] = np.nan          # -666666666 = missing
    m = hud.merge(t, left_on="TRACT", right_on="tract", how="inner")
    rows = []
    for z, g in m.groupby("ZIP"):
        w = g["RES_RATIO"].values
        if w.sum() <= 0:
            continue
        ok = ~np.isnan(g["B19013_001E"].values)
        wmean = np.average(g["B19013_001E"].values[ok], weights=w[ok]) if w[ok].sum() > 0 else np.nan
        counts = np.array([(g[v].values * w).sum() for v, _, _ in B19001_BINS])
        tot = counts.sum()
        med = np.nan
        if tot > 0:
            cum = np.cumsum(counts)
            i = int(np.searchsorted(cum, tot / 2))
            lo, hi = B19001_BINS[i][1], B19001_BINS[i][2]
            prev = cum[i - 1] if i > 0 else 0
            med = lo + (tot / 2 - prev) / counts[i] * (hi - lo) if counts[i] > 0 else lo
        rows.append({"zcta": z, "income_hud_wmean": wmean, "income_hud_median": med,
                     "hud_households": tot, "hud_n_tracts": int((w > 0).sum())})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- geometry
def haversine_mi(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 3958.8 * 2 * np.arcsin(np.sqrt(a))


def nearest_dist(df: pd.DataFrame, stores: pd.DataFrame) -> np.ndarray:
    if stores is None or stores.empty:
        return np.full(len(df), np.nan)
    d = haversine_mi(df["lat"].values[:, None], df["lon"].values[:, None],
                     stores["lat"].values[None, :], stores["lon"].values[None, :])
    return d.min(axis=1)


# ---------------------------------------------------------------- selection
def pick_tier(cands: pd.DataFrame, k: int, near_mi: float, min_sep_mi: float,
              rng: random.Random) -> pd.DataFrame:
    """Pick k per tier, mixing "near rival" and "far from rival" ZIPs, with ZIPs at least min_sep_mi apart."""
    near = cands[cands["rival_mi"] <= near_mi].sample(frac=1, random_state=rng.randint(0, 10**9))
    far = cands[cands["rival_mi"] > near_mi].sample(frac=1, random_state=rng.randint(0, 10**9))
    order = []
    # alternate between the two groups so both types are represented
    for a, b in zip(near.itertuples(), far.itertuples()):
        order += [a, b]
    longer = near if len(near) > len(far) else far
    order += list(longer.itertuples())[min(len(near), len(far)):]
    chosen = []
    for r in order:
        if all(haversine_mi(r.lat, r.lon, c.lat, c.lon) >= min_sep_mi for c in chosen):
            chosen.append(r)
        if len(chosen) == k:
            break
    return pd.DataFrame(chosen).drop(columns=["Index"], errors="ignore")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--center", nargs=2, type=float, required=True, metavar=("LAT", "LON"))
    ap.add_argument("--radius-mi", type=float, default=40)
    ap.add_argument("--year", type=int, default=2024, help="end year of the ACS 5-year data")
    ap.add_argument("--census-key", default=None)
    ap.add_argument("--acs-csv", default=None, help="offline: columns zcta,income,income_moe,pop")
    ap.add_argument("--gaz-csv", default=None, help="offline: Gazetteer ZCTA file (tab-separated)")
    ap.add_argument("--rivals", default=None, help="CSV: name,lat,lon")
    ap.add_argument("--osm-rivals", action="store_true")
    ap.add_argument("--rival-regex", default="^(Office Depot|OfficeMax)")
    ap.add_argument("--own-regex", default="^Staples", help="own stores (distance recorded; usable as a covariate)")
    ap.add_argument("--crosswalk", default=None, help="UDS ZIP->ZCTA crosswalk csv/xlsx")
    ap.add_argument("--hud", default=None, help="method B: HUD ZIP_TRACT crosswalk (xlsx or csv)")
    ap.add_argument("--hud-income", default="median", choices=["median", "wmean"],
                    help="income used by method B: median=bracket-interpolated median (default), wmean=weighted average of tract medians")
    ap.add_argument("--tract-csv", default=None, help="offline: previously downloaded tract income table")
    ap.add_argument("--min-pop", type=int, default=10000)
    ap.add_argument("--max-moe-ratio", type=float, default=0.15)
    ap.add_argument("--quantile", type=float, default=0.2, help="low/high tier = bottom/top share of ZIPs")
    ap.add_argument("--near-mi", type=float, default=20, help="the 20-mile threshold from WSJ 2012")
    ap.add_argument("--min-sep-mi", type=float, default=3)
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--seed", type=int, default=20261126)
    ap.add_argument("--out", default=None, help="output folder (default data/zips); use when changing region to avoid overwriting")
    a = ap.parse_args()
    global OUT
    if a.out:
        OUT = Path(a.out) if Path(a.out).is_absolute() else ROOT / a.out
    OUT.mkdir(parents=True, exist_ok=True)
    rng = random.Random(a.seed)

    # 1. ACS + centroids
    acs = (pd.read_csv(a.acs_csv, dtype={"zcta": str}) if a.acs_csv
           else fetch_acs(a.year, a.census_key))
    gaz = (_clean_gaz(pd.read_csv(a.gaz_csv, sep="\t", dtype={"GEOID": str})) if a.gaz_csv
           else fetch_gazetteer(a.year))
    df = acs.merge(gaz, on="zcta")
    for c in ("income", "income_moe", "pop"):
        df[c] = pd.to_numeric(df[c], errors="coerce")

    # 2. Region
    lat0, lon0 = a.center
    df["dist_center_mi"] = haversine_mi(lat0, lon0, df["lat"], df["lon"])
    df = df[df["dist_center_mi"] <= a.radius_mi].copy()

    # 2b. Method B: convert tract income to ZIPs with the HUD crosswalk, replacing ZCTA income
    if a.hud:
        hud = read_hud(a.hud)
        hud = hud[hud["ZIP"].isin(set(df["zcta"]))]
        states = sorted(set(hud["TRACT"].str[:2]))
        print(f"Method B: {hud['ZIP'].nunique()} ZIPs in the region cover {hud['TRACT'].nunique()} tracts, state codes {states}")
        tracts = (pd.read_csv(a.tract_csv, dtype=str) if a.tract_csv
                  else fetch_tracts(a.year, states, a.census_key))
        tracts.to_csv(OUT / "tract_income.csv", index=False)
        hi = hud_income(hud, tracts)
        df = df.merge(hi, on="zcta", how="left")
        df["income_zcta"] = df["income"]
        df["income"] = df[f"income_hud_{a.hud_income}"]
        both = df[["income_zcta", "income"]].dropna()
        if len(both) > 5:
            r = both.corr(method="spearman").iloc[0, 1]
            print(f"Spearman correlation, ZCTA income vs HUD-converted income = {r:.3f} (should be close to 1; robustness check)")

    # 3. Quality filters (Census uses -666666666 for missing)
    n0 = len(df)
    df = df[(df["income"] > 0) & (df["pop"] >= a.min_pop)]
    base = df["income_zcta"] if "income_zcta" in df else df["income"]
    df = df[(df["income_moe"] / base) <= a.max_moe_ratio]          # MOE filter uses the ZCTA estimate
    if a.crosswalk:
        cw = (pd.read_excel(a.crosswalk, dtype=str) if a.crosswalk.endswith("xlsx")
              else pd.read_csv(a.crosswalk, dtype=str))
        cw.columns = [c.lower() for c in cw.columns]
        ok = set(cw.loc[cw["zip_code"] == cw["zcta"], "zcta"])   # ZIP equal to ZCTA = ordinary residential ZIP
        df = df[df["zcta"].isin(ok)]
    print(f"{n0} ZCTAs in the region, {len(df)} remain after quality filters")

    # 4. Store distances
    pad = a.radius_mi / 55 + 0.5
    bbox = (lat0 - pad, lon0 - pad * 1.3, lat0 + pad, lon0 + pad * 1.3)
    rivals = own = None
    if a.rivals:
        rivals = pd.read_csv(a.rivals)
    elif a.osm_rivals:
        rivals = fetch_osm_stores(a.rival_regex, bbox)
        own = fetch_osm_stores(a.own_regex, bbox)
        rivals.to_csv(OUT / "rival_stores_osm.csv", index=False)
        own.to_csv(OUT / "own_stores_osm.csv", index=False)
    print(f"Rival stores: {0 if rivals is None else len(rivals)}")
    df["rival_mi"] = nearest_dist(df, rivals)
    df["own_mi"] = nearest_dist(df, own)

    # 5. Income tiers
    lo_cut, hi_cut = df["income"].quantile([a.quantile, 1 - a.quantile])
    df["tier"] = np.where(df["income"] <= lo_cut, "low",
                          np.where(df["income"] >= hi_cut, "high", "mid"))
    df = df.sort_values("income")
    df.to_csv(OUT / "candidates.csv", index=False)

    # Correlation between income and rival distance: if too high, the two channels are hard to separate
    if df["rival_mi"].notna().any():
        rho = df[["income", "rival_mi"]].corr(method="spearman").iloc[0, 1]
        print(f"Spearman correlation, income vs rival distance in the region = {rho:.2f}"
              + ("  (|rho|>0.5: the two channels are hard to separate; consider another region)" if abs(rho) > 0.5 else ""))

    # 6. Select ZIPs
    sel = []
    for t in ("low", "high"):
        c = df[df["tier"] == t]
        if c["rival_mi"].isna().all():
            c = c.assign(rival_mi=0.0)
        p = pick_tier(c, a.k, a.near_mi, a.min_sep_mi, rng)
        if len(p) < a.k:
            print(f"!! Only {len(p)} selected for tier {t}; relax --min-sep-mi or enlarge --radius-mi")
        sel.append(p)
    sel = pd.concat(sel)
    cols = ["tier", "zcta", "income"] + (["income_zcta"] if "income_zcta" in sel else []) + \
           ["income_moe", "pop", "rival_mi", "own_mi", "lat", "lon"]
    sel[cols].to_csv(OUT / "selected.csv", index=False)
    print("\n=== Selected ZIPs ===")
    print(sel[cols].round(1).to_string(index=False))
    print(f"\nLow-income tier ceiling ${lo_cut:,.0f}, high-income tier floor ${hi_cut:,.0f}")
    print("\nPaste into config.yaml:\n  exp1:")
    for t in ("low", "high"):
        print(f"    {t}:  {sel.loc[sel['tier'] == t, 'zcta'].tolist()}")
    print("\nNext: enter each of these 6 ZIPs on the website and note the store it maps to (if shown), "
          "then fill the new store_id column in data/zips/selected.csv for store fixed effects in Exp 1.")


if __name__ == "__main__":
    main()
