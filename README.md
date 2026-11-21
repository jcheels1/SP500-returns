# S&P 500 price returns by sector and industry

Live page: https://jcheels1.github.io/SP500-returns/

Two tabs: (1) 6-month and 12-month price returns for every S&P 500 constituent, nested by GICS sector › industry (or industry group) › security, with cap-weighted, equal-weighted and median group returns and the share of names down in each group; (2) moving averages: a daily chart since 9/30/2022 of the share of constituents above their 20-, 50-, 100- and 200-day simple moving averages (with SPY candles), and each stock's position against those averages, with group-level breadth. `breadth.json` holds the daily series.

## Data
- **Constituents and weights:** State Street SPY daily holdings file.
- **Classification:** GICS sub-industry per ticker from Wikipedia's S&P 500 list; hierarchy from Wikipedia's GICS table. Manual overrides are in `SUB_INDUSTRY_OVERRIDES` in `scripts/build.py`.
- **Prices:** Nasdaq.com daily closes (Yahoo Finance as fallback). Returns are price returns (no dividends). Spin-offs inside the window are adjusted (`SPINOFFS` in `scripts/build.py`).

## Refresh
`.github/workflows/refresh.yml` runs every weekday at 6:47 AM Central and on demand (Actions → Refresh data → Run workflow). It rebuilds `data.json`, checks it, logs constituent adds and deletions, and commits it. GitHub Pages republishes on each commit.

Run locally: `python scripts/build.py --workdir /tmp/spx --out data.json --prev data.json`

## Style weights page (`style-weights/`)

Live page: https://jcheels1.github.io/SP500-returns/style-weights/

Compares the weights of the S&P 500, S&P 500 Value and S&P 500 Growth, nested by GICS sector › industry group › company. Every level rolls up and shows the weight in each index, the differences in percentage points (Value − S&P 500, Growth − S&P 500, Growth − Value), and how much of each company's or group's market cap sits in Value vs Growth.

- **Weights:** State Street daily holdings files for SPY, SPYV and SPYG (VOOV and VOOG track the same style indices). Cash and non-equity lines are excluded; stock weights are rescaled to 100%.
- **Classification:** same Wikipedia sources and `SUB_INDUSTRY_OVERRIDES` as `scripts/build.py`; the last good mapping is cached in `style-weights/data/classification_cache.json`.
- **Value / Growth split:** S&P 500 weight = a × Value weight + (1 − a) × Growth weight, where a is Value's share of S&P 500 cap, solved by least squares from the holdings.
- **Refresh:** `.github/workflows/refresh-style-weights.yml`, weekdays at 7:17 AM and 5:43 PM Central and on demand (Actions → Refresh style weights → Run workflow). Run locally: `python scripts/build_style_weights.py`.
