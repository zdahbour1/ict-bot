"""
Alpaca Paper Trading Broker Client
Connects to Alpaca's paper trading API to place and track option trades.
No OAuth needed — just API key + secret.
"""
import logging
from datetime import date, datetime

import config

log = logging.getLogger(__name__)

PAPER_BASE_URL = "https://paper-api.alpaca.markets"


class AlpacaClient:
    def __init__(self):
        self.trading_client = None
        self.data_client    = None

    # ── Authentication ─────────────────────────────────────
    def connect(self):
        from alpaca.trading.client import TradingClient
        from alpaca.data.historical import StockHistoricalDataClient

        self.trading_client = TradingClient(
            api_key    = config.ALPACA_API_KEY,
            secret_key = config.ALPACA_SECRET_KEY,
            paper      = True,
        )
        self.data_client = StockHistoricalDataClient(
            api_key    = config.ALPACA_API_KEY,
            secret_key = config.ALPACA_SECRET_KEY,
        )

        account = self.trading_client.get_account()
        log.info(f"Alpaca paper trading connected — account: {account.id}")
        log.info(f"  Buying power: ${float(account.buying_power):,.2f}")
        log.info(f"  Portfolio value: ${float(account.portfolio_value):,.2f}")

    # ── Real-time Pricing ──────────────────────────────────
    def get_realtime_equity_price(self, ticker: str) -> float:
        try:
            from alpaca.data.requests import StockLatestQuoteRequest
            req   = StockLatestQuoteRequest(symbol_or_symbols=ticker)
            quote = self.data_client.get_stock_latest_quote(req)[ticker]
            mid   = round((float(quote.bid_price) + float(quote.ask_price)) / 2, 2)
            log.info(f"[ALPACA] {ticker}: ${mid:.2f}")
            return mid
        except Exception as e:
            log.warning(f"Alpaca price failed ({e}) — falling back to yfinance")
            return self._get_equity_price_yf(ticker)

    def _get_equity_price_yf(self, ticker: str) -> float:
        import yfinance as yf
        price = float(yf.Ticker(ticker).fast_info["lastPrice"])
        log.info(f"{ticker} price (yfinance fallback): ${price:.2f}")
        return price

    # ── ATM Option Symbol ──────────────────────────────────
    def get_atm_call_symbol(self, ticker: str) -> str:
        return self._get_atm_symbol(ticker, "C")

    def get_atm_put_symbol(self, ticker: str) -> str:
        return self._get_atm_symbol(ticker, "P")

    def _get_atm_symbol(self, ticker: str, option_type: str) -> str:
        """
        Build an OCC option symbol for the ATM 0DTE contract.
        Alpaca options use the standard OCC format: QQQ250329C00480000
        """
        price  = self.get_realtime_equity_price(ticker)
        strike = round(price)  # nearest whole dollar ATM strike
        today  = date.today()

        # OCC format: TICKER + YYMMDD + C/P + 8-digit strike (price * 1000, zero-padded)
        exp    = today.strftime("%y%m%d")
        strike_str = str(int(strike * 1000)).zfill(8)
        symbol = f"{ticker}{exp}{option_type}{strike_str}"
        log.info(f"ATM {option_type} symbol: {symbol} (strike ${strike})")
        return symbol

    # ── Option Price ───────────────────────────────────────
    def get_option_price(self, symbol: str) -> float:
        try:
            from alpaca.data.historical.option import OptionHistoricalDataClient
            from alpaca.data.requests import OptionLatestQuoteRequest
            opt_client = OptionHistoricalDataClient(
                api_key    = config.ALPACA_API_KEY,
                secret_key = config.ALPACA_SECRET_KEY,
            )
            req   = OptionLatestQuoteRequest(symbol_or_symbols=symbol)
            quote = opt_client.get_option_latest_quote(req)[symbol]
            bid   = float(quote.bid_price)
            ask   = float(quote.ask_price)
            mid   = round((bid + ask) / 2, 2)
            log.info(f"[ALPACA] {symbol}: bid={bid:.2f} ask={ask:.2f} mid={mid:.2f}")
            return mid
        except Exception as e:
            log.warning(f"Alpaca option price failed ({e}) — returning 1.00")
            return 1.00

    # ── Order Placement ────────────────────────────────────
    def buy_call(self, option_symbol: str, contracts: int) -> dict:
        return self._place_order(option_symbol, contracts, "buy")

    def buy_put(self, option_symbol: str, contracts: int) -> dict:
        return self._place_order(option_symbol, contracts, "buy")

    def sell_call(self, option_symbol: str, contracts: int) -> dict:
        return self._place_order(option_symbol, contracts, "sell")

    def sell_put(self, option_symbol: str, contracts: int) -> dict:
        return self._place_order(option_symbol, contracts, "sell")

    def _place_order(self, symbol: str, contracts: int, side: str) -> dict:
        if config.DRY_RUN:
            log.info(f"[DRY RUN] ALPACA {side.upper()} {contracts}x {symbol}")
            return {"dry_run": True, "symbol": symbol}
        try:
            from alpaca.trading.requests import MarketOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass

            order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
            req = MarketOrderRequest(
                symbol        = symbol,
                qty           = contracts,
                side          = order_side,
                time_in_force = TimeInForce.DAY,
            )
            order = self.trading_client.submit_order(req)
            log.info(f"[ALPACA] {side.upper()} order placed: {contracts}x {symbol} — ID: {order.id}")
            return {"symbol": symbol, "contracts": contracts, "order_id": str(order.id)}
        except Exception as e:
            log.error(f"Alpaca order failed: {e}")
            raise

    # ── Positions ──────────────────────────────────────────
    def get_open_positions(self) -> list:
        try:
            positions = self.trading_client.get_all_positions()
            return [
                {
                    "symbol": p.symbol,
                    "qty":    float(p.qty),
                    "pnl":    float(p.unrealized_pl),
                }
                for p in positions
            ]
        except Exception as e:
            log.warning(f"Could not fetch Alpaca positions: {e}")
            return []
