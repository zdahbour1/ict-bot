"""
Interactive Brokers Paper Trading Broker Client
All pricing uses IB real-time market data.

Architecture: IB event loop runs on the main thread. All IB API calls
are dispatched via a queue and executed by process_orders() on main thread.

Features:
- Contract validation before order placement
- Bracket orders (OCO TP+SL) for server-side enforcement
- Timeout hardening with fill status checking
- Position reconciliation
"""
import logging
import re
import threading
import queue
from datetime import date, datetime

from ib_async import IB, Stock, Option, MarketOrder, LimitOrder, StopOrder

import config

log = logging.getLogger(__name__)


def _check_not_flex(contract, option_symbol: str):
    """Reject Flex options before order placement. IB rejects these with code 201.
    Flex options have secType='FOP' or tradingClass different from symbol."""
    sec_type = getattr(contract, 'secType', '') or ''
    trading_class = getattr(contract, 'tradingClass', '') or ''
    symbol = getattr(contract, 'symbol', '') or ''

    if sec_type == 'FOP':
        raise RuntimeError(f"Flex option detected for {option_symbol} — "
                           f"secType={sec_type} tradingClass={trading_class}. "
                           f"IB does not allow standard orders on Flex options.")

    # Some Flex contracts have secType='OPT' but unusual tradingClass
    # IB error message mentions "IB-cleared orders are not allowed for Flex options"
    # Log the contract details for debugging
    log.debug(f"[FLEX CHECK] {option_symbol}: secType={sec_type} "
              f"tradingClass={trading_class} symbol={symbol}")


class IBClient:
    """
    IB API client. Wraps an IBConnection from the connection pool.

    Two modes:
    1. Pool mode: pass an IBConnection + shared cache (preferred)
    2. Legacy mode: no args — creates its own IB() and queues (backwards compatible)
    """

    def __init__(self, connection=None, contract_cache=None, cache_lock=None):
        if connection is not None:
            # ── Pool mode: use provided IBConnection ──
            self._conn = connection
            self.ib = connection.ib
            self._contract_cache = contract_cache if contract_cache is not None else {}
            self._cache_lock = cache_lock if cache_lock is not None else threading.Lock()
            self._connected = connection.connected
            self._pool_mode = True
        else:
            # ── Legacy mode: standalone IB connection ──
            self._conn = None
            self.ib = IB()
            self._contract_cache = {}
            self._cache_lock = threading.Lock()
            self._connected = False
            self._pool_mode = False
            self._order_queue = queue.Queue()
            self._priority_queue = queue.Queue()
            self._last_errors = {}
            self._last_errors_lock = threading.Lock()

    # ── Connection ────────────────────────────────────────────
    def connect(self):
        """Connect to IB. Only used in legacy mode — pool mode connects via pool."""
        if self._pool_mode:
            raise RuntimeError("IBClient in pool mode — use pool.connect_all() instead")
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
        self._register_error_handler()
        self._connected = True

    # ── IB Error Event Handling (legacy mode only) ──────────────
    def _register_error_handler(self):
        if self._pool_mode:
            return  # Pool connections register their own handlers
        self.ib.errorEvent += self._on_ib_error
        log.info("IB errorEvent handler registered")

    _IB_INFO_CODES = {2104, 2106, 2107, 2108, 2119, 2158}
    _IB_ACTIONABLE_CODES = {104, 110, 125, 135, 161, 201, 202, 203, 399, 10147}
    _IB_CRITICAL_CODES = {201, 202, 203}

    def _on_ib_error(self, reqId, errorCode, errorString, contract):
        """Legacy mode error handler. Non-blocking."""
        if errorCode in self._IB_INFO_CODES:
            return
        if errorCode in self._IB_ACTIONABLE_CODES:
            with self._last_errors_lock:
                self._last_errors[reqId] = {
                    "code": errorCode, "message": errorString,
                    "reqId": reqId, "contract": str(contract) if contract else None,
                }
        if errorCode in self._IB_CRITICAL_CODES:
            log.error(f"[IB ERROR] reqId={reqId} code={errorCode}: {errorString}")
        else:
            log.warning(f"[IB ERROR] reqId={reqId} code={errorCode}: {errorString}")

    def _get_last_error(self, order_id: int) -> dict | None:
        """Retrieve and remove the last captured IB error for an order_id."""
        if self._pool_mode:
            return self._conn.get_last_error(order_id)
        with self._last_errors_lock:
            return self._last_errors.pop(order_id, None)

    def process_orders(self):
        """Legacy mode only: process queues on the main thread."""
        if self._pool_mode:
            return  # Pool connections have their own event loops
        while not self._priority_queue.empty():
            try:
                func, args, result_event, result_holder = self._priority_queue.get_nowait()
                try:
                    result_holder["value"] = func(*args)
                except Exception as e:
                    result_holder["error"] = e
                finally:
                    result_event.set()
            except queue.Empty:
                break
        if not self._order_queue.empty():
            try:
                func, args, result_event, result_holder = self._order_queue.get_nowait()
                try:
                    result_holder["value"] = func(*args)
                except Exception as e:
                    result_holder["error"] = e
                finally:
                    result_event.set()
            except queue.Empty:
                pass
        self.ib.sleep(0.1)

    def _submit_to_ib(self, func, *args, timeout=60, priority=False):
        """Submit a function to run on the IB event loop thread."""
        if self._pool_mode:
            return self._conn.submit(func, *args, timeout=timeout)
        # Legacy mode: use internal queues
        result_event = threading.Event()
        result_holder = {}
        q = self._priority_queue if priority else self._order_queue
        q.put((func, args, result_event, result_holder))
        if not result_event.wait(timeout=timeout):
            raise TimeoutError(f"IB call timed out after {timeout}s: {func.__name__}")
        if "error" in result_holder:
            raise result_holder["error"]
        return result_holder.get("value")

    # ── Real-time Equity Price (IB market data) ───────────────
    def get_realtime_equity_price(self, ticker: str) -> float:
        return self._submit_to_ib(self._ib_get_equity_price, ticker)

    def _ib_get_equity_price(self, ticker: str) -> float:
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
            return float(ticker_data.last)
        if ticker_data.close > 0:
            return float(ticker_data.close)
        raise ValueError(f"No IB price data for {ticker}")

    # ── ATM Option Symbol (delegates to ib_contracts.py) ────────
    def get_atm_call_symbol(self, ticker: str) -> str:
        return self._submit_to_ib(self._ib_get_atm_symbol, ticker, "C")

    def get_atm_put_symbol(self, ticker: str) -> str:
        return self._submit_to_ib(self._ib_get_atm_symbol, ticker, "P")

    def _ib_get_atm_symbol(self, ticker: str, option_type: str) -> str:
        from broker.ib_contracts import ib_get_atm_symbol
        return ib_get_atm_symbol(self.ib, ticker, option_type, self._contract_cache)

    # ── Contract Validation (delegates to ib_contracts.py) ────
    def validate_contract(self, occ_symbol: str) -> bool:
        try:
            return self._submit_to_ib(self._ib_validate_contract, occ_symbol)
        except Exception:
            return False

    def _ib_validate_contract(self, occ_symbol: str) -> bool:
        from broker.ib_contracts import ib_validate_contract
        return ib_validate_contract(self.ib, occ_symbol, self._contract_cache)

    def _occ_to_contract(self, occ_symbol: str):
        from broker.ib_contracts import ib_occ_to_contract
        return ib_occ_to_contract(self.ib, occ_symbol, self._contract_cache)

    # ── Option Price (IB real-time) ───────────────────────────
    def get_option_price(self, symbol: str, priority: bool = False) -> float:
        timeout = 10 if priority else 30
        return self._submit_to_ib(self._ib_get_option_price, symbol, priority=priority, timeout=timeout)

    def get_option_prices_batch(self, symbols: list[str]) -> dict[str, float]:
        """Get prices for multiple options in one IB call. Much faster than sequential."""
        try:
            return self._submit_to_ib(self._ib_get_option_prices_batch, symbols, priority=True, timeout=20)
        except Exception as e:
            log.warning(f"Batch price fetch failed: {e}")
            return {}

    def _ib_get_option_prices_batch(self, symbols: list[str]) -> dict[str, float]:
        """Subscribe to all contracts, sleep once, read all prices."""
        contracts = []
        symbol_map = {}
        for sym in symbols:
            try:
                c = self._occ_to_contract(sym)
                contracts.append(c)
                symbol_map[c.conId] = sym
            except Exception as e:
                continue

        if not contracts:
            return {}

        # Subscribe all at once
        tickers = []
        for c in contracts:
            tickers.append(self.ib.reqMktData(c, "", False, False))

        # Single sleep for all
        self.ib.sleep(2)

        # Read all prices
        prices = {}
        for ticker_data, contract in zip(tickers, contracts):
            sym = symbol_map.get(contract.conId, "")
            bid = ticker_data.bid if ticker_data.bid and ticker_data.bid > 0 else 0.0
            ask = ticker_data.ask if ticker_data.ask and ticker_data.ask > 0 else 0.0
            self.ib.cancelMktData(contract)
            if bid > 0 and ask > 0:
                prices[sym] = round((bid + ask) / 2, 2)
            elif ticker_data.last and ticker_data.last > 0:
                prices[sym] = float(ticker_data.last)
            elif ticker_data.close and ticker_data.close > 0:
                prices[sym] = float(ticker_data.close)

        log.info(f"[IB] Batch prices: {len(prices)}/{len(symbols)} symbols priced")
        return prices

    def _ib_get_option_price(self, symbol: str) -> float:
        contract = self._occ_to_contract(symbol)
        ticker_data = self.ib.reqMktData(contract, "", False, False)
        self.ib.sleep(1.5)
        bid = ticker_data.bid if ticker_data.bid > 0 else 0.0
        ask = ticker_data.ask if ticker_data.ask > 0 else 0.0
        self.ib.cancelMktData(contract)
        if bid > 0 and ask > 0:
            mid = round((bid + ask) / 2, 2)
            log.info(f"[IB] {symbol}: bid={bid:.2f} ask={ask:.2f} mid={mid:.2f}")
            return mid
        if ticker_data.last > 0:
            return float(ticker_data.last)
        if ticker_data.close > 0:
            return float(ticker_data.close)
        raise ValueError(f"No IB option price data for {symbol}")

    # ── Option Greeks (IB real-time) ──────────────────────────
    def get_option_greeks(self, symbol: str) -> dict:
        try:
            return self._submit_to_ib(self._ib_get_greeks, symbol)
        except Exception as e:
            log.warning(f"Greeks fetch failed for {symbol}: {e}")
            return {"delta": None, "gamma": None, "theta": None, "vega": None}

    def _ib_get_greeks(self, symbol: str) -> dict:
        contract = self._occ_to_contract(symbol)
        ticker_data = self.ib.reqMktData(contract, "", False, False)
        self.ib.sleep(3)
        self.ib.cancelMktData(contract)
        greeks = {"delta": None, "gamma": None, "theta": None, "vega": None}
        mg = ticker_data.modelGreeks
        if mg:
            greeks["delta"] = round(mg.delta, 4) if mg.delta is not None else None
            greeks["gamma"] = round(mg.gamma, 6) if mg.gamma is not None else None
            greeks["theta"] = round(mg.theta, 4) if mg.theta is not None else None
            greeks["vega"] = round(mg.vega, 4) if mg.vega is not None else None
        return greeks

    # ── VIX (IB real-time) ────────────────────────────────────
    def get_vix(self) -> float | None:
        try:
            return self._submit_to_ib(self._ib_get_vix)
        except Exception as e:
            log.warning(f"VIX fetch failed: {e}")
            return None

    def _ib_get_vix(self) -> float:
        from ib_async import Index
        contract = Index("VIX", "CBOE")
        self.ib.qualifyContracts(contract)
        ticker_data = self.ib.reqMktData(contract, "", False, False)
        self.ib.sleep(2)
        self.ib.cancelMktData(contract)
        if ticker_data.last and ticker_data.last > 0:
            return round(float(ticker_data.last), 2)
        if ticker_data.close and ticker_data.close > 0:
            return round(float(ticker_data.close), 2)
        raise ValueError("No VIX data received")

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
        """Place order with contract validation and fill confirmation."""
        # ── Contract validation ───────────────────────────
        contract = self._occ_to_contract(option_symbol)
        if not contract or not contract.conId:
            raise RuntimeError(f"Contract validation failed for {option_symbol} — order NOT placed")

        # ── Flex option check — IB rejects these with code 201 ──
        _check_not_flex(contract, option_symbol)

        order = MarketOrder(action, contracts)
        if config.IB_ACCOUNT:
            order.account = config.IB_ACCOUNT
        trade = self.ib.placeOrder(contract, order)

        # ── Wait for fill (up to 5 seconds) ───────────────
        # Market orders fill in <1s during market hours.
        # Don't block the queue waiting longer — return Submitted status
        # and let exit manager monitor it.
        for _ in range(10):
            self.ib.sleep(0.5)
            if trade.orderStatus.status == "Filled":
                break

        fill_price = trade.orderStatus.avgFillPrice
        status = trade.orderStatus.status

        # ── Quick check if not filled yet ─────────────────
        if status != "Filled" and status not in ("Cancelled", "Inactive"):
            self.ib.sleep(1)
            if trade.orderStatus.status == "Filled":
                fill_price = trade.orderStatus.avgFillPrice
                status = "Filled"
            elif trade.fills:
                fill_price = trade.fills[0].execution.avgPrice
                status = "Filled"
            else:
                log.warning(f"[IB] Order {trade.order.orderId} not filled after 6s — "
                            f"status: {trade.orderStatus.status} (will track as Submitted)")

        perm_id = trade.order.permId
        con_id = contract.conId
        log.info(f"[IB] {action} {desc.upper()}: {contracts}x {option_symbol} — "
                 f"orderId={trade.order.orderId} permId={perm_id} conId={con_id} "
                 f"status={status} fill=${fill_price:.2f}")

        result = {
            "symbol": option_symbol,
            "contracts": contracts,
            "order_id": trade.order.orderId,
            "perm_id": perm_id,
            "con_id": con_id,
            "status": status,
            "fill_price": fill_price,
        }

        # Attach IB error info if the order was rejected/errored
        ib_error = self._get_last_error(trade.order.orderId)
        if ib_error:
            result["ib_error"] = ib_error
            log.warning(f"[IB] Order {trade.order.orderId} error: "
                        f"code={ib_error['code']} {ib_error['message']}")

        return result

    # ── Bracket Orders (OCO TP + SL on IB) ────────────────────
    def place_bracket_order(self, option_symbol: str, contracts: int,
                            action: str, tp_price: float, sl_price: float) -> dict:
        """Place a bracket order: parent (market) + TP (limit) + SL (stop)."""
        if config.DRY_RUN:
            log.info(f"[DRY RUN] Bracket {action}: {contracts}x {option_symbol} TP=${tp_price:.2f} SL=${sl_price:.2f}")
            return {"dry_run": True, "symbol": option_symbol}
        return self._submit_to_ib(
            self._ib_place_bracket, option_symbol, contracts, action, tp_price, sl_price
        )

    def _ib_place_bracket(self, option_symbol, contracts, action, tp_price, sl_price):
        """Runs on IB thread. Places bracket order."""
        contract = self._occ_to_contract(option_symbol)
        if not contract or not contract.conId:
            raise RuntimeError(f"Contract validation failed for {option_symbol}")

        # ── Flex option check — IB rejects these with code 201 ──
        _check_not_flex(contract, option_symbol)

        # Exit side is opposite of entry
        exit_action = "SELL" if action == "BUY" else "BUY"

        # Parent: market order
        parent = MarketOrder(action, contracts)
        parent.orderId = self.ib.client.getReqId()
        parent.transmit = False
        if config.IB_ACCOUNT:
            parent.account = config.IB_ACCOUNT

        # Take profit: limit order
        tp_order = LimitOrder(exit_action, contracts, tp_price)
        tp_order.orderId = self.ib.client.getReqId()
        tp_order.parentId = parent.orderId
        tp_order.transmit = False
        if config.IB_ACCOUNT:
            tp_order.account = config.IB_ACCOUNT

        # Stop loss: stop order
        sl_order = StopOrder(exit_action, contracts, sl_price)
        sl_order.orderId = self.ib.client.getReqId()
        sl_order.parentId = parent.orderId
        sl_order.transmit = True  # last child triggers all
        if config.IB_ACCOUNT:
            sl_order.account = config.IB_ACCOUNT

        # Place all three
        parent_trade = self.ib.placeOrder(contract, parent)
        tp_trade = self.ib.placeOrder(contract, tp_order)
        sl_trade = self.ib.placeOrder(contract, sl_order)

        # Wait for parent fill (up to 5 seconds — market orders fill in <1s)
        for _ in range(10):
            self.ib.sleep(0.5)
            if parent_trade.orderStatus.status == "Filled":
                break

        fill_price = parent_trade.orderStatus.avgFillPrice
        status = parent_trade.orderStatus.status

        # Quick extra check if not filled yet
        if status != "Filled" and status not in ("Cancelled", "Inactive"):
            self.ib.sleep(1)
            if parent_trade.orderStatus.status == "Filled":
                fill_price = parent_trade.orderStatus.avgFillPrice
                status = "Filled"

        perm_id = parent_trade.order.permId
        tp_perm_id = tp_trade.order.permId
        sl_perm_id = sl_trade.order.permId
        con_id = contract.conId

        log.info(f"[IB] BRACKET {action}: {contracts}x {option_symbol} — "
                 f"parent={parent.orderId} permId={perm_id} conId={con_id} "
                 f"status={status} fill=${fill_price:.2f} "
                 f"TP={tp_order.orderId}(perm={tp_perm_id}) @ ${tp_price:.2f} "
                 f"SL={sl_order.orderId}(perm={sl_perm_id}) @ ${sl_price:.2f}")

        result = {
            "symbol": option_symbol,
            "contracts": contracts,
            "order_id": parent.orderId,
            "perm_id": perm_id,
            "con_id": con_id,
            "tp_order_id": tp_order.orderId,
            "tp_perm_id": tp_perm_id,
            "sl_order_id": sl_order.orderId,
            "sl_perm_id": sl_perm_id,
            "status": status,
            "fill_price": fill_price,
        }

        # Attach IB error info if the parent order was rejected/errored
        ib_error = self._get_last_error(parent.orderId)
        if ib_error:
            result["ib_error"] = ib_error
            log.warning(f"[IB] Bracket parent {parent.orderId} error: "
                        f"code={ib_error['code']} {ib_error['message']}")

        return result

    def update_bracket_sl(self, sl_order_id: int, new_sl_price: float) -> bool:
        """Update the stop loss leg of a bracket order."""
        if config.DRY_RUN:
            return True
        try:
            return self._submit_to_ib(self._ib_update_bracket_sl, sl_order_id, new_sl_price)
        except Exception as e:
            log.warning(f"Failed to update bracket SL: {e}")
            return False

    def _ib_update_bracket_sl(self, sl_order_id: int, new_sl_price: float) -> bool:
        """Modify the SL order price on IB."""
        for trade in self.ib.openTrades():
            if trade.order.orderId == sl_order_id:
                trade.order.auxPrice = new_sl_price
                self.ib.placeOrder(trade.contract, trade.order)
                log.info(f"[IB] Updated bracket SL orderId={sl_order_id} → ${new_sl_price:.2f}")
                return True
        log.warning(f"[IB] SL orderId={sl_order_id} not found in open trades")
        return False

    def cancel_bracket_children(self, tp_order_id: int, sl_order_id: int):
        """Cancel TP and SL legs by stored order IDs (legacy)."""
        if config.DRY_RUN:
            return
        try:
            self._submit_to_ib(self._ib_cancel_orders, tp_order_id, sl_order_id)
        except Exception as e:
            log.warning(f"Failed to cancel bracket children: {e}")

    def _ib_cancel_orders(self, *order_ids):
        for trade in self.ib.openTrades():
            if trade.order.orderId in order_ids:
                self.ib.cancelOrder(trade.order)
                log.info(f"[IB] Cancelled orderId={trade.order.orderId}")

    def find_open_orders_for_contract(self, con_id: int, symbol: str) -> list:
        """Find ALL open orders on IB for a specific contract.
        Searches by conId (primary) and symbol (fallback). Thread-safe."""
        try:
            return self._submit_to_ib(self._ib_find_orders_for_contract, con_id, symbol, timeout=10)
        except Exception as e:
            log.warning(f"Failed to query open orders: {e}")
            return []

    def _ib_find_orders_for_contract(self, con_id: int, symbol: str) -> list:
        """Runs on IB thread. Returns all open orders matching this contract."""
        symbol_clean = (symbol or "").replace(" ", "")
        results = []
        for trade in self.ib.openTrades():
            matched = False
            # Match by conId (exact)
            if con_id and trade.contract and trade.contract.conId == con_id:
                matched = True
            # Fallback: match by symbol
            elif symbol_clean and trade.contract:
                local = (trade.contract.localSymbol or "").replace(" ", "")
                if local == symbol_clean:
                    matched = True

            if matched:
                status = trade.orderStatus.status
                if status not in ("Cancelled", "Inactive", "ApiCancelled"):
                    results.append({
                        "orderId": trade.order.orderId,
                        "permId": trade.order.permId,
                        "action": trade.order.action,
                        "orderType": trade.order.orderType,
                        "totalQty": float(trade.order.totalQuantity),
                        "status": status,
                        "conId": trade.contract.conId if trade.contract else None,
                    })
        return results

    def cancel_order_by_id(self, order_id: int):
        """Cancel a specific order by orderId. Thread-safe."""
        try:
            self._submit_to_ib(self._ib_cancel_single_order, order_id, timeout=5)
        except Exception as e:
            log.warning(f"Failed to cancel orderId={order_id}: {e}")

    def _ib_cancel_single_order(self, order_id: int):
        """Runs on IB thread. Cancel one order."""
        for trade in self.ib.openTrades():
            if trade.order.orderId == order_id:
                self.ib.cancelOrder(trade.order)
                log.info(f"[IB] Cancel sent for orderId={order_id}")
                return
        log.warning(f"[IB] orderId={order_id} not found in openTrades")

    def check_bracket_orders_active(self, trade: dict) -> bool:
        """Check if bracket orders (TP/SL) are still active on IB.
        Returns True if any bracket orders are still open/submitted."""
        tp_id = trade.get("ib_tp_order_id")
        sl_id = trade.get("ib_sl_order_id")
        if not tp_id and not sl_id:
            return False
        try:
            return self._submit_to_ib(self._ib_check_brackets_active, tp_id, sl_id, timeout=5)
        except Exception:
            return False  # If can't check, assume cancelled (safe to proceed)

    def _ib_check_brackets_active(self, tp_id, sl_id) -> bool:
        """Runs on IB thread. Returns True if any bracket legs still active."""
        check_ids = {i for i in [tp_id, sl_id] if i}
        for trade in self.ib.openTrades():
            if trade.order.orderId in check_ids:
                status = trade.orderStatus.status
                if status in ("Submitted", "PreSubmitted", "PendingSubmit"):
                    return True  # Still active
        return False  # All cancelled or not found

    # ── Orphaned Order Cleanup ─────────────────────────────────
    def cleanup_orphaned_orders(self) -> int:
        """Cancel all open IB orders that don't match a DB open trade.
        Returns count of orders cancelled. Call on startup."""
        try:
            return self._submit_to_ib(self._ib_cleanup_orphans, timeout=30)
        except Exception as e:
            log.warning(f"Orphaned order cleanup failed: {e}")
            return 0

    def _ib_cleanup_orphans(self) -> int:
        """Runs on IB thread. Finds and cancels unmatched orders."""
        # Get all open trade IDs from DB
        db_order_ids = set()
        try:
            from db.connection import get_session
            from sqlalchemy import text
            session = get_session()
            if session:
                rows = session.execute(
                    text("SELECT ib_order_id FROM trades WHERE status='open' AND ib_order_id IS NOT NULL "
                         "UNION SELECT ib_tp_perm_id FROM trades WHERE status='open' AND ib_tp_perm_id IS NOT NULL "
                         "UNION SELECT ib_sl_perm_id FROM trades WHERE status='open' AND ib_sl_perm_id IS NOT NULL")
                ).fetchall()
                db_order_ids = {int(r[0]) for r in rows if r[0]}
                session.close()
        except Exception as e:
            log.warning(f"Could not query DB for order IDs: {e}")
            return 0

        # Check all open IB orders
        cancelled = 0
        for trade in self.ib.openTrades():
            order_id = trade.order.orderId
            perm_id = trade.order.permId
            status = trade.orderStatus.status

            # Skip if matched to a DB trade
            if order_id in db_order_ids or perm_id in db_order_ids:
                continue

            # Skip already-cancelled orders
            if status in ("Cancelled", "Inactive", "ApiCancelled"):
                continue

            # Orphan — cancel it
            symbol = ""
            if trade.contract and trade.contract.localSymbol:
                symbol = trade.contract.localSymbol.strip()
            log.warning(f"[CLEANUP] Cancelling orphaned order: orderId={order_id} "
                        f"permId={perm_id} status={status} symbol={symbol}")
            try:
                self.ib.cancelOrder(trade.order)
                cancelled += 1
            except Exception as e:
                log.warning(f"[CLEANUP] Failed to cancel orderId={order_id}: {e}")

        if cancelled:
            log.info(f"[CLEANUP] Cancelled {cancelled} orphaned order(s)")
        else:
            log.info("[CLEANUP] No orphaned orders found")
        return cancelled

    # ── IB Reconciliation ─────────────────────────────────────
    def get_ib_positions_raw(self) -> list:
        """Get raw IB positions for reconciliation. Thread-safe.
        RAISES on failure — caller must handle. Never returns empty on timeout."""
        return self._submit_to_ib(self._ib_get_positions_raw, timeout=45)

    def _ib_get_positions_raw(self) -> list:
        """Returns detailed position info for reconciliation."""
        positions = self.ib.positions()
        result = []
        for p in positions:
            if p.contract.secType == "OPT" and p.position != 0:
                result.append({
                    "symbol": p.contract.localSymbol.strip() if p.contract.localSymbol else "",
                    "conId": p.contract.conId,
                    "ticker": p.contract.symbol,
                    "expiry": p.contract.lastTradeDateOrContractMonth,
                    "strike": p.contract.strike,
                    "right": p.contract.right,
                    "qty": float(p.position),
                    "avg_cost": float(p.avgCost) / 100,  # IB avgCost is per share, convert to per contract
                    "market_price": float(p.marketPrice) if hasattr(p, 'marketPrice') else 0,
                })
        return result

    # ── Position Quantity Check (safety guard for sell orders) ──
    def get_position_quantity(self, con_id: int) -> int:
        """Get the current position quantity for a specific conId on IB.
        Returns: positive int for long, negative for short, 0 if no position.
        Thread-safe. Returns 0 on error (safe — prevents selling)."""
        if not con_id:
            return 0
        try:
            return self._submit_to_ib(self._ib_get_position_qty, con_id, timeout=15)
        except Exception as e:
            log.warning(f"Could not check position for conId={con_id}: {e}")
            return 0  # Safe default — don't sell if we can't verify

    def _ib_get_position_qty(self, con_id: int) -> int:
        """Get position quantity for a specific conId. Runs on IB thread."""
        for p in self.ib.positions():
            if p.contract.conId == con_id:
                return int(p.position)
        return 0

    # ── Check Recent Executions (for timeout hardening) ───────
    def check_recent_fills(self, symbol: str) -> dict | None:
        """Check if a symbol was recently filled on IB. Thread-safe."""
        try:
            return self._submit_to_ib(self._ib_check_fills, symbol)
        except Exception as e:
            return None

    def _ib_check_fills(self, symbol: str) -> dict | None:
        """Check recent executions for a symbol."""
        fills = self.ib.fills()
        for fill in reversed(fills):
            local_sym = fill.contract.localSymbol.strip() if fill.contract.localSymbol else ""
            if symbol in local_sym or (fill.contract.symbol and fill.contract.symbol in symbol):
                return {
                    "symbol": local_sym,
                    "qty": float(fill.execution.shares),
                    "price": float(fill.execution.price),
                    "side": fill.execution.side,
                    "time": str(fill.execution.time),
                    "order_id": fill.execution.orderId,
                    "conId": fill.contract.conId if fill.contract else None,
                }
        return None

    def check_fill_by_conid(self, con_id: int) -> dict | None:
        """Check recent fills for a specific conId. More precise than symbol search."""
        if not con_id:
            return None
        try:
            return self._submit_to_ib(self._ib_check_fill_by_conid, con_id, timeout=10)
        except Exception:
            return None

    def _ib_check_fill_by_conid(self, con_id: int) -> dict | None:
        """Search fills by exact conId match. Returns most recent SELL fill."""
        for fill in reversed(self.ib.fills()):
            if fill.contract and fill.contract.conId == con_id:
                if fill.execution.side == "SLD":  # Sell fill
                    return {
                        "symbol": (fill.contract.localSymbol or "").strip(),
                        "qty": float(fill.execution.shares),
                        "price": float(fill.execution.price),
                        "side": fill.execution.side,
                        "time": str(fill.execution.time),
                        "order_id": fill.execution.orderId,
                        "conId": con_id,
                    }
        return None

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
