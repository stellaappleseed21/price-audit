"""
Step 1b: inspect a site's structure (run once).

Usage:
    python discover.py --url "https://www.staples.com/<a product page>" --headed

What it does:
  1. Opens the home page and lists elements that look like a "ZIP input/button" -> helps fill config.zip_ui
  2. Opens the product page and saves the HTML and a screenshot
  3. Records all JSON network responses and flags those with a price field -> helps fill price_api_url_pattern
  4. Lists page elements containing "$" with CSS hints -> helps fill price_dom_selector
Results are written to data/discover/ and a summary is printed.
"""
import argparse
import asyncio
import json
from pathlib import Path

from core import ROOT, find_keys, load_config, parse_jsonld, parse_meta
from playwright.async_api import async_playwright

OUT = ROOT / "data" / "discover"

ZIP_PROBE_JS = """
() => [...document.querySelectorAll('input,button,a,[role=button]')]
  .filter(e => /zip|postal|location|store|deliver/i.test(
      (e.outerHTML || '').slice(0, 400) + ' ' + (e.innerText || '')))
  .slice(0, 40)
  .map(e => ({tag: e.tagName, id: e.id, name: e.getAttribute('name'),
              testid: e.getAttribute('data-testid'), aria: e.getAttribute('aria-label'),
              placeholder: e.getAttribute('placeholder'),
              text: (e.innerText || '').trim().slice(0, 60)}))
"""

PRICE_PROBE_JS = """
() => [...document.querySelectorAll('body *')]
  .filter(e => e.children.length === 0 && /\\$\\s?\\d/.test(e.innerText || ''))
  .slice(0, 40)
  .map(e => ({tag: e.tagName, id: e.id, cls: (e.className && e.className.toString()) || '',
              testid: e.getAttribute('data-testid'),
              parent_testid: e.parentElement && e.parentElement.getAttribute('data-testid'),
              text: (e.innerText || '').trim().slice(0, 40)}))
"""


CARD_PROBE_JS = """
(sel) => { const els = [...document.querySelectorAll(sel)];
  return {count: els.length,
          samples: els.slice(0, 4).map(e => (e.innerText || '').replace(/\\s+/g, ' ').slice(0, 220))}; }
"""

# common candidate selectors for "result cards" (search results pages)
CARD_CANDIDATES = ["[data-resultid]", "[data-testid*='result']", "[class*='resultWrapper']",
                   "[class*='result-item']", "li[class*='result']", "[role='listitem']"]


async def main(url: str, headed: bool, device: str, config: str | None):
    cfg = load_config(config) if config else load_config()
    OUT.mkdir(parents=True, exist_ok=True)
    json_hits = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not headed)
        dev = dict(pw.devices[cfg["browser"]["devices"][device]])
        dev.pop("default_browser_type", None)
        ctx = await browser.new_context(**dev, locale=cfg["browser"]["locale"],
                                        timezone_id=cfg["browser"]["timezone_id"])
        page = await ctx.new_page()

        async def on_resp(resp):
            ct = resp.headers.get("content-type") or ""
            if "json" not in ct:
                return
            try:
                body = await resp.json()
            except Exception:
                return
            hits = find_keys(body, cfg["site"].get("price_json_keys", ["price", "totalPrice"]) +
                             cfg["site"].get("list_price_json_keys", []))
            json_hits.append({"url": resp.url, "status": resp.status,
                              "price_fields": hits[:10], "has_price": bool(hits)})

        page.on("response", on_resp)

        # -- home page: find ZIP controls
        await page.goto(cfg["site"]["base_url"], wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)
        zip_elems = await page.evaluate(ZIP_PROBE_JS)
        await page.screenshot(path=str(OUT / "home.png"))
        if headed:
            input("\n>>> Browser is open. You can try the ZIP setting manually, then come back and press Enter to continue...")

        # -- product page
        await page.goto(url, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        await page.screenshot(path=str(OUT / "product.png"), full_page=True)
        (OUT / "product.html").write_text(await page.content(), encoding="utf-8")
        price_elems = await page.evaluate(PRICE_PROBE_JS)
        cards = {}
        for sel in CARD_CANDIDATES + ([cfg["search"]["card_selector"]] if "search" in cfg else []):
            try:
                cards[sel] = await page.evaluate(CARD_PROBE_JS, sel)
            except Exception:
                pass
        jsonld = await parse_jsonld(page)
        meta = await parse_meta(page)
        cookies = await ctx.cookies()
        await browser.close()

    report = {"result_cards": cards, "zip_elements": zip_elems, "price_elements": price_elems,
              "jsonld_prices": jsonld, "meta_prices": meta,
              "json_responses_with_price": [h for h in json_hits if h["has_price"]],
              "all_json_urls": [h["url"] for h in json_hits],
              "cookies": [{"name": c["name"], "value": c["value"][:60], "domain": c["domain"]}
                          for c in cookies]}
    (OUT / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n=== Search result card candidates (for search.card_selector in the Kayak config) ===")
    for sel, c in cards.items():
        if c and c["count"]:
            print(f"  {sel!r}: {c['count']} found")
            for t in c["samples"][:2]:
                print(f"      e.g.: {t[:160]}")
    print("\n=== ZIP-related elements (for zip_ui) ===")
    for e in zip_elems[:15]:
        print(" ", {k: v for k, v in e.items() if v})
    print("\n=== Price elements on the page (for price_dom_selector) ===")
    for e in price_elems[:15]:
        print(" ", {k: v for k, v in e.items() if v})
    print("\n=== JSON-LD price:", jsonld, "  meta price:", meta)
    print("\n=== JSON requests with price fields (for price_api_url_pattern) ===")
    for h in report["json_responses_with_price"][:10]:
        print(" ", h["url"][:140], h["price_fields"][:3])
    print("\n=== Cookies possibly related to ZIP (consider zip_method: cookie) ===")
    for c in report["cookies"]:
        if any(k in (c["name"] + c["value"]).lower() for k in ("zip", "postal", "store", "loc")):
            print(" ", c)
    print(f"\nFull results: {OUT}/report.json, product.html, product.png")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    ap.add_argument("--device", default="desktop", choices=["desktop", "mobile"])
    ap.add_argument("--config", default=None)
    a = ap.parse_args()
    asyncio.run(main(a.url, a.headed, a.device, a.config))
