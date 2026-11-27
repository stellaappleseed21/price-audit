# ZIP Price Audit: Operating Manual

Collect product prices without logging in, using only ZIP and device settings, in this order: manual check → scraper → ACS ZIP selection → Pilot → Exp 0 → Exp 1.
The default platform is Staples.com; to switch platforms, change only `site` and `items` in `config.yaml`.

```
zip_price_audit/
├── config.yaml          all parameters (items marked [TODO] must be filled in)
├── config_kayak.yaml    Kayak configuration
├── core.py              scraper core: sessions, set ZIP, read prices, detect blocks, write SQLite
├── discover.py          inspects a site's structure to help fill the config
├── run.py               collection entry point: test / check / pilot / exp0 / exp1
├── acs_zips.py          ACS income + rival-store distance -> select the 6 ZIPs for Exp 1
├── pilot_analysis.py    choose the sampling interval
├── exp0_analysis.py     multi-price rate, spread, noise, ICC, correlation ceiling
├── exp1_analysis.py     fixed-effects regressions, permutation tests, BH correction
├── simulate.py          generate simulated data with known answers to validate the analysis code
└── research_csv/        copies of all CSVs used in the research, with a README
```

All data is written to the `loads` table in `data/loads.sqlite`; one row = one product-page load.

---

## Step 0: Before running (no code)

1. **Counsel / IRB**: confirm that logged-out pages may be collected and the daily load cap. Enter the cap in `politeness.daily_page_cap`. Do not run the pilot or later steps before this is confirmed.
2. **Manual platform check**: in a private window, open Staples, enter a ZIP near an Office Depot, then one with none nearby, and check 5 products each. If prices differ, the platform varies prices by ZIP and is usable.

## Step 1: Set up the environment and configure the scraper

```bash
python3 -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium webkit
python3 simulate.py                                    # optional: confirm the analysis code runs
```

**1a. Choose 18 products**: pick 18 products on the site and put their product-page URLs in `items` in `config.yaml`.

- Cover several categories (stationery, paper, ink...). `category` is used to group BH multiple testing.
- Include a Swingline stapler for comparison with WSJ 2012.
- Prefer products sold by Staples itself, not third-party sellers.
- The first 3 products are also used in the pilot.

**1b. Inspect the site structure** (once):

```bash
python3 discover.py --url "<any product page URL>" --headed
```

A browser opens; you can try the ZIP setting manually. The terminal prints four kinds of hints, also saved to `data/discover/report.json`:

| Printed output | Where it goes in the config |
|---|---|
| ZIP-related elements | `zip_ui.open_selector / input_selector / submit_selector / confirm_selector` |
| Price elements on the page | `price_dom_selector` (pick the main product price, not a recommendation slot) |
| JSON requests with price fields | `price_api_url_pattern`, and `price_json_keys` if needed |
| ZIP-related cookies | if the ZIP is stored in a cookie, switch to `zip_method: cookie` (more stable) |

Prices are tried in the order `dom -> api -> jsonld -> meta`, using the first that succeeds. All candidate prices for each load are stored in the `price_raw` column for later checking.

**1c. Test loads**:

```bash
python3 run.py test --zip 94704 --n 3
python3 run.py check --zips <ZIP near a rival> <ZIP far from rivals> --n 5
```

Check three things: `zip_confirmed=1`; `price` matches what you see in the browser; `src` is the source you want.
`check` is an automated version of the Step 0 manual check and reports how many products differ in price.
Also spot-check a few screenshots in `data/screenshots/`.

> About IP: the script changes only the "declared ZIP"; all sessions share one outgoing IP. The paper should state that the treatment is the declared ZIP.
> Testing the effect of IP location requires residential proxies and counsel approval first.

## Step 2: ACS income + ZIP selection

```bash
# Example: within 40 miles of Berkeley
python3 acs_zips.py --center 37.87 -122.27 --radius-mi 40 --osm-rivals
```

- Data: ACS 5-year table B19013 (median household income) at ZCTA level; ZCTA centroids from the Census Gazetteer. A Census API key is free; pass it with `--census-key` (works without one but with rate limits).
- HUD method: `--hud <ZIP_TRACT file>` converts tract income to ZIPs with the HUD-USPS crosswalk (recommended; the ZCTA estimate is kept as a cross-check). `--tract-csv data/zips/tract_income.csv` reuses previously downloaded tract data.
- Rival stores: `--osm-rivals` pulls Office Depot / OfficeMax locations from OpenStreetMap. OSM may miss stores or keep closed ones, so verify against the Office Depot store locator. A verified list is in `data/zips/rival_stores_verified.csv`; pass it with `--rivals`.
- ZIP and ZCTA are not the same thing: optionally pass the UDS Mapper ZIP->ZCTA crosswalk with `--crosswalk` to exclude non-residential ZIPs such as PO boxes.
- Filters: population >= 10,000; MOE/estimate <= 0.15; low-income tier = bottom 20% in the region, high-income tier = top 20%. Three per tier, mixing "rival within 20 miles" and "beyond 20 miles" so the income and competition channels can be separated.
- The script prints the **correlation between income and rival distance**. If it is too high (|rho| > 0.5) the channels are hard to separate; consider another region.
- `--out <folder>` writes results to a separate folder (e.g. when changing region) instead of overwriting `data/zips/`.

Paste the printed `exp1:` snippet into the config. Then enter each of the 6 ZIPs on the website, note which store each maps to, and record it in the new `store_id` column of `data/zips/selected.csv`. **ZIPs that map to the same store give the cleanest comparison.**

## Step 3: Pilot (about 2,160 loads, 24 hours)

```bash
python3 run.py pilot          # 3 products, 1 ZIP, desktop, every 2 minutes, 24 hours
python3 pilot_analysis.py
```

- One session throughout, so it measures the platform's own repricing (if the session is blocked, cool down 10 minutes and start a new session).
- Keep the computer on and awake (on a Mac: `caffeinate -i python3 run.py pilot`).
- The analysis reports, for each sampling interval, the share of changes that revert within 10 minutes, and recommends the shortest interval with a share < 5%. If it is above 20 minutes, set it as `exp0.round_gap_min` and `exp1.round_gap_min`.

## Step 4: Exp 0 (about 6,500 loads, 5 days)

```bash
python3 run.py exp0 --now     # run one slot manually first to make sure everything works
python3 run.py exp0           # auto-schedule 5 days x 4 slots from slots_local
python3 exp0_analysis.py
```

Each slot: 6 identical fresh sessions start within 2 minutes (random order). Each session visits each of the 18 products in 3 rounds, 20 minutes apart, with product order shuffled each round. So each slot = 6 x 18 x 3 = 324 loads.

You can also trigger runs with cron instead of the built-in scheduler, e.g.:
`0 9,13,17,21 * * * cd /path/zip_price_audit && .venv/bin/python run.py exp0 --now`

Output: multi-price rate (mainly within-round), spread, within-session stability, ICC, dev standard deviation, block rate, completion rate, rough MDE, correlation ceiling, and a decision based on the multi-price rate (< 2% / 2-10% / > 10%). Results are also saved to `data/exp0_summary.json`.

## Step 5: Exp 1 (about 13,000 loads, 5 days)

```bash
python3 run.py exp1 --now
python3 run.py exp1
python3 exp1_analysis.py --zipmeta data/zips/selected.csv
```

Each slot: 12 sessions (6 ZIPs x desktop/mobile), random start order. 4 slots x 5 days = 240 sessions, 60 per cell.

Output:
1. Regression: `log_price ~ low_income + mobile | item^slot`, two-way clustered by product + session. Three more versions: with interaction; with `rival_near` (the change in the income coefficient is the competition channel); with store fixed effects.
2. Permutation test (10,000 draws)
3. Estimates by category with BH correction
4. Automatic interpretation: when the CI is within +/-1% and not significant, write "no evidence at this power"; also reports whether a ~10% gap like WSJ 2012 can be ruled out.

---

## Kayak version (third-party flight metasearch)

Add `--config config_kayak.yaml` to every command; data is written to `data/kayak.sqlite`.

**Main method: read Kayak's results API `/i/api/search/dynamic/flights/poll` directly.** This is the API the Kayak page itself uses to fetch results, returning structured JSON. After opening a search page, the scraper waits for this API to return `status=complete`, then:
- **One row per seller offer per flight** (`price_source = api`). Item = flight `legId` x seller `providerCode` x fare class (cabin + baggage/change/refund amenity codes + ordinal within class). These codes are language-independent, so English and Spanish sites match directly.
- **One extra row with each flight's lowest price** (`price_source = card`), matching what consumers see on the card.
- Ads (`type` other than `core`) are skipped automatically.
- `price_raw` also stores the seller name, whether it is an airline website, currency, the seller's country and whether it is Instant Book.
- One load yields about 500 rows (50 flights x about 10 sellers each), **with no simulated clicks**.

`exp1_analysis.py` also reports **estimates by seller** (`data/exp1_by_seller.csv`), showing which sellers change prices across conditions.

Page parsing (cards, expanded seller lists) below is a fallback, used only when `api_only: false` and the API returns nothing. Other differences from the Staples version:

- **"Item" = flight x seller**: one search results page captures the top 15 results, one row each. `item_id = query id|itinerary fingerprint`.
  - The fingerprint uses 24-hour times + day offset + airport codes + seller name, so it **matches across languages** (English "11:00 pm – 5:10 am" and Spanish "23:00 – 5:10" give the same fingerprint).
  - Same flight, different seller = different item (so Cheapflightsfares $770 and an organic $756 are not treated as a price gap).
  - Ad slots (cards labeled "Ad" / "Sponsored") are skipped by default.
- **Conditions are set in `exp1.personas`**, of two kinds, usable alone or together:
  - Browsing-history personas `clean / budget / luxury`: before each session, browse the pages in `search.personas` to leave cookies (the approach of Karan et al. 2023).
  - Region/language versions such as `us_en / us_es / ec_es`: defined in `search.variants` with domain and browser language. Note that Kayak's "language" menu is a country-site selector; `es.kayak.com` offers are tagged with Uruguay as the market country.
- The `rank` column stores the result position, for later analysis of steering.
- **Expanded seller lists** (`expand_providers: true`): click Select / Seleccionar on the top 5 flights per page and read the "choose where to book" list, one row per seller (`price_source = provider_list`). Labels such as "Airline / Aerolínea / Precio más bajo" are removed from seller names automatically.
- **`exp1_analysis.py` reports two comparisons separately**:
  1. **Same flight x same seller** (provider_list / api): does the same seller change price by language or persona? The cleanest comparison.
  2. **Lowest card price for the same flight** (card): the price consumers actually see, including the effect of "switching language also switches which sellers you see".
- **Money parsing handles locale formats**: the Spanish "$ 1.004" is read as 1004, not 1.

**Testing language**: set `exp1.personas` to `[us_en, us_es]`, `reference_persona` to `us_en`, and `exp0.persona` to `us_en`.

```bash
python3 discover.py --config config_kayak.yaml --url "https://www.kayak.com/flights/SFO-ICN/2027-01-15" --headed
python3 run.py test  --config config_kayak.yaml --n 1 --persona clean      # check that results parse correctly
python3 run.py test  --config config_kayak.yaml --n 1 --persona us_es
python3 run.py exp0  --config config_kayak.yaml --now
python3 exp0_analysis.py --config config_kayak.yaml --db data/kayak.sqlite
python3 exp1_analysis.py --config config_kayak.yaml --db data/kayak.sqlite
```

**Check three things in the `test` output**: prices are correct; seller names are recognized (if not, add them to `known_providers` or set `provider_selector`); ad slots are skipped.

**Region-testing limitation**: the script changes only the domain and browser language; **the IP stays the same**. If Kayak determines country by IP, changing language alone cannot test IP effects. Testing IP requires proxies and counsel approval first.

**Bot detection**: after a few dozen automated loads in a short period, Kayak redirects to `kayak.com/help/bots.html`; the scraper records these loads as `blocked`. Do not try to bypass it. Because of this, the six-site comparison in `research_csv/1_kayak/` was collected by an AI agent operating a real browser, with the researcher completing every security check by hand.

## Two departures from the original design document (both corrections)

1. **Correlation ceiling**: the original computed deviations from the "product x slot mean". That mechanically forces the two sessions in a cell into a negative correlation of -1/(n-1) (-0.2 with 6 sessions), so nothing can be measured. The code now subtracts only the product's mean across all slots (removing only the price level). The part shared by two same-slot sessions is what an agent looking at the same time would match, so the correlation reflects the predictable share.
2. **Permutation test**: the original shuffled labels independently within each product x slot. But the unit of random assignment is the session, and one session's prices across 18 products are correlated, so independent shuffling makes p-values too small. In simulated data with a true device effect of 0, the original method gave p = 0.03. The code now shuffles whole-session labels within slots (device within slot x ZIP), and regressions are clustered two-way by product + session.

## FAQ

- **`zip_confirmed=0`**: the ZIP selectors are wrong or the site changed. Rerun `discover.py --headed`.
- **High block rate**: increase `min_delay_s`; run fewer sessions at once; try `browser.engine: webkit`. Do not try to get around CAPTCHAs; being blocked is itself a result to record.
- **Capturing a recommendation-slot price**: make `price_dom_selector` more specific, or use the API source.
- **Switching platforms**: change `site` and `items` and redo Step 1. No other code changes are needed.
