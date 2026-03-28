"""
Backtest Runner — replays historical 1m bars, generates 5m signals,
simulates fills, and reports performance metrics.
"""
import json
import csv
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd
import pytz
from loguru import logger

from bot import config
from bot.data.aggregator import build_all_timeframes
from bot.strategy.ict_long import ICTLongStrategy, Signal
from bot.alerts.emailer import send_alert

PT = pytz.timezone("America/Los_Angeles")

RESULTS_DIR = Path("backtest_results")


# ─────────────────────────────────────────────────────────────────────────────
# Fill Simulation
# ─────────────────────────────────────────────────────────────────────────────

def _simulate_fill(sig: Signal, df_5m: pd.DataFrame) -> dict:
    """
    Walk forward from signal bar and determine if SL or TP was hit first.
    Returns a result dict.
    """
    entry     = sig.entry
    sl        = sig.sl
    tp        = sig.tp
    sig_idx   = df_5m.index.get_loc(sig.bar_time)

    outcome   = "OPEN"
    exit_price = None
    exit_time  = None
    pnl        = 0.0

    for i in range(sig_idx + 1, len(df_5m)):
        bar = df_5m.iloc[i]
        # SL hit (low touches stop)
        if bar["low"] <= sl:
            outcome    = "LOSS"
            exit_price = sl
            exit_time  = df_5m.index[i]
            pnl        = sl - entry
            break
        # TP hit (high touches target)
        if bar["high"] >= tp:
            outcome    = "WIN"
            exit_price = tp
            exit_time  = df_5m.index[i]
            pnl        = tp - entry
            break

    risk = entry - sl
    r_multiple = (pnl / risk) if risk > 0 else 0.0

    return {
        "signal_id":        sig.signal_id,
        "signal_type":      sig.signal_type,
        "bar_time_utc":     sig.bar_time.isoformat(),
        "bar_time_pt":      sig.bar_time.tz_convert(PT).isoformat(),
        "raided_level":     sig.raided_level.name,
        "entry":            round(entry, 4),
        "sl":               round(sl, 4),
        "tp":               round(tp, 4),
        "risk":             round(risk, 4),
        "outcome":          outcome,
        "exit_price":       round(exit_price, 4) if exit_price else None,
        "exit_time_utc":    exit_time.isoformat() if exit_time else None,
        "pnl":              round(pnl, 4),
        "r_multiple":       round(r_multiple, 4),
        "displacement_ratio": round(sig.displacement_ratio, 4),
        "reasoning":        sig.reasoning,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Metrics
# ─────────────────────────────────────────────────────────────────────────────

def _compute_metrics(results: list[dict]) -> dict:
    if not results:
        return {"total_trades": 0}

    closed = [r for r in results if r["outcome"] in ("WIN", "LOSS")]
    wins   = [r for r in closed if r["outcome"] == "WIN"]
    losses = [r for r in closed if r["outcome"] == "LOSS"]

    if not closed:
        return {"total_trades": len(results), "closed_trades": 0}

    r_multiples = [r["r_multiple"] for r in closed]
    pnls        = [r["pnl"] for r in closed]

    # Max drawdown (cumulative paper P&L)
    cumulative = 0.0
    peak       = 0.0
    max_dd     = 0.0
    for p in pnls:
        cumulative += p
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

    # Trades per day
    days = {}
    for r in results:
        day = r["bar_time_pt"][:10]
        days[day] = days.get(day, 0) + 1

    # Signal frequency by type
    by_type = {}
    for r in results:
        by_type[r["signal_type"]] = by_type.get(r["signal_type"], 0) + 1

    return {
        "total_signals":     len(results),
        "closed_trades":     len(closed),
        "open_trades":       len(results) - len(closed),
        "wins":              len(wins),
        "losses":            len(losses),
        "win_rate_pct":      round(100 * len(wins) / len(closed), 2),
        "avg_r_multiple":    round(sum(r_multiples) / len(r_multiples), 4),
        "total_pnl":         round(sum(pnls), 4),
        "max_drawdown":      round(max_dd, 4),
        "trades_per_day_avg": round(len(results) / max(len(days), 1), 2),
        "by_signal_type":    by_type,
        "trading_days":      len(days),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────

def run_backtest(df_1m: pd.DataFrame, dry_run_alerts: bool = True) -> dict:
    """
    Main backtest entry point.
    - Builds all timeframes
    - Replays 5m bars bar by bar
    - Collects signals and simulates fills
    - Saves results to CSV + JSON
    Returns summary metrics dict.
    """
    logger.info("Building timeframes...")
    tfs = build_all_timeframes(df_1m)
    df_5m = tfs["5m"]

    logger.info(f"Backtesting on {len(df_5m)} 5m bars ({df_5m.index[0]} → {df_5m.index[-1]})")

    engine  = ICTLongStrategy(df_1m=df_1m)
    all_signals: list[Signal] = []

    for i in range(len(df_5m)):
        new_signals = engine.process_bar(df_5m, i)
        for sig in new_signals:
            all_signals.append(sig)
            send_alert(sig, dry_run=dry_run_alerts)

    logger.info(f"Total signals generated: {len(all_signals)}")

    # Simulate fills
    results = [_simulate_fill(sig, df_5m) for sig in all_signals]

    # Compute metrics
    metrics = _compute_metrics(results)

    # Save outputs
    RESULTS_DIR.mkdir(exist_ok=True)
    run_tag = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    csv_path = RESULTS_DIR / f"trades_{run_tag}.csv"
    if results:
        keys = results[0].keys()
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(results)
        logger.info(f"Trade log saved: {csv_path}")

    json_path = RESULTS_DIR / f"metrics_{run_tag}.json"
    with open(json_path, "w") as f:
        json.dump({"metrics": metrics, "config": {
            "SYMBOL":                 config.SYMBOL,
            "RAID_THRESHOLD":         config.RAID_THRESHOLD,
            "DISPLACEMENT_BODY_MULT": config.DISPLACEMENT_BODY_MULT,
            "N_CONFIRM_BARS":         config.N_CONFIRM_BARS,
            "FVG_MIN_SIZE":           config.FVG_MIN_SIZE,
            "TRADE_WINDOW":           f"{config.TRADE_WINDOW_START_PT}-{config.TRADE_WINDOW_END_PT} PT",
        }}, f, indent=2)
    logger.info(f"Metrics saved: {json_path}")

    # Print summary
    logger.info("=" * 50)
    logger.info("BACKTEST SUMMARY")
    logger.info("=" * 50)
    for k, v in metrics.items():
        logger.info(f"  {k}: {v}")
    logger.info("=" * 50)

    return metrics
