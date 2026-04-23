"""
Backtest simulation engine.

Reuses strategy/signal_engine.py (signal detection) and
strategy/exit_conditions.evaluate_exit (TP/SL/trail/roll decisions) —
the same code the live bot runs. No divergent logic.

Each bar:
  1. If a trade is open, call evaluate_exit — exit if triggered
  2. If not, call SignalEngine.detect — enter on any signal

Options P&L proxy: since we're simulating the option's price from the
underlying price (no option chain in yfinance), we track underlying-
relative P&L and apply the strategy's profit_target / stop_loss
fractions as the exit levels. This is the same approximation used by
the legacy run_backtest.py scripts; good enough for relative strategy
comparison even if absolute dollar P&L isn't exact for options.
"""
from __future__ import annotations

import logging
import traceback
from datetime import datetime, timezone
from typing import Callable, Optional
import pytz

import pandas as pd

from strategy.exit_conditions import evaluate_exit
from strategy.signal_engine import SignalEngine
from strategy.levels import get_all_levels

from backtest_engine.data_provider import fetch_multi_timeframe
from backtest_engine.fill_model import (
    FillConfig, simulate_entry_fill, simulate_exit_fill, compute_pnl,
)
from backtest_engine.indicators import snapshot_at, context_at
from backtest_engine.metrics import compute_summary
from backtest_engine import writer as bt_writer
from backtest_engine.multi_leg_sim import (
    build_leg_state, price_legs_now, entry_basis, synth_price,
    build_legs_for_writer,
)

log = logging.getLogger(__name__)

PT = pytz.timezone("America/Los_Angeles")


DEFAULT_CONFIG = {
    "profit_target": 1.00,        # option-price % gain for TP
    "stop_loss": 0.60,            # option-price % loss for SL
    "contracts": 2,
    "base_interval": "5m",        # yfinance interval for primary bars
    "max_trades_per_day": 8,
    "cooldown_minutes": 15,
    "slippage_pct": 0.002,
    "commission_per_contract": 0.65,
    # Strategy-specific config is passed through untouched — the
    # strategy's own .configure(settings) reads what it needs.
}


def _signal_to_dict(s) -> dict | None:
    """Normalize any Signal-ish object into a plain dict the engine uses.

    Accepts:
    - plain dicts (legacy ICT internal representation via _raw)
    - the legacy strategy.signal_engine.Signal dataclass
    - the plugin strategy.base_strategy.Signal dataclass (has .to_dict())
    Returns None for unrecognized shapes.
    """
    if s is None:
        return None
    if isinstance(s, dict):
        return s
    if hasattr(s, "to_dict") and callable(s.to_dict):
        try:
            return s.to_dict()
        except Exception:
            pass
    # Legacy dataclass: build from attributes
    if hasattr(s, "signal_type"):
        d = {
            "signal_type": getattr(s, "signal_type", None),
            "direction": getattr(s, "direction", "LONG"),
            "entry_price": getattr(s, "entry_price", None),
            "sl": getattr(s, "sl", None),
            "tp": getattr(s, "tp", None),
            "setup_id": getattr(s, "setup_id", ""),
            "ticker": getattr(s, "ticker", ""),
        }
        details = getattr(s, "details", None) or {}
        # Preserve raw if it was attached
        raw = details.get("_raw") if isinstance(details, dict) else None
        if isinstance(raw, dict):
            d.update({k: v for k, v in raw.items() if k not in d or d[k] is None})
        return d
    return None


def _option_pnl_from_underlying(entry_price: float, current_price: float,
                                direction: str) -> float:
    """Legacy flat-5× leverage proxy. Kept for unit tests that asserted
    against it. New code should use _option_price_bs() + bs_option_pct()
    below which use proper Black-Scholes pricing."""
    underlying_pct = (current_price - entry_price) / entry_price
    if direction == "SHORT":
        underlying_pct = -underlying_pct
    return underlying_pct * 5.0


def _option_price_bs(
    underlying: float,
    strike: float,
    dte_days: float,
    right: str,               # 'C' or 'P'
    *,
    sigma: float = 0.20,       # annualized vol; 20% default for equity
    r: float = 0.04,           # risk-free rate
    model: str = "bs",         # or 'black76' for FOP
) -> float:
    """Price a single option contract at the given parameters via BS.
    Returns price per contract-share (multiply by 100 for equity
    option dollars; FOP multipliers vary)."""
    from backtest_engine.option_pricer import bs_price
    T = max(dte_days, 0.0) / 365.0
    return bs_price(underlying, strike, T, r, sigma, right, model=model)


def bs_option_pct(
    underlying_entry: float,
    underlying_now: float,
    *,
    direction: str,               # 'LONG' or 'SHORT'
    strike: float | None = None,  # defaults to ATM = underlying_entry
    dte_at_entry_days: float = 7.0,
    bars_held: int = 0,
    bar_minutes: int = 5,
    sigma: float = 0.20,
    r: float = 0.04,
    model: str = "bs",
) -> float:
    """Compute option P&L as a fraction of entry price using BS.

    Replaces the flat-5× leverage proxy with realistic option behavior:
    delta-weighted gains + theta decay. For ATM options near expiry
    this will show small wins getting eaten by theta — exactly the
    frictions issue the user wants to see accurately.

    Returns (exit_price - entry_price) / entry_price. Sign is adjusted
    for SHORT direction (positive = profit regardless of direction).
    """
    if strike is None:
        strike = underlying_entry
    right = "C" if direction == "LONG" else "P"

    # Days elapsed during the hold
    minutes_held = bars_held * bar_minutes
    days_held = minutes_held / (24 * 60)  # calendar days approximation
    dte_at_exit = max(dte_at_entry_days - days_held, 1e-4)

    entry_opt = _option_price_bs(
        underlying_entry, strike, dte_at_entry_days, right,
        sigma=sigma, r=r, model=model,
    )
    exit_opt = _option_price_bs(
        underlying_now, strike, dte_at_exit, right,
        sigma=sigma, r=r, model=model,
    )
    if entry_opt <= 0:
        return 0.0
    return (exit_opt - entry_opt) / entry_opt


def _bar_time_to_pt(ts: pd.Timestamp) -> datetime:
    """Convert a UTC bar timestamp to a PT-tz-aware datetime."""
    if ts.tz is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert(PT).to_pydatetime()


def run_backtest(
    *,
    tickers: list[str],
    start_date,
    end_date,
    strategy_id: int = 1,
    strategy=None,  # BaseStrategy instance; defaults to ICT via SignalEngine
    config: Optional[dict] = None,
    run_name: Optional[str] = None,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> dict:
    """Run one backtest end-to-end.

    Creates the run row, simulates every ticker across the date range,
    writes trades, finalizes summary, returns the run dict.

    If `strategy` is None, uses the legacy SignalEngine (ICT) directly.
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    pnl_target = float(cfg["profit_target"])
    sl_target = float(cfg["stop_loss"])
    contracts = int(cfg["contracts"])
    fill_cfg = FillConfig(
        slippage_pct=float(cfg.get("slippage_pct", 0.002)),
        commission_per_contract=float(cfg.get("commission_per_contract", 0.65)),
    )

    run_id = bt_writer.create_run(
        name=run_name or f"backtest-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
        strategy_id=strategy_id,
        tickers=tickers,
        start_date=start_date,
        end_date=end_date,
        config=cfg,
    )
    if run_id is None:
        raise RuntimeError("Failed to create backtest_runs row")

    def _progress(msg: str) -> None:
        log.info(f"[bt#{run_id}] {msg}")
        if progress_cb:
            try:
                progress_cb(msg)
            except Exception:
                pass

    try:
        bt_writer.mark_run_started(run_id)
        _progress(f"Started — {len(tickers)} tickers "
                  f"{start_date} → {end_date}")

        all_trades: list[dict] = []

        for ticker in tickers:
            try:
                ticker_trades = _simulate_ticker(
                    ticker=ticker,
                    strategy=strategy,
                    start_date=start_date,
                    end_date=end_date,
                    pnl_target=pnl_target,
                    sl_target=sl_target,
                    contracts=contracts,
                    fill_cfg=fill_cfg,
                    cfg=cfg,
                    progress=_progress,
                )
                for t in ticker_trades:
                    # ENH-038: if the trade carries multi-leg data
                    # (emitted by a strategy that implements place_legs),
                    # route to the multi-leg writer which also persists
                    # backtest_trade_legs rows. Single-leg trades continue
                    # writing a single backtest_trades row via record_trade.
                    legs = t.get("_legs")
                    if legs:
                        bt_writer.record_multi_leg_trade(
                            run_id, strategy_id, t, legs,
                        )
                    else:
                        bt_writer.record_trade(run_id, strategy_id, t)
                    all_trades.append(t)
                _progress(f"{ticker}: {len(ticker_trades)} trades")
            except Exception as e:
                _progress(f"{ticker}: ERROR — {e}")
                log.exception(f"Ticker {ticker} failed in backtest")

        # Order trades chronologically for drawdown/streak calc
        all_trades.sort(key=lambda t: t.get("exit_time") or t.get("entry_time"))
        summary = compute_summary(all_trades)
        bt_writer.finalize_run(run_id, summary)

        _progress(f"Done — {summary.total_trades} trades, "
                  f"P&L=${summary.total_pnl:+.2f}, "
                  f"win={summary.win_rate:.1f}%")

        return {
            "run_id": run_id,
            "summary": summary.to_dict(),
            "trade_count": len(all_trades),
        }
    except Exception as e:
        tb = traceback.format_exc()
        bt_writer.mark_run_failed(run_id, f"{e}\n{tb}")
        raise


def _simulate_ticker(
    *,
    ticker: str,
    strategy,  # Optional BaseStrategy instance
    start_date,
    end_date,
    pnl_target: float,
    sl_target: float,
    contracts: int,
    fill_cfg: FillConfig,
    cfg: dict,
    progress: Callable[[str], None],
) -> list[dict]:
    """Simulate one ticker over the date range. Returns a list of trade dicts."""
    progress(f"{ticker}: loading bars…")

    # Dispatch data provider by sec_type. Equity options + stocks go via
    # yfinance (free, sub-minute-fast). Futures + FOP go via IB historical
    # data (requires TWS). The config can force a provider with
    # `cfg['data_provider']` = 'yfinance' | 'ib'.
    sec_type = (cfg.get("sec_type") or "OPT").upper()
    forced = (cfg.get("data_provider") or "").lower()
    use_ib = forced == "ib" or (forced != "yfinance" and sec_type in ("FOP", "FUT"))

    if use_ib:
        from backtest_engine.data_provider_ib import (
            fetch_multi_timeframe_ib, IBContractSpec, spec_from_ticker_row,
        )
        # Caller must supply FOP contract details in cfg (expiry, strike,
        # right). For STK/FUT, we build a minimal spec from config.
        try:
            if sec_type == "FOP":
                spec = spec_from_ticker_row(
                    ticker_symbol=ticker,
                    last_trade_date=cfg["fop_expiry"],
                    strike=float(cfg["fop_strike"]),
                    right=cfg.get("fop_right", "C"),
                )
            else:
                spec = IBContractSpec(
                    sec_type=sec_type,
                    symbol=ticker,
                    exchange=cfg.get("exchange", "SMART"),
                    currency=cfg.get("currency", "USD"),
                    contract_month=cfg.get("contract_month"),
                )
        except Exception as e:
            progress(f"{ticker}: IB spec build failed — {e}")
            return []

        duration_days = (end_date - start_date).days or 1
        tf = fetch_multi_timeframe_ib(
            spec,
            base_interval=cfg.get("base_interval", "5m"),
            end=end_date,
            duration_days=duration_days,
        )
    else:
        tf = fetch_multi_timeframe(
            ticker,
            base_interval=cfg.get("base_interval", "5m"),
            start=start_date,
            end=end_date,
        )

    base = tf["base"]
    if base.empty:
        progress(f"{ticker}: no bars — skipped")
        return []

    # Signal engine (legacy path — ICT).  When `strategy` is a BaseStrategy
    # we'd call strategy.detect(...) instead. Keeping both paths gives
    # flexibility for the ORB plugin later without touching this file.
    sig_engine = SignalEngine(ticker) if strategy is None else None

    trades: list[dict] = []
    open_trade: Optional[dict] = None
    trade_counter = 0
    max_trades = int(cfg.get("max_trades_per_day", 8))
    trades_today_by_date: dict = {}
    cooldown_min = int(cfg.get("cooldown_minutes", 15))
    last_exit_ts = None

    # Warm-up: skip the first 60 bars so indicators have data
    start_idx = min(60, len(base) - 1)

    for i in range(start_idx, len(base)):
        bar = base.iloc[i]
        ts = base.index[i]
        now_pt = _bar_time_to_pt(ts)
        bar_day = ts.normalize()
        current_price = float(bar["close"])

        # ── Exit check for open trade ───────────────────────
        if open_trade is not None and open_trade.get("_is_multi_leg"):
            # Multi-leg: reprice every leg, collapse net P&L into a
            # synthetic option_price so evaluate_exit still applies the
            # strategy's TP/SL/trail semantics.
            leg_prices, net_pnl_ps = price_legs_now(
                open_trade["_legs_state"],
                current_price,
                ts.to_pydatetime(),
                sigma=cfg.get("option_vol", 0.20),
                r=cfg.get("option_rate", 0.04),
            )
            option_price = synth_price(
                open_trade["entry_price"],
                net_pnl_ps,
                open_trade["_entry_basis"],
            )
            exit_result = evaluate_exit(open_trade, option_price, now_pt)
            if exit_result is not None:
                exit_dt = ts.to_pydatetime()
                legs_out = build_legs_for_writer(
                    open_trade["_legs_state"], leg_prices, exit_dt,
                )
                # Aggregate per-leg dollar P&L (writer also computes this,
                # but we surface pnl_usd/pct on the envelope for metrics).
                total_pnl_usd = sum(
                    lg["_sign"] * (xp - lg["entry_price"])
                    * lg["contracts"] * lg["multiplier"]
                    for lg, xp in zip(open_trade["_legs_state"], leg_prices)
                )
                pnl_pct_val = (
                    total_pnl_usd / (open_trade["_entry_basis"]
                                     * open_trade["_legs_state"][0]["multiplier"])
                ) if open_trade["_entry_basis"] > 0 else 0.0
                open_trade["exit_price"] = option_price
                open_trade["exit_time"] = exit_dt
                open_trade["hold_minutes"] = (
                    (ts - open_trade["_entry_ts"]).total_seconds() / 60.0
                )
                open_trade["pnl_usd"] = total_pnl_usd
                open_trade["pnl_pct"] = pnl_pct_val
                open_trade["exit_reason"] = exit_result["reason"]
                open_trade["exit_result"] = exit_result["result"]
                open_trade["peak_pnl_pct"] = open_trade.get("peak_pnl_pct", 0)
                open_trade["dynamic_sl_pct"] = open_trade.get("dynamic_sl_pct", -sl_target)
                open_trade["slippage_paid"] = 0.0
                open_trade["commission"] = 0.0
                open_trade["exit_indicators"] = snapshot_at(base, i)
                open_trade["_legs"] = legs_out   # kept for writer routing

                clean_trade = {k: v for k, v in open_trade.items()
                               if not k.startswith("_") or k == "_legs"}
                trades.append(clean_trade)
                last_exit_ts = ts
                open_trade = None
                continue

        if open_trade is not None and not open_trade.get("_is_multi_leg"):
            # Price the option with Black-Scholes (configurable DTE + vol).
            # This is the ACCURATE option-P&L model — replaces the old
            # flat-5× leverage proxy. Small underlying moves get eaten by
            # theta decay + bid/ask; large moves show the delta-gamma
            # convexity that real options have.
            option_pct = bs_option_pct(
                underlying_entry=open_trade["_underlying_entry"],
                underlying_now=current_price,
                direction=open_trade["direction"],
                strike=open_trade.get("_strike"),
                dte_at_entry_days=cfg.get("option_dte_days", 7.0),
                bars_held=i - open_trade["entry_bar_idx"],
                bar_minutes=cfg.get("bar_minutes", 5),
                sigma=cfg.get("option_vol", 0.20),
                r=cfg.get("option_rate", 0.04),
                model=("black76" if cfg.get("sec_type") == "FOP" else "bs"),
            )
            option_price = open_trade["entry_price"] * (1 + option_pct)

            exit_result = evaluate_exit(open_trade, option_price, now_pt)
            if exit_result is not None:
                # Close the trade
                exit_fill = simulate_exit_fill(
                    option_price, contracts, open_trade["direction"], fill_cfg
                )
                entry_fill_px = open_trade["entry_price"]
                total_comm = open_trade["_entry_commission"] + exit_fill["commission"]
                pnl = compute_pnl(
                    entry_fill_px, exit_fill["fill_price"], contracts,
                    open_trade["direction"], total_comm,
                )

                open_trade["exit_price"] = exit_fill["fill_price"]
                open_trade["exit_time"] = ts.to_pydatetime()
                open_trade["hold_minutes"] = (
                    (ts - open_trade["_entry_ts"]).total_seconds() / 60.0
                )
                open_trade["pnl_pct"] = pnl["pnl_pct"]
                open_trade["pnl_usd"] = pnl["pnl_usd"]
                open_trade["exit_reason"] = exit_result["reason"]
                open_trade["exit_result"] = exit_result["result"]
                open_trade["peak_pnl_pct"] = open_trade.get("peak_pnl_pct", 0)
                open_trade["dynamic_sl_pct"] = open_trade.get("dynamic_sl_pct", -sl_target)
                open_trade["slippage_paid"] = exit_fill["slippage_paid"]
                open_trade["commission"] = total_comm
                open_trade["exit_indicators"] = snapshot_at(base, i)

                # Strip engine-internal keys before returning
                clean_trade = {k: v for k, v in open_trade.items()
                               if not k.startswith("_")}
                trades.append(clean_trade)

                last_exit_ts = ts
                open_trade = None
                continue  # no new entry on the same bar as exit

        # ── Entry check ─────────────────────────────────────
        if open_trade is not None:
            continue

        # Cooldown
        if last_exit_ts is not None and cooldown_min > 0:
            if (ts - last_exit_ts).total_seconds() / 60.0 < cooldown_min:
                continue

        # Daily trade cap
        today_count = trades_today_by_date.get(bar_day, 0)
        if today_count >= max_trades:
            continue

        # Build the 3 timeframe frames the strategy expects — slice to current bar
        bars_1m = base.iloc[: i + 1]
        bars_1h = tf["1h"][tf["1h"].index <= ts]
        bars_4h = tf["4h"][tf["4h"].index <= ts]

        # Compute levels (ICT needs PDH/PDL/session/OR/PWH/PWL for raids)
        try:
            levels = get_all_levels(bars_1m, bars_1h, bars_4h)
        except Exception as e:
            log.debug(f"{ticker}@{ts}: get_all_levels failed: {e}")
            levels = []

        signal_objs: list = []
        if strategy is not None:
            # Plugin path (ORB, VWAP, etc.)
            try:
                signals = strategy.detect(bars_1m, bars_1h, bars_4h, levels, ticker)
                signal_objs = list(signals or [])
                raw_signals = [_signal_to_dict(s) for s in signal_objs]
            except Exception as e:
                log.debug(f"{ticker}@{ts}: strategy detect failed: {e}")
                raw_signals = []
        else:
            # Legacy ICT path
            try:
                signals = sig_engine.detect(bars_1m, bars_1h, bars_4h, levels)
                raw_signals = [_signal_to_dict(s) for s in signals]
            except Exception as e:
                log.debug(f"{ticker}@{ts}: SignalEngine failed: {e}")
                raw_signals = []

        # Drop any that couldn't be normalized
        raw_signals = [s for s in raw_signals if s]
        if not raw_signals:
            continue

        sig = raw_signals[0]
        sig_obj = signal_objs[0] if signal_objs else None
        direction = sig.get("direction", "LONG")

        # ── Multi-leg branch (ENH-038 Part 2) ───────────────
        # If the strategy implements place_legs(), take that path: price
        # every leg with BS/Black-76, track a combined position, and emit
        # a trade dict carrying "_legs" for record_multi_leg_trade.
        legs = None
        if strategy is not None and sig_obj is not None:
            try:
                legs = strategy.place_legs(sig_obj)
            except Exception as e:
                log.debug(f"{ticker}@{ts}: place_legs failed: {e}")
                legs = None

        if legs:
            underlying_entry = current_price
            option_entry_proxy = 2.00  # synthetic basis for evaluate_exit
            entry_dt = ts.to_pydatetime()
            leg_state = build_leg_state(
                legs, underlying_entry, entry_dt,
                sigma=cfg.get("option_vol", 0.20),
                r=cfg.get("option_rate", 0.04),
            )
            basis = entry_basis(leg_state)

            trade_counter += 1
            trades_today_by_date[bar_day] = today_count + 1
            if sig_engine is not None:
                sig_engine.mark_used(sig.get("setup_id", f"idx-{i}"))

            open_trade = {
                "ticker": ticker,
                "symbol": leg_state[0].get("symbol") or f"{ticker}_multileg",
                "direction": direction,
                "contracts": contracts,
                "entry_price": option_entry_proxy,
                "entry_time": entry_dt,
                "entry_bar_idx": i,
                "peak_pnl_pct": 0.0,
                "dynamic_sl_pct": -sl_target,
                "profit_target": option_entry_proxy * (1 + pnl_target),
                "stop_loss": option_entry_proxy * (1 - sl_target),
                "signal_type": sig.get("signal_type"),
                "tp_level": option_entry_proxy * (1 + pnl_target),
                "sl_level": option_entry_proxy * (1 - sl_target),
                "entry_indicators": snapshot_at(base, i),
                "entry_context": context_at(base, i),
                "signal_details": {
                    k: sig.get(k) for k in ("confidence", "strategy_name")
                    if sig.get(k) is not None
                },
                "_underlying_entry": underlying_entry,
                "_strike": underlying_entry,
                "_entry_ts": ts,
                "_entry_commission": 0.0,  # per-leg commissions summed at exit
                "_legs_state": leg_state,
                "_entry_basis": basis,
                "_is_multi_leg": True,
            }
            continue

        # Fill the option at the current bar's close as entry price proxy
        # Option "entry price" is an arbitrary base — we use the underlying
        # close as the reference point and track relative moves.
        underlying_entry = current_price
        option_entry_proxy = 2.00  # canonical $2 ATM option baseline
        entry_fill = simulate_entry_fill(
            option_entry_proxy, contracts, direction, fill_cfg
        )

        trade_counter += 1
        trades_today_by_date[bar_day] = today_count + 1
        if sig_engine is not None:
            sig_engine.mark_used(sig.get("setup_id", f"idx-{i}"))

        open_trade = {
            "ticker": ticker,
            "symbol": sig.get("symbol") or f"{ticker}_proxy",
            "direction": direction,
            "contracts": contracts,
            "entry_price": entry_fill["fill_price"],
            "entry_time": ts.to_pydatetime(),
            "entry_bar_idx": i,
            "peak_pnl_pct": 0.0,
            "dynamic_sl_pct": -sl_target,
            "profit_target": option_entry_proxy * (1 + pnl_target),
            "stop_loss": option_entry_proxy * (1 - sl_target),
            "signal_type": sig.get("signal_type"),
            "tp_level": option_entry_proxy * (1 + pnl_target),
            "sl_level": option_entry_proxy * (1 - sl_target),
            "entry_indicators": snapshot_at(base, i),
            "entry_context": context_at(base, i),
            "signal_details": {
                k: sig.get(k) for k in ("raid", "confirmation", "fvg", "ob",
                                         "confidence", "strategy_name")
                if sig.get(k) is not None
            },
            # Engine-internal (underscore) keys stripped before write
            "_underlying_entry": underlying_entry,
            "_strike": underlying_entry,   # ATM at open
            "_entry_ts": ts,
            "_entry_commission": entry_fill["commission"],
        }

    # Close any still-open trade at the last bar
    if open_trade is not None and open_trade.get("_is_multi_leg"):
        last_idx = len(base) - 1
        last_bar = base.iloc[last_idx]
        last_ts = base.index[last_idx]
        leg_prices, _ = price_legs_now(
            open_trade["_legs_state"],
            float(last_bar["close"]),
            last_ts.to_pydatetime(),
            sigma=cfg.get("option_vol", 0.20),
            r=cfg.get("option_rate", 0.04),
        )
        exit_dt = last_ts.to_pydatetime()
        legs_out = build_legs_for_writer(
            open_trade["_legs_state"], leg_prices, exit_dt,
        )
        total_pnl_usd = sum(
            lg["_sign"] * (xp - lg["entry_price"])
            * lg["contracts"] * lg["multiplier"]
            for lg, xp in zip(open_trade["_legs_state"], leg_prices)
        )
        open_trade.update({
            "exit_price": open_trade["entry_price"],  # nominal; real P&L on legs
            "exit_time": exit_dt,
            "hold_minutes": (last_ts - open_trade["_entry_ts"]).total_seconds() / 60.0,
            "pnl_usd": total_pnl_usd,
            "pnl_pct": 0.0,
            "exit_reason": "END_OF_RANGE",
            "exit_result": "WIN" if total_pnl_usd > 0 else
                           "LOSS" if total_pnl_usd < 0 else "SCRATCH",
            "slippage_paid": 0.0,
            "commission": 0.0,
            "exit_indicators": snapshot_at(base, last_idx),
            "_legs": legs_out,
        })
        trades.append({k: v for k, v in open_trade.items()
                       if not k.startswith("_") or k == "_legs"})
        return trades

    if open_trade is not None:
        last_idx = len(base) - 1
        last_bar = base.iloc[last_idx]
        last_ts = base.index[last_idx]
        now_pt = _bar_time_to_pt(last_ts)
        option_pct = bs_option_pct(
            underlying_entry=open_trade["_underlying_entry"],
            underlying_now=float(last_bar["close"]),
            direction=open_trade["direction"],
            strike=open_trade.get("_strike"),
            dte_at_entry_days=cfg.get("option_dte_days", 7.0),
            bars_held=last_idx - open_trade["entry_bar_idx"],
            bar_minutes=cfg.get("bar_minutes", 5),
            sigma=cfg.get("option_vol", 0.20),
            r=cfg.get("option_rate", 0.04),
            model=("black76" if cfg.get("sec_type") == "FOP" else "bs"),
        )
        option_price = open_trade["entry_price"] * (1 + option_pct)
        exit_fill = simulate_exit_fill(
            option_price, contracts, open_trade["direction"], fill_cfg
        )
        total_comm = open_trade["_entry_commission"] + exit_fill["commission"]
        pnl = compute_pnl(
            open_trade["entry_price"], exit_fill["fill_price"], contracts,
            open_trade["direction"], total_comm,
        )
        open_trade.update({
            "exit_price": exit_fill["fill_price"],
            "exit_time": last_ts.to_pydatetime(),
            "hold_minutes": (last_ts - open_trade["_entry_ts"]).total_seconds() / 60.0,
            "pnl_pct": pnl["pnl_pct"],
            "pnl_usd": pnl["pnl_usd"],
            "exit_reason": "END_OF_RANGE",
            "exit_result": "WIN" if pnl["pnl_usd"] > 0 else
                           "LOSS" if pnl["pnl_usd"] < 0 else "SCRATCH",
            "slippage_paid": exit_fill["slippage_paid"],
            "commission": total_comm,
            "exit_indicators": snapshot_at(base, last_idx),
        })
        trades.append({k: v for k, v in open_trade.items() if not k.startswith("_")})

    return trades
