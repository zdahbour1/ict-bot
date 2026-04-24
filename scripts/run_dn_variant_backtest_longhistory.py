"""ENH-060 — DN variant backtest with IB historical data (long window).

yfinance caps 5m at 60 days. For longer validation we fetch daily
bars from IB going back 1-5 years for each ticker and run the
variant engine at daily granularity.

Trade-off: we lose intraday exit fidelity (V2 hold-to-day and EOD
exits become end-of-day exits → which is what daily bars give us
anyway, so V2 still works). V5 delta-hedging intraday also degrades
to daily rebalance — not ideal but the P&L signal is still
directional.

Usage:

    DATABASE_URL=postgresql://ict_bot:ict_bot_dev@localhost:5432/ict_bot \
    python scripts/run_dn_variant_backtest_longhistory.py --years 1
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from backtest_engine.dn_variants_engine import run_variant_backtest
from strategy.delta_neutral_variants import VARIANTS, TIERS, all_tier_tickers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-6s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("dn-longhistory")


def _fetch_ib_daily(ib, symbol: str, years: int):
    """Pull daily TRADES bars for ``years`` via IB historical data."""
    from ib_async import Stock, Index
    import pandas as pd
    contract = Stock(symbol, "SMART", "USD") if symbol not in ("^VIX", "^VIX3M") else \
               Index(symbol.replace("^", ""), "CBOE")
    try:
        ib.qualifyContracts(contract)
    except Exception as e:
        log.warning(f"qualify {symbol}: {e}")
        return None
    if not contract.conId:
        return None
    try:
        bars = ib.reqHistoricalData(
            contract, endDateTime="",
            durationStr=f"{years} Y",
            barSizeSetting="1 day",
            whatToShow="TRADES", useRTH=True, formatDate=1,
        )
    except Exception as e:
        log.warning(f"reqHistoricalData {symbol}: {e}")
        return None
    if not bars:
        return None
    df = pd.DataFrame([{
        "datetime": b.date, "open": b.open, "high": b.high,
        "low": b.low, "close": b.close, "volume": int(b.volume or 0),
    } for b in bars])
    df["datetime"] = pd.to_datetime(df["datetime"]).dt.tz_localize("UTC")
    df = df.set_index("datetime").sort_index()
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=1)
    ap.add_argument("--tickers", default=None,
                    help="Comma-separated override (default: all tier 0-3)")
    ap.add_argument("--variants", default=None,
                    help="Comma-separated variant names (default: all 5)")
    ap.add_argument("--client-id", type=int, default=96)
    ap.add_argument("--host", default=config.IB_HOST)
    ap.add_argument("--port", type=int, default=config.IB_PORT)
    args = ap.parse_args()

    log.info(f"Connecting IB @ {args.host}:{args.port} clientId={args.client_id}")
    from ib_async import IB
    ib = IB()
    ib.connect(args.host, args.port, clientId=args.client_id,
                readonly=True, timeout=8)
    log.info("IB connected")

    # Fetch VIX for regime filter
    vix = _fetch_ib_daily(ib, "^VIX", args.years)
    vix3m = _fetch_ib_daily(ib, "^VIX3M", args.years)
    log.info(f"VIX rows: {len(vix) if vix is not None else 0}; "
             f"VIX3M rows: {len(vix3m) if vix3m is not None else 0}")

    # Pick universe
    if args.tickers:
        tickers_raw = [(0, t) for t in args.tickers.split(",")]
    else:
        tickers_raw = all_tier_tickers()

    # Variants
    if args.variants:
        from strategy.delta_neutral_variants import VARIANT_BY_NAME
        variants = [VARIANT_BY_NAME[n] for n in args.variants.split(",")]
    else:
        variants = VARIANTS

    # Pre-fetch underlying daily bars for each ticker, cached in-memory
    bars_cache: dict[str, "pd.DataFrame"] = {}
    log.info(f"Pre-fetching daily bars for {len(tickers_raw)} tickers…")
    for _, t in tickers_raw:
        df = _fetch_ib_daily(ib, t, args.years)
        if df is None or df.empty:
            log.warning(f"  {t}: no bars")
            continue
        # The variant engine expects 5m-like intraday bars so we
        # monkey-patch fetch_multi_timeframe behavior by stashing
        # the daily frame as if it were "base" + "1h" + "4h".
        bars_cache[t] = df
    ib.disconnect()
    log.info(f"IB disconnected. Cached {len(bars_cache)} tickers.")

    # Patch data_provider.fetch_multi_timeframe to return our cache
    import backtest_engine.data_provider as dp
    orig = dp.fetch_multi_timeframe
    def _patched(ticker, *, base_interval="5m", start=None, end=None):
        df = bars_cache.get(ticker)
        if df is None:
            return {"base": None, "1h": None, "4h": None}
        return {"base": df, "1h": df, "4h": df}
    dp.fetch_multi_timeframe = _patched

    end_d = date.today()
    start_d = end_d - timedelta(days=args.years * 365)
    log.info(f"Running {len(variants)} variants × {len(bars_cache)} tickers "
             f"= {len(variants) * len(bars_cache)} backtests…")

    results: list[dict] = []
    all_trades: list[dict] = []
    for _, t in tickers_raw:
        if t not in bars_cache:
            continue
        for v in variants:
            log.info(f"  {t} × {v.name}")
            try:
                r = run_variant_backtest(v, t, start_d, end_d, vix, vix3m)
                m = r.metrics()
                results.append(m)
                for tr in r.trades:
                    all_trades.append(tr)
            except Exception as e:
                log.error(f"  {t}/{v.name}: {e}", exc_info=True)

    # Restore
    dp.fetch_multi_timeframe = orig

    # Write outputs
    out_dir = Path("data"); out_dir.mkdir(exist_ok=True)
    csv_path = out_dir / f"backtest_longhist_{args.years}y_{end_d}.csv"
    if results:
        fields = list(results[0].keys())
        with csv_path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(results)
        log.info(f"CSV: {csv_path}")

    md_path = Path("docs") / f"backtest_longhist_{args.years}y_{end_d}.md"
    lines = [f"# DN Variant Long-History Backtest — {args.years}y",
             f"Window: {start_d} → {end_d} (daily bars via IB)", ""]
    from collections import defaultdict
    by_v = defaultdict(list)
    for r in results:
        by_v[r["variant"]].append(r)
    lines.append("## Summary by variant")
    lines.append("")
    lines.append("| Variant | Trades | Net P&L | Win% | Max DD | PF |")
    lines.append("|---|---|---|---|---|---|")
    for vname, rows in by_v.items():
        tc = sum(r["trades"] for r in rows)
        total = sum(r["total_pnl"] for r in rows)
        wins = sum(r["wins"] for r in rows)
        losses = sum(r["losses"] for r in rows)
        wr = wins / max(wins + losses, 1) * 100
        gw = sum(r["total_pnl"] for r in rows if r["total_pnl"] > 0)
        gl = abs(sum(r["total_pnl"] for r in rows if r["total_pnl"] < 0))
        pf = gw / gl if gl > 0 else 0
        dd = min(r["max_drawdown"] for r in rows) if rows else 0
        lines.append(f"| {vname} | {tc} | {total:+.0f} | {wr:.0f}% | {dd:.0f} | {pf:.2f} |")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info(f"Report: {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
