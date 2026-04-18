"""
IB Market Data mixin — real-time prices, greeks, VIX, contract validation.

All methods dispatch onto the IB event loop thread via self._submit_to_ib().
Separated from ib_client.py for readability (ARCH-003 Phase 2).
"""
import logging

from ib_async import Stock

log = logging.getLogger(__name__)


class IBMarketDataMixin:
    """Read-only market data methods. Mixed into IBClient."""

    # ── Real-time Equity Price ────────────────────────────────
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

    # ── ATM Option Symbol (delegates to ib_contracts.py) ──────
    def get_atm_call_symbol(self, ticker: str) -> str:
        return self._submit_to_ib(self._ib_get_atm_symbol, ticker, "C")

    def get_atm_put_symbol(self, ticker: str) -> str:
        return self._submit_to_ib(self._ib_get_atm_symbol, ticker, "P")

    def _ib_get_atm_symbol(self, ticker: str, option_type: str) -> str:
        from broker.ib_contracts import ib_get_atm_symbol
        return ib_get_atm_symbol(self.ib, ticker, option_type, self._contract_cache)

    # ── Contract Validation ───────────────────────────────────
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

    # ── Option Price (single) ─────────────────────────────────
    def get_option_price(self, symbol: str, priority: bool = False) -> float:
        timeout = 10 if priority else 30
        return self._submit_to_ib(
            self._ib_get_option_price, symbol, priority=priority, timeout=timeout
        )

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

    # ── Option Price (batch — much faster than sequential) ────
    def get_option_prices_batch(self, symbols: list[str]) -> dict[str, float]:
        try:
            return self._submit_to_ib(
                self._ib_get_option_prices_batch, symbols, priority=True, timeout=20
            )
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
            except Exception:
                continue

        if not contracts:
            return {}

        tickers = []
        for c in contracts:
            tickers.append(self.ib.reqMktData(c, "", False, False))

        self.ib.sleep(2)

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

    # ── Option Greeks ─────────────────────────────────────────
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

    # ── VIX ───────────────────────────────────────────────────
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
