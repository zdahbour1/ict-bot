"""
Charles Schwab Broker Client
Handles authentication, option chain lookup, order placement for paperMoney.
Uses schwab-py library with OAuth2.

First run: opens browser for login → saves token to schwab_token.json
Subsequent runs: uses saved token (auto-refreshed).
"""
import logging
import os
from datetime import date, datetime

import schwab
from schwab.orders.options import (
    option_buy_to_open_market,
    option_sell_to_close_market,
    OptionSymbol
)

import config

log = logging.getLogger(__name__)

TOKEN_PATH = os.path.join(os.path.dirname(__file__), "..", "schwab_token.json")


class SchwabClient:
    def __init__(self):
        self.client       = None
        self.account_hash = None

    # ── Authentication ────────────────────────────────────
    def connect(self):
        """
        Authenticate with Schwab via OAuth2.
        First run: opens browser → you log in → token saved to schwab_token.json
        Subsequent runs: token loaded automatically from file.
        """
        log.info("Connecting to Schwab paperMoney...")
        try:
            self.client = schwab.auth.easy_client(
                api_key      = config.SCHWAB_APP_KEY,
                app_secret   = config.SCHWAB_APP_SECRET,
                callback_url = config.SCHWAB_CALLBACK_URL,
                token_path   = TOKEN_PATH,
            )
        except Exception as e:
            log.error(f"Schwab auth failed: {e}")
            raise

        # Get account hash for paper account
        resp     = self.client.get_accounts()
        accounts = resp.json()

        paper_account = None
        for acct in accounts:
            acct_data = acct.get("securitiesAccount", {})
            acct_type = acct_data.get("type", "")
            acct_num  = acct_data.get("accountNumber", "")
            # Paper accounts show as type "MARGIN" with paperMoney flag
            is_paper  = acct_data.get("isDayTrader", False) or "paper" in acct_num.lower()
            log.info(f"Found account: {acct_num} (type={acct_type})")
            if config.SCHWAB_PAPER_ACCOUNT and acct_num == config.SCHWAB_PAPER_ACCOUNT:
                paper_account = acct
                break

        if paper_account is None:
            # Fall back to first account
            paper_account = accounts[0]
            log.warning("No paper account matched — using first account.")

        self.account_hash = paper_account["hashValue"] if "hashValue" in paper_account \
            else paper_account.get("securitiesAccount", {}).get("accountNumber", "")

        log.info(f"Schwab connected — account hash: {self.account_hash}")

    # ── Option Symbol Builder ─────────────────────────────
    def _build_option_symbol(self, ticker: str, exp_date: date,
                              option_type: str, strike: float) -> str:
        """
        Build OCC option symbol for schwab-py.
        option_type: 'C' or 'P'
        """
        exp_dt = datetime(exp_date.year, exp_date.month, exp_date.day)
        symbol = OptionSymbol(
            ticker,
            exp_dt,
            option_type,
            str(int(strike))
        ).build()
        return symbol

    # ── ATM Option Selection ──────────────────────────────
    def get_atm_call_symbol(self, ticker: str) -> str:
        return self._get_atm_symbol(ticker, 'C')

    def get_atm_put_symbol(self, ticker: str) -> str:
        return self._get_atm_symbol(ticker, 'P')

    def _get_atm_symbol(self, ticker: str, option_type: str) -> str:
        """Get ATM 0DTE option symbol from Schwab option chain."""
        today     = date.today()
        today_str = today.strftime('%Y-%m-%d')

        # Get current price
        current_price = self.get_realtime_equity_price(ticker)
        log.info(f"{ticker} current price: ${current_price:.2f}")

        # Get option chain
        resp  = self.client.get_option_chain(
            ticker,
            contract_type = schwab.client.Client.Options.ContractType.CALL
                            if option_type == 'C'
                            else schwab.client.Client.Options.ContractType.PUT,
            expiration_date = today,
            strike_count    = 10,
        )
        chain = resp.json()

        exp_map = chain.get("callExpDateMap" if option_type == 'C' else "putExpDateMap", {})
        if not exp_map:
            raise RuntimeError(f"No option chain returned for {ticker}")

        # Find today's expiration
        today_key = None
        for key in exp_map:
            if today_str in key:
                today_key = key
                break

        if today_key is None:
            raise RuntimeError(f"No 0DTE expiration found for {ticker} on {today_str}")

        strikes = exp_map[today_key]

        # Find ATM strike (closest to current price)
        best_strike = None
        best_dist   = float("inf")
        for strike_str, contracts in strikes.items():
            strike_val = float(strike_str)
            dist       = abs(strike_val - current_price)
            if dist < best_dist:
                best_dist   = dist
                best_strike = strike_val
                symbol      = contracts[0].get("symbol", "")

        log.info(f"ATM {option_type} selected: {symbol} (strike ${best_strike:.2f})")
        return symbol

    # ── Real-time Pricing ─────────────────────────────────
    def get_realtime_equity_price(self, ticker: str) -> float:
        """Get real-time stock/ETF price from Schwab."""
        try:
            resp  = self.client.get_quote(ticker)
            data  = resp.json()
            quote = data.get(ticker, {}).get("quote", {})
            bid   = float(quote.get("bidPrice", 0))
            ask   = float(quote.get("askPrice", 0))
            mid   = round((bid + ask) / 2, 2) if bid and ask else float(quote.get("lastPrice", 0))
            log.info(f"[SCHWAB REALTIME] {ticker}: ${mid:.2f}")
            return mid
        except Exception as e:
            log.warning(f"Schwab equity price failed ({e}) — falling back to yfinance")
            return self._get_equity_price_yf(ticker)

    def get_option_price(self, symbol: str) -> float:
        """Get real-time option mid price from Schwab."""
        try:
            resp  = self.client.get_quote(symbol)
            data  = resp.json()
            quote = data.get(symbol, {}).get("quote", {})
            bid   = float(quote.get("bidPrice", 0))
            ask   = float(quote.get("askPrice", 0))
            mid   = round((bid + ask) / 2, 2) if bid and ask else float(quote.get("lastPrice", 0))
            log.info(f"[SCHWAB REALTIME] {symbol}: bid={bid:.2f} ask={ask:.2f} mid={mid:.2f}")
            return mid
        except Exception as e:
            log.warning(f"Schwab option price failed ({e}) — falling back to yfinance")
            return self._get_option_price_yf(symbol)

    # ── Fallbacks (yfinance) ──────────────────────────────
    def _get_equity_price_yf(self, ticker: str) -> float:
        import yfinance as yf
        data  = yf.Ticker(ticker).fast_info
        price = float(data["lastPrice"])
        log.info(f"{ticker} price (yfinance fallback): ${price:.2f}")
        return price

    def _get_option_price_yf(self, symbol: str) -> float:
        import yfinance as yf
        try:
            i = 0
            while i < len(symbol) and symbol[i].isalpha():
                i += 1
            ticker   = symbol[:i].strip()
            exp_str  = symbol[i:i+6]
            opt_type = symbol[i+6]
            strike   = int(symbol[i+7:]) / 1000
            exp_date = f"20{exp_str[:2]}-{exp_str[2:4]}-{exp_str[4:6]}"

            chain = yf.Ticker(ticker).option_chain(exp_date)
            df    = chain.calls if opt_type == 'C' else chain.puts
            row   = df[df['contractSymbol'] == symbol]
            if row.empty:
                df['dist'] = abs(df['strike'] - strike)
                row = df.loc[[df['dist'].idxmin()]]
            bid = float(row['bid'].values[0])
            ask = float(row['ask'].values[0])
            return round((bid + ask) / 2, 2)
        except Exception as e:
            log.warning(f"yfinance option fallback failed: {e}")
            return 1.00

    # ── Order Placement ───────────────────────────────────
    def buy_call(self, option_symbol: str, contracts: int) -> dict:
        """Buy to open a call option on paper account."""
        if config.DRY_RUN:
            log.info(f"[DRY RUN] SCHWAB BUY CALL: {contracts}x {option_symbol}")
            return {"dry_run": True, "symbol": option_symbol}
        try:
            order = option_buy_to_open_market(option_symbol, contracts)
            resp  = self.client.place_order(self.account_hash, order)
            log.info(f"[SCHWAB] BUY CALL placed: {contracts}x {option_symbol}")
            return {"symbol": option_symbol, "contracts": contracts, "order": resp.json()}
        except Exception as e:
            log.error(f"Schwab BUY CALL failed: {e}")
            raise

    def buy_put(self, option_symbol: str, contracts: int) -> dict:
        """Buy to open a put option on paper account."""
        if config.DRY_RUN:
            log.info(f"[DRY RUN] SCHWAB BUY PUT: {contracts}x {option_symbol}")
            return {"dry_run": True, "symbol": option_symbol}
        try:
            order = option_buy_to_open_market(option_symbol, contracts)
            resp  = self.client.place_order(self.account_hash, order)
            log.info(f"[SCHWAB] BUY PUT placed: {contracts}x {option_symbol}")
            return {"symbol": option_symbol, "contracts": contracts, "order": resp.json()}
        except Exception as e:
            log.error(f"Schwab BUY PUT failed: {e}")
            raise

    def sell_call(self, option_symbol: str, contracts: int) -> dict:
        """Sell to close a call option on paper account."""
        if config.DRY_RUN:
            log.info(f"[DRY RUN] SCHWAB SELL CALL: {contracts}x {option_symbol}")
            return {"dry_run": True}
        try:
            order = option_sell_to_close_market(option_symbol, contracts)
            resp  = self.client.place_order(self.account_hash, order)
            log.info(f"[SCHWAB] SELL CALL placed: {contracts}x {option_symbol}")
            return resp.json()
        except Exception as e:
            log.error(f"Schwab SELL CALL failed: {e}")
            raise

    def sell_put(self, option_symbol: str, contracts: int) -> dict:
        """Sell to close a put option on paper account."""
        if config.DRY_RUN:
            log.info(f"[DRY RUN] SCHWAB SELL PUT: {contracts}x {option_symbol}")
            return {"dry_run": True}
        try:
            order = option_sell_to_close_market(option_symbol, contracts)
            resp  = self.client.place_order(self.account_hash, order)
            log.info(f"[SCHWAB] SELL PUT placed: {contracts}x {option_symbol}")
            return resp.json()
        except Exception as e:
            log.error(f"Schwab SELL PUT failed: {e}")
            raise

    def get_open_positions(self) -> list:
        """Return open positions from paper account."""
        try:
            resp      = self.client.get_account(
                self.account_hash,
                fields=[schwab.client.Client.Account.Fields.POSITIONS]
            )
            data      = resp.json()
            positions = data.get("securitiesAccount", {}).get("positions", [])
            return positions
        except Exception as e:
            log.warning(f"Could not fetch Schwab positions: {e}")
            return []
