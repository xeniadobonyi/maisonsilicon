# Maison & Silicon

A public website that grades luxury maisons on **real-user web performance** and
refreshes once a month. Scores come from Google's Chrome UX Report (CrUX): the
75th-percentile LCP, INP, and CLS across a rolling 28-day window of actual Chrome
traffic — the same field data Google uses to judge sites.

`index.html` is the site itself; the Python engine and the monthly GitHub Action
keep it fed. Nothing to host beyond GitHub Pages, no server, no build step.

**Why field data and not Lighthouse.** Lighthouse is a lab test: one synthetic
run on one machine, noisy from run to run, and not representative of your real
visitors. CrUX is thousands of real sessions, so it's stable — and it's the only
source that reports real INP. The trade-off is that reliable field data is
**origin-level** (the whole site), not per-page, so each maison is graded on its
origin rather than a hand-picked Home/PLP/PDP.

## Files

```
perf-monitor/
├─ index.html                     # the website (reads the results below)
├─ check_performance.py           # the engine (Python stdlib only)
├─ brands.json                    # the maisons and their origins
├─ results/
│  ├─ latest.json                 # newest scores — the site reads this
│  └─ history.csv                 # weekly trend, backfilled ~25 weeks on run one
└─ .github/workflows/
   └─ monthly-perf.yml            # the monthly scheduler
```

## One-time setup

1. **Get a free API key.** In Google Cloud Console, enable the **Chrome UX
   Report API** and create an API key. (This is a *different* API from PageSpeed
   Insights, though one key can enable both.)
2. **Put these files in a GitHub repo.**
3. **Add the key as a secret** named `CRUX_API_KEY`: repo → Settings → Secrets
   and variables → Actions → *New repository secret*.
4. **Verify the origins in `brands.json`.** CrUX is origin-exact. Paste each
   origin into a browser and copy whatever stays in the address bar *after*
   redirects. Louis Vuitton in particular splits traffic across regional
   subdomains (`us.`, `fr.`, `www.`), so confirm the one your audience uses.
5. **Turn on Pages** (see below) to get the live URL.

After that it's automatic: on the 1st of each month the job pulls fresh field
data, commits it, and the site redeploys itself.

## Publish the site (GitHub Pages)

1. Repo → Settings → **Pages** → Source: *Deploy from a branch* → pick your
   branch (e.g. `main`) and `/ (root)`. Save.
2. Wait a minute; GitHub gives you a URL like `https://<you>.github.io/<repo>/`.
   That's the live register.

Because the monthly Action commits to the same branch, every run redeploys the
page — the grades stay current with no manual step.

- Free Pages needs a **public repo** (private Pages needs a paid plan). To keep
  the repo private, the same files deploy free on Netlify, Cloudflare Pages, or
  Vercel.
- The page fetches its data on the same origin, so there's no CORS setup.
- To preview locally, run `python -m http.server` in this folder and open the
  localhost URL. Opening `index.html` directly won't work — browsers block file
  reads over `file://`, and the page says so if you try.

## Running it by hand

- **First run / on demand:** Actions tab → *Monthly performance check* → *Run
  workflow*. Takes under a minute.
- **Locally:** `python check_performance.py --dry-run` (synthetic data, no key)
  or `CRUX_API_KEY=xxxx python check_performance.py`.

## How the grade is built

For each origin, on phone and desktop: pull p75 LCP, INP, CLS. Each metric maps
to a 0-100 sub-score against the published thresholds (good/poor boundaries as
anchors, linear between — the anchors sit at the top of `check_performance.py`
and are yours to tune). The three average to a form-factor score; phone and
desktop blend `0.65 / 0.35` into `score_0_100`, then `÷ 10` for the `1-10` axis.
The site turns that into a letter grade (AAA→CC). `latest.json` also stores a
`cwv_pass` flag (true only when all three vitals are "good") plus FCP and TTFB.

## Trend, even on a monthly schedule

Every run also calls the CrUX **History API**, which returns up to ~25 *weekly*
points regardless of how often you poll. So `history.csv` arrives ~6 months
pre-filled on the first run, and monthly runs still capture every weekly point
(re-runs dedupe by date+brand). The trend line stays week-granular while the
grades refresh monthly.

## If a maison returns no data

Small or heavily region-split origins can lack enough traffic to report; the API
returns a clean 404, and that brand shows as **NR** (not rated) with a null
score rather than breaking the run. Try a regional origin, or fall back to lab
testing for that one brand.
