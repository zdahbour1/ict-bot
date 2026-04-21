"""
IB-backed historical data provider for FOP (and any OPT/STK that needs
intraday history yfinance doesn't offer).

Uses `reqHistoricalData` on a dedicated IB connection. Parquet caches
per (symbol, interval, start, end) — same pattern as the yfinance
provider so repeat backtests don't re-hit IB.

Key differences from the yfinance path:
- Requires an active TWS/Gateway connection
- Limits: ~1 year of 1-min bars per contract, more for larger bar sizes
- Contracts are instrument-specific (need exchange, multiplier for FOP)
- fetch_bars takes an `instrument_spec` dict instead of just a ticker

Designed so it can't be imported without IB being available, but the
module itself loads fine — the actual IB calls happen inside
fetch_bars_ib() which is the only thing that needs TWS.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

# Module-level import so tests can patch('backtest_engine.data_provider_ib.IB').
# ib_async is already a project dep (used by broker/); importing here costs
# nothing if it's available and fails cleanly at import time if it isn't.
try:
    from ib_async import IB  # noqa: F401  (re-exported for test patching)
except ImportError:  # pragma: no cover
    IB = None

log = logging.getLogger(__name__)

_CACHE_DIR = Path(os.getenv("BACKTEST_CACHE_DIR",
                            Path.home() / ".ict_bot_cache" / "backtest"))
_CACHE_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class IBContractSpec:
    """Minimal contract spec the caller gives us. For FOP, all fields
    except maybe `multiplier` are required."""
    sec_type: str                # 'FOP' | 'OPT' | 'STK' | 'FUT'
    symbol: str                  # underlying (MNQ, ES, QQQ, etc.)
    exchange: str                # GLOBEX, NYMEX, SMART, CBOE...
    currency: str = "USD"
    # Option-specific (ignored for STK/FUT)
    last_trade_date: Optional[str] = None  # YYYYMMDD (FOP expiry)
    strike: Optional[float] = None
    right: Optional[str] = None            # 'C' or 'P'
    multiplier: Optional[int] = None
    # Futures-specific
    contract_month: Optional[str] = None   # YYYYMM for FUT

    def cache_key(self, interval: str, end: date, duration_days: int) -> str:
        """Stable filename slug for caching."""
        parts = [self.sec_type, self.symbol.upper(), self.exchange]
        if self.last_trade_date:
            parts.append(self.last_trade_date)
        if self.strike:
            parts.append(f"{self.strike:g}")
        if self.right:
            parts.append(self.right)
        if self.contract_month:
            parts.append(self.contract_month)
        parts.append(interval)
        parts.append(f"d{duration_days}")
        parts.append(end.isoformat())
        slug = "_".join(parts)
        # Remove anything funky for the filesystem
        slug = re.sub(r"[^A-Za-z0-9_\.-]", "_", slug)
        return f"{slug}.parquet"

    def cache_path(self, interval: str, end: date, duration_days: int) -> Path:
        return _CACHE_DIR / self.cache_key(interval, end, duration_days)


# ── IB interval + duration strings ───────────────────────────
# reqHistoricalData takes bar size like "1 min", "5 mins", "1 hour"
# and duration like "30 D", "1 Y".

_BARSIZE_MAP = {
    "1m":  "1 min",
    "5m":  "5 mins",
    "15m": "15 mins",
    "30m": "30 mins",
    "1h":  "1 hour",
    "4h":  "4 hours",
    "1d":  "1 day",
}

# Max lookback per bar size (IB docs + empirical).
# We clamp requests so IB doesn't reject.
_MAX_LOOKBACK_DAYS = {
    "1m":  30,
    "5m":  90,
    "15m": 365,
    "30m": 365,
    "1h":  365,
    "4h":  365,
    "1d": 3650,
}


def _interval_to_ib(interval: str) -> str:
    if interval not in _BARSIZE_MAP:
        raise ValueError(f"Unsupported interval {interval!r} for IB. "
                         f"Valid: {list(_BARSIZE_MAP.keys())}")
    return _BARSIZE_MAP[interval]


def _duration_string(days: int) -> str:
    """IB accepts durations like '1800 S', '90 D', '1 Y'. Keep it simple:
    always pass days."""
    # IB caps at "365 D" for most, accepts "1 Y" for longer
    if days <= 0:
        days = 1
    if days >= 365:
        years = min(5, max(1, days // 365))
        return f"{years} Y"
    return f"{days} D"


def _build_ib_contract(spec: IBContractSpec):
    """Convert an IBContractSpec into the ib_async Contract object."""
    from ib_async import FuturesOption, Option, Future, Stock

    if spec.sec_type == "FOP":
        if not (spec.last_trade_date and spec.strike and spec.right):
            raise ValueError("FOP requires last_trade_date, strike, and right")
        c = FuturesOption(
            symbol=spec.symbol,
            lastTradeDateOrContractMonth=spec.last_trade_date,
            strike=spec.strike,
            right=spec.right,
            exchange=spec.exchange,
            currency=spec.currency,
            multiplier=str(spec.multiplier) if spec.multiplier else "",
        )
        return c
    if spec.sec_type == "OPT":
        if not (spec.last_trade_date and spec.strike and spec.right):
            raise ValueError("OPT requires last_trade_date, strike, and right")
        return Option(
            symbol=spec.symbol,
            lastTradeDateOrContractMonth=spec.last_trade_date,
            strike=spec.strike,
            right=spec.right,
            exchange=spec.exchange,
            currency=spec.currency,
        )
    if spec.sec_type == "FUT":
        if not spec.contract_month:
            raise ValueError("FUT requires contract_month (YYYYMM)")
        return Future(
            symbol=spec.symbol,
            lastTradeDateOrContractMonth=spec.contract_month,
            exchange=spec.exchange,
            currency=spec.currency,
        )
    if spec.sec_type == "STK":
        return Stock(spec.symbol, spec.exchange, spec.currency)
    raise ValueError(f"Unsupported sec_type: {spec.sec_type}")


def _ib_bars_to_df(bars) -> pd.DataFrame:
    """Convert ib_async BarData list → canonical OHLCV DataFrame."""
    if not bars:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    rows = []
    for b in bars:
        # ib_async BarData.date can be a date or datetime depending on bar size
        dt = b.date
        if isinstance(dt, date) and not isinstance(dt, datetime):
            dt = datetime.combine(dt, datetime.min.time())
        # Ensure UTC tz-aware for consistency with the yfinance provider
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        rows.append({
            "timestamp": dt,
            "open": float(b.open),
            "high": float(b.high),
            "low": float(b.low),
            "close": float(b.close),
            "volume": float(b.volume) if b.volume is not None else 0.0,
        })
    df = pd.DataFrame(rows).set_index("timestamp")
    df.index = df.index.tz_convert("UTC") if df.index.tz else df.index.tz_localize("UTC")
    return df


# ── Public API ───────────────────────────────────────────────

def fetch_bars_ib(
    spec: IBContractSpec,
    *,
    interval: str = "5m",
    end: Optional[date] = None,
    duration_days: int = 30,
    use_cache: bool = True,
    ib_host: Optional[str] = None,
    ib_port: Optional[int] = None,
    client_id: int = 99,
    what_to_show: str = "TRADES",
    use_rth: bool = False,
) -> pd.DataFrame:
    """Fetch historical OHLCV bars from IB.

    Returns the canonical OHLCV DataFrame shape used by the rest of
    the backtest engine (UTC tz-aware index, lowercase columns).

    `spec` — an IBContractSpec describing the contract
    `interval` — '1m' / '5m' / '15m' / '1h' / '4h' / '1d'
    `end` — end date (defaults to today UTC)
    `duration_days` — how far back from end (clamped by IB limits)
    `use_cache` — parquet cache on disk
    `client_id` — 99 by default so this doesn't collide with the live bot's 1-4
    """
    if end is None:
        end = datetime.now(timezone.utc).date()

    max_days = _MAX_LOOKBACK_DAYS.get(interval, 365)
    if duration_days > max_days:
        log.warning(f"Clamping duration {duration_days}d → {max_days}d for {interval}")
        duration_days = max_days

    cache_file = spec.cache_path(interval, end, duration_days)
    if use_cache and cache_file.exists():
        try:
            df = pd.read_parquet(cache_file)
            log.info(f"IB cache hit: {cache_file.name} ({len(df)} bars)")
            return df
        except Exception as e:
            log.warning(f"IB cache read failed {cache_file.name}: {e}")

    # Live fetch — needs TWS
    if IB is None:
        raise RuntimeError(
            "ib_async is not installed; cannot fetch historical data from IB."
        )
    host = ib_host or os.getenv("IB_HOST", "127.0.0.1")
    port = ib_port or int(os.getenv("IB_PORT", "7497"))
    log.info(f"IB historical fetch: {spec.symbol} {interval} "
             f"{duration_days}d ending {end} (client {client_id})")

    ib = IB()
    try:
        ib.connect(host=host, port=port, clientId=client_id, readonly=True)
        # Force real-time data type (1). If not entitled IB auto-falls
        # back to delayed; logging makes that visible.
        try:
            ib.reqMarketDataType(1)
        except Exception as e:
            log.warning(f"reqMarketDataType(1) failed: {e}")
        contract = _build_ib_contract(spec)
        qualified = ib.qualifyContracts(contract)
        if not qualified or not qualified[0] or not getattr(qualified[0], "conId", 0):
            raise RuntimeError(
                f"Could not qualify contract for {spec.symbol} "
                f"{spec.sec_type} on {spec.exchange}"
            )
        qualified_contract = qualified[0]

        # IB's endDateTime format: preferred "YYYYMMDD-HH:MM:SS" (UTC)
        # per IB 2025 deprecation notice. The old "US/Eastern" space
        # format now trips error 10314 on many feeds. Empty string means
        # "now" — use that when end is today or in the future.
        from datetime import datetime as _dt, time as _time, timezone as _tz
        end_date_obj = end if isinstance(end, _dt) else _dt.combine(end, _time(23, 59, 59))
        if end_date_obj.tzinfo is None:
            end_date_obj = end_date_obj.replace(tzinfo=_tz.utc)
        now_utc = _dt.now(_tz.utc)
        if end_date_obj >= now_utc:
            end_str = ""                          # IB treats empty as "now"
        else:
            end_str = end_date_obj.astimezone(_tz.utc).strftime("%Y%m%d-%H:%M:%S")
        bars = ib.reqHistoricalData(
            qualified_contract,
            endDateTime=end_str,
            durationStr=_duration_string(duration_days),
            barSizeSetting=_interval_to_ib(interval),
            whatToShow=what_to_show,
            useRTH=use_rth,
            formatDate=2,  # UTC-aware
        )
        df = _ib_bars_to_df(bars)
        log.info(f"IB returned {len(df)} bars for {spec.symbol}")
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass

    if not df.empty and use_cache:
        try:
            df.to_parquet(cache_file)
        except Exception as e:
            log.warning(f"IB cache write failed {cache_file.name}: {e}")

    return df


def fetch_multi_timeframe_ib(
    spec: IBContractSpec,
    *,
    base_interval: str = "5m",
    end: Optional[date] = None,
    duration_days: int = 30,
    **kwargs,
) -> dict[str, pd.DataFrame]:
    """Same shape as data_provider.fetch_multi_timeframe — returns
    {'base', '1h', '4h'} DataFrames. Aggregates from the base timeframe
    via pandas resample so we only hit IB once."""
    from backtest_engine.data_provider import aggregate_bars

    base = fetch_bars_ib(
        spec, interval=base_interval, end=end,
        duration_days=duration_days, **kwargs,
    )
    if base.empty:
        return {"base": base, "1h": base, "4h": base}
    return {
        "base": base,
        "1h": aggregate_bars(base, "1h"),
        "4h": aggregate_bars(base, "4h"),
    }


# ── Helper: resolve a FOP contract spec from a ticker row ────

def spec_from_ticker_row(
    ticker_symbol: str,
    last_trade_date: str,
    strike: float,
    right: str,
) -> IBContractSpec:
    """Build an IBContractSpec for a FOP using the canonical per-symbol
    defaults from broker.ib_contracts.FOP_SPECS. Keeps callers from
    having to remember exchange/multiplier for each instrument."""
    from broker.ib_contracts import FOP_SPECS

    specs = FOP_SPECS.get(ticker_symbol.upper())
    if specs is None:
        raise ValueError(
            f"Unknown FOP underlying {ticker_symbol!r}. "
            f"Add to broker.ib_contracts.FOP_SPECS or build the "
            f"IBContractSpec manually."
        )
    return IBContractSpec(
        sec_type="FOP",
        symbol=ticker_symbol.upper(),
        exchange=specs["exchange"],
        currency=specs.get("currency", "USD"),
        last_trade_date=last_trade_date,
        strike=strike,
        right=right,
        multiplier=specs.get("multiplier"),
    )
