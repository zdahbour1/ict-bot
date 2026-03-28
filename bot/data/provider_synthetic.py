"""
Synthetic QQQ data generator for backtesting when live data is unavailable.
Generates realistic intraday 1m bars with:
- Trending price action during session hours
- Realistic volatility and spread
- Liquidity raids at key levels
- Gap behavior between sessions
This is for TESTING ONLY — use real data for actual backtesting.
"""
import numpy as np
import pandas as pd
import pytz
from datetime import timedelta
from loguru import logger

PT  = pytz.timezone("America/Los_Angeles")
UTC = pytz.utc

np.random.seed(42)


def generate_synthetic_qqq(days: int = 10, base_price: float = 450.0) -> pd.DataFrame:
    """
    Generate synthetic 1m OHLCV bars for QQQ over `days` trading days.
    Produces realistic intraday structure including:
    - Pre-market (low volume), regular hours, after-hours
    - Occasional liquidity raids below prior day lows
    - Fair value gaps via fast-moving displacement candles
    """
    logger.warning("Using SYNTHETIC data — for demo/testing only. Use real data for live trading.")

    all_bars = []
    # Start on a recent Monday (realistic market day)
    current_date = pd.Timestamp("2024-12-02", tz=UTC)  # Monday
    price = base_price

    trading_day_count = 0
    date_iter = current_date

    while trading_day_count < days:
        # Skip weekends
        weekday = date_iter.tz_convert(PT).weekday()
        if weekday >= 5:
            date_iter += timedelta(days=1)
            continue

        # Trading day: generate 1m bars from 06:30 to 13:00 PT (regular session)
        session_start_pt = date_iter.tz_convert(PT).replace(hour=6, minute=30, second=0, microsecond=0)
        session_end_pt   = date_iter.tz_convert(PT).replace(hour=13, minute=0,  second=0, microsecond=0)

        # Convert to UTC for internal timestamps
        session_start = session_start_pt.astimezone(UTC)
        session_end   = session_end_pt.astimezone(UTC)

        # Random daily gap/drift
        gap = np.random.normal(0, 0.5)
        price = max(price + gap, 100.0)

        # Simulate intraday price path using GBM with mean reversion
        minutes = int((session_end - session_start).total_seconds() / 60)
        mu    = 0.0002   # slight upward drift
        sigma = 0.0008   # per-minute volatility (realistic for QQQ)

        returns = np.random.normal(mu, sigma, minutes)

        # Inject occasional strong moves (displacement-like)
        for i in np.random.choice(minutes, size=int(minutes * 0.02), replace=False):
            returns[i] = np.random.choice([-1, 1]) * np.random.uniform(0.003, 0.008)

        prices = price * np.exp(np.cumsum(returns))
        prices = np.insert(prices, 0, price)[:-1]

        ts = pd.date_range(start=session_start, periods=minutes, freq="1min")

        for i, (t, close_p) in enumerate(zip(ts, prices)):
            spread = np.random.uniform(0.02, 0.08)
            vol_mult = 2.0 if (t.tz_convert(PT).hour == 7 and t.tz_convert(PT).minute < 30) else 1.0
            high  = close_p + spread * vol_mult
            low   = close_p - spread * vol_mult
            open_ = prices[i - 1] if i > 0 else close_p

            # Occasional wick extension (liquidity raid)
            if np.random.random() < 0.008:
                low = low - np.random.uniform(0.05, 0.25)

            all_bars.append({
                "timestamp": t,
                "open":      round(open_,  4),
                "high":      round(high,   4),
                "low":       round(low,    4),
                "close":     round(close_p, 4),
                "volume":    int(np.random.uniform(5000, 50000) * vol_mult),
            })

        price = prices[-1]
        trading_day_count += 1
        date_iter += timedelta(days=1)

    df = pd.DataFrame(all_bars).set_index("timestamp")
    df.index = pd.DatetimeIndex(df.index)
    if df.index.tz is None:
        df.index = df.index.tz_localize(UTC)

    df = df.sort_index()
    logger.info(f"Generated {len(df)} synthetic 1m bars over {days} trading days")
    return df


def load_from_csv(filepath: str) -> pd.DataFrame:
    """
    Load 1m bars from a CSV file.
    Expected columns: timestamp (or datetime), open, high, low, close, volume
    Timestamp can be in any common format; will be converted to UTC.
    """
    logger.info(f"Loading data from CSV: {filepath}")
    df = pd.read_csv(filepath)

    # Find timestamp column
    ts_col = None
    for col in ["timestamp", "datetime", "date", "time", "Datetime", "Date"]:
        if col in df.columns:
            ts_col = col
            break
    if ts_col is None:
        raise ValueError(f"Cannot find timestamp column. Found: {list(df.columns)}")

    df[ts_col] = pd.to_datetime(df[ts_col])
    df = df.set_index(ts_col)
    df.index.name = "timestamp"

    # Normalize column names
    df.columns = [c.lower() for c in df.columns]
    required = ["open", "high", "low", "close", "volume"]
    for col in required:
        if col not in df.columns:
            raise ValueError(f"Missing required column: '{col}'. Found: {list(df.columns)}")

    df = df[required].copy()

    # Ensure UTC
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")

    df = df.sort_index()
    logger.info(f"Loaded {len(df)} bars from {df.index[0]} to {df.index[-1]}")
    return df
