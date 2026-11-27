"""
Core scraper: one "session" = one fresh browser context (no cookies),
fixed device and fixed ZIP; read-only, no login, no purchases; stops when it hits a block page.

Each product-page load writes one row to the SQLite loads table.
"""
from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import random
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from playwright.async_api import Browser, BrowserContext, Page, Response, async_playwright

ROOT = Path(__file__).resolve().parent


# ---------------------------------------------------------------- config / db
def load_config(path: str | Path = ROOT / "config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


SCHEMA = """
CREATE TABLE IF NOT EXISTS loads (
    load_id        TEXT PRIMARY KEY,
    ts_utc         TEXT,      -- ISO timestamp (millisecond precision)
    mode           TEXT,      -- pilot / exp0 / exp1 / test
    slot_id        TEXT,      -- time-slot ID, e.g. 2026-12-01_09:00
    session_id     TEXT,
    session_order  INTEGER,   -- start order within the slot
    round          INTEGER,   -- visit round
    zip            TEXT,
    zip_confirmed  INTEGER,   -- 1=page confirmed the ZIP, 0=not confirmed, NULL=cannot check
    device         TEXT,
    item_id        TEXT,
    category       TEXT,
    url            TEXT,
    http_status    INTEGER,
    price          REAL,
    list_price     REAL,
    price_source   TEXT,      -- dom / api / jsonld / meta
    price_raw      TEXT,      -- all candidate prices (JSON), for auditing
    blocked        INTEGER,
    block_reason   TEXT,
    latency_ms     INTEGER,
    screenshot     TEXT,
    error          TEXT,
    persona        TEXT,      -- browsing-history persona (Kayak): clean / budget / luxury ...
    rank           INTEGER    -- position in search results (Kayak; NULL in product-page mode)
);
CREATE INDEX IF NOT EXISTS idx_loads_mode ON loads(mode, slot_id, item_id);
"""
COLUMNS = ["load_id", "ts_utc", "mode", "slot_id", "session_id", "session_order", "round", "zip",
           "zip_confirmed", "device", "item_id", "category", "url", "http_status", "price",
           "list_price", "price_source", "price_raw", "blocked", "block_reason", "latency_ms",
           "screenshot", "error", "persona", "rank"]


def open_db(cfg: dict) -> sqlite3.Connection:
    p = ROOT / cfg["storage"]["db_path"]
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p, isolation_level=None, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(loads)")}
    for c, t in (("persona", "TEXT"), ("rank", "INTEGER")):   # add columns to old databases automatically
        if c not in cols:
            con.execute(f"ALTER TABLE loads ADD COLUMN {c} {t}")
    return con


def loads_today(con: sqlite3.Connection) -> int:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # one page load may write several rows (one per search result); count distinct session+timestamp
    return con.execute("SELECT COUNT(DISTINCT session_id || ts_utc) FROM loads WHERE ts_utc LIKE ?",
                       (day + "%",)).fetchone()[0]


# ---------------------------------------------------------------- price parsing
_MONEY = re.compile(r"[0-9](?:[0-9.,\u00a0 ]*[0-9])?")


_PRICE_IN_TEXT = r"(?:US)?\$\s?[0-9][0-9.,\u00a0]*[0-9]|(?:US)?\$\s?[0-9]"


def parse_money(txt: str) -> float | None:
    """Parse money amounts written in different locales:
    $1,004 / $ 1.004 (Spanish thousands dot) / 1 004 / 12.99 / 12,99 / 1.004,50 / 1,004.50"""
    m = _MONEY.search(txt)
    if not m:
        return None
    t = m.group(0).replace("\u00a0", "").replace(" ", "")
    if "," in t and "." in t:                       # whichever appears last is the decimal point
        dec = "," if t.rfind(",") > t.rfind(".") else "."
        thou = "." if dec == "," else ","
        t = t.replace(thou, "").replace(dec, ".")
    elif "," in t or "." in t:
        sep = "," if "," in t else "."
        parts = t.split(sep)
        if len(parts) > 2 or all(len(p) == 3 for p in parts[1:]):   # 1.004 / 1,004 / 1.234.567 = thousands
            t = t.replace(sep, "")
        else:                                        # 12.99 / 12,9 = decimal
            t = t.replace(sep, ".")
    try:
        v = float(t)
    except ValueError:
        return None
    return v if 0 < v < 100000 else None


def _to_float(x: Any) -> float | None:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x) if 0 < float(x) < 100000 else None
    return parse_money(str(x))


def find_keys(obj: Any, keys: list[str], out: list | None = None, depth: int = 0) -> list:
    """Recursively find the given key names in nested JSON; return a list of (key, value)."""
    out = [] if out is None else out
    if depth > 12:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and not isinstance(v, (dict, list)):
                out.append((k, v))
            elif k in keys and isinstance(v, dict):
                # e.g. {"price": {"value": 12.99}}
                for kk in ("value", "amount", "finalPrice"):
                    if kk in v:
                        out.append((k, v[kk]))
            find_keys(v, keys, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            find_keys(v, keys, out, depth + 1)
    return out


async def parse_jsonld(page: Page) -> list[float]:
    prices = []
    for txt in await page.locator("script[type='application/ld+json']").all_text_contents():
        try:
            data = json.loads(txt)
        except Exception:
            continue
        for k, v in find_keys(data, ["price", "lowPrice"]):
            f = _to_float(v)
            if f:
                prices.append(f)
    return prices


async def parse_meta(page: Page) -> list[float]:
    prices = []
    for sel in ("meta[itemprop='price']", "meta[property='product:price:amount']",
                "meta[property='og:price:amount']"):
        loc = page.locator(sel)
        if await loc.count():
            f = _to_float(await loc.first.get_attribute("content"))
            if f:
                prices.append(f)
    return prices


# ---------------------------------------------------------------- session
@dataclass
class LoadResult:
    price: float | None = None
    list_price: float | None = None
    source: str | None = None
    raw: dict = field(default_factory=dict)
    status: int | None = None
    blocked: bool = False
    block_reason: str | None = None
    latency_ms: int | None = None
    screenshot: str | None = None
    error: str | None = None


class Session:
    """One session = one new browser context."""

    def __init__(self, browser: Browser, pw, cfg: dict, con: sqlite3.Connection, *,
                 mode: str, zip_code: str, device: str, slot_id: str = "",
                 session_order: int = 0, persona: str = ""):
        self.browser, self.pw, self.cfg, self.con = browser, pw, cfg, con
        self.mode, self.zip, self.device = mode, zip_code, device
        self.slot_id, self.session_order = slot_id, session_order
        self.persona = persona
        self.session_id = f"{mode}-{uuid.uuid4().hex[:10]}"
        self.ctx: BrowserContext | None = None
        self.page: Page | None = None
        self.zip_confirmed: int | None = None
        self.dead = False
        self._api_payloads: list[Any] = []
        self._poll: list[Any] = []

    # -- setup
    async def start(self):
        b = self.cfg["browser"]
        dev_name = b["devices"][self.device]
        dev = dict(self.pw.devices[dev_name])
        dev.pop("default_browser_type", None)
        # "region/language version" condition (Kayak): switch site domain + browser language
        var = self.cfg.get("search", {}).get("variants", {}).get(self.persona, {})
        self.base = var.get("base", self.cfg["site"]["base_url"].rstrip("/"))
        locale = var.get("locale", b["locale"])
        self.ctx = await self.browser.new_context(
            **dev, locale=locale, timezone_id=var.get("timezone_id", b["timezone_id"]),
            extra_http_headers={"Accept-Language": locale})
        self.page = await self.ctx.new_page()
        self.page.set_default_timeout(b["page_timeout_ms"])
        self.page.on("response", self._on_response)
        await self._set_zip()

    async def _on_response(self, resp: Response):
        sp = self.cfg.get("search", {}).get("api_url_pattern")
        if sp and sp in resp.url:
            try:
                self._poll.append(await resp.json())
            except Exception:
                pass
            return
        pat = self.cfg["site"].get("price_api_url_pattern") or ""
        if not pat or pat not in resp.url:
            return
        try:
            if "json" in (resp.headers.get("content-type") or ""):
                self._api_payloads.append(await resp.json())
        except Exception:
            pass

    async def _set_zip(self):
        s = self.cfg["site"]
        method = s.get("zip_method", "none")
        if method == "cookie":
            c = s["zip_cookie"]
            await self.ctx.add_cookies([{
                "name": c["name"], "value": c["value_template"].format(zip=self.zip),
                "domain": c["domain"], "path": "/"}])
            await self.page.goto(s["base_url"], wait_until="domcontentloaded")
        elif method == "ui":
            u = s["zip_ui"]
            await self.page.goto(s["base_url"], wait_until="domcontentloaded")
            if await self._check_block(None):
                self.dead = True
                return
            try:
                if u.get("open_selector"):
                    await self.page.locator(u["open_selector"]).first.click(timeout=10000)
                inp = self.page.locator(u["input_selector"]).first
                await inp.fill("", timeout=10000)
                await inp.type(self.zip, delay=random.randint(60, 140))
                if u.get("submit_selector"):
                    await self.page.locator(u["submit_selector"]).first.click(timeout=10000)
                else:
                    await inp.press("Enter")
                await self.page.wait_for_timeout(2500)
            except Exception as e:  # record but do not stop; zip_confirmed will be 0
                self.zip_confirmed = 0
                self._zip_error = f"zip_ui: {type(e).__name__}"
                return
        if s.get("zip_ui", {}).get("confirm_selector") and method == "ui":
            try:
                txt = await self.page.locator(s["zip_ui"]["confirm_selector"]).first.inner_text(timeout=5000)
                self.zip_confirmed = int(self.zip in txt)
            except Exception:
                self.zip_confirmed = 0

    async def _check_block(self, status: int | None) -> str | None:
        s = self.cfg["site"]
        if status in s["block_status_codes"]:
            return f"http_{status}"
        try:
            title = (await self.page.title()) or ""
            body = (await self.page.locator("body").inner_text(timeout=3000))[:4000]
        except Exception:
            return None
        hay = (title + " " + body).lower()
        for m in s["block_text_markers"]:
            if m.lower() in hay:
                return f"text:{m}"
        return None

    # -- one product page load
    async def load_item(self, item: dict, round_no: int = 0) -> LoadResult:
        r = LoadResult()
        if self.dead:
            r.blocked, r.block_reason = True, "session_dead"
            return r
        s = self.cfg["site"]
        self._api_payloads.clear()
        t0 = time.perf_counter()
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        try:
            resp = await self.page.goto(item["url"], wait_until="domcontentloaded")
            r.status = resp.status if resp else None
            try:
                await self.page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass
            r.latency_ms = int((time.perf_counter() - t0) * 1000)
            r.block_reason = await self._check_block(r.status)
            r.blocked = r.block_reason is not None
            if not r.blocked:
                await self._extract(item, r)
        except Exception as e:
            r.error = f"{type(e).__name__}: {str(e)[:200]}"
            r.latency_ms = int((time.perf_counter() - t0) * 1000)

        if self.cfg["browser"].get("save_screenshots") and self.page:
            d = ROOT / self.cfg["storage"]["screenshot_dir"] / self.mode
            d.mkdir(parents=True, exist_ok=True)
            fn = d / f"{self.session_id}_{item['id']}_r{round_no}_{int(time.time())}.png"
            try:
                await self.page.screenshot(path=str(fn), full_page=False)
                r.screenshot = str(fn.relative_to(ROOT))
            except Exception:
                pass

        self._write(item, round_no, ts, r)
        if r.blocked and self.cfg["politeness"]["stop_session_on_block"]:
            self.dead = True
        return r

    async def _extract(self, item: dict, r: LoadResult):
        s = self.cfg["site"]
        cands: dict[str, list] = {}
        # 1) DOM
        if s.get("price_dom_selector"):
            loc = self.page.locator(s["price_dom_selector"])
            if await loc.count():
                cands["dom"] = [_to_float(t) for t in await loc.all_inner_texts()]
        # 2) network JSON
        if self._api_payloads:
            api, lp = [], []
            for p in self._api_payloads:
                api += [_to_float(v) for _, v in find_keys(p, s["price_json_keys"])]
                lp += [_to_float(v) for _, v in find_keys(p, s["list_price_json_keys"])]
            cands["api"] = api
            cands["api_list"] = lp
        # 3) JSON-LD 4) meta
        cands["jsonld"] = await parse_jsonld(self.page)
        cands["meta"] = await parse_meta(self.page)
        cands = {k: [x for x in v if x] for k, v in cands.items()}
        r.raw = cands
        for src in ("dom", "api", "jsonld", "meta"):
            if cands.get(src):
                r.price, r.source = cands[src][0], src
                break
        if cands.get("api_list"):
            r.list_price = cands["api_list"][0]

    def _write(self, item: dict, round_no: int, ts: str, r: LoadResult, rank: int | None = None):
        err = r.error or getattr(self, "_zip_error", None)
        self.con.execute(
            f"INSERT INTO loads ({','.join(COLUMNS)}) VALUES ({','.join('?' * len(COLUMNS))})",
            (uuid.uuid4().hex, ts, self.mode, self.slot_id, self.session_id, self.session_order,
             round_no, self.zip, self.zip_confirmed, self.device, item["id"],
             item.get("category", ""), item["url"], r.status, r.price, r.list_price, r.source,
             json.dumps(r.raw, ensure_ascii=False), int(r.blocked), r.block_reason, r.latency_ms,
             r.screenshot, err, self.persona, rank))

    # ------------------------------------------------------------ Kayak: persona cultivation
    async def cultivate(self, urls: list[str], dwell_s: tuple[float, float] = (6, 15)):
        """Browse a set of pages for the persona (view only, never buy), leaving cookies / browsing history."""
        for u in urls:
            if self.dead:
                return
            try:
                resp = await self.page.goto(u, wait_until="domcontentloaded")
                if await self._check_block(resp.status if resp else None):
                    self.dead = True
                    return
                for _ in range(random.randint(2, 4)):            # simulate scrolling and reading
                    await self.page.mouse.wheel(0, random.randint(400, 1200))
                    await self.page.wait_for_timeout(random.randint(800, 2000))
                await asyncio.sleep(random.uniform(*dwell_s))
            except Exception:
                continue

    # ------------------------------------------------------------ Kayak: search results page
    async def load_search(self, query: dict, round_no: int = 0) -> list[LoadResult]:
        """Open a search results page and capture the top N results. One row per result:
        item_id = query id|itinerary fingerprint, category = query id, rank = position.
        Itinerary fingerprint = hash of the result card text with all prices removed (same flight + same seller => same fingerprint)."""
        k = self.cfg["search"]
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        t0 = time.perf_counter()
        base = LoadResult()
        if self.dead:
            base.blocked, base.block_reason = True, "session_dead"
            return [base]
        cards = []
        api_rows = None
        self._poll.clear()
        try:
            resp = await self.page.goto(query["url"].format(base=self.base), wait_until="domcontentloaded")
            base.status = resp.status if resp else None
            base.block_reason = await self._check_block(base.status)
            if not base.block_reason and k.get("api_url_pattern"):
                # API mode: wait for the poll API to return status=complete (or take the last one on timeout)
                for _ in range(int(k["settle_max_s"] * 2)):
                    if any(isinstance(p, dict) and p.get("status") == "complete" for p in self._poll):
                        break
                    await self.page.wait_for_timeout(500)
                done = [p for p in self._poll if isinstance(p, dict) and p.get("results")]
                if done:
                    comp = [p for p in done if p.get("status") == "complete"]
                    api_rows = parse_poll((comp or done)[-1], k)
                    base.source = "api_complete" if comp else "api_partial"
                else:
                    base.block_reason = await self._check_block(None)
            if not base.block_reason and api_rows is None and not k.get("api_only"):
                # results load progressively: wait for cards, then wait until the result count is stable
                try:
                    await self.page.wait_for_selector(k["card_selector"], timeout=k["first_result_timeout_ms"])
                except Exception:
                    base.block_reason = await self._check_block(None)
                last, stable = -1, 0
                for _ in range(int(k["settle_max_s"] / 2)):
                    n = await self.page.locator(k["card_selector"]).count()
                    stable = stable + 1 if n == last else 0
                    last = n
                    if stable >= 2 and n > 0:
                        break
                    await self.page.wait_for_timeout(2000)
                await self._dismiss_popups()
                cards = await self._parse_cards()
                if cards and k.get("expand_providers"):
                    await self._expand_providers(cards, k)
        except Exception as e:
            base.error = f"{type(e).__name__}: {str(e)[:200]}"
        base.latency_ms = int((time.perf_counter() - t0) * 1000)
        base.blocked = base.block_reason is not None

        if self.cfg["browser"].get("save_screenshots") and self.page:
            d = ROOT / self.cfg["storage"]["screenshot_dir"] / self.mode
            d.mkdir(parents=True, exist_ok=True)
            fn = d / f"{self.session_id}_{query['id']}_r{round_no}_{int(time.time())}.png"
            try:
                await self.page.screenshot(path=str(fn))
                base.screenshot = str(fn.relative_to(ROOT))
            except Exception:
                pass

        out = []
        if api_rows:
            for row in api_rows:
                r = LoadResult(price=row["price"], source=row["source"], status=base.status,
                               latency_ms=base.latency_ms, screenshot=base.screenshot, raw=row["raw"],
                               list_price=row.get("total"))
                self._write({"id": f"{query['id']}|{row['key']}", "category": query["id"],
                             "url": query["url"]}, round_no, ts, r, rank=row["rank"])
                out.append(r)
            if base.blocked and self.cfg["politeness"]["stop_session_on_block"]:
                self.dead = True
            return out
        if base.blocked or base.error or not cards:
            if not base.blocked and not base.error:
                base.error = "no_results_parsed"
            self._write({"id": f"{query['id']}|NONE", "category": query["id"], "url": query["url"]},
                        round_no, ts, base)
            out.append(base)
        else:
            for c in cards:
                for pr in c.get("providers", []):          # expanded seller list: one row per seller
                    r = LoadResult(price=pr["price"], source="provider_list", status=base.status,
                                   latency_ms=base.latency_ms, screenshot=base.screenshot,
                                   raw={"text": pr["text"][:300], "provider": pr["provider"],
                                        "card_text": c["text"][:300]})
                    self._write({"id": f"{query['id']}|{pr['key']}", "category": query["id"],
                                 "url": query["url"]}, round_no, ts, r, rank=c["rank"])
                    out.append(r)
                r = LoadResult(price=c["price"], source="card", status=base.status,
                               latency_ms=base.latency_ms, screenshot=base.screenshot,
                               raw={"text": c["text"][:400], "provider": c["provider"],
                                    "is_ad": c["is_ad"]})
                self._write({"id": f"{query['id']}|{c['key']}", "category": query["id"],
                             "url": query["url"]}, round_no, ts, r, rank=c["rank"])
                out.append(r)
        if base.blocked and self.cfg["politeness"]["stop_session_on_block"]:
            self.dead = True
        return out

    async def _dismiss_popups(self, page=None):
        """Close pop-ups ("Still hoping for a better deal?", price alerts, cookie banners, etc.)."""
        page = page or self.page
        k = self.cfg.get("search", {})
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        for sel in k.get("dismiss_selectors", []):
            try:
                loc = page.locator(sel)
                for i in range(min(await loc.count(), 3)):
                    if await loc.nth(i).is_visible():
                        await loc.nth(i).click(timeout=2000)
                        await page.wait_for_timeout(300)
            except Exception:
                continue

    async def _expand_providers(self, cards: list[dict], k: dict):
        """Click "Select / Seleccionar" on the top N results and read the "choose where to book" seller list.
        Approach: tag existing booking buttons before clicking, then read only rows with newly appeared buttons."""
        mark_js = """(re) => { const r = new RegExp(re, 'i');
            document.querySelectorAll('a,button,[role=button]').forEach(b => {
              if (r.test((b.innerText||'').trim())) b.setAttribute('data-audit-seen','1'); }); }"""
        rows_js = """(re) => { const r = new RegExp(re, 'i'); const out = [];
            const btns = [...document.querySelectorAll('a,button,[role=button]')]
              .filter(b => r.test((b.innerText||'').trim()) && !b.hasAttribute('data-audit-seen'));
            for (const b of btns) {
              let el = b;
              for (let i = 0; i < 8 && el.parentElement; i++) {
                const p = el.parentElement;
                const n = [...p.querySelectorAll('a,button,[role=button]')]
                    .filter(x => r.test((x.innerText||'').trim())).length;
                if (n > 1) break;
                el = p;
              }
              b.setAttribute('data-audit-seen','1');
              out.push(el.innerText || '');
            }
            return out; }"""
        loc = self.page.locator(k["card_selector"])
        expanded = 0
        for c in cards:
            if expanded >= k.get("expand_top_n", 5):
                break
            try:
                await self._dismiss_popups()
                await self.page.evaluate(mark_js, k["book_button_regex"])
                card = loc.nth(c["rank"] - 1)
                btn = card.get_by_text(re.compile(k["select_button_regex"], re.I)).first
                n_pages = len(self.ctx.pages)
                url0 = self.page.url
                await (btn.click(timeout=8000) if await btn.count() else card.click(timeout=8000))
                await self.page.wait_for_timeout(k.get("expand_wait_ms", 4000))
                target = self.page
                if len(self.ctx.pages) > n_pages:            # opened in a new tab
                    target = self.ctx.pages[-1]
                    await target.wait_for_load_state("domcontentloaded")
                    await target.wait_for_timeout(k.get("expand_wait_ms", 4000))
                texts = await target.evaluate(rows_js, k["book_button_regex"])
                provs = []
                for t in texts:
                    lines = [x.strip() for x in t.splitlines() if x.strip()]
                    m = re.search(_PRICE_IN_TEXT, t)
                    price = parse_money(m.group(0)) if m else None
                    if not lines or not price:
                        continue
                    prov = clean_provider(t, k)
                    provs.append({"provider": prov, "price": price, "text": " | ".join(lines),
                                  "key": itinerary_key(c["text"], prov)})
                c["providers"] = provs
                expanded += 1
                if target is not self.page:
                    await target.close()
                elif self.page.url != url0:                  # navigated to a details page: go back to results
                    await self.page.go_back(wait_until="domcontentloaded")
                    await self.page.wait_for_selector(k["card_selector"], timeout=k["first_result_timeout_ms"])
                    loc = self.page.locator(k["card_selector"])
                await asyncio.sleep(random.uniform(1.5, 3.5))
            except Exception as e:
                c["providers"] = []
                c["expand_error"] = f"{type(e).__name__}"

    async def _parse_cards(self) -> list[dict]:
        k = self.cfg["search"]
        loc = self.page.locator(k["card_selector"])
        n = min(await loc.count(), k["max_results"])
        res = []
        for i in range(n):
            card = loc.nth(i)
            try:
                text = " ".join((await card.inner_text(timeout=3000)).split())
            except Exception:
                continue
            price = None
            if k.get("price_selector"):
                pl = card.locator(k["price_selector"])
                if await pl.count():
                    price = _to_float(await pl.first.inner_text())
            if price is None:
                m = re.search(_PRICE_IN_TEXT, text)
                price = parse_money(m.group(0)) if m else None
            provider = ""
            if k.get("provider_selector"):
                pv = card.locator(k["provider_selector"])
                if await pv.count():
                    provider = " ".join((await pv.first.inner_text()).split())
            if not provider and k.get("known_providers"):     # no seller selector configured: identify seller from card text
                m = re.search(r"\b(" + "|".join(map(re.escape, k["known_providers"])) + r")\b", text, re.I)
                provider = m.group(1) if m else ""
            if k.get("key_mode") == "normalized":
                key = itinerary_key(text, provider if k.get("key_include_provider", True) else "")
                is_ad = bool(re.search(k.get("ad_pattern", r"\bAd\b|Sponsored"), text))
                if is_ad and k.get("skip_ads", True):
                    continue
                if price:
                    res.append({"rank": i + 1, "price": price, "provider": provider,
                                "text": text, "key": key, "is_ad": is_ad})
                continue
            # itinerary fingerprint: remove prices, "x% off", "deal" and other volatile text
            key_text = re.sub(_PRICE_IN_TEXT, "", text)
            for pat in k.get("volatile_text_patterns", []):
                key_text = re.sub(pat, "", key_text, flags=re.I)
            key_text = " ".join(key_text.split()).lower()
            if k.get("key_include_provider", True) and provider:
                key_text += "|" + provider.lower()
            key = hashlib.sha1(key_text.encode()).hexdigest()[:12]
            is_ad = bool(re.search(k.get("ad_pattern", r"\bAd\b|Sponsored"), text))
            if is_ad and k.get("skip_ads", True):
                continue
            if price:
                res.append({"rank": i + 1, "price": price, "provider": provider,
                            "text": text, "key": key, "is_ad": is_ad})
        return res

    async def restart(self):
        """Switch to a fresh browser context but keep the same session_id (Kayak caches routes searched in a session,
        so a second load no longer calls the poll API; hence restart every round). The persona is re-cultivated."""
        if self.ctx:
            await self.ctx.close()
        self.dead = False
        await self.start()
        if self.persona and self.cfg.get("search", {}).get("personas", {}).get(self.persona):
            await self.cultivate(self.cfg["search"]["personas"][self.persona])

    async def close(self):
        if self.ctx:
            await self.ctx.close()


def parse_poll(d: dict, k: dict) -> list[dict]:
    """Parse Kayak's /i/api/search/dynamic/flights/poll response.

    One row per booking option (bookingOptions = one seller's offer) of each organic result (type=core):
      source="api"  item = flight (legId) x seller (providerCode) x fare class (cabin+baggage)  <- same-seller comparison
    Plus one lowest-price row per result:
      source="card" item = flight (legId) x fare class                                         <- lowest price consumers see
    legId / providerCode / cabin codes are language-independent, so English and Spanish sites match directly.
    Ads (type other than core, e.g. inlineAd) are skipped by default."""
    provs = d.get("providers") or {}
    rows = []
    rank = 0
    for res in d.get("results") or []:
        if res.get("type") != "core":
            if k.get("skip_ads", True):
                continue
        rank += 1
        if rank > k.get("max_results", 50):
            break
        best = None
        for bo in res.get("bookingOptions") or []:
            dp = (bo.get("displayPrice") or {})
            price = dp.get("price")
            if price is None:
                continue
            legs = [lf.get("legId", "") for lf in bo.get("legFarings") or []]
            cabins = [sf.get("cabinCode", "") for lf in bo.get("legFarings") or []
                      for sf in lf.get("segmentFarings") or []]
            am = {a.get("type"): a for a in bo.get("fareAmenities") or []}
            chk = (am.get("CHECKED_BAG") or {})
            # fare-class signature: cabin + all amenities (carry-on/checked/seat/change/refund...), all language-independent codes
            amen = ",".join(f"{t[:4]}:{(am[t].get('restriction') or '')[:3]}" for t in sorted(am))
            fare_sig = f"{''.join(cabins)}/{amen}/k{chk.get('includedCheckedBagCount', '')}"
            pc = bo.get("providerCode", "")
            pinfo = provs.get(pc) or {}
            is_air = ((pinfo.get("providerQualityScore") or {}).get("badgeType") == "AIRLINE")
            total = ((bo.get("fees") or {}).get("totalPrice") or {}).get("price")
            raw = {"provider": pc, "provider_name": pinfo.get("displayName", ""), "is_airline": is_air,
                   "legs": legs, "fare": fare_sig, "fare_name": bo.get("localizedFareDisplayNames"),
                   "currency": dp.get("currency"), "country": bo.get("countryName"),
                   "instant_book": (bo.get("flags") or {}).get("isInstantBook", False),
                   "result_id": res.get("resultId")}
            key_txt = "|".join(legs) + "|" + pc + "|" + fare_sig
            rows.append({"key": hashlib.sha1(key_txt.encode()).hexdigest()[:12], "price": float(price),
                         "total": float(total) if total is not None else None, "rank": rank,
                         "source": "api", "raw": raw})
            if best is None or price < best[0]:
                best = (price, legs, fare_sig, raw)
        # several branded fares under the same seller and amenity signature (e.g. Base / Standard / Refundable): number them by price
        grp = collections.defaultdict(list)
        for row in rows:
            if row.get("_res") is None and row["rank"] == rank and row["source"] == "api":
                grp[(tuple(row["raw"]["legs"]), row["raw"]["provider"], row["raw"]["fare"])].append(row)
        for g in grp.values():
            for j, row in enumerate(sorted(g, key=lambda r: r["price"])):
                row["raw"]["fare_ordinal"] = j
                kt = "|".join(row["raw"]["legs"]) + "|" + row["raw"]["provider"] + "|" + row["raw"]["fare"] + f"|{j}"
                row["key"] = hashlib.sha1(kt.encode()).hexdigest()[:12]
                row["_res"] = rank
        if best:
            key_txt = "|".join(best[1]) + "|" + best[2]
            rows.append({"key": hashlib.sha1(("card|" + key_txt).encode()).hexdigest()[:12],
                         "price": float(best[0]), "rank": rank, "source": "card",
                         "raw": {**best[3], "note": "min over sellers"}})
    for row in rows:
        row.pop("_res", None)
    return rows


def clean_provider(row_text: str, k: dict) -> str:
    """Seller name = text before the price, minus labels such as "Airline / Aerolínea / Precio más bajo"."""
    head = re.split(_PRICE_IN_TEXT, row_text)[0]
    if k.get("known_providers"):
        m = re.search(r"\b(" + "|".join(map(re.escape, k["known_providers"])) + r")\b", head, re.I)
        if m:
            return m.group(1)
    first = next((x.strip() for x in head.splitlines() if x.strip()), head.strip())
    for b in k.get("provider_badges", []):
        first = re.sub(r"\s*\b" + re.escape(b) + r"\b\s*", " ", first, flags=re.I)
    return " ".join(first.split())


_TIME = re.compile(r"\b(\d{1,2}):(\d{2})\s*([ap])?\.?\s*m?\.?", re.I)


def itinerary_key(text: str, provider: str = "") -> str:
    """Cross-language itinerary fingerprint: departure/arrival time (24-hour) + day offset + airport codes + seller.
    e.g. "11:00 pm – 5:10 am +2 ... SFO-ICN" and "23:00 – 5:10 +2 ... SFO ... ICN" give the same fingerprint."""
    times = []
    for h, m, ap in _TIME.findall(text)[:2]:
        h = int(h)
        if ap:
            h = h % 12 + (12 if ap.lower() == "p" else 0)
        times.append(f"{h:02d}{m}")
    plus = re.findall(r"\+(\d)\b", text[:120])
    codes = sorted(set(re.findall(r"\b[A-Z]{3}\b", text)) - {"USD", "EUR", "MXN", "CAD", "GBP"})
    raw = "|".join(["-".join(times), plus[0] if plus else "0", ",".join(codes), provider.lower().strip()])
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


async def polite_sleep(cfg: dict):
    p = cfg["politeness"]
    await asyncio.sleep(random.uniform(p["min_delay_s"], p["max_delay_s"]))


async def launch(cfg: dict):
    pw = await async_playwright().start()
    engine = getattr(pw, cfg["browser"]["engine"])
    browser = await engine.launch(headless=cfg["browser"]["headless"])
    return pw, browser
