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


class IBClient:
    def __init__(self):
        self.ib = IB()
        self._order_queue = queue.Queue()
        self._priority_queue = queue.Queue()  # Exit manager gets priority
        self._connected = False
        # Cache of validated contracts: occ_symbol → Option contract
        self._contract_cache = {}

    # ── Connection ────────────────────────────────────────────
    def connect(self):
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
        """Must be called in a loop on the MAIN thread.
        Priority queue (exit manager) always processed first."""
        # Process ALL priority items first (trade monitoring)
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
        # Then process one normal item (scanner requests)
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

    def _submit_to_ib(self, func, *args, timeout=30, priority=False):
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

    # ── ATM Option Symbol (IB option chain) ───────────────────
    def get_atm_call_symbol(self, ticker: str) -> str:
        return self._submit_to_ib(self._ib_get_atm_symbol, ticker, "C")

    def get_atm_put_symbol(self, ticker: str) -> str:
        return self._submit_to_ib(self._ib_get_atm_symbol, ticker, "P")

    def _ib_get_atm_symbol(self, ticker: str, option_type: str) -> str:
        contract = Stock(ticker, "SMART", "USD")
        self.ib.qualifyContracts(contract)
        ticker_data = self.ib.reqMktData(contract, "", False, False)
        self.ib.sleep(2)
        price = 0.0
        if ticker_data.bid and ticker_data.bid > 0 and ticker_data.ask and ticker_data.ask > 0:
            price = round((ticker_data.bid + ticker_data.ask) / 2, 2)
        elif ticker_data.last and ticker_data.last > 0:
            price = float(ticker_data.last)
        elif ticker_data.close and ticker_data.close > 0:
            price = float(ticker_data.close)
        self.ib.cancelMktData(contract)
        if price <= 0:
            raise ValueError(f"No IB price data for {ticker}")
        log.info(f"[{ticker}] IB price: ${price:.2f}")

        chains = self.ib.reqSecDefOptParams(ticker, "", contract.secType, contract.conId)
        if not chains:
            raise RuntimeError(f"No option chain found on IB for {ticker}")

        chain = None
        for c in chains:
            if c.exchange == "SMART":
                chain = c
                break
        if chain is None:
            chain = chains[0]

        today_str = date.today().strftime("%Y%m%d")
        expirations = sorted(chain.expirations)
        exp = None
        for e in expirations:
            if e >= today_str:
                exp = e
                break
        if exp is None:
            raise RuntimeError(f"No future expirations found on IB for {ticker}")

        exp_display = f"{exp[:4]}-{exp[4:6]}-{exp[6:8]}"
        if exp == today_str:
            log.info(f"[{ticker}] 0DTE expiration on IB: {exp_display}")
        else:
            log.info(f"[{ticker}] Nearest IB expiry: {exp_display}")

        strikes = sorted(chain.strikes)
        atm_strike = min(strikes, key=lambda s: abs(s - price))
        log.info(f"[{ticker}] ATM strike from IB chain: ${atm_strike} (price ${price:.2f})")

        right = "C" if option_type == "C" else "P"
        opt_contract = Option(ticker, exp, atm_strike, right, "SMART")
        qualified = self.ib.qualifyContracts(opt_contract)
        if not qualified:
            raise RuntimeError(f"Could not qualify IB option: {ticker} {exp} {atm_strike} {right}")

        # Cache the validated contract
        exp_short = exp[2:]
        strike_str = str(int(atm_strike * 1000)).zfill(8)
        occ_symbol = f"{ticker}{exp_short}{option_type}{strike_str}"
        self._contract_cache[occ_symbol] = qualified[0]

        log.info(f"[{ticker}] ATM {option_type} symbol: {occ_symbol} (strike ${atm_strike}, exp {exp_display}) ✓ validated")
        return occ_symbol

    # ── Contract Validation ───────────────────────────────────
    def validate_contract(self, occ_symbol: str) -> bool:
        """Validate an option contract exists on IB. Thread-safe."""
        try:
            return self._submit_to_ib(self._ib_validate_contract, occ_symbol)
        except Exception:
            return False

    def _ib_validate_contract(self, occ_symbol: str) -> bool:
        """Qualify contract on IB. Cache if valid."""
        if occ_symbol in self._contract_cache:
            return True
        try:
            contract = self._occ_to_contract(occ_symbol)
            if contract:
                self._contract_cache[occ_symbol] = contract
                log.info(f"[IB] Contract validated: {occ_symbol}")
                return True
        except Exception as e:
            log.warning(f"[IB] Contract validation failed for {occ_symbol}: {e}")
        return False

    def _occ_to_contract(self, occ_symbol: str) -> Option:
        # Return cached contract if available
        if occ_symbol in self._contract_cache:
            return self._contract_cache[occ_symbol]

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
        self._contract_cache[occ_symbol] = qualified[0]
        return qualified[0]

    # ── Option Price (IB real-time) ───────────────────────────
    def get_option_price(self, symbol: str, priority: bool = False) -> float:
        timeout = 10 if priority else 30  # faster timeout for monitoring
        return self._submit_to_ib(self._ib_get_option_price, symbol, priority=priority, timeout=timeout)

    def _ib_get_option_price(self, symbol: str) -> float:
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

        order = MarketOrder(action, contracts)
        if config.IB_ACCOUNT:
            order.account = config.IB_ACCOUNT
        trade = self.ib.placeOrder(contract, order)

        # ── Wait for fill (up to 15 seconds) ──────────────
        for _ in range(30):
            self.ib.sleep(0.5)
            if trade.orderStatus.status == "Filled":
                break

        fill_price = trade.orderStatus.avgFillPrice
        status = trade.orderStatus.status

        # ── Timeout hardening: check actual fill ──────────
        if status != "Filled" and status not in ("Cancelled", "Inactive"):
            # Order may still be working — check executions
            self.ib.sleep(2)
            if trade.orderStatus.status == "Filled":
                fill_price = trade.orderStatus.avgFillPrice
                status = "Filled"
            elif trade.fills:
                fill_price = trade.fills[0].execution.avgPrice
                status = "Filled"
            else:
                log.warning(f"[IB] Order {trade.order.orderId} not filled after 17s — status: {trade.orderStatus.status}")

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

        # Wait for parent fill
        for _ in range(30):
            self.ib.sleep(0.5)
            if parent_trade.orderStatus.status == "Filled":
                break

        fill_price = parent_trade.orderStatus.avgFillPrice
        status = parent_trade.orderStatus.status

        log.info(f"[IB] BRACKET {action}: {contracts}x {option_symbol} — "
                 f"parent={parent.orderId} status={status} fill=${fill_price:.2f} "
                 f"TP={tp_order.orderId}@${tp_price:.2f} SL={sl_order.orderId}@${sl_price:.2f}")

        return {
            "symbol": option_symbol,
            "contracts": contracts,
            "order_id": parent.orderId,
            "tp_order_id": tp_order.orderId,
            "sl_order_id": sl_order.orderId,
            "status": status,
            "fill_price": fill_price,
        }

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
        """Cancel TP and SL legs when bot closes a trade manually."""
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

    # ── IB Reconciliation ─────────────────────────────────────
    def get_ib_positions_raw(self) -> list:
        """Get raw IB positions for reconciliation. Thread-safe."""
        try:
            return self._submit_to_ib(self._ib_get_positions_raw)
        except Exception as e:
            log.warning(f"Reconciliation position fetch failed: {e}")
            return []

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

    # ── Check Recent Executions (for timeout hardening) ───────
    def check_recent_fills(self, symbol: str) -> dict | None:
        """Check if a symbol was recently filled on IB. Thread-safe."""
        try:
            return self._submit_to_ib(self._ib_check_fills, symbol)
        except Exception:
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
