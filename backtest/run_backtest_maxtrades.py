"""
ICT Strategy Backtest — Max Trades Per Day Comparison
======================================================
Tests different max trades/day limits and shows which produces the best P&L.
Window fixed at 6:30 AM – 12:00 PM PT (best window from window comparison).
Does NOT touch any live code.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import math
import logging
import warnings
import pandas as pd
import numpy as np
import yfinance as yf
import pytz
from datetime import datetime, timedelta, date

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.WARNING)

from strategy.ict_long  import run_strategy as run_long
from strategy.ict_short import run_strategy_short as run_short
from strategy.levels    import get_all_levels

# ── Fixed Settings ──────────────────────────────────────────
TICKER          = "QQQ"
CONTRACTS       = 2
PROFIT_TARGET   = 1.00
STOP_LOSS       = 0.60
VIX_THRESHOLD   = 35.0
EMA_PERIOD_1H   = 20
NEWS_BUFFER_MIN = 30
TRADE_START_H   = 6
TRADE_START_MIN = 30
TRADE_END_H     = 12

PT = pytz.timezone("America/Los_Angeles")

OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "ICT_MaxTrades_Comparison.xlsx")
CSV_FILE    = os.path.join(os.path.dirname(__file__), "ICT_MaxTrades_Comparison.csv")

# ── Max trades limits to test ───────────────────────────────
MAX_TRADES_OPTIONS = [1, 2, 3, 4, 5, 6, 999]  # 999 = no limit

import datetime as _dt
NEWS_EVENTS = [
    (_dt.date(2026, 1, 29), 11,  0,  "FOMC Decision"),
    (_dt.date(2026, 1, 29), 11, 30,  "FOMC Press Conference"),
    (_dt.date(2026, 2,  7),  5, 30,  "NFP Jobs Report"),
    (_dt.date(2026, 2, 12),  5, 30,  "CPI"),
    (_dt.date(2026, 2, 13),  5, 30,  "PPI"),
    (_dt.date(2026, 2, 14),  5, 30,  "Retail Sales"),
    (_dt.date(2026, 2, 26),  5, 30,  "GDP"),
    (_dt.date(2026, 3,  7),  5, 30,  "NFP Jobs Report"),
    (_dt.date(2026, 3, 12),  5, 30,  "CPI"),
    (_dt.date(2026, 3, 13),  5, 30,  "PPI"),
    (_dt.date(2026, 3, 14),  5, 30,  "Retail Sales"),
    (_dt.date(2026, 3, 19), 11,  0,  "FOMC Decision"),
    (_dt.date(2026, 3, 19), 11, 30,  "FOMC Press Conference"),
    (_dt.date(2026, 3, 26),  5, 30,  "GDP"),
]


def bar_to_pt(ts):
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert(PT)


def compute_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def is_near_news(entry_time_pt):
    for (ev_date, ev_h, ev_m, label) in NEWS_EVENTS:
        ev_dt = PT.localize(_dt.datetime(ev_date.year, ev_date.month, ev_date.day, ev_h, ev_m))
        diff  = abs((entry_time_pt - ev_dt).total_seconds() / 60)
        if diff <= NEWS_BUFFER_MIN:
            return True
    return False


def get_trend_bias(bars_1h, as_of_time):
    hist = bars_1h[bars_1h.index <= as_of_time]
    if len(hist) < EMA_PERIOD_1H:
        return "NEUTRAL"
    ema        = compute_ema(hist["close"], EMA_PERIOD_1H)
    last_close = hist["close"].iloc[-1]
    last_ema   = ema.iloc[-1]
    if last_close > last_ema:
        return "BULLISH"
    elif last_close < last_ema:
        return "BEARISH"
    return "NEUTRAL"


def estimate_option_price(qqq_price, bars_5m, entry_bar):
    lookback = min(20, entry_bar)
    recent   = bars_5m.iloc[max(0, entry_bar - lookback):entry_bar]
    if len(recent) < 2:
        iv = 0.22
    else:
        rets = np.log(recent["close"] / recent["close"].shift(1)).dropna()
        iv   = rets.std() * math.sqrt(252 * 78)
        iv   = max(iv, 0.10)
    T     = 3.0 / (252 * 6.5)
    price = qqq_price * iv * math.sqrt(T) * 0.4
    return max(round(price, 2), 0.05)


def simulate_exit(bars_5m, entry_bar, entry_opt_px, direction):
    tp_px        = entry_opt_px * (1 + PROFIT_TARGET)
    sl_pct       = -STOP_LOSS
    peak_pnl_pct = 0.0
    entry_close  = bars_5m.iloc[entry_bar]["close"]
    MAX_BARS     = 18

    for i in range(entry_bar + 1, min(entry_bar + 300, len(bars_5m))):
        bar     = bars_5m.iloc[i]
        bar_pt  = bar_to_pt(bars_5m.index[i])
        bars_in = i - entry_bar

        underlying_chg = bar["close"] - entry_close
        opt_chg = underlying_chg * 0.5 if direction == "LONG" else -underlying_chg * 0.5
        cur_opt = max(round(entry_opt_px + opt_chg, 2), 0.01)
        pnl_pct = (cur_opt - entry_opt_px) / entry_opt_px

        if pnl_pct > peak_pnl_pct:
            peak_pnl_pct = pnl_pct

        if peak_pnl_pct >= 0.20:
            sl_pct = peak_pnl_pct - 0.10
        elif peak_pnl_pct >= 0.10:
            sl_pct = 0.00

        hit_tp    = pnl_pct >= PROFIT_TARGET
        hit_sl    = pnl_pct <= sl_pct
        eod       = bar_pt.hour >= 13
        time_exit = bars_in >= MAX_BARS

        if hit_tp or hit_sl or eod or time_exit:
            if hit_tp:
                exit_px = round(tp_px, 2)
                pnl_pct = PROFIT_TARGET
                result  = "WIN"
            elif hit_sl and sl_pct == 0.0:
                exit_px = entry_opt_px
                pnl_pct = 0.0
                result  = "SCRATCH"
            elif hit_sl:
                exit_px = max(round(entry_opt_px * (1 + sl_pct), 2), 0.01)
                result  = "WIN" if sl_pct > 0 else "LOSS"
            elif time_exit:
                exit_px = cur_opt
                result  = "WIN" if pnl_pct > 0 else "LOSS" if pnl_pct < 0 else "SCRATCH"
            else:
                exit_px = cur_opt
                result  = "WIN" if pnl_pct > 0 else "LOSS"

            pnl_usd = round((exit_px - entry_opt_px) * 100 * CONTRACTS, 2)
            return {"result": result, "pnl_usd": pnl_usd, "exit_bar": i}

    last    = bars_5m.iloc[min(entry_bar + 299, len(bars_5m) - 1)]
    chg     = last["close"] - entry_close
    opt_chg = chg * 0.5 if direction == "LONG" else -chg * 0.5
    cur     = max(round(entry_opt_px + opt_chg, 2), 0.01)
    pnl     = (cur - entry_opt_px) / entry_opt_px
    return {
        "result":  "WIN" if pnl > 0 else "LOSS",
        "pnl_usd": round((cur - entry_opt_px) * 100 * CONTRACTS, 2),
        "exit_bar": entry_bar + 299,
    }


def run_max_trades(bars_5m, bars_1h, bars_4h, vix_by_date, trading_days, max_trades):
    trades = []

    for day_date in trading_days:
        vix_today = vix_by_date.get(day_date, 0)
        if vix_today > VIX_THRESHOLD:
            continue

        day_end = pd.Timestamp(day_date, tz=PT) + pd.Timedelta(hours=23, minutes=59)
        hist_5m = bars_5m[bars_5m.index <= day_end].copy()
        hist_1h = bars_1h[bars_1h.index <= day_end].copy()
        hist_4h = bars_4h[bars_4h.index <= day_end].copy()

        if len(hist_5m) < 50:
            continue

        day_5m = bars_5m[bars_5m.index.date == day_date]
        if day_5m.empty:
            continue

        try:
            levels = get_all_levels(hist_5m, hist_1h, hist_4h)
            levels = [l for l in levels if l["price"] > 0]
        except Exception:
            continue

        if not levels:
            continue

        try:
            long_signals  = run_long(day_5m,  hist_1h, hist_4h, levels)
            short_signals = run_short(day_5m, hist_1h, hist_4h, levels)
        except Exception:
            continue

        all_signals = []
        for s in long_signals:
            s["direction"] = "LONG"
            all_signals.append(s)
        for s in short_signals:
            s["direction"] = "SHORT"
            all_signals.append(s)

        all_signals.sort(key=lambda x: x.get("entry_time", pd.Timestamp.min))

        trades_today  = 0
        last_exit_bar = -1

        for sig in all_signals:
            if trades_today >= max_trades:
                break

            entry_time = sig.get("entry_time")
            if entry_time is None:
                continue

            et_pt = bar_to_pt(entry_time)
            in_window = (
                (et_pt.hour > TRADE_START_H or (et_pt.hour == TRADE_START_H and et_pt.minute >= TRADE_START_MIN))
                and et_pt.hour < TRADE_END_H
            )
            if not in_window:
                continue

            direction = sig["direction"]
            trend     = get_trend_bias(hist_1h, entry_time)
            if direction == "LONG"  and trend != "BULLISH":
                continue
            if direction == "SHORT" and trend != "BEARISH":
                continue

            entry_bar   = sig.get("entry_bar", 0)
            entry_price = sig.get("entry_price", 0)

            if entry_bar <= last_exit_bar:
                continue
            if entry_price <= 0:
                continue

            if is_near_news(et_pt):
                continue

            opt_px    = estimate_option_price(entry_price, day_5m, entry_bar)
            exit_info = simulate_exit(day_5m, entry_bar, opt_px, direction)
            last_exit_bar = exit_info["exit_bar"]

            trades.append({
                "result":  exit_info["result"],
                "pnl_usd": exit_info["pnl_usd"],
            })
            trades_today += 1

    return trades


def summarize(trades, label):
    if not trades:
        return {
            "Max Trades/Day": label, "Trades": 0, "Wins": 0, "Losses": 0,
            "Scratches": 0, "Win Rate": "N/A", "Total P&L": "$0.00",
            "Avg Win": "N/A", "Avg Loss": "N/A",
            "_total_pnl": 0, "_trades": 0,
        }
    wins      = sum(1 for t in trades if t["result"] == "WIN")
    losses    = sum(1 for t in trades if t["result"] == "LOSS")
    scratches = sum(1 for t in trades if t["result"] == "SCRATCH")
    total     = len(trades)
    win_rate  = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0
    total_pnl = sum(t["pnl_usd"] for t in trades)
    win_pnls  = [t["pnl_usd"] for t in trades if t["result"] == "WIN"]
    loss_pnls = [t["pnl_usd"] for t in trades if t["result"] == "LOSS"]
    avg_win   = sum(win_pnls)  / len(win_pnls)  if win_pnls  else 0
    avg_loss  = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0

    return {
        "Max Trades/Day": label,
        "Trades":         total,
        "Wins":           wins,
        "Losses":         losses,
        "Scratches":      scratches,
        "Win Rate":       f"{win_rate:.1f}%",
        "Total P&L":      f"${total_pnl:+,.2f}",
        "Avg Win":        f"${avg_win:+.2f}",
        "Avg Loss":       f"${avg_loss:+.2f}",
        "_total_pnl":     total_pnl,
        "_trades":        total,
    }


def main():
    print("=" * 65)
    print("  ICT Strategy — Max Trades/Day Comparison")
    print("  Window: 6:30 AM–12:00 PM PT | TP=100% | SL=60% | Trail ON")
    print("=" * 65)
    print()
    print("Downloading QQQ + VIX data (60 days)...")

    raw_5m      = yf.download(TICKER, period="60d", interval="5m", auto_adjust=True, progress=False)
    raw_1h      = yf.download(TICKER, period="60d", interval="1h", auto_adjust=True, progress=False)
    raw_1h_base = yf.download(TICKER, period="60d", interval="1h", auto_adjust=True, progress=False)
    vix_raw     = yf.download("^VIX",  period="60d", interval="1d", auto_adjust=True, progress=False)

    if raw_5m.empty:
        print("ERROR: Could not download data.")
        return

    def fix_cols(df):
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0).str.lower()
        else:
            df.columns = df.columns.str.lower()
        return df

    raw_5m = fix_cols(raw_5m); raw_1h = fix_cols(raw_1h)
    raw_1h_base = fix_cols(raw_1h_base); vix_raw = fix_cols(vix_raw)

    raw_4h = raw_1h_base.resample("4h").agg({
        "open": "first", "high": "max",
        "low": "min", "close": "last", "volume": "sum"
    }).dropna()

    def to_pt(df):
        if df.index.tzinfo is None:
            df.index = df.index.tz_localize("UTC")
        df.index = df.index.tz_convert(PT)
        return df

    bars_5m = to_pt(raw_5m.copy())
    bars_1h = to_pt(raw_1h.copy())
    bars_4h = to_pt(raw_4h.copy())

    vix_by_date = {}
    if not vix_raw.empty:
        for idx, row in vix_raw.iterrows():
            d = idx.date() if hasattr(idx, "date") else idx
            vix_by_date[d] = float(row.get("close", 20))

    trading_days = sorted(set(bars_5m.index.date))
    print(f"Data: {len(trading_days)} trading days | {len(bars_5m):,} bars\n")

    results = []

    for max_t in MAX_TRADES_OPTIONS:
        label = f"No limit" if max_t == 999 else f"Max {max_t}/day"
        print(f"  Testing {label}...", end="", flush=True)
        trades  = run_max_trades(bars_5m, bars_1h, bars_4h, vix_by_date, trading_days, max_t)
        summary = summarize(trades, label)
        results.append(summary)
        print(f"  {summary['Trades']} trades  |  {summary['Win Rate']}  |  {summary['Total P&L']}")

    results.sort(key=lambda x: x["_total_pnl"], reverse=True)

    print(f"\n{'='*65}")
    print(f"  RESULTS — Ranked by Total P&L")
    print(f"{'='*65}")
    print(f"  {'Max Trades/Day':<18} {'Trades':>6}  {'Win Rate':>9}  {'Total P&L':>12}")
    print(f"  {'-'*50}")
    for i, r in enumerate(results):
        marker = " ← BEST" if i == 0 else ""
        print(f"  {r['Max Trades/Day']:<18} {r['Trades']:>6}  {r['Win Rate']:>9}  {r['Total P&L']:>12}{marker}")

    print(f"\n  Best setting: {results[0]['Max Trades/Day']}")
    print(f"  P&L: {results[0]['Total P&L']}  |  Win Rate: {results[0]['Win Rate']}  |  Trades: {results[0]['Trades']}")

    export_cols = ["Max Trades/Day", "Trades", "Wins", "Losses", "Scratches",
                   "Win Rate", "Total P&L", "Avg Win", "Avg Loss"]
    export_df = pd.DataFrame(results)[export_cols]

    try:
        export_df.to_excel(OUTPUT_FILE, index=False)
        print(f"\nSaved to: {OUTPUT_FILE}")
    except Exception:
        pass

    export_df.to_csv(CSV_FILE, index=False)
    print(f"Saved to: {CSV_FILE}")


if __name__ == "__main__":
    main()
