#!/usr/bin/env python3
"""
Agentic-commerce readiness checker for the brand register.

Hybrid scoring: a script measures the machine-checkable signals, and you supply
the judgment calls in agentic_manual.json. The two halves combine into one
0-100 / 1-10 agentic score per brand, with a coverage figure so you always know
how much of the grade was actually measured versus blocked or unfilled.

Six KPIs (weights):
  A Structured product data ....... 25  [auto] JSON-LD Product w/ price+stock on the PDP
  B Readable without heavy JS ..... 15  [auto] key fields present in raw HTML
  C Agent access .................. 10  [auto] robots.txt AI-bot access, sitemap, llms.txt
  D Guest checkout ................ 15  [you]  agentic_manual.json
  E Purchase completability ....... 25  [you]  agentic_manual.json  (the gating signal)
  F Agentic payment rails ......... 10  [you]  agentic_manual.json

Auto signals need a PDP URL per brand (the "pdp" field in brands.json). Origin
signals (C) run without one. Luxury sites are heavily bot-protected, so blocked
fetches are expected — they're marked, not guessed, and handed back to you.

No third-party dependencies — standard library only.

Usage:
    python check_agentic.py
    python check_agentic.py --dry-run     # synthetic auto-signals, no network
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

WEIGHTS = {"A": 25, "B": 15, "C": 10, "D": 15, "E": 25, "F": 10}
TOTAL_WEIGHT = sum(WEIGHTS.values())

# Manual enum -> score maps
PURCHASE = {"open": 100, "partial": 50, "gated": 15, "none": 0}          # E
GUEST = {"yes": 100, "account": 40, "none": 0}                            # D
RAILS = {"live": 100, "announced": 50, "none": 0}                         # F

AI_AGENTS = ["gptbot", "claudebot", "perplexitybot", "google-extended",
             "ccbot", "bytespider", "applebot-extended", "amazonbot"]

BLOCK_MARKERS = [
    "just a moment", "captcha", "access denied", "pardon our interruption",
    "request unsuccessful", "enable javascript and cookies",
    "cf-browser-verification", "px-captcha", "are you a human", "incapsula",
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# --------------------------------------------------------------------------
# Networking
# --------------------------------------------------------------------------
def fetch(url, timeout=20):
    """Return (status, text). status None on network failure."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(3_000_000).decode("utf-8", "replace")
            return resp.status, body
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:  # noqa: BLE001
        return None, ""


# --------------------------------------------------------------------------
# robots.txt (C)
# --------------------------------------------------------------------------
def parse_robots(text):
    """Return ({agent_lower: set(disallow_paths)}, [sitemap_urls])."""
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
            if last_was_rule:      # a new record begins after any rule line
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
    """Fraction of major AI agents allowed at root -> 0..100, plus detail."""
    if robots_missing:
        return 100.0, "no robots.txt (open by default)"
    blocked = []
    for agent in AI_AGENTS:
        rules = groups.get(agent)
        if rules is None:
            rules = groups.get("*", set())
        if "/" in rules:  # Disallow: /
            blocked.append(agent)
    allowed = len(AI_AGENTS) - len(blocked)
    detail = "all AI agents allowed" if not blocked else "blocks " + ", ".join(blocked)
    return round(100 * allowed / len(AI_AGENTS), 1), detail


def score_access(origin, dry_run):
    if dry_run:
        return round(random.uniform(40, 100), 1), {"note": "dry-run synthetic"}
    r_status, r_text = fetch(origin.rstrip("/") + "/robots.txt")
    robots_missing = (r_status == 404) or (r_status is None)
    groups, sitemaps = parse_robots(r_text) if r_text else ({}, [])
    ai_score, ai_detail = ai_access_score(groups, robots_missing)

    has_sitemap = bool(sitemaps)
    if not has_sitemap:
        s_status, _ = fetch(origin.rstrip("/") + "/sitemap.xml")
        has_sitemap = s_status == 200

    l_status, _ = fetch(origin.rstrip("/") + "/llms.txt")
    has_llms = l_status == 200

    c = round(ai_score * 0.5 + (100 if has_sitemap else 0) * 0.3 + (100 if has_llms else 0) * 0.2, 1)
    return c, {"ai_access": ai_detail, "ai_score": ai_score,
               "sitemap": has_sitemap, "llms_txt": has_llms}


# --------------------------------------------------------------------------
# PDP structured data + readability (A, B)
# --------------------------------------------------------------------------
JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.I | re.S)


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
                if "@graph" in node and isinstance(node["@graph"], list):
                    stack.extend(node["@graph"])
                objs.append(node)
    return objs


def _types(node):
    t = node.get("@type", "")
    if isinstance(t, list):
        return [str(x).lower() for x in t]
    return [str(t).lower()]


def find_products(objs):
    return [o for o in objs if any("product" == t or t.endswith("product") for t in _types(o))]


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


def score_structured(products):
    """A: quality of JSON-LD product markup on the page."""
    if not products:
        return 0.0, {"products": 0}
    has_price = has_avail = False
    for p in products:
        for off in _offers(p):
            if _offer_price(off) is not None:
                has_price = True
            if isinstance(off, dict) and off.get("availability"):
                has_avail = True
    if has_price and has_avail:
        a = 100.0
    elif has_price:
        a = 70.0
    elif any(_offers(p) for p in products):
        a = 40.0
    else:
        a = 25.0
    return a, {"products": len(products), "price": has_price, "availability": has_avail}


META_RE = {
    "og_title": re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', re.I),
    "product_price": re.compile(r'<meta[^>]+property=["\']product:price:amount["\'][^>]+content=["\']([^"\']+)', re.I),
    "og_price": re.compile(r'<meta[^>]+property=["\']og:price:amount["\'][^>]+content=["\']([^"\']+)', re.I),
    "og_avail": re.compile(r'<meta[^>]+property=["\']product:availability["\'][^>]+content=["\']([^"\']+)', re.I),
}


def score_readability(html, products):
    """B: are name / price / availability present in the raw (un-run) HTML?"""
    name = bool(products and (products[0].get("name"))) or bool(META_RE["og_title"].search(html))
    price = False
    avail = False
    for p in products:
        if p.get("name"):
            name = True
        for off in _offers(p):
            if _offer_price(off) is not None:
                price = True
            if isinstance(off, dict) and off.get("availability"):
                avail = True
    if META_RE["product_price"].search(html) or META_RE["og_price"].search(html):
        price = True
    if META_RE["og_avail"].search(html):
        avail = True
    hits = sum([name, price, avail])
    return round(100 * hits / 3, 1), {"name": name, "price": price, "availability": avail}


def detect_block(status, html):
    if status in (401, 403, 429, 503):
        return True
    low = html[:4000].lower()
    return any(m in low for m in BLOCK_MARKERS)


def check_pdp(pdp, dry_run):
    if dry_run:
        a = random.choice([0, 40, 70, 100, 100])
        return a, round(min(100, a + random.uniform(-10, 20)), 1), {"dry_run": True}, False
    status, html = fetch(pdp)
    if status is None:
        return None, None, {"error": "fetch failed"}, False
    if detect_block(status, html):
        return None, None, {"blocked": True, "status": status}, True
    objs = extract_jsonld(html)
    products = find_products(objs)
    a, a_det = score_structured(products)
    b, b_det = score_readability(html, products)
    return a, b, {"status": status, "structured": a_det, "readability": b_det}, False


# --------------------------------------------------------------------------
# Combine (pure)
# --------------------------------------------------------------------------
def combine(components):
    """components: {A..F: value or None}. Renormalize over what's present."""
    num = den = 0.0
    for k, w in WEIGHTS.items():
        v = components.get(k)
        if v is not None:
            num += v * w
            den += w
    if den == 0:
        return None, 0
    return round(num / den, 1), round(den / TOTAL_WEIGHT * 100)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def manual_scores(entry):
    return {
        "D": GUEST.get((entry or {}).get("guest_checkout")),
        "E": PURCHASE.get((entry or {}).get("purchase")),
        "F": RAILS.get((entry or {}).get("payment_rails")),
    }


def run(brands, manual, dry_run, sleep, log):
    out = []
    for b in brands:
        name, origin, pdp = b["name"], b["origin"], b.get("pdp") or ""
        log(f"- {name}")
        c, c_det = score_access(origin, dry_run)
        if pdp:
            a, bb, ab_det, blocked = check_pdp(pdp, dry_run)
        else:
            a, bb, ab_det, blocked = None, None, {"note": "no pdp url"}, False
        man = manual_scores(manual.get(name))
        comps = {"A": a, "B": bb, "C": c, "D": man["D"], "E": man["E"], "F": man["F"]}
        score, coverage = combine(comps)
        missing = [k for k in WEIGHTS if comps[k] is None]
        log(f"    score={score} coverage={coverage}%"
            + (f" blocked" if blocked else "")
            + (f" missing={','.join(missing)}" if missing else ""))
        out.append({
            "name": name, "origin": origin, "pdp": pdp or None,
            "agentic": {"score_0_100": score, "score_1_10": round(score / 10, 1) if score is not None else None,
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
    ap.add_argument("--sleep", type=float, default=1.5)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as f:
        brands = [b for b in json.load(f).get("brands", []) if b.get("origin")]
    manual = {}
    if os.path.exists(args.manual):
        with open(args.manual, encoding="utf-8") as f:
            manual = json.load(f)
    else:
        print(f"(no {args.manual} yet — D/E/F will be blank; auto signals only)", file=sys.stderr)

    def log(m):
        print(m, flush=True)

    log(f"Agentic check — {len(brands)} brands, weights {WEIGHTS}")
    results = run(brands, manual, args.dry_run, args.sleep, log)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "weights": WEIGHTS,
        "scoring_notes": {
            "purchase": PURCHASE, "guest_checkout": GUEST, "payment_rails": RAILS,
            "auto": "A=structured data, B=raw-HTML readability, C=agent access",
        },
        "brands": results,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    graded = sum(1 for r in results if r["agentic"]["score_0_100"] is not None)
    log(f"Wrote {args.out} — {graded}/{len(results)} brands with a score")


if __name__ == "__main__":
    main()
