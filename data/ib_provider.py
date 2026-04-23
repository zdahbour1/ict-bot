"""
IB Data Provider — fetches real-time 1-minute OHLCV bars from Interactive Brokers.
Falls back to yfinance for historical data beyond IB's limits.
"""
import logging
import pandas as pd
from datetime import datetime, timedelta
import pytz

log = logging.getLogger(__name__)


def get_bars_1m_ib(client, symbol: str = "QQQ", days_back: int = 5,
                    spec: dict | None = None) -> pd.DataFrame:
    """
    Fetch 1-minute OHLCV bars using IB real-time historical data.
    Routes through the IB worker queue (thread-safe).

    ENH-048: ``spec`` carries the ticker's sec_type / exchange /
    contract_month so futures-options (MNQ, MES, ES, NQ) can be
    qualified as FOP/FUT contracts instead of the default Stock.
    When None, defaults to Stock on SMART exchange (prior behavior).
    """
    try:
        bars = client._submit_to_ib(
            _ib_fetch_bars, client.ib, symbol, days_back, spec,
            timeout=60  # historical data can take longer
        )
        if bars is not None and not bars.empty:
            log.info(f"[IB] Fetched {len(bars)} real-time 1m bars for {symbol} "
                     f"({bars.index[0]} → {bars.index[-1]})")
            return bars
    except Exception as e:
        log.warning(f"[IB] Historical bars failed for {symbol}: {e} — falling back to yfinance")

    # Fallback to yfinance for historical data
    return _yf_fallback(symbol, days_back)


def _ib_fetch_bars(ib, symbol: str, days_back: int,
                    spec: dict | None = None) -> pd.DataFrame:
    """
    Fetch historical 1m bars from IB. Runs on the IB event loop thread.
    IB allows up to 5-6 days of 1m data in a single request.

    ENH-048 — contract type is driven by ``spec['sec_type']``:
      STK (default) → Stock(symbol, SMART, USD)
      FUT           → Future(symbol, lastTradeDateOrContractMonth, exchange)
      FOP           → qualified via ib_contracts futures-option path
    """
    sec = (spec or {}).get("sec_type", "STK").upper() if spec else "STK"
    exchange = (spec or {}).get("exchange") or "SMART"
    currency = (spec or {}).get("currency", "USD")

    if sec == "FUT":
        from ib_async import Future
        contract_month = (spec or {}).get("contract_month") or ""
        contract = Future(symbol, contract_month, exchange, currency=currency)
    elif sec == "FOP":
        # Defer to the backtest engine's FOP spec + qualification path
        # — same logic works for live data requests.
        from backtest_engine.data_provider_ib import (
            IBContractSpec, spec_from_ticker_row,
        )
        try:
            fop_spec = spec_from_ticker_row(
                ticker_symbol=symbol,
                last_trade_date=(spec or {}).get("contract_month") or "",
                strike=float((spec or {}).get("fop_strike") or 0),
                right=(spec or {}).get("fop_right", "C"),
            ) if (spec or {}).get("fop_strike") else IBContractSpec(
                sec_type="FOP",
                symbol=symbol,
                exchange=exchange,
                currency=currency,
                contract_month=(spec or {}).get("contract_month"),
            )
        except Exception:
            fop_spec = IBContractSpec(
                sec_type="FOP", symbol=symbol,
                exchange=exchange, currency=currency,
            )
        from backtest_engine.data_provider_ib import _build_ib_contract
        contract = _build_ib_contract(fop_spec)
    else:
        from ib_async import Stock
        contract = Stock(symbol, exchange, currency)
    ib.qualifyContracts(contract)

    # IB duration string: "5 D" for 5 days
    duration = f"{days_back} D"

    bars = ib.reqHistoricalData(
        contract,
        endDateTime="",           # empty = up to now (real-time)
        durationStr=duration,
        barSizeSetting="1 min",
        whatToShow="TRADES",
        useRTH=True,              # regular trading hours only
        formatDate=1,
    )

    if not bars:
        raise ValueError(f"No IB historical bars returned for {symbol}")

    # Convert to DataFrame
    data = []
    for bar in bars:
        data.append({
            "datetime": bar.date,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": int(bar.volume),
        })

    df = pd.DataFrame(data)
    df["datetime"] = pd.to_datetime(df["datetime"])

    # Ensure UTC timezone
    if df["datetime"].dt.tz is None:
        df["datetime"] = df["datetime"].dt.tz_localize("UTC")
    else:
        df["datetime"] = df["datetime"].dt.tz_convert("UTC")

    df = df.set_index("datetime")
    df = df[~df.index.duplicated(keep="first")]
    df = df.sort_index()

    return df


def _yf_fallback(symbol: str, days_back: int) -> pd.DataFrame:
    """Fallback: fetch bars from yfinance (delayed but works without IB)."""
    import yfinance as yf
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=f"{days_back}d", interval="1m")
        if df.empty:
            log.warning(f"No 1m data returned for {symbol} (yfinance fallback)")
            return pd.DataFrame()

        df.columns = [c.lower() for c in df.columns]
        df = df[["open", "high", "low", "close", "volume"]]

        if df.index.tzinfo is None:
            df.index = df.index.tz_localize("UTC")
        else:
            df.index = df.index.tz_convert("UTC")

        df = df[~df.index.duplicated(keep="first")]
        df = df.sort_index()

        log.info(f"[yfinance fallback] Fetched {len(df)} 1m bars for {symbol}")
        return df
    except Exception as e:
        log.error(f"yfinance fallback also failed for {symbol}: {e}")
        return pd.DataFrame()
