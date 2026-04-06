"""
Interactive Brokers Paper Trading Broker Client
Connects to IB TWS or IB Gateway via ib_insync for option trading.
Requires TWS/Gateway running with API connections enabled.
Default paper trading port: 7497 (TWS) or 4002 (Gateway).
"""
import logging
import re
from datetime import date, datetime

from ib_async import IB, Stock, Option, MarketOrder, Contract

import config

log = logging.getLogger(__name__)


class IBClient:
    def __init__(self):
        self.ib = IB()

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

    # ── Real-time Equity Price ────────────────────────────────
    def get_realtime_equity_price(self, ticker: str) -> float:
        """Get real-time mid price for a stock/ETF via IB market data."""
        try:
            contract = Stock(ticker, "SMART", "USD")
            self.ib.qualifyContracts(contract)
            ticker_data = self.ib.reqMktData(contract, "", False, False)
            self.ib.sleep(2)  # allow data to arrive
            bid = ticker_data.bid if ticker_data.bid > 0 else 0.0
            ask = ticker_data.ask if ticker_data.ask > 0 else 0.0
            if bid > 0 and ask > 0:
                mid = round((bid + ask) / 2, 2)
                log.info(f"[IB] {ticker}: bid={bid:.2f} ask={ask:.2f} mid={mid:.2f}")
                self.ib.cancelMktData(contract)
                return mid
            # If no live data, try last price
            if ticker_data.last > 0:
                log.info(f"[IB] {ticker}: last={ticker_data.last:.2f} (no bid/ask)")
                self.ib.cancelMktData(contract)
                return float(ticker_data.last)
            self.ib.cancelMktData(contract)
            raise ValueError("No price data received")
        except Exception as e:
            log.warning(f"IB equity price failed ({e}) — falling back to yfinance")
            return self._get_equity_price_yf(ticker)

    def _get_equity_price_yf(self, ticker: str) -> float:
        """Fallback: get stock price via yfinance."""
        import yfinance as yf
        price = float(yf.Ticker(ticker).fast_info["lastPrice"])
        log.info(f"{ticker} price (yfinance fallback): ${price:.2f}")
        return price

    # ── ATM Option Symbol ─────────────────────────────────────
    def get_atm_call_symbol(self, ticker: str) -> str:
        return self._get_atm_symbol(ticker, "C")

    def get_atm_put_symbol(self, ticker: str) -> str:
        return self._get_atm_symbol(ticker, "P")

    def _get_atm_symbol(self, ticker: str, option_type: str) -> str:
        """
        Build an OCC option symbol for the ATM 0DTE contract.
        Uses yfinance for chain lookup (consistent with other clients).
        Format: QQQ250402C00480000
        """
        price = self.get_realtime_equity_price(ticker)
        strike = round(price)
        today = date.today()
        exp = today.strftime("%y%m%d")
        strike_str = str(int(strike * 1000)).zfill(8)
        symbol = f"{ticker}{exp}{option_type}{strike_str}"
        log.info(f"ATM {option_type} symbol: {symbol} (strike ${strike})")
        return symbol

    # ── OCC Symbol → IB Contract ──────────────────────────────
    def _occ_to_contract(self, occ_symbol: str) -> Option:
        """
        Parse an OCC option symbol (e.g. QQQ250402C00480000) and return
        an ib_insync Option contract.
        """
        match = re.match(r'^([A-Z]+)(\d{6})([CP])(\d{8})$', occ_symbol)
        if not match:
            raise ValueError(f"Invalid OCC symbol: {occ_symbol}")
        ticker = match.group(1)
        exp_str = match.group(2)    # YYMMDD
        right = "C" if match.group(3) == "C" else "P"
        strike = int(match.group(4)) / 1000

        expiry = f"20{exp_str}"  # YYYYMMDD
        contract = Option(ticker, expiry, strike, right, "SMART")
        qualified = self.ib.qualifyContracts(contract)
        if not qualified:
            raise RuntimeError(f"Could not qualify IB contract for {occ_symbol}")
        return qualified[0]

    # ── Option Price ──────────────────────────────────────────
    def get_option_price(self, symbol: str) -> float:
        """Get real-time mid price for an option via IB market data."""
        try:
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
        except Exception as e:
            log.warning(f"IB option price failed ({e}) — falling back to yfinance")
            return self._get_option_price_yf(symbol)

    def _get_option_price_yf(self, symbol: str) -> float:
        """Fallback: get option mid price via yfinance."""
        import yfinance as yf
        try:
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
            log.info(f"Option price (yfinance fallback) {symbol}: mid={mid:.2f}")
            return mid
        except Exception as e:
            log.warning(f"yfinance option price failed ({e}), returning 1.00 fallback")
            return 1.00

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
        try:
            contract = self._occ_to_contract(option_symbol)
            order = MarketOrder(action, contracts)
            if config.IB_ACCOUNT:
                order.account = config.IB_ACCOUNT
            trade = self.ib.placeOrder(contract, order)
            self.ib.sleep(1)  # allow order to be acknowledged
            log.info(f"[IB] {action} {desc.upper()}: {contracts}x {option_symbol} — "
                     f"orderId={trade.order.orderId} status={trade.orderStatus.status}")
            return {
                "symbol": option_symbol,
                "contracts": contracts,
                "order_id": trade.order.orderId,
                "status": trade.orderStatus.status,
            }
        except Exception as e:
            log.error(f"IB order failed: {e}")
            raise

    # ── Positions ─────────────────────────────────────────────
    def get_open_positions(self) -> list:
        """Return list of open option positions."""
        try:
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
        except Exception as e:
            log.warning(f"Could not fetch IB positions: {e}")
            return []
