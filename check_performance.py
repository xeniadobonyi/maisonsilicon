#!/usr/bin/env python3
"""
Weekly performance check for the brand register — CrUX field-data edition.

Reads brands.json and, for each brand's ORIGIN, pulls real-user Core Web Vitals
from Google's Chrome UX Report (CrUX) at the 75th percentile over the rolling
28-day window — the same field data Google uses to judge sites. It scores each
brand 0-100 / 1-10, and on every run rebuilds results/history.csv from the CrUX
History API so you get up to ~25 weeks of real trend without waiting.

Why field data and not Lighthouse: lab scores describe one synthetic run on one
machine and are noisy. CrUX is thousands of real sessions, so it's stable and
representative — and it's the only source that reports real INP.

No third-party dependencies — standard library only.

Usage:
    CRUX_API_KEY=xxxx python check_performance.py
    python check_performance.py --dry-run          # synthetic data, no key
"""

import argparse
import csv
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

RECORD_URL = "https://chromeuxreport.googleapis.com/v1/records:queryRecord"
HISTORY_URL = "https://chromeuxreport.googleapis.com/v1/records:queryHistoryRecord"

CWV = ("largest_contentful_paint", "interaction_to_next_paint", "cumulative_layout_shift")
DIAGNOSTICS = ("first_contentful_paint", "experimental_time_to_first_byte")
FORM_FACTORS = ("PHONE", "DESKTOP")

# "Good" boundaries per the published Core Web Vitals thresholds.
GOOD = {
    "largest_contentful_paint": 2500,     # ms
    "interaction_to_next_paint": 200,     # ms
    "cumulative_layout_shift": 0.1,       # unitless
}

# Piecewise-linear scoring anchors: (p75 value, score 0-100). Between anchors we
# interpolate; outside the ends we clamp. Tune these to taste — they're the whole
# opinion of the scoring model, kept explicit on purpose.
ANCHORS = {
    "largest_contentful_paint": [(0, 100), (2500, 90), (4000, 50), (6000, 0)],
    "interaction_to_next_paint": [(0, 100), (200, 90), (500, 50), (1000, 0)],
    "cumulative_layout_shift": [(0.0, 100), (0.1, 90), (0.25, 50), (0.5, 0)],
}


# --------------------------------------------------------------------------
# Networking
# --------------------------------------------------------------------------
def _post(url, body, timeout=60, retries=3):
    data = json.dumps(body).encode("utf-8")
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json",
                         "User-Agent": "brand-register-perf/2.0"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None  # no CrUX data for this origin/url — expected sometimes
            if e.code == 429 and attempt < retries:  # rate limited
                time.sleep(5 * (attempt + 1))
                continue
            last_err = e
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
    raise RuntimeError(f"CrUX request failed for {body}: {last_err}")


def fetch_record(origin, form_factor, key):
    return _post(f"{RECORD_URL}?key={key}", {"origin": origin, "formFactor": form_factor})


def fetch_history(origin, form_factor, key):
    return _post(f"{HISTORY_URL}?key={key}", {"origin": origin, "formFactor": form_factor})


# --------------------------------------------------------------------------
# Parsing + scoring (pure functions)
# --------------------------------------------------------------------------
def _num(v):
    if v is None:
        return None
    try:
        return float(v)  # CLS often arrives as a string like "0.03"
    except (TypeError, ValueError):
        return None


def parse_record(resp):
    """CrUX queryRecord response -> {metric: p75_value}. Missing -> None."""
    if not resp:
        return None
    metrics = resp.get("record", {}).get("metrics", {})
    out = {}
    for key in CWV + DIAGNOSTICS:
        out[key] = _num(metrics.get(key, {}).get("percentiles", {}).get("p75"))
    return out


def score_metric(key, p75):
    if p75 is None:
        return None
    anchors = ANCHORS[key]
    if p75 <= anchors[0][0]:
        return float(anchors[0][1])
    if p75 >= anchors[-1][0]:
        return float(anchors[-1][1])
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if x0 <= p75 <= x1:
            t = (p75 - x0) / (x1 - x0) if x1 != x0 else 0
            return round(y0 + t * (y1 - y0), 1)
    return None


def score_form_factor(p75s, weights=None):
    """Average the three CWV sub-scores (over whatever is present) -> 0-100."""
    subs, wts = [], []
    for key in CWV:
        s = score_metric(key, p75s.get(key))
        if s is not None:
            subs.append(s)
            wts.append((weights or {}).get(key, 1))
    if not subs:
        return None
    return round(sum(s * w for s, w in zip(subs, wts)) / sum(wts), 1)


def cwv_pass(p75s):
    vals = [p75s.get(k) for k in CWV]
    if any(v is None for v in vals):
        return None  # can't certify a pass without all three
    return all(p75s[k] <= GOOD[k] for k in CWV)


def blend(phone_score, desktop_score, mobile_weight):
    if phone_score is not None and desktop_score is not None:
        return round(phone_score * mobile_weight + desktop_score * (1 - mobile_weight), 1)
    return phone_score if phone_score is not None else desktop_score


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------
def parse_history(resp):
    """queryHistoryRecord -> list of (date_str, {metric: p75}) oldest->newest."""
    if not resp:
        return []
    rec = resp.get("record", {})
    periods = rec.get("collectionPeriods", [])
    metrics = rec.get("metrics", {})
    series = {}
    for key in CWV:
        series[key] = metrics.get(key, {}).get("percentilesTimeseries", {}).get("p75s", [])
    out = []
    for i, period in enumerate(periods):
        d = period.get("lastDate", {})
        try:
            date_str = f"{d['year']:04d}-{d['month']:02d}-{d['day']:02d}"
        except (KeyError, TypeError):
            continue
        point = {key: _num(series[key][i]) if i < len(series[key]) else None for key in CWV}
        out.append((date_str, point))
    return out


def history_rows_for_brand(brand, key, mobile_weight, dry_run, log):
    """Build weekly rows (date, blended score, per-CWV p75) for one brand."""
    origin = brand["origin"]
    if dry_run:
        phone = synthetic_history("PHONE")
        desktop = synthetic_history("DESKTOP")
    else:
        phone = parse_history(fetch_history(origin, "PHONE", key))
        desktop = parse_history(fetch_history(origin, "DESKTOP", key))
    desktop_by_date = {d: p for d, p in desktop}
    rows = []
    for date_str, p_point in phone:
        d_point = desktop_by_date.get(date_str, {})
        p_score = score_form_factor(p_point)
        d_score = score_form_factor(d_point)
        blended = blend(p_score, d_score, mobile_weight)
        if blended is None:
            continue
        rows.append({
            "date": date_str, "brand": brand["name"], "category": brand.get("category"),
            "score_0_100": blended, "score_1_10": round(blended / 10, 1),
            "phone_lcp": round(p_point["largest_contentful_paint"]) if p_point.get("largest_contentful_paint") else None,
            "phone_inp": round(p_point["interaction_to_next_paint"]) if p_point.get("interaction_to_next_paint") else None,
            "phone_cls": round(p_point["cumulative_layout_shift"], 3) if p_point.get("cumulative_layout_shift") else None,
        })
    log(f"    history: {len(rows)} weekly points")
    return rows


HISTORY_FIELDS = ["date", "brand", "category", "score_0_100", "score_1_10",
                  "phone_lcp", "phone_inp", "phone_cls"]


def merge_history(path, new_rows):
    """Append + dedup by (date, brand) so re-runs stay correct and history grows."""
    merged = {}
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                merged[(row["date"], row["brand"])] = row
    for row in new_rows:
        merged[(row["date"], row["brand"])] = row
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        w.writeheader()
        for kkey in sorted(merged):
            w.writerow(merged[kkey])
    return len(merged)


# --------------------------------------------------------------------------
# Synthetic data for --dry-run
# --------------------------------------------------------------------------
def synthetic_point(form_factor):
    slow = form_factor == "PHONE"
    return {
        "largest_contentful_paint": random.uniform(2600, 4800) if slow else random.uniform(1400, 2900),
        "interaction_to_next_paint": random.uniform(160, 420) if slow else random.uniform(80, 220),
        "cumulative_layout_shift": round(random.uniform(0.02, 0.22), 3),
        "first_contentful_paint": random.uniform(1400, 2800),
        "experimental_time_to_first_byte": random.uniform(200, 900),
    }


def synthetic_history(form_factor):
    rows, y, m = [], 2026, 3
    for _ in range(25):
        rows.append((f"{y:04d}-{m:02d}-15", synthetic_point(form_factor)))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return rows


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def run_brand(brand, key, mobile_weight, dry_run, log):
    origin = brand["origin"]
    ff_out, ff_scores = {}, {}
    for ff in FORM_FACTORS:
        p75s = synthetic_point(ff) if dry_run else parse_record(fetch_record(origin, ff, key))
        if p75s is None:
            ff_out[ff.lower()] = None
            ff_scores[ff] = None
            log(f"  {ff:<7} no field data")
            continue
        s = score_form_factor(p75s)
        ff_scores[ff] = s
        ff_out[ff.lower()] = {
            "score": s,
            "cwv_pass": cwv_pass(p75s),
            "lcp_ms": round(p75s["largest_contentful_paint"]) if p75s.get("largest_contentful_paint") else None,
            "inp_ms": round(p75s["interaction_to_next_paint"]) if p75s.get("interaction_to_next_paint") else None,
            "cls": p75s.get("cumulative_layout_shift"),
            "fcp_ms": round(p75s["first_contentful_paint"]) if p75s.get("first_contentful_paint") else None,
            "ttfb_ms": round(p75s["experimental_time_to_first_byte"]) if p75s.get("experimental_time_to_first_byte") else None,
        }
        log(f"  {ff:<7} score={s} pass={ff_out[ff.lower()]['cwv_pass']}")

    score_100 = blend(ff_scores["PHONE"], ff_scores["DESKTOP"], mobile_weight)
    return {
        "name": brand["name"],
        "category": brand.get("category"),
        "origin": origin,
        "field_data": score_100 is not None,
        "performance": {
            "score_0_100": score_100,
            "score_1_10": round(score_100 / 10, 1) if score_100 is not None else None,
            "phone": ff_out["phone"],
            "desktop": ff_out["desktop"],
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="brands.json")
    ap.add_argument("--out", default="results/latest.json")
    ap.add_argument("--history", default="results/history.csv")
    ap.add_argument("--mobile-weight", type=float, default=0.65)
    ap.add_argument("--sleep", type=float, default=0.5)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    key = os.environ.get("CRUX_API_KEY", "")
    if not key and not args.dry_run:
        print("No CRUX_API_KEY set. Use --dry-run to test, or export the key.", file=sys.stderr)
        sys.exit(1)

    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    brands = [b for b in cfg.get("brands", []) if b.get("origin")]
    mobile_weight = cfg.get("mobile_weight", args.mobile_weight)

    def log(msg):
        print(msg, flush=True)

    log(f"CrUX field check — {len(brands)} brands, mobile weight {mobile_weight}")
    brands_out, all_history = [], []
    for brand in brands:
        log(f"- {brand['name']} ({brand['origin']})")
        brands_out.append(run_brand(brand, key, mobile_weight, args.dry_run, log))
        all_history.extend(history_rows_for_brand(brand, key, mobile_weight, args.dry_run, log))
        if args.sleep and not args.dry_run:
            time.sleep(args.sleep)

    brands_out.sort(key=lambda b: (b["performance"]["score_0_100"] or -1), reverse=True)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "config": {
            "source": "CrUX field data (real users, p75, rolling 28 days)",
            "mobile_weight": mobile_weight,
            "cwv_good_thresholds": GOOD,
            "scoring_anchors": ANCHORS,
            "dry_run": args.dry_run,
        },
        "brands": brands_out,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    total = merge_history(args.history, all_history)
    log(f"Wrote {args.out}; history now holds {total} rows across all weeks/brands")


if __name__ == "__main__":
    main()
