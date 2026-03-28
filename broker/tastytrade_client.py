"""
Tastytrade Broker Client
Handles authentication, option chain lookup, order placement, and position monitoring.
Uses tastytrade SDK v8.5

Option price monitoring uses Tastytrade's DXLinkStreamer (real-time).
Stock price uses Tastytrade streamer first, falls back to yfinance.
"""
import logging
import asyncio
import threading
from decimal import Decimal
from datetime import date

from tastytrade import Session, Account, DXLinkStreamer
from tastytrade.instruments import Option, NestedOptionChain
from tastytrade.dxfeed import Quote
from tastytrade.order import (
    NewOrder, OrderAction, OrderTimeInForce, OrderType, InstrumentType, Leg
)
import config

log = logging.getLogger(__name__)


def run(coro):
    """Helper to run async tastytrade calls from sync code (thread-safe)."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


class TastytradeClient:
    def __init__(self):
        self.session  = None
        self.account  = None
        # Real-time price cache: symbol → mid price
        self._price_cache = {}
        self._cache_lock  = threading.Lock()

    # ── Authentication ───────────────────────────────────
    def connect(self):
        """Log in to Tastytrade, handling 2FA device challenge if required."""
        from tastytrade.utils import TastytradeError
        log.info(f"Connecting to Tastytrade ({'PAPER' if config.PAPER_TRADING else 'LIVE'})...")
        try:
            self.session = Session(
                login=config.TASTYTRADE_USERNAME,
                password=config.TASTYTRADE_PASSWORD,
                is_test=config.PAPER_TRADING
            )
        except TastytradeError as e:
            if "device_challenge" in str(e):
                code = input("\n>>> Tastytrade sent you a verification code. Enter it here: ").strip()
                self.session = Session(
                    login=config.TASTYTRADE_USERNAME,
                    password=config.TASTYTRADE_PASSWORD,
                    is_test=config.PAPER_TRADING,
                    two_factor_authentication=code
                )
            else:
                raise
        accounts = Account.get_accounts(self.session)
        if config.TASTYTRADE_ACCOUNT:
            self.account = next(
                (a for a in accounts if a.account_number == config.TASTYTRADE_ACCOUNT),
                accounts[0]
            )
        else:
            self.account = accounts[0]
        log.info(f"Connected — account: {self.account.account_number}")

    # ── Real-time price via DXLinkStreamer ────────────────
    def get_realtime_price(self, symbol: str) -> float:
        """
        Fetch real-time mid price for a symbol using Tastytrade's
        DXLinkStreamer. Returns cached value or fetches fresh quote.
        Falls back to yfinance if streamer fails.
        """
        async def _fetch():
            try:
                async with DXLinkStreamer(self.session) as streamer:
                    await streamer.subscribe(Quote, [symbol])
                    quote = await streamer.get_event(Quote)
                    bid = float(quote.bid_price) if quote.bid_price else 0.0
                    ask = float(quote.ask_price) if quote.ask_price else 0.0
                    mid = round((bid + ask) / 2, 2)
                    log.info(f"[REALTIME] {symbol}: bid={bid:.2f} ask={ask:.2f} mid={mid:.2f}")
                    return mid
            except Exception as e:
                log.warning(f"DXLink quote failed for {symbol}: {e}")
                return None

        try:
            price = run(_fetch())
            if price is not None and price > 0:
                with self._cache_lock:
                    self._price_cache[symbol] = price
                return price
        except Exception as e:
            log.warning(f"Real-time price fetch error: {e}")

        # ── Fallback: yfinance (delayed) ──────────────────
        log.warning(f"Falling back to yfinance for {symbol} (15-min delay)")
        return self._get_option_price_yf(symbol)

    def get_realtime_equity_price(self, ticker: str) -> float:
        """
        Fetch real-time mid price for a stock/ETF (e.g. QQQ)
        using DXLinkStreamer. Falls back to yfinance.
        """
        async def _fetch():
            try:
                async with DXLinkStreamer(self.session) as streamer:
                    await streamer.subscribe(Quote, [ticker])
                    quote = await streamer.get_event(Quote)
                    bid = float(quote.bid_price) if quote.bid_price else 0.0
                    ask = float(quote.ask_price) if quote.ask_price else 0.0
                    mid = round((bid + ask) / 2, 2)
                    log.info(f"[REALTIME] {ticker}: bid={bid:.2f} ask={ask:.2f} mid={mid:.2f}")
                    return mid
            except Exception as e:
                log.warning(f"DXLink equity quote failed for {ticker}: {e}")
                return None

        try:
            price = run(_fetch())
            if price is not None and price > 0:
                return price
        except Exception as e:
            log.warning(f"Real-time equity price error: {e}")

        # Fallback
        return self._get_equity_price_yf(ticker)

    # ── Option Chain (via yfinance) ───────────────────────
    def _get_atm_symbol(self, ticker: str, option_type: str) -> str:
        """Get ATM 0DTE option symbol using yfinance."""
        import yfinance as yf
        today_str = date.today().strftime('%Y-%m-%d')

        yf_ticker = yf.Ticker(ticker)
        if today_str not in yf_ticker.options:
            raise RuntimeError(f"No 0DTE expiration found for {ticker} on {today_str}")

        current_price = self.get_realtime_equity_price(ticker)
        log.info(f"{ticker} current price: ${current_price:.2f}")

        chain_data = yf_ticker.option_chain(today_str)
        chain      = chain_data.calls if option_type == 'call' else chain_data.puts
        chain      = chain.copy()
        chain['dist'] = abs(chain['strike'] - current_price)
        atm_row    = chain.loc[chain['dist'].idxmin()]

        symbol = atm_row['contractSymbol']
        log.info(f"ATM {option_type} selected: {symbol} (strike ${atm_row['strike']:.2f})")
        return symbol

    def get_atm_call_symbol(self, ticker: str) -> str:
        return self._get_atm_symbol(ticker, 'call')

    def get_atm_put_symbol(self, ticker: str) -> str:
        return self._get_atm_symbol(ticker, 'put')

    def _get_equity_price_yf(self, ticker: str) -> float:
        """Fallback: get stock price via yfinance."""
        import yfinance as yf
        data  = yf.Ticker(ticker).fast_info
        price = float(data['lastPrice'])
        log.info(f"{ticker} price (yfinance fallback): ${price:.2f}")
        return price

    def _get_option_price_yf(self, symbol: str) -> float:
        """Fallback: get option mid price via yfinance (15-min delayed)."""
        import yfinance as yf
        try:
            i = 0
            while i < len(symbol) and symbol[i].isalpha():
                i += 1
            ticker   = symbol[:i]
            exp_str  = symbol[i:i+6]
            opt_type = symbol[i+6]
            strike   = int(symbol[i+7:]) / 1000
            exp_date = f"20{exp_str[:2]}-{exp_str[2:4]}-{exp_str[4:6]}"

            yf_ticker = yf.Ticker(ticker)
            chain     = yf_ticker.option_chain(exp_date)
            df        = chain.calls if opt_type == 'C' else chain.puts

            row = df[df['contractSymbol'] == symbol]
            if row.empty:
                df['dist'] = abs(df['strike'] - strike)
                row = df.loc[[df['dist'].idxmin()]]

            bid = float(row['bid'].values[0])
            ask = float(row['ask'].values[0])
            mid = round((bid + ask) / 2, 2)
            log.info(f"Option price (yfinance fallback) {symbol}: mid={mid:.2f}")
            return mid

        except Exception as e:
            log.warning(f"yfinance option price failed ({e}), returning 1.00 fallback")
            return 1.00

    # ── Order Placement ──────────────────────────────────
    def buy_call(self, option_symbol: str, contracts: int) -> object:
        """Buy to open a call option."""
        if config.DRY_RUN:
            log.info(f"[DRY RUN] BUY CALL: {contracts}x {option_symbol}")
            return {"dry_run": True}
        leg = Leg(
            instrument_type=InstrumentType.EQUITY_OPTION,
            symbol=option_symbol,
            quantity=Decimal(contracts),
            action=OrderAction.BUY_TO_OPEN
        )
        order    = NewOrder(time_in_force=OrderTimeInForce.DAY,
                            order_type=OrderType.MARKET, legs=[leg])
        response = self.account.place_order(self.session, order, dry_run=False)
        log.info(f"BUY CALL placed: {contracts}x {option_symbol}")
        return response

    def buy_put(self, option_symbol: str, contracts: int) -> object:
        """Buy to open a put option."""
        if config.DRY_RUN:
            log.info(f"[DRY RUN] BUY PUT: {contracts}x {option_symbol}")
            return {"dry_run": True}
        leg = Leg(
            instrument_type=InstrumentType.EQUITY_OPTION,
            symbol=option_symbol,
            quantity=Decimal(contracts),
            action=OrderAction.BUY_TO_OPEN
        )
        order    = NewOrder(time_in_force=OrderTimeInForce.DAY,
                            order_type=OrderType.MARKET, legs=[leg])
        response = self.account.place_order(self.session, order, dry_run=False)
        log.info(f"BUY PUT placed: {contracts}x {option_symbol}")
        return response

    def sell_call(self, option_symbol: str, contracts: int) -> object:
        """Sell to close a call option."""
        if config.DRY_RUN:
            log.info(f"[DRY RUN] SELL CALL: {contracts}x {option_symbol}")
            return {"dry_run": True}
        leg = Leg(
            instrument_type=InstrumentType.EQUITY_OPTION,
            symbol=option_symbol,
            quantity=Decimal(contracts),
            action=OrderAction.SELL_TO_CLOSE
        )
        order    = NewOrder(time_in_force=OrderTimeInForce.DAY,
                            order_type=OrderType.MARKET, legs=[leg])
        response = self.account.place_order(self.session, order, dry_run=False)
        log.info(f"SELL CALL placed: {contracts}x {option_symbol}")
        return response

    def sell_put(self, option_symbol: str, contracts: int) -> object:
        """Sell to close a put option."""
        if config.DRY_RUN:
            log.info(f"[DRY RUN] SELL PUT: {contracts}x {option_symbol}")
            return {"dry_run": True}
        leg = Leg(
            instrument_type=InstrumentType.EQUITY_OPTION,
            symbol=option_symbol,
            quantity=Decimal(contracts),
            action=OrderAction.SELL_TO_CLOSE
        )
        order    = NewOrder(time_in_force=OrderTimeInForce.DAY,
                            order_type=OrderType.MARKET, legs=[leg])
        response = self.account.place_order(self.session, order, dry_run=False)
        log.info(f"SELL PUT placed: {contracts}x {option_symbol}")
        return response

    def get_option_price(self, symbol: str) -> float:
        """
        Public method used by exit_manager.
        Uses real-time DXLinkStreamer — falls back to yfinance if needed.
        """
        return self.get_realtime_price(symbol)

    # ── Positions ─────────────────────────────────────────
    def get_open_positions(self) -> list:
        """Return list of open option positions."""
        positions = self.account.get_positions(self.session)
        return [p for p in positions if p.instrument_type == InstrumentType.EQUITY_OPTION]
