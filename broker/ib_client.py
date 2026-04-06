"""
Interactive Brokers Paper Trading Broker Client
All pricing uses IB real-time market data (no delayed yfinance).
yfinance is only used for option chain metadata (expirations, strikes).

Architecture: IB event loop runs on the main thread. All IB API calls
are dispatched via a queue and executed by process_orders() on main thread.
"""
import logging
import re
import threading
import queue
from datetime import date, datetime

from ib_async import IB, Stock, Option, MarketOrder

import config

log = logging.getLogger(__name__)


class IBClient:
    def __init__(self):
        self.ib = IB()
        self._order_queue = queue.Queue()
        self._connected = False

    # ── Connection ────────────────────────────────────────────
    def connect(self):
        """Connect to IB TWS or Gateway on the calling (main) thread."""
        log.info(f"Connecting to Interactive Brokers at {config.IB_HOST}:{config.IB_PORT} "
                 f"(clientId={config.IB_CLIENT_ID})...")
        self.ib.connect(
            host=config.IB_HOST,
            port=config.IB_PORT,
            clientId=config.IB_CLIENT_ID,
            readonly=config.DRY_RUN,
        )
        accounts = self.ib.managedAccounts()
        if config.IB_ACCOUNT:
            if config.IB_ACCOUNT not in accounts:
                log.warning(f"Configured account {config.IB_ACCOUNT} not found in {accounts}")
        log.info(f"Connected to IB — accounts: {accounts}")
        self._connected = True

    def process_orders(self):
        """
        Must be called in a loop on the MAIN thread (where connect() ran).
        Processes all queued IB API calls and drives the event loop.
        """
        while not self._order_queue.empty():
            try:
                func, args, result_event, result_holder = self._order_queue.get_nowait()
                try:
                    result_holder["value"] = func(*args)
                except Exception as e:
                    result_holder["error"] = e
                finally:
                    result_event.set()
            except queue.Empty:
                break
        self.ib.sleep(0.1)

    def _submit_to_ib(self, func, *args, timeout=30):
        """Submit a function to be executed on the main/IB thread."""
        result_event = threading.Event()
        result_holder = {}
        self._order_queue.put((func, args, result_event, result_holder))
        if not result_event.wait(timeout=timeout):
            raise TimeoutError(f"IB call timed out after {timeout}s: {func.__name__}")
        if "error" in result_holder:
            raise result_holder["error"]
        return result_holder.get("value")

    # ── Real-time Equity Price (IB market data) ───────────────
    def get_realtime_equity_price(self, ticker: str) -> float:
        """Get real-time mid price for a stock/ETF via IB. Thread-safe."""
        return self._submit_to_ib(self._ib_get_equity_price, ticker)

    def _ib_get_equity_price(self, ticker: str) -> float:
        """Runs on IB thread."""
        contract = Stock(ticker, "SMART", "USD")
        self.ib.qualifyContracts(contract)
        ticker_data = self.ib.reqMktData(contract, "", False, False)
        self.ib.sleep(2)
        bid = ticker_data.bid if ticker_data.bid > 0 else 0.0
        ask = ticker_data.ask if ticker_data.ask > 0 else 0.0
        self.ib.cancelMktData(contract)
        if bid > 0 and ask > 0:
            mid = round((bid + ask) / 2, 2)
            log.info(f"[IB] {ticker}: bid={bid:.2f} ask={ask:.2f} mid={mid:.2f}")
            return mid
        if ticker_data.last > 0:
            log.info(f"[IB] {ticker}: last={ticker_data.last:.2f}")
            return float(ticker_data.last)
        if ticker_data.close > 0:
            log.info(f"[IB] {ticker}: close={ticker_data.close:.2f}")
            return float(ticker_data.close)
        raise ValueError(f"No IB price data for {ticker}")

    # ── ATM Option Symbol ─────────────────────────────────────
    def get_atm_call_symbol(self, ticker: str) -> str:
        return self._get_atm_symbol(ticker, "C")

    def get_atm_put_symbol(self, ticker: str) -> str:
        return self._get_atm_symbol(ticker, "P")

    def _get_atm_symbol(self, ticker: str, option_type: str) -> str:
        """
        Build OCC symbol for ATM contract with nearest expiration.
        Uses IB for real-time price, yfinance only for chain metadata.
        """
        import yfinance as yf

        # Get real-time price from IB for ATM strike
        price = self.get_realtime_equity_price(ticker)
        strike = round(price)
        today = date.today()
        today_str = today.strftime("%Y-%m-%d")

        # Use yfinance only for expiration date list (metadata, not pricing)
        yf_ticker = yf.Ticker(ticker)
        expirations = yf_ticker.options

        if not expirations:
            raise RuntimeError(f"No option expirations found for {ticker}")

        if today_str in expirations:
            exp_date = today_str
            log.info(f"[{ticker}] 0DTE expiration available: {exp_date}")
        else:
            future_exps = [e for e in expirations if e >= today_str]
            if not future_exps:
                raise RuntimeError(f"No future expirations found for {ticker}")
            exp_date = future_exps[0]
            log.info(f"[{ticker}] No 0DTE — using nearest expiry: {exp_date}")

        exp_dt = datetime.strptime(exp_date, "%Y-%m-%d")
        exp = exp_dt.strftime("%y%m%d")
        strike_str = str(int(strike * 1000)).zfill(8)
        symbol = f"{ticker}{exp}{option_type}{strike_str}"
        log.info(f"[{ticker}] ATM {option_type} symbol: {symbol} (strike ${strike}, exp {exp_date})")
        return symbol

    # ── OCC Symbol → IB Contract (runs on IB thread) ─────────
    def _occ_to_contract(self, occ_symbol: str) -> Option:
        match = re.match(r'^([A-Z]+)(\d{6})([CP])(\d{8})$', occ_symbol)
        if not match:
            raise ValueError(f"Invalid OCC symbol: {occ_symbol}")
        ticker = match.group(1)
        exp_str = match.group(2)
        right = "C" if match.group(3) == "C" else "P"
        strike = int(match.group(4)) / 1000

        expiry = f"20{exp_str}"
        contract = Option(ticker, expiry, strike, right, "SMART")
        qualified = self.ib.qualifyContracts(contract)
        if not qualified:
            raise RuntimeError(f"Could not qualify IB contract for {occ_symbol}")
        return qualified[0]

    # ── Option Price (IB real-time) ───────────────────────────
    def get_option_price(self, symbol: str) -> float:
        """Get real-time option mid price via IB. Thread-safe."""
        return self._submit_to_ib(self._ib_get_option_price, symbol)

    def _ib_get_option_price(self, symbol: str) -> float:
        """Runs on IB thread."""
        contract = self._occ_to_contract(symbol)
        ticker_data = self.ib.reqMktData(contract, "", False, False)
        self.ib.sleep(2)
        bid = ticker_data.bid if ticker_data.bid > 0 else 0.0
        ask = ticker_data.ask if ticker_data.ask > 0 else 0.0
        self.ib.cancelMktData(contract)
        if bid > 0 and ask > 0:
            mid = round((bid + ask) / 2, 2)
            log.info(f"[IB] {symbol}: bid={bid:.2f} ask={ask:.2f} mid={mid:.2f}")
            return mid
        if ticker_data.last > 0:
            log.info(f"[IB] {symbol}: last={ticker_data.last:.2f}")
            return float(ticker_data.last)
        if ticker_data.close > 0:
            log.info(f"[IB] {symbol}: close={ticker_data.close:.2f}")
            return float(ticker_data.close)
        raise ValueError(f"No IB option price data for {symbol}")

    # ── Order Placement ───────────────────────────────────────
    def buy_call(self, option_symbol: str, contracts: int) -> object:
        return self._place_order(option_symbol, contracts, "BUY", "call")

    def buy_put(self, option_symbol: str, contracts: int) -> object:
        return self._place_order(option_symbol, contracts, "BUY", "put")

    def sell_call(self, option_symbol: str, contracts: int) -> object:
        return self._place_order(option_symbol, contracts, "SELL", "call")

    def sell_put(self, option_symbol: str, contracts: int) -> object:
        return self._place_order(option_symbol, contracts, "SELL", "put")

    def _place_order(self, option_symbol: str, contracts: int, action: str, desc: str) -> object:
        if config.DRY_RUN:
            log.info(f"[DRY RUN] IB {action} {desc.upper()}: {contracts}x {option_symbol}")
            return {"dry_run": True, "symbol": option_symbol}
        return self._submit_to_ib(self._ib_place_order, option_symbol, contracts, action, desc)

    def _ib_place_order(self, option_symbol, contracts, action, desc):
        """Runs on IB thread. Returns dict with actual fill price."""
        contract = self._occ_to_contract(option_symbol)
        order = MarketOrder(action, contracts)
        if config.IB_ACCOUNT:
            order.account = config.IB_ACCOUNT
        trade = self.ib.placeOrder(contract, order)

        # Wait for fill (up to 10 seconds)
        for _ in range(20):
            self.ib.sleep(0.5)
            if trade.orderStatus.status == "Filled":
                break

        fill_price = trade.orderStatus.avgFillPrice
        status = trade.orderStatus.status
        log.info(f"[IB] {action} {desc.upper()}: {contracts}x {option_symbol} — "
                 f"orderId={trade.order.orderId} status={status} "
                 f"avgFillPrice=${fill_price:.2f}")
        return {
            "symbol": option_symbol,
            "contracts": contracts,
            "order_id": trade.order.orderId,
            "status": status,
            "fill_price": fill_price,
        }

    # ── Positions ─────────────────────────────────────────────
    def get_open_positions(self) -> list:
        try:
            return self._submit_to_ib(self._ib_get_positions)
        except Exception as e:
            log.warning(f"Could not fetch IB positions: {e}")
            return []

    def _ib_get_positions(self) -> list:
        positions = self.ib.positions()
        return [
            {
                "symbol": p.contract.localSymbol or str(p.contract),
                "qty": float(p.position),
                "avg_cost": float(p.avgCost),
            }
            for p in positions
            if p.contract.secType == "OPT"
        ]
