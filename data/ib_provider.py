"""
IB Data Provider — fetches real-time 1-minute OHLCV bars from Interactive Brokers.
Falls back to yfinance for historical data beyond IB's limits.
"""
import logging
import pandas as pd
from datetime import datetime, timedelta
import pytz

log = logging.getLogger(__name__)


def get_bars_1m_ib(client, symbol: str = "QQQ", days_back: int = 5) -> pd.DataFrame:
    """
    Fetch 1-minute OHLCV bars using IB real-time historical data.
    Routes through the IB worker queue (thread-safe).

    :param client: IBClient instance with _submit_to_ib method
    :param symbol: Ticker symbol
    :param days_back: Number of trading days of history
    :returns: DataFrame with UTC datetime index, columns: open/high/low/close/volume
    """
    try:
        bars = client._submit_to_ib(
            _ib_fetch_bars, client.ib, symbol, days_back,
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


def _ib_fetch_bars(ib, symbol: str, days_back: int) -> pd.DataFrame:
    """
    Fetch historical 1m bars from IB. Runs on the IB event loop thread.
    IB allows up to 5-6 days of 1m data in a single request.
    """
    from ib_async import Stock

    contract = Stock(symbol, "SMART", "USD")
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
