#!/usr/bin/env python3
"""Build data.json for the S&P 500 price-return dashboard.

Sources
- Constituents and weights: State Street SPY daily holdings file (xlsx).
- GICS sector / sub-industry per ticker: Wikipedia "List of S&P 500 companies"
  (fallback: github.com/datasets/s-and-p-500-companies constituents.csv).
- GICS hierarchy (sub-industry -> industry -> industry group -> sector):
  Wikipedia "Global Industry Classification Standard".
- Prices: Yahoo Finance chart API, daily split-adjusted close (not dividend-adjusted),
  so returns are price returns.

Usage: python3 -I build.py --workdir DIR --out data.json [--prev old_data.json]
"""
import argparse, datetime as dt, io, json, re, sys, time, os
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import openpyxl
from dateutil.relativedelta import relativedelta

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
SPY_URL = "https://www.ssga.com/library-content/products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx"
WIKI_LIST = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
WIKI_GICS = "https://en.wikipedia.org/wiki/Global_Industry_Classification_Standard"
GH_LIST = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
YAHOO = "https://query{n}.finance.yahoo.com/v8/finance/chart/{sym}?range=5y&interval=1d&includeAdjustedClose=true"
NY = ZoneInfo("America/New_York")


def get(url, binary=False, tries=4):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=45)
            if r.status_code == 200:
                return r.content if binary else r.text
            last = f"HTTP {r.status_code}"
        except Exception as e:  # network hiccup
            last = str(e)
        time.sleep(2 + 3 * i)
    raise RuntimeError(f"GET failed {url}: {last}")


# ---------------- constituents ----------------
def load_spy(workdir):
    path = os.path.join(workdir, "spy_holdings.xlsx")
    with open(path, "wb") as f:
        f.write(get(SPY_URL, binary=True))
    ws = openpyxl.load_workbook(path, read_only=True)["holdings"]
    rows = list(ws.iter_rows(values_only=True))
    asof = None
    for r in rows[:5]:
        if r[0] == "Holdings:":
            asof = dt.datetime.strptime(str(r[1]).replace("As of ", "").strip(), "%d-%b-%Y").date().isoformat()
    hdr = next(i for i, r in enumerate(rows) if r[0] == "Name" and r[1] == "Ticker")
    out = []
    for r in rows[hdr + 1:]:
        name, tic, cusip, sedol, w = r[0], r[1], r[2], r[3], r[4]
        if not name or not tic:
            continue
        tic = str(tic).strip()
        # skip cash, CVRs and other stub lines: real equity tickers are letters with optional .X
        if not re.fullmatch(r"[A-Z]{1,5}(\.[A-Z])?", tic) or sedol in (None, "-"):
            continue
        out.append({"ticker": tic, "spy_name": str(name).strip(), "weight": float(w or 0)})
    if len(out) < 480:
        raise RuntimeError(f"Only {len(out)} SPY holdings parsed; file format may have changed")
    return asof, out


def load_gics(workdir):
    html = get(WIKI_GICS)
    tables = pd.read_html(io.StringIO(html))
    t = next(x for x in tables if "Sub-Industry" in x.columns and "Industry Group" in x.columns)
    # Build the hierarchy from the 8-digit sub-industry code (2 = sector, 4 = group, 6 = industry),
    # taking the most common label per code, so a misaligned table row cannot mislabel a level.
    from collections import Counter, defaultdict
    names = {2: defaultdict(Counter), 4: defaultdict(Counter), 6: defaultdict(Counter)}
    subs = []
    for _, r in t.iterrows():
        try:
            code = str(int(float(r["Sub-Industry"])))
        except Exception:
            continue
        if len(code) != 8:
            continue
        subs.append((code, str(r["Sub-Industry.1"]).strip()))
        for n, col in ((2, "Sector"), (4, "Industry Group"), (6, "Industry")):
            try:
                c = str(int(float(r[col])))
            except Exception:
                continue
            if c == code[:n]:
                names[n][c][str(r[col + ".1"]).strip()] += 1
    lab = {n: {c: cnt.most_common(1)[0][0] for c, cnt in names[n].items()} for n in names}
    m = {}
    for code, sub in subs:
        m[norm(sub)] = {"sector": lab[2].get(code[:2], "Unclassified"),
                        "industry_group": lab[4].get(code[:4], "Unclassified"),
                        "industry": lab[6].get(code[:6], "Unclassified"), "sub_industry": sub}
    if len(m) < 150:
        raise RuntimeError(f"GICS table parse looks wrong: {len(m)} sub-industries")
    return m


def load_class(workdir):
    try:
        html = get(WIKI_LIST)
        t = pd.read_html(io.StringIO(html))[0]
        src = "Wikipedia"
    except Exception:
        t = pd.read_csv(io.StringIO(get(GH_LIST)))
        src = "datasets/s-and-p-500-companies (GitHub)"
    m = {}
    for _, r in t.iterrows():
        m[str(r["Symbol"]).strip().replace("-", ".")] = {
            "name": str(r["Security"]).strip(),
            "gics_sector": str(r["GICS Sector"]).strip(),
            "gics_sub": str(r["GICS Sub-Industry"]).strip(),
        }
    return src, m


# Manual GICS sub-industry overrides (applied before the hierarchy lookup), for names Joel
# wants grouped differently from the published classification.
SUB_INDUSTRY_OVERRIDES = {
    "ROP": "Application Software",  # Roper: grouped with software at Joel's request (Nov 2026)
    # Health Care: match Bloomberg's GICS industry grouping (Nov 2026)
    "SOLV": "Health Care Supplies",            # Solventum -> Health Care Equipment & Supplies
    "RVTY": "Life Sciences Tools & Services",  # Revvity -> Life Sciences Tools & Services
    "WST": "Life Sciences Tools & Services",   # West Pharmaceutical -> Life Sciences Tools & Services
}


def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower().replace("&", "and"))


# ---------------- prices ----------------
NASDAQ = "https://api.nasdaq.com/api/quote/{sym}/historical?assetclass={ac}&fromdate={f}&todate={t}&limit=9999"
NASDAQ_HDR = {**UA, "Accept": "application/json, text/plain, */*", "Origin": "https://www.nasdaq.com",
              "Referer": "https://www.nasdaq.com/"}
ETFS = {"SPY", "RSP"}
HISTORY_START = dt.date(2021, 10, 1)   # enough trading days for a 200-day average on 2022-09-30
BREADTH_START = dt.date(2022, 9, 30)
MA_WINDOWS = (20, 50, 100, 200)
OHLC = {}  # full daily bars kept for SPY (price panel on the breadth chart)


def nasdaq_series(tic):
    try:
        return _nasdaq_series(tic)
    except Exception:
        if "." in tic:  # Nasdaq spells some class shares as BF%sl%B
            return _nasdaq_series(tic.replace(".", "%sl%"))
        raise


def _nasdaq_series(tic):
    today = dt.datetime.now(NY).date()
    url = NASDAQ.format(sym=tic, ac="etf" if tic in ETFS else "stocks",
                        f=HISTORY_START.isoformat(), t=today.isoformat())
    last = None
    for i in range(4):
        try:
            r = requests.get(url, headers=NASDAQ_HDR, timeout=30)
            if r.status_code == 200:
                rows = (((r.json().get("data") or {}).get("tradesTable") or {}).get("rows")) or []
                s = {}
                for x in rows:
                    try:
                        d = dt.datetime.strptime(x["date"], "%m/%d/%Y").date()
                        num = lambda v: float(str(v).replace("$", "").replace(",", ""))
                        s[d] = num(x["close"])
                        if tic == "SPY":
                            OHLC.setdefault("SPY", {})[d] = (num(x["open"]), num(x["high"]), num(x["low"]), s[d])
                    except Exception:
                        continue
                if len(s) > 20:
                    return s
                last = "empty"
            else:
                last = f"HTTP {r.status_code}"
        except Exception as e:
            last = str(e)
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"nasdaq {tic}: {last}")


def price_series(tic):
    try:
        return nasdaq_series(tic), "nasdaq"
    except Exception as e1:
        try:
            return yahoo_series(tic), "yahoo"
        except Exception as e2:
            raise RuntimeError(f"{e1}; {e2}")


def yahoo_series(tic):
    sym = tic.replace(".", "-")
    last = None
    for i in range(2):
        try:
            r = requests.get(YAHOO.format(n=1 + (i % 2), sym=sym), headers=UA, timeout=30)
            if r.status_code == 200:
                res = r.json()["chart"]["result"][0]
                ts = res.get("timestamp") or []
                closes = res["indicators"]["quote"][0].get("close") or []
                s = {}
                for t, c in zip(ts, closes):
                    if c is None:
                        continue
                    d = dt.datetime.fromtimestamp(t, NY).date()
                    s[d] = float(c)
                return s
            last = f"HTTP {r.status_code}"
            if r.status_code == 404:
                break
        except Exception as e:
            last = str(e)
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{tic}: {last}")


# Spin-offs inside the 12M window for current constituents. ratio = child shares per parent share,
# in the parent's split-adjusted share units. Parent prices before ex-date are scaled by
# 1 - ratio * child_close(ex) / parent_close(day before ex), the standard price-adjustment factor,
# so the parent's price return is not distorted by the distribution.
SPINOFFS = [
    {"parent": "HON", "child": "HONA", "ex": "2026-06-29", "ratio": 1.0,
     "note": "1 HONA per 2 HON, with 1-for-2 HON reverse split on the same day"},
    {"parent": "FDX", "child": "FDXF", "ex": "2026-06-01", "ratio": 0.5, "note": "1 FDXF per 2 FDX"},
]


def apply_spinoffs(series):
    applied = []
    for sp in SPINOFFS:
        p, c = series.get(sp["parent"]), series.get(sp["child"])
        ex = dt.date.fromisoformat(sp["ex"])
        if not p or not c or ex not in c or ex not in p:
            continue
        prev = max(k for k in p if k < ex)
        f = 1 - sp["ratio"] * c[ex] / p[prev]
        for k in list(p):
            if k < ex:
                p[k] = p[k] * f
        applied.append({**sp, "factor": round(f, 6)})
    return applied


def close_on_or_before(series, d):
    ds = [x for x in series if x <= d]
    if not ds:
        return None, None
    k = max(ds)
    return k, series[k]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--prev")
    ap.add_argument("--breadth-out", help="moving-average breadth series (default: breadth.json next to --out)")
    a = ap.parse_args()
    os.makedirs(a.workdir, exist_ok=True)

    spy_asof, holdings = load_spy(a.workdir)
    gics = load_gics(a.workdir)
    class_src, cls = load_class(a.workdir)

    tickers = [h["ticker"] for h in holdings]
    refs = ["SPY", "RSP"]
    series, errors, psrc = {}, {}, {}
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {t: ex.submit(price_series, t) for t in refs + tickers}
        for i, (t, f) in enumerate(futs.items()):
            try:
                series[t], psrc[t] = f.result()
            except Exception as e:
                errors[t] = str(e)
            if i % 100 == 0:
                print(f"prices {i}/{len(futs)}", file=sys.stderr, flush=True)

    with open(os.path.join(a.workdir, "series.json"), "w") as f:
        json.dump({t: {k.isoformat(): v for k, v in s.items()} for t, s in series.items()}, f)
    spin_applied = apply_spinoffs(series)

    # flag single-day moves over the last ~13 months that look like unadjusted splits or spin-offs
    split_flags = {}
    flag_from = dt.datetime.now(NY).date() - relativedelta(months=13)
    for t, s in series.items():
        ks = [k for k in sorted(s) if k >= flag_from]
        for a_, b_ in zip(ks, ks[1:]):
            ch = s[b_] / s[a_] - 1
            if ch < -0.30 or ch > 0.9:
                split_flags.setdefault(t, []).append(f"{b_}: {ch:+.0%}")

    # Last completed session: drop a bar for today if the US session has not closed yet.
    now = dt.datetime.now(NY)
    spy = series["SPY"]
    if now.date() in spy and (now.hour, now.minute) < (16, 20):
        for s in series.values():
            s.pop(now.date(), None)
    end = max(spy)
    base6 = close_on_or_before(spy, end - relativedelta(months=6))[0]
    base12 = close_on_or_before(spy, end - relativedelta(months=12))[0]

    def rets(s):
        if not s:
            return None, None, None
        e = close_on_or_before(s, end)
        if e[0] is None or (end - e[0]).days > 7:
            return None, None, None
        out = []
        for b in (base6, base12):
            k, c = close_on_or_before(s, b)
            # require a print within 7 days of the base date (excludes names listed later)
            out.append(None if k is None or (b - k).days > 7 or min(s) > b else round(e[1] / c - 1, 6))
        return out[0], out[1], round(e[1], 4)

    # ---- moving averages: per-stock levels today and % of constituents above each, daily ----
    import pandas as _pd
    tick_set = [t for t in tickers if series.get(t)]
    days = sorted(d for d in spy if d <= end)
    px = _pd.DataFrame({t: _pd.Series(series[t]) for t in tick_set}).sort_index()
    px.index = _pd.to_datetime(px.index)
    px = px.reindex(_pd.to_datetime(days)).ffill(limit=5)
    ma_now, breadth = {}, {"dates": [], "pct": {}, "eligible": {}}
    sel = px.index >= _pd.Timestamp(BREADTH_START)
    breadth["dates"] = [d.strftime("%Y-%m-%d") for d in px.index[sel]]
    for n in MA_WINDOWS:
        sma = px.rolling(n, min_periods=n).mean()
        elig = sma.notna() & px.notna()
        above = (px > sma) & elig
        pct = (above.sum(axis=1) / elig.sum(axis=1).where(elig.sum(axis=1) > 0)) * 100
        breadth["pct"][str(n)] = [None if _pd.isna(v) else round(float(v), 2) for v in pct[sel]]
        breadth["eligible"][str(n)] = [int(v) for v in elig.sum(axis=1)[sel]]
        last = sma.iloc[-1]
        for t in tick_set:
            v = last.get(t)
            ma_now.setdefault(t, {})[f"ma{n}"] = None if _pd.isna(v) else round(float(v), 4)
    spy_s = _pd.Series({_pd.Timestamp(d): spy[d] for d in days})
    spy_ohlc = OHLC.get("SPY", {})
    sd = [d for d in days if d >= BREADTH_START]
    breadth["spy"] = {
        "o": [spy_ohlc.get(d, (None,) * 4)[0] for d in sd], "h": [spy_ohlc.get(d, (None,) * 4)[1] for d in sd],
        "l": [spy_ohlc.get(d, (None,) * 4)[2] for d in sd], "c": [spy[d] for d in sd],
        "sma50": [round(float(v), 2) for v in spy_s.rolling(50).mean()[spy_s.index >= _pd.Timestamp(BREADTH_START)]],
        "sma200": [round(float(v), 2) for v in spy_s.rolling(200).mean()[spy_s.index >= _pd.Timestamp(BREADTH_START)]],
    }
    breadth["generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    breadth["price_asof"] = end.isoformat()
    breadth["universe"] = "Current S&P 500 constituents (SPY holdings); each day counts names with enough history for that average"
    bout = a.breadth_out or os.path.join(os.path.dirname(os.path.abspath(a.out)), "breadth.json")
    with open(bout, "w") as f:
        json.dump(breadth, f, separators=(",", ":"))
    print("breadth latest:", {n: breadth["pct"][n][-1] for n in breadth["pct"]}, "eligible:", {n: breadth["eligible"][n][-1] for n in breadth["eligible"]}, file=sys.stderr)

    rows, unclassified = [], []
    for h in holdings:
        t = h["ticker"]
        c = cls.get(t)
        if c and t in SUB_INDUSTRY_OVERRIDES:
            c = {**c, "gics_sub": SUB_INDUSTRY_OVERRIDES[t], "gics_sector": gics[norm(SUB_INDUSTRY_OVERRIDES[t])]["sector"]}
        g = gics.get(norm(c["gics_sub"])) if c else None
        if g and c and g["sector"] != c["gics_sector"]:
            g = None  # hierarchy lookup disagrees with the listed sector: keep the sector, flag the rest
        if not g:
            unclassified.append(t)
            g = {"sector": (c or {}).get("gics_sector", "Unclassified"), "industry_group": "Unclassified",
                 "industry": "Unclassified", "sub_industry": (c or {}).get("gics_sub", "Unclassified")}
        r6, r12, px_ = rets(series.get(t))
        rows.append({
            "ticker": t,
            "name": c["name"] if c else h["spy_name"].title(),
            "sector": g["sector"], "industry_group": g["industry_group"],
            "industry": g["industry"], "sub_industry": g["sub_industry"],
            "weight": round(h["weight"], 6), "price": px_, "r6m": r6, "r12m": r12,
            **ma_now.get(t, {f"ma{n}": None for n in MA_WINDOWS}),
        })

    # constituent change log vs previous build
    changes = []
    if a.prev and os.path.exists(a.prev):
        prev = json.load(open(a.prev))
        changes = prev.get("changes", [])
        old = {r["ticker"]: r["name"] for r in prev.get("rows", [])}
        new = {r["ticker"]: r["name"] for r in rows}
        added = sorted(set(new) - set(old)); removed = sorted(set(old) - set(new))
        if added or removed:
            changes.insert(0, {"holdings_asof": spy_asof,
                               "added": [{"ticker": t, "name": new[t]} for t in added],
                               "removed": [{"ticker": t, "name": old[t]} for t in removed]})
        changes = changes[:50]

    ref = {}
    for t in refs:
        r6, r12, px = rets(series.get(t))
        ref[t] = {"r6m": r6, "r12m": r12, "price": px}

    out = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "holdings_asof": spy_asof,
        "price_asof": end.isoformat(),
        "base_6m": base6.isoformat(), "base_12m": base12.isoformat(),
        "sources": {
            "constituents": "State Street SPY daily holdings file",
            "classification": f"GICS sub-industry from {class_src}; hierarchy from Wikipedia GICS table",
            "prices": "Nasdaq.com historical daily close (Yahoo Finance as fallback); price return excludes dividends",
        },
        "reference": ref,
        "count": len(rows),
        "overrides": SUB_INDUSTRY_OVERRIDES,
        "unclassified": unclassified,
        "price_errors": {k: v for k, v in errors.items()},
        "large_daily_moves": split_flags,
        "spinoff_adjustments": spin_applied,
        "price_source_counts": {s: list(psrc.values()).count(s) for s in set(psrc.values())},
        "changes": changes,
        "rows": rows,
    }
    with open(a.out, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(json.dumps({k: out[k] for k in ["holdings_asof", "price_asof", "base_6m", "base_12m", "count", "unclassified", "price_errors", "large_daily_moves", "spinoff_adjustments", "price_source_counts", "reference"]}, indent=1))
    print("missing 6m:", [r["ticker"] for r in rows if r["r6m"] is None])
    print("missing 12m:", [r["ticker"] for r in rows if r["r12m"] is None])


if __name__ == "__main__":
    main()
