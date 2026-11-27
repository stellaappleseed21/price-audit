# Research CSV Collection

All CSV files used in the online price differentiation audit. These are copies; the originals remain under `data/` so the scripts keep working.

## Data collection methods

- **Automated scraper** (Playwright): `1_kayak/kayak_scraper_loads.csv`, collected before Kayak's bot detection blocked the scraper.
- **AI agent + human-assisted verification**: because of anti-bot measures, the Kayak six-site comparison and the Target / Office Depot store data were collected by an AI agent (Claude) operating a real, visible browser on the researcher's computer. Whenever a site showed a security check or CAPTCHA, the researcher completed it by hand; the agent never bypassed any protection. The agent then read the offers the site itself returned to the page.
- **Public data downloads**: Census ACS, HUD-USPS ZIP–Tract crosswalk, OpenStreetMap.

## 1_kayak (Kayak cross-site pricing)

- `kayak_SFO-ICN_by_seller_6sites.csv`: SFO→ICN, lowest USD price per flight × seller on 6 Kayak sites (wide format), with % difference vs. kayak.com. Columns: `flights` flight numbers; `prov` seller code; `us`, `us_es`, `ec`, `ar`, `th`, `kr` price on each site; `*_pct` % difference vs. `us`.
- `kayak_SFO-ICN_offers_long.csv`: the same data in long format (`site, leg, flights, prov, price`); `leg` is a short hash of Kayak's leg ID used to match identical flights across sites.
- `kayak_scraper_loads.csv`: every offer the Playwright scraper collected before it was blocked (exported from `data/kayak.sqlite`).
- Sites: `us` = kayak.com; `us_es` = es.kayak.com (offers tagged with Uruguay as the market country); `ec` = kayak.com.ec; `ar` = kayak.com.ar (display switched to USD); `th` = en.kayak.co.th; `kr` = kayak.co.kr (display switched to USD).

## 2_zip_income (ZIP income tiers)

- `candidates.csv`: qualifying Bay Area ZIPs with ZCTA income, HUD-converted income, population, rival-store distance and income tier.
- `candidates_verified.csv`: adds the rival distance and nearest store ID based on the verified Office Depot store list.
- `selected.csv`: the 6 selected ZIPs.
- `tract_income.csv`: California census-tract income (ACS B19013, B19001).

## 3_target_and_stores (store verification and Target pricing)

- `target_prices_wide.csv` / `target_prices_by_store.csv`: prices for 20 products at 7 Target stores.
- `selected_target_stores.csv`: the Target store serving each of the 6 ZIPs, with distance.
- `rival_stores_verified.csv`: 21 Northern California Office Depot / OfficeMax stores verified on the official store locator.
- `rival_stores_osm.csv` / `rival_stores_osm_check.csv`: OpenStreetMap stores and their point-by-point match against the official list.
- `own_stores_osm.csv`: Staples stores from OpenStreetMap.
