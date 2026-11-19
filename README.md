# S&P 500 price returns by sector and industry

Live page: https://jcheels1.github.io/SP500-returns/

A dashboard of 6-month and 12-month price returns for every S&P 500 constituent, nested by GICS sector › industry (or industry group) › security, with cap-weighted, equal-weighted and median group returns and the share of names down in each group.

## Data
- **Constituents and weights:** State Street SPY daily holdings file.
- **Classification:** GICS sub-industry per ticker from Wikipedia's S&P 500 list; hierarchy from Wikipedia's GICS table. Manual overrides are in `SUB_INDUSTRY_OVERRIDES` in `scripts/build.py`.
- **Prices:** Nasdaq.com daily closes (Yahoo Finance as fallback). Returns are price returns (no dividends). Spin-offs inside the window are adjusted (`SPINOFFS` in `scripts/build.py`).

## Refresh
`.github/workflows/refresh.yml` runs every weekday at 6:47 AM Central and on demand (Actions → Refresh data → Run workflow). It rebuilds `data.json`, checks it, logs constituent adds and deletions, and commits it. GitHub Pages republishes on each commit.

Run locally: `python scripts/build.py --workdir /tmp/spx --out data.json --prev data.json`
