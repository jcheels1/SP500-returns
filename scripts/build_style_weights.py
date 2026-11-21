#!/usr/bin/env python3
"""Build style-weights/data/holdings.json for the S&P 500 / Value / Growth comparison dashboard.

Sources
- Holdings and weights: State Street daily holdings files for SPY (S&P 500),
  SPYV (S&P 500 Value) and SPYG (S&P 500 Growth).
- GICS sub-industry per company: Wikipedia "List of S&P 500 companies".
- GICS hierarchy (sub-industry -> industry group -> sector): Wikipedia
  "Global Industry Classification Standard".

Cash, money market and non-equity lines (no SEDOL, e.g. CVRs) are excluded and
equity weights are rescaled to sum to 100% in each index.
"""
import io
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "style-weights" / "data" / "holdings.json"
CLASS_CACHE = ROOT / "style-weights" / "data" / "classification_cache.json"
# Same manual overrides as scripts/build.py, so both pages classify companies identically
SUB_INDUSTRY_OVERRIDES = {
    "ROP": "Application Software",
    "SOLV": "Health Care Supplies",
    "RVTY": "Life Sciences Tools & Services",
    "WST": "Life Sciences Tools & Services",
}
UA = {"User-Agent": "sp500-style-dashboard/1.0 (GitHub Actions; data refresh)"}

FUNDS = {
    "sp500": ("SPY", "https://www.ssga.com/library-content/products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx"),
    "value": ("SPYV", "https://www.ssga.com/library-content/products/fund-data/etfs/us/holdings-daily-us-en-spyv.xlsx"),
    "growth": ("SPYG", "https://www.ssga.com/library-content/products/fund-data/etfs/us/holdings-daily-us-en-spyg.xlsx"),
}
WIKI_SP500 = "https://en.wikipedia.org/w/index.php?title=List_of_S%26P_500_companies&action=raw"
WIKI_GICS = "https://en.wikipedia.org/w/index.php?title=Global_Industry_Classification_Standard&action=raw"


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def norm_ticker(t: str) -> str:
    return str(t).strip().upper().replace("/", ".").replace("-", ".")


def norm_name(s: str) -> str:
    s = s.lower().replace("&", "and")
    return re.sub(r"[^a-z0-9]+", "", s)


def strip_wiki(s: str) -> str:
    s = re.sub(r"<ref[^>]*/>", "", s)
    s = re.sub(r"<ref.*?</ref>", "", s, flags=re.S)
    s = re.sub(r"\{\{[^{}]*\|([^{}|]+)\}\}", r"\1", s)  # {{NyseSymbol|MMM}} -> MMM
    s = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", s)  # [[a|b]] -> b
    s = re.sub(r"<[^>]+>", "", s)
    return " ".join(s.split())


# ---------------------------------------------------------------- holdings
def parse_holdings(raw: bytes):
    ws = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True).active
    as_of, header_row, out = None, None, []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if row[0] == "Holdings:" and row[1]:
            as_of = str(row[1]).replace("As of", "").strip()
        if row[0] == "Name" and row[1] == "Ticker":
            header_row = i
            continue
        if header_row is None or i <= header_row:
            continue
        name, ticker, cusip, sedol, weight = row[0], row[1], row[2], row[3], row[4]
        if not ticker or ticker == "-" or not isinstance(weight, (int, float)):
            continue
        if not sedol or sedol == "-":  # cash, CVRs and other non-equity lines
            continue
        out.append({"ticker": norm_ticker(ticker), "name": " ".join(str(name).split()).title(),
                    "cusip": cusip, "w": float(weight)})
    total = sum(h["w"] for h in out)
    for h in out:
        h["w"] = h["w"] / total * 100.0
    return as_of, out, total


# ---------------------------------------------------------------- GICS
def parse_gics_structure(text: str):
    """Return {sub-industry name(normalized): (sector, industry group, industry, sub-industry)}."""
    lines = text.splitlines()
    names = {}  # code -> name
    for idx, line in enumerate(lines):
        m = re.match(r"^\|\s*(?:rowspan=\"?\d+\"?\s*\|)?\s*(\d{2}|\d{4}|\d{6}|\d{8})\s*$", line)
        if m:
            code = m.group(1)
            nxt = lines[idx + 1] if idx + 1 < len(lines) else ""
            nm = re.sub(r"^\|\s*(?:rowspan=\"?\d+\"?\s*\|)?", "", nxt)
            names[code] = strip_wiki(nm)
        m = re.match(r"^\|\s*(\d{8})\s*\|\|\s*(.+)$", line)
        if m:
            names[m.group(1)] = strip_wiki(m.group(2))
    mapping = {}
    for code, nm in names.items():
        if len(code) == 8:
            sec, ig, ind = names.get(code[:2]), names.get(code[:4]), names.get(code[:6])
            if sec and ig:
                mapping[norm_name(nm)] = {"sector": sec, "group": ig, "industry": ind, "sub": nm}
    return mapping


def parse_sp500_list(text: str):
    """Return {ticker: (sector, sub-industry)} from the Wikipedia constituents table."""
    start = text.find('id="constituents"')
    body = text[start:text.find("|}", start)]
    out = {}
    for rec in body.split("|-")[1:]:
        flat = re.sub(r"\n\|(?!\|)", "||", "\n" + rec.strip())  # single-pipe line starts -> cell breaks
        cells = [strip_wiki(c) for c in flat.split("||") if c.strip()]
        if len(cells) >= 4:
            out[norm_ticker(cells[0])] = {"name": cells[1], "sector": cells[2], "sub": cells[3]}
    return out


def load_classification():
    try:
        gics = parse_gics_structure(fetch(WIKI_GICS).decode("utf-8"))
        members = parse_sp500_list(fetch(WIKI_SP500).decode("utf-8"))
        cls = {}
        for t, m in members.items():
            m["sub"] = SUB_INDUSTRY_OVERRIDES.get(t, m["sub"])
            g = gics.get(norm_name(m["sub"]))
            if g:
                cls[t] = dict(g, name=m["name"])
            else:
                cls[t] = {"name": m["name"], "sector": m["sector"], "group": None, "industry": None, "sub": m["sub"]}
        if len(cls) < 450:
            raise RuntimeError(f"only {len(cls)} classified tickers parsed")
        # merge with cache so names that drop off Wikipedia keep a class
        cache = json.loads(CLASS_CACHE.read_text()) if CLASS_CACHE.exists() else {}
        cache.update(cls)
        CLASS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        CLASS_CACHE.write_text(json.dumps(cache, indent=1, sort_keys=True))
        return cache, gics
    except Exception as e:  # fall back to last good cache
        print(f"WARNING: classification refresh failed ({e}); using cache", file=sys.stderr)
        return json.loads(CLASS_CACHE.read_text()), None


# ---------------------------------------------------------------- main
def main():
    data, as_of_dates, raw_totals = {}, {}, {}
    for key, (tkr, url) in FUNDS.items():
        as_of, rows, total = parse_holdings(fetch(url))
        data[key], as_of_dates[key], raw_totals[key] = rows, as_of, total
        print(f"{tkr}: {len(rows)} equity lines, raw equity weight {total:.3f}%, as of {as_of}")

    cls, _ = load_classification()

    # group-name fallback by sector when a sub-industry did not map
    companies = {}
    for key, rows in data.items():
        for h in rows:
            c = companies.setdefault(h["ticker"], {"t": h["ticker"], "n": h["name"],
                                                    "sp500": 0.0, "value": 0.0, "growth": 0.0})
            c[key] += h["w"]
    unclassified = []
    for t, c in companies.items():
        k = cls.get(t) or cls.get(t.replace(".", "")) or cls.get(t.replace(".", "-"))
        if k and k.get("name"):
            c["n"] = k["name"]
        if k and k.get("group"):
            c["s"], c["g"], c["i"], c["si"] = k["sector"], k["group"], k.get("industry"), k.get("sub")
        else:
            c["s"] = (k or {}).get("sector") or "Unclassified"
            c["g"] = "Unclassified"
            c["i"] = c["si"] = None
            unclassified.append(t)
        for f in ("sp500", "value", "growth"):
            c[f] = round(c[f], 6)

    # S&P splits each company's float cap between Value and Growth, so
    # w_sp500 = a * w_value + (1 - a) * w_growth, where a = Value cap / S&P 500 cap.
    # Solve for a by least squares, then derive each company's Value share of its cap.
    num = sum((c["sp500"] - c["growth"]) * (c["value"] - c["growth"]) for c in companies.values())
    den = sum((c["value"] - c["growth"]) ** 2 for c in companies.values())
    a = num / den
    resid = max(abs(c["sp500"] - (a * c["value"] + (1 - a) * c["growth"])) for c in companies.values())
    for c in companies.values():
        v, g = a * c["value"], (1 - a) * c["growth"]
        c["vs"] = round(v / (v + g), 4) if v + g > 0 else None
    print(f"Value share of S&P 500 cap a={a:.4f}; max abs residual {resid:.4f} pts")

    # sanity checks: refuse to publish a bad download
    problems = []
    if not 495 <= len(data["sp500"]) <= 510: problems.append(f"S&P 500 has {len(data['sp500'])} lines")
    if len(data["value"]) < 300 or len(data["growth"]) < 100: problems.append("style index line counts too low")
    if len(unclassified) > 5: problems.append(f"{len(unclassified)} unclassified: {unclassified}")
    if resid > 0.05: problems.append(f"S&P 500 weights do not reconcile to Value/Growth (max residual {resid:.3f})")
    if len(set(as_of_dates.values())) > 1: print(f"NOTE: as-of dates differ: {as_of_dates}")
    if problems:
        sys.exit("Not publishing: " + "; ".join(problems))

    out = {
        "valueCapShare": round(a, 5),
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "asOf": as_of_dates,
        "funds": {k: v[0] for k, v in FUNDS.items()},
        "counts": {k: len(v) for k, v in data.items()},
        "rawEquityWeight": {k: round(v, 4) for k, v in raw_totals.items()},
        "unclassified": unclassified,
        "companies": sorted(companies.values(), key=lambda c: -c["sp500"]),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, separators=(",", ":")))
    print(f"wrote {OUT} ({len(companies)} companies; unclassified: {unclassified})")


if __name__ == "__main__":
    main()
