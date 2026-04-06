"""
Interactive Brokers Paper Trading Broker Client
Connects to IB TWS or IB Gateway via ib_async for option trading.
Requires TWS/Gateway running with API connections enabled.
Default paper trading port: 7497 (TWS) or 4002 (Gateway).

All IB API calls are dispatched to a dedicated worker thread to avoid
asyncio event loop conflicts with multi-threaded scanners.
"""
import logging
import re
import threading
import queue
from datetime import date, datetime

from ib_async import IB, Stock, Option, MarketOrder, Contract

import config

log = logging.getLogger(__name__)


class IBClient:
    def __init__(self):
        self.ib = IB()
        self._queue = queue.Queue()
        self._worker = None

    def _start_worker(self):
        """Start a dedicated thread for all IB API calls."""
        self._worker = threading.Thread(target=self._worker_loop, daemon=True, name="ib-worker")
        self._worker.start()

    def _worker_loop(self):
        """Process IB API calls sequentially from the queue."""
        while True:
            func, args, result_event, result_holder = self._queue.get()
            try:
                result_holder["value"] = func(*args)
            except Exception as e:
                result_holder["error"] = e
            finally:
                result_event.set()
                self._queue.task_done()

    def _run_on_ib(self, func, *args, timeout=30):
        """Submit a function to the IB worker thread and wait for result."""
        result_event = threading.Event()
        result_holder = {}
        self._queue.put((func, args, result_event, result_holder))
        if not result_event.wait(timeout=timeout):
            raise TimeoutError(f"IB call timed out after {timeout}s: {func.__name__}")
        if "error" in result_holder:
            raise result_holder["error"]
        return result_holder.get("value")

    # ── Authentication / Connection ───────────────────────────
    def connect(self):
        """Connect to IB TWS or Gateway."""
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
        # Start the worker thread after connection is established
        self._start_worker()

    # ── Real-time Equity Price ────────────────────────────────
    def get_realtime_equity_price(self, ticker: str) -> float:
        """Get equity price via yfinance (thread-safe, non-blocking)."""
        return self._get_equity_price_yf(ticker)

    def _get_equity_price_yf(self, ticker: str) -> float:
        """Get stock price via yfinance."""
        import yfinance as yf
        price = float(yf.Ticker(ticker).fast_info["lastPrice"])
        log.info(f"{ticker} price (yfinance): ${price:.2f}")
        return price

    # ── ATM Option Symbol ─────────────────────────────────────
    def get_atm_call_symbol(self, ticker: str) -> str:
        return self._get_atm_symbol(ticker, "C")

    def get_atm_put_symbol(self, ticker: str) -> str:
        return self._get_atm_symbol(ticker, "P")

    def _get_atm_symbol(self, ticker: str, option_type: str) -> str:
        """
        Build an OCC option symbol for the ATM contract using the nearest
        available expiration. Uses yfinance (thread-safe).
        Prefers 0DTE if available, otherwise picks the closest expiry.
        """
        import yfinance as yf

        price = self._get_equity_price_yf(ticker)
        strike = round(price)
        today = date.today()
        today_str = today.strftime("%Y-%m-%d")

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

    # ── OCC Symbol → IB Contract ──────────────────────────────
    def _occ_to_contract(self, occ_symbol: str) -> Option:
        """Parse OCC symbol → IB Option contract. Must run on IB worker thread."""
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

    # ── Option Price ──────────────────────────────────────────
    def get_option_price(self, symbol: str) -> float:
        """Get option price. Uses yfinance (thread-safe). Falls back to IB worker."""
        try:
            price = self._get_option_price_yf(symbol)
            if price and price > 0 and price != 1.00:
                return price
        except Exception:
            pass

        # Fallback: IB market data via worker thread
        try:
            return self._run_on_ib(self._ib_get_option_price, symbol)
        except Exception as e:
            log.warning(f"IB option price failed ({e}) — using $1.00 fallback")
            return 1.00

    def _ib_get_option_price(self, symbol: str) -> float:
        """Get option price via IB. Runs on worker thread."""
        contract = self._occ_to_contract(symbol)
        ticker_data = self.ib.reqMktData(contract, "", False, False)
        self.ib.sleep(2)
        bid = ticker_data.bid if ticker_data.bid > 0 else 0.0
        ask = ticker_data.ask if ticker_data.ask > 0 else 0.0
        if bid > 0 and ask > 0:
            mid = round((bid + ask) / 2, 2)
            log.info(f"[IB] {symbol}: bid={bid:.2f} ask={ask:.2f} mid={mid:.2f}")
            self.ib.cancelMktData(contract)
            return mid
        if ticker_data.last > 0:
            log.info(f"[IB] {symbol}: last={ticker_data.last:.2f}")
            self.ib.cancelMktData(contract)
            return float(ticker_data.last)
        self.ib.cancelMktData(contract)
        raise ValueError("No option price data received")

    def _get_option_price_yf(self, symbol: str) -> float:
        """Get option mid price via yfinance."""
        import yfinance as yf
        i = 0
        while i < len(symbol) and symbol[i].isalpha():
            i += 1
        ticker = symbol[:i]
        exp_str = symbol[i:i+6]
        opt_type = symbol[i+6]
        strike = int(symbol[i+7:]) / 1000
        exp_date = f"20{exp_str[:2]}-{exp_str[2:4]}-{exp_str[4:6]}"

        yf_ticker = yf.Ticker(ticker)
        chain = yf_ticker.option_chain(exp_date)
        df = chain.calls if opt_type == "C" else chain.puts

        row = df[df["contractSymbol"] == symbol]
        if row.empty:
            df["dist"] = abs(df["strike"] - strike)
            row = df.loc[[df["dist"].idxmin()]]

        bid = float(row["bid"].values[0])
        ask = float(row["ask"].values[0])
        mid = round((bid + ask) / 2, 2)
        log.info(f"Option price (yfinance) {symbol}: mid={mid:.2f}")
        return mid

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
        # Dispatch to IB worker thread
        return self._run_on_ib(self._ib_place_order, option_symbol, contracts, action, desc)

    def _ib_place_order(self, option_symbol: str, contracts: int, action: str, desc: str) -> dict:
        """Place order on IB. Runs on worker thread."""
        contract = self._occ_to_contract(option_symbol)
        order = MarketOrder(action, contracts)
        if config.IB_ACCOUNT:
            order.account = config.IB_ACCOUNT
        trade = self.ib.placeOrder(contract, order)
        self.ib.sleep(1)
        log.info(f"[IB] {action} {desc.upper()}: {contracts}x {option_symbol} — "
                 f"orderId={trade.order.orderId} status={trade.orderStatus.status}")
        return {
            "symbol": option_symbol,
            "contracts": contracts,
            "order_id": trade.order.orderId,
            "status": trade.orderStatus.status,
        }

    # ── Positions ─────────────────────────────────────────────
    def get_open_positions(self) -> list:
        """Return list of open option positions."""
        try:
            positions = self._run_on_ib(self._ib_get_positions)
            return positions
        except Exception as e:
            log.warning(f"Could not fetch IB positions: {e}")
            return []

    def _ib_get_positions(self) -> list:
        """Get positions from IB. Runs on worker thread."""
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
