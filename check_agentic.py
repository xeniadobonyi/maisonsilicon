#!/usr/bin/env python3
"""
Agentic-commerce readiness checker for the brand register — v2.

What's new vs v1:
  * Renders each PDP in headless Chromium (Playwright) so structured data that
    only appears after JavaScript is counted, and so heavy bot-walls are more
    often cleared. Falls back to a plain fetch when Playwright isn't installed.
  * Reads JSON-LD *and* microdata (itemprop) *and* Open Graph product tags, so
    brands that don't use JSON-LD aren't unfairly zeroed.
  * KPI A (structured data) is scored from the rendered page; KPI B (readable
    without JS) is scored from the RAW pre-render HTML — so B now genuinely
    measures JS-independence (present raw = high, only-after-render = low).

Hybrid scoring, six KPIs (weights):
  A Structured product data ....... 25  [auto] JSON-LD / microdata / OG on the PDP
  B Readable without heavy JS ..... 15  [auto] key fields present in RAW html
  C Agent access .................. 10  [auto] robots.txt AI-bot access, sitemap, llms.txt
  D Guest checkout ................ 15  [you]  agentic_manual.json
  E Purchase completability ....... 25  [you]  agentic_manual.json
  F Agentic payment rails ......... 10  [you]  agentic_manual.json

Usage:
    python check_agentic.py                 # renders if Playwright present
    python check_agentic.py --no-render     # plain fetch only
    python check_agentic.py --dry-run       # synthetic, no network
"""

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# Payment rails (F) are intentionally dropped from the weighting: they are
# "none" for every brand today, so the factor discriminates nothing and only
# depresses scores. That industry-wide zero is surfaced as the headline instead.
WEIGHTS = {"A": 25, "B": 15, "C": 10, "D": 20, "E": 30}
TOTAL_WEIGHT = sum(WEIGHTS.values())

PURCHASE = {"open": 100, "partial": 50, "gated": 15, "none": 0}          # E
GUEST = {"yes": 100, "account": 40, "none": 0}                            # D
RAILS = {"live": 100, "announced": 50, "none": 0}                         # F

AI_AGENTS = ["gptbot", "claudebot", "perplexitybot", "google-extended",
             "ccbot", "bytespider", "applebot-extended", "amazonbot"]

BLOCK_MARKERS = [
    "just a moment", "captcha", "access denied", "pardon our interruption",
    "request unsuccessful", "enable javascript and cookies",
    "cf-browser-verification", "px-captcha", "are you a human", "incapsula",
    "attention required",
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Headers that make a headless context resemble a real Chrome.
CH_HEADERS = {
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "sec-ch-ua": '"Chromium";v="131", "Google Chrome";v="131", "Not.A/Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Upgrade-Insecure-Requests": "1",
}

# Injected before any page script runs: erase the obvious automation tells.
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['fr-FR','fr','en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
window.chrome = window.chrome || {runtime: {}};
const _q = navigator.permissions && navigator.permissions.query;
if (_q) navigator.permissions.query = (p) =>
  p && p.name === 'notifications'
    ? Promise.resolve({state: Notification.permission})
    : _q(p);
const _gp = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(p){
  if (p === 37445) return 'Intel Inc.';
  if (p === 37446) return 'Intel Iris OpenGL Engine';
  return _gp.call(this, p);
};
"""

# Best-effort cookie-consent accept (OneTrust + Didomi cover most EU luxury).
CONSENT_SELECTORS = [
    "#onetrust-accept-btn-handler",
    "#didomi-notice-agree-button",
    "button[aria-label*='accept' i]",
    "button[aria-label*='accepter' i]",
    "button:has-text('Tout accepter')",
    "button:has-text('Accept all')",
    "button:has-text('Accepter')",
    "button:has-text('Accept')",
]


# --------------------------------------------------------------------------
# Plain fetch (robots/llms/sitemap, and PDP fallback)
# --------------------------------------------------------------------------
def fetch(url, timeout=20):
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(3_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:  # noqa: BLE001
        return None, ""


# --------------------------------------------------------------------------
# Headless rendering (Playwright) — optional
# --------------------------------------------------------------------------
def playwright_available():
    try:
        import playwright  # noqa: F401
        return True
    except Exception:
        return False


class Renderer:
    """Launch one browser, reuse it across brands. Degrades to unavailable."""
    def __init__(self):
        self.ok = False
        self.err = None
        self._pw = None
        self._browser = None

    def start(self):
        try:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                      "--disable-dev-shm-usage"],
                ignore_default_args=["--enable-automation"],
            )
            self.ok = True
        except Exception as e:  # noqa: BLE001
            self.err = f"{type(e).__name__}: {e}"
            self.ok = False
        return self

    def render(self, url, _retry=True):
        if not self.ok:
            return None, "unavailable"
        ctx = None
        try:
            ctx = self._browser.new_context(
                user_agent=UA, locale="fr-FR", timezone_id="Europe/Paris",
                viewport={"width": 1366, "height": 900}, extra_http_headers=CH_HEADERS)
            ctx.add_init_script(STEALTH_JS)
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            # dismiss a cookie wall if one is up — it can gate the product markup
            for sel in CONSENT_SELECTORS:
                try:
                    btn = page.locator(sel).first
                    if btn.count() and btn.is_visible():
                        btn.click(timeout=2000)
                        break
                except Exception:  # noqa: BLE001
                    pass
            # give client-rendered structured data a chance to appear
            try:
                page.wait_for_selector("script[type='application/ld+json']", timeout=6000)
            except Exception:  # noqa: BLE001
                pass
            try:
                page.wait_for_load_state("networkidle", timeout=6000)
            except Exception:  # noqa: BLE001
                pass
            html = page.content()
            if _retry and detect_block(200, html):   # one retry on an interstitial
                ctx.close()
                ctx = None
                return self.render(url, _retry=False)
            return html, "ok"
        except Exception as e:  # noqa: BLE001
            return None, f"render error: {type(e).__name__}"
        finally:
            if ctx:
                try:
                    ctx.close()
                except Exception:  # noqa: BLE001
                    pass

    def close(self):
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# robots.txt (C)
# --------------------------------------------------------------------------
def parse_robots(text):
    groups, sitemaps = {}, []
    current = []
    last_was_rule = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            current = []
            last_was_rule = False
            continue
        if ":" not in line:
            continue
        field, value = line.split(":", 1)
        field, value = field.strip().lower(), value.strip()
        if field == "user-agent":
            if last_was_rule:
                current = []
                last_was_rule = False
            agent = value.lower()
            groups.setdefault(agent, set())
            current.append(agent)
        elif field == "disallow" and current:
            for agent in current:
                groups[agent].add(value if value else "<none>")
            last_was_rule = True
        elif field == "allow" and current:
            last_was_rule = True
        elif field == "sitemap":
            sitemaps.append(value)
    return groups, sitemaps


def ai_access_score(groups, robots_missing):
    if robots_missing:
        return 100.0, "no robots.txt (open by default)"
    blocked = []
    for agent in AI_AGENTS:
        rules = groups.get(agent)
        if rules is None:
            rules = groups.get("*", set())
        if "/" in rules:
            blocked.append(agent)
    allowed = len(AI_AGENTS) - len(blocked)
    detail = "all AI agents allowed" if not blocked else "blocks " + ", ".join(blocked)
    return round(100 * allowed / len(AI_AGENTS), 1), detail


def score_access(origin, dry_run):
    if dry_run:
        return round(random.uniform(40, 100), 1), {"note": "dry-run synthetic"}
    base = origin.rstrip("/")
    r_status, r_text = fetch(base + "/robots.txt")
    robots_missing = (r_status == 404) or (r_status is None)
    groups, sitemaps = parse_robots(r_text) if r_text else ({}, [])
    ai_score, ai_detail = ai_access_score(groups, robots_missing)
    has_sitemap = bool(sitemaps)
    if not has_sitemap:
        s_status, _ = fetch(base + "/sitemap.xml")
        has_sitemap = s_status == 200
    l_status, _ = fetch(base + "/llms.txt")
    has_llms = l_status == 200
    c = round(ai_score * 0.5 + (100 if has_sitemap else 0) * 0.3 + (100 if has_llms else 0) * 0.2, 1)
    return c, {"ai_access": ai_detail, "ai_score": ai_score,
               "sitemap": has_sitemap, "llms_txt": has_llms}


# --------------------------------------------------------------------------
# Product signals: JSON-LD + microdata + Open Graph
# --------------------------------------------------------------------------
JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.I | re.S)

META = {
    "og_title": re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', re.I),
    "og_type_product": re.compile(r'<meta[^>]+property=["\']og:type["\'][^>]+content=["\']product', re.I),
    "product_price": re.compile(r'<meta[^>]+property=["\']product:price:amount["\'][^>]+content=["\']([^"\']+)', re.I),
    "og_price": re.compile(r'<meta[^>]+property=["\']og:price:amount["\'][^>]+content=["\']([^"\']+)', re.I),
    "product_avail": re.compile(r'<meta[^>]+property=["\']product:availability["\'][^>]+content=["\']([^"\']+)', re.I),
    "microdata_product": re.compile(r'itemtype=["\'][^"\']*schema\.org/Product', re.I),
    "microdata_price": re.compile(r'itemprop=["\']price["\']', re.I),
    "microdata_avail": re.compile(r'itemprop=["\']availability["\']', re.I),
    "microdata_name": re.compile(r'itemprop=["\']name["\']', re.I),
}


def extract_jsonld(html):
    objs = []
    for block in JSONLD_RE.findall(html):
        block = block.strip()
        if not block:
            continue
        try:
            data = json.loads(block)
        except (ValueError, TypeError):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                if isinstance(node.get("@graph"), list):
                    stack.extend(node["@graph"])
                objs.append(node)
    return objs


def _types(node):
    t = node.get("@type", "")
    return [str(x).lower() for x in (t if isinstance(t, list) else [t])]


def find_products(objs):
    # "product" as a substring catches Product, ProductGroup, ProductModel,
    # IndividualProduct, etc. — markup shapes that an exact/suffix match missed.
    return [o for o in objs if any("product" in t for t in _types(o))]


def _offers(product):
    off = product.get("offers")
    if off is None:
        return []
    return off if isinstance(off, list) else [off]


def _offer_price(offer):
    if not isinstance(offer, dict):
        return None
    if offer.get("price") not in (None, ""):
        return offer.get("price")
    spec = offer.get("priceSpecification")
    if isinstance(spec, dict) and spec.get("price") not in (None, ""):
        return spec.get("price")
    if isinstance(spec, list):
        for s in spec:
            if isinstance(s, dict) and s.get("price") not in (None, ""):
                return s.get("price")
    return None


def product_signals(html):
    """Union of JSON-LD, microdata, and OG signals. Returns presence flags."""
    sig = {"name": False, "price": False, "availability": False, "product": False, "via": []}
    if not html:
        return sig

    products = find_products(extract_jsonld(html))
    if products:
        sig["product"] = True
        sig["via"].append("json-ld")
        for p in products:
            if p.get("name"):
                sig["name"] = True
            for off in _offers(p):
                if _offer_price(off) is not None:
                    sig["price"] = True
                if isinstance(off, dict) and off.get("availability"):
                    sig["availability"] = True

    if META["microdata_product"].search(html):
        sig["product"] = True
        sig["via"].append("microdata")
        if META["microdata_price"].search(html):
            sig["price"] = True
        if META["microdata_avail"].search(html):
            sig["availability"] = True
        if META["microdata_name"].search(html):
            sig["name"] = True

    og_hit = False
    if META["og_type_product"].search(html):
        sig["product"] = True
        og_hit = True
    if META["product_price"].search(html) or META["og_price"].search(html):
        sig["price"] = True
        og_hit = True
    if META["product_avail"].search(html):
        sig["availability"] = True
        og_hit = True
    if META["og_title"].search(html):
        sig["name"] = True
    if og_hit:
        sig["via"].append("og")

    # Fallback: a Product declared in JSON-LD that failed to parse (malformed
    # JSON, HTML entities) still counts as a product signal.
    if not sig["product"] and re.search(r'"@type"\s*:\s*"[^"]*[Pp]roduct"', html):
        sig["product"] = True
        sig["via"].append("jsonld-raw")

    return sig


def score_structured(sig):
    if sig["product"] and sig["price"] and sig["availability"]:
        return 100.0
    if sig["product"] and sig["price"]:
        return 70.0
    if sig["product"]:
        return 40.0
    if sig["price"]:
        return 45.0
    if sig["name"]:
        return 15.0
    return 0.0


def score_readability(sig):
    hits = sig["name"] + sig["price"] + sig["availability"]
    return round(100 * hits / 3, 1)


def detect_block(status, html):
    if status in (401, 403, 429, 503):
        return True
    return any(m in html[:5000].lower() for m in BLOCK_MARKERS)


def check_pdp(pdp, dry_run, renderer):
    if dry_run:
        a = random.choice([0, 40, 70, 100, 100])
        return a, round(min(100, a + random.uniform(-15, 15)), 1), {"dry_run": True}, False

    raw_status, raw_html = fetch(pdp)
    raw_blocked = (raw_status is None) or detect_block(raw_status, raw_html)

    rendered_html, render_note = (None, "no-render")
    if renderer and renderer.ok:
        rendered_html, render_note = renderer.render(pdp)
        if rendered_html and detect_block(200, rendered_html):
            rendered_html, render_note = None, "render blocked"

    # KPI A from the best available page (rendered preferred)
    if rendered_html:
        a_html, a_src = rendered_html, "rendered"
    elif not raw_blocked and raw_html:
        a_html, a_src = raw_html, "raw"
    else:
        return None, None, {"blocked": True, "raw_status": raw_status, "render": render_note}, True

    sig_a = product_signals(a_html)
    a = score_structured(sig_a)

    # KPI B from RAW html only (measures JS-independence)
    if not raw_blocked and raw_html:
        b = score_readability(product_signals(raw_html))
    else:
        b = None

    return a, b, {"a_source": a_src, "raw_blocked": raw_blocked, "render": render_note,
                  "signals": sig_a}, False


# --------------------------------------------------------------------------
# Combine (pure)
# --------------------------------------------------------------------------
def combine(components):
    num = den = 0.0
    for k, w in WEIGHTS.items():
        v = components.get(k)
        if v is not None:
            num += v * w
            den += w
    if den == 0:
        return None, 0
    return round(num / den, 1), round(den / TOTAL_WEIGHT * 100)


def manual_scores(entry):
    e = entry or {}
    return {"D": GUEST.get(e.get("guest_checkout")),
            "E": PURCHASE.get(e.get("purchase")),
            "F": RAILS.get(e.get("payment_rails"))}


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def run(brands, manual, dry_run, sleep, renderer, log):
    out = []
    for b in brands:
        name, origin, pdp = b["name"], b["origin"], (b.get("pdp") or "").strip()
        log(f"- {name}")
        c, c_det = score_access(origin, dry_run)
        if pdp:
            a, bb, ab_det, blocked = check_pdp(pdp, dry_run, renderer)
        else:
            a, bb, ab_det, blocked = None, None, {"note": "no pdp url"}, False
        man = manual_scores(manual.get(name))
        comps = {"A": a, "B": bb, "C": c, "D": man["D"], "E": man["E"], "F": man["F"]}
        score, coverage = combine(comps)
        missing = [k for k in WEIGHTS if comps[k] is None]
        log(f"    score={score} coverage={coverage}%"
            + (" blocked" if blocked else "")
            + (f" via={','.join(ab_det.get('signals', {}).get('via', []))}" if pdp and not blocked and 'signals' in ab_det else "")
            + (f" missing={','.join(missing)}" if missing else ""))
        out.append({
            "name": name, "origin": origin, "pdp": pdp or None,
            "agentic": {"score_0_100": score,
                        "score_1_10": round(score / 10, 1) if score is not None else None,
                        "coverage_pct": coverage, "blocked": blocked, "missing": missing},
            "components": comps,
            "detail": {"access": c_det, "pdp": ab_det, "manual": manual.get(name) or {}},
        })
        if sleep and not dry_run:
            time.sleep(sleep)
    out.sort(key=lambda x: (x["agentic"]["score_0_100"] is None, -(x["agentic"]["score_0_100"] or 0)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="brands.json")
    ap.add_argument("--manual", default="agentic_manual.json")
    ap.add_argument("--out", default="results/agentic.json")
    ap.add_argument("--sleep", type=float, default=1.0)
    ap.add_argument("--no-render", action="store_true", help="skip headless rendering")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as f:
        brands = [b for b in json.load(f).get("brands", []) if b.get("origin")]
    manual = {}
    if os.path.exists(args.manual):
        with open(args.manual, encoding="utf-8") as f:
            manual = json.load(f)
    else:
        print(f"(no {args.manual} yet — D/E/F blank; auto signals only)", file=sys.stderr)

    def log(m):
        print(m, flush=True)

    renderer = None
    if not args.dry_run and not args.no_render:
        if playwright_available():
            renderer = Renderer().start()
            log("rendering: Playwright " + ("active" if renderer.ok else f"failed ({renderer.err}); using plain fetch"))
        else:
            log("rendering: Playwright not installed; using plain fetch")

    log(f"Agentic check — {len(brands)} brands, weights {WEIGHTS}")
    try:
        results = run(brands, manual, args.dry_run, args.sleep, renderer, log)
    finally:
        if renderer:
            renderer.close()

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "weights": WEIGHTS,
        "rendered": bool(renderer and renderer.ok),
        "scoring_notes": {"purchase": PURCHASE, "guest_checkout": GUEST, "payment_rails": RAILS},
        "brands": results,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    graded = sum(1 for r in results if r["agentic"]["score_0_100"] is not None)
    log(f"Wrote {args.out} — {graded}/{len(results)} brands scored")


if __name__ == "__main__":
    main()
