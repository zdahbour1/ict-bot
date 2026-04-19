"""
IB Contract Operations — ATM symbol lookup, contract validation, OCC→contract conversion.

All functions run on the IB event loop thread (called via _submit_to_ib).
They take the IB instance and shared contract cache as parameters.

Extracted from ib_client.py as part of ARCH-003 refactoring.
"""
import logging
import re
from datetime import date

from ib_async import Stock, Option

import config

log = logging.getLogger(__name__)

# Exchanges to try when qualifying option contracts (in priority order)
OPTION_EXCHANGES = ["SMART", "AMEX", "CBOE", "PSE", "BATS", "ISE"]


def ib_get_atm_symbol(ib, ticker: str, option_type: str,
                       contract_cache: dict) -> str:
    """
    Find the ATM option symbol for a ticker. Runs on IB thread.

    1. Get stock price from IB
    2. Get option chain, prefer chain with 0DTE expiry
    3. Find ATM strike, try nearby strikes if ATM doesn't qualify
    4. Return OCC symbol string
    """
    # Get stock price
    contract = Stock(ticker, "SMART", "USD")
    qualified_stock = ib.qualifyContracts(contract)
    if not qualified_stock or not contract.conId:
        raise RuntimeError(f"Could not qualify stock {ticker} on IB — "
                           f"conId={getattr(contract, 'conId', 'N/A')}")

    ticker_data = ib.reqMktData(contract, "", False, False)
    ib.sleep(2)
    price = 0.0
    if ticker_data.bid and ticker_data.bid > 0 and ticker_data.ask and ticker_data.ask > 0:
        price = round((ticker_data.bid + ticker_data.ask) / 2, 2)
    elif ticker_data.last and ticker_data.last > 0:
        price = float(ticker_data.last)
    elif ticker_data.close and ticker_data.close > 0:
        price = float(ticker_data.close)
    ib.cancelMktData(contract)
    if price <= 0:
        raise ValueError(f"No IB price data for {ticker}")
    log.info(f"[{ticker}] IB price: ${price:.2f}")

    # Get option chain
    chains = ib.reqSecDefOptParams(ticker, "", contract.secType, contract.conId)
    if not chains:
        raise RuntimeError(f"No option chain found on IB for {ticker}")

    # Select best chain: prefer one with today's expiry (0DTE)
    today_str = date.today().strftime("%Y%m%d")
    log.info(f"[{ticker}] Found {len(chains)} option chains: "
             f"{[c.exchange for c in chains]}")

    chain = None
    for c in chains:
        if today_str in c.expirations:
            chain = c
            log.info(f"[{ticker}] Using chain from {c.exchange} (has 0DTE expiry)")
            break

    if chain is None:
        for c in chains:
            if c.exchange == "SMART":
                chain = c
                break
    if chain is None:
        chain = chains[0]
        log.warning(f"[{ticker}] No SMART chain found, using {chain.exchange}")

    # Find nearest expiry
    expirations = sorted(chain.expirations)
    exp = None
    for e in expirations:
        if e >= today_str:
            exp = e
            break
    if exp is None:
        raise RuntimeError(f"No future expirations found on IB for {ticker} "
                           f"(chain={chain.exchange}, expirations={expirations[:5]})")

    exp_display = f"{exp[:4]}-{exp[4:6]}-{exp[6:8]}"
    if exp == today_str:
        log.info(f"[{ticker}] 0DTE expiration on IB: {exp_display} (chain={chain.exchange})")
    else:
        log.info(f"[{ticker}] Nearest IB expiry: {exp_display} (chain={chain.exchange})")

    # Find ATM strike + qualify
    strikes = sorted(chain.strikes)
    atm_strike = min(strikes, key=lambda s: abs(s - price))
    log.info(f"[{ticker}] ATM strike from IB chain: ${atm_strike} (price ${price:.2f})")

    right = "C" if option_type == "C" else "P"
    candidates = sorted(strikes, key=lambda s: abs(s - price))[:7]
    log.info(f"[{ticker}] Candidate strikes: {[f'${s}' for s in candidates[:5]]}...")

    qualified = None
    opt_contract = None
    winning_strike = None

    for strike in candidates:
        for exchange in OPTION_EXCHANGES:
            opt_contract = Option(ticker, exp, strike, right, exchange)
            result = ib.qualifyContracts(opt_contract)
            if result and opt_contract.conId:
                # Check for Flex options — IB rejects these with code 201
                # Flex options have tradingClass different from the ticker symbol
                trading_class = getattr(opt_contract, 'tradingClass', '') or ''
                sec_type = getattr(opt_contract, 'secType', '') or ''
                if trading_class and trading_class != ticker and sec_type == 'FOP':
                    log.warning(f"[{ticker}] Skipping Flex option: ${strike} {right} on {exchange} "
                                f"(tradingClass={trading_class}, secType={sec_type})")
                    continue

                qualified = result
                winning_strike = strike
                if strike != atm_strike:
                    log.info(f"[{ticker}] ATM ${atm_strike} not available, using ${strike} "
                             f"on {exchange} (conId={opt_contract.conId})")
                else:
                    log.info(f"[{ticker}] Option qualified on {exchange}: "
                             f"{ticker} {exp} ${strike} {right} conId={opt_contract.conId}"
                             f" tradingClass={trading_class}")
                break
        if qualified:
            break

    if not qualified or not opt_contract or not opt_contract.conId:
        raise RuntimeError(f"Could not qualify IB option for {ticker} {exp} {right} "
                           f"near ${atm_strike} on any exchange "
                           f"(tried {len(candidates)} strikes × {len(OPTION_EXCHANGES)} exchanges)")

    atm_strike = winning_strike

    # Build OCC symbol and cache
    exp_short = exp[2:]
    strike_str = str(int(atm_strike * 1000)).zfill(8)
    occ_symbol = f"{ticker}{exp_short}{option_type}{strike_str}"
    contract_cache[occ_symbol] = qualified[0]

    log.info(f"[{ticker}] ATM {option_type} symbol: {occ_symbol} "
             f"(strike ${atm_strike}, exp {exp_display}) ✓ validated")
    return occ_symbol


def ib_validate_contract(ib, occ_symbol: str, contract_cache: dict) -> bool:
    """Qualify contract on IB. Cache if valid. Runs on IB thread."""
    if occ_symbol in contract_cache:
        return True
    try:
        contract = ib_occ_to_contract(ib, occ_symbol, contract_cache)
        if contract:
            contract_cache[occ_symbol] = contract
            log.info(f"[IB] Contract validated: {occ_symbol}")
            return True
    except Exception as e:
        log.warning(f"[IB] Contract validation failed for {occ_symbol}: {e}")
    return False


def ib_occ_to_contract(ib, occ_symbol: str, contract_cache: dict):
    """Convert OCC symbol to IB Option contract. Runs on IB thread."""
    # Fast path: cache hit
    if occ_symbol in contract_cache:
        cached = contract_cache[occ_symbol]
        if cached is not None and getattr(cached, 'conId', 0):
            return cached

    match = re.match(r'^([A-Z]+)(\d{6})([CP])(\d{8})$', occ_symbol)
    if not match:
        raise ValueError(f"Invalid OCC symbol: {occ_symbol}")
    ticker = match.group(1)
    exp_str = match.group(2)
    right = "C" if match.group(3) == "C" else "P"
    strike = int(match.group(4)) / 1000
    expiry = f"20{exp_str}"

    # Try SMART first, then fallback
    contract = Option(ticker, expiry, strike, right, "SMART")
    qualified = ib.qualifyContracts(contract)
    if qualified and qualified[0] and getattr(qualified[0], 'conId', 0):
        contract_cache[occ_symbol] = qualified[0]
        return qualified[0]

    for exchange in OPTION_EXCHANGES[1:]:
        contract = Option(ticker, expiry, strike, right, exchange)
        qualified = ib.qualifyContracts(contract)
        if qualified and qualified[0] and getattr(qualified[0], 'conId', 0):
            contract_cache[occ_symbol] = qualified[0]
            log.info(f"[IB] Contract {occ_symbol} qualified on {exchange} (SMART failed)")
            return qualified[0]

    raise RuntimeError(f"Could not qualify IB contract for {occ_symbol}")


# ─────────────────────────────────────────────────────────────
# Futures Options (FOP) support
# See docs/futures_options_implementation.md.
# ─────────────────────────────────────────────────────────────

# Per-instrument contract specs. Expiry intervals + multipliers are
# fixed by the exchange; strike intervals are the minimum strike step.
FOP_SPECS = {
    "MNQ": {"exchange": "GLOBEX", "multiplier": 2,    "strike_interval": 25,   "currency": "USD"},
    "NQ":  {"exchange": "GLOBEX", "multiplier": 20,   "strike_interval": 25,   "currency": "USD"},
    "MES": {"exchange": "GLOBEX", "multiplier": 5,    "strike_interval": 5,    "currency": "USD"},
    "ES":  {"exchange": "GLOBEX", "multiplier": 50,   "strike_interval": 5,    "currency": "USD"},
    "GC":  {"exchange": "NYMEX",  "multiplier": 100,  "strike_interval": 5,    "currency": "USD"},
    "MGC": {"exchange": "NYMEX",  "multiplier": 10,   "strike_interval": 5,    "currency": "USD"},
    "CL":  {"exchange": "NYMEX",  "multiplier": 1000, "strike_interval": 0.5,  "currency": "USD"},
    "MCL": {"exchange": "NYMEX",  "multiplier": 100,  "strike_interval": 0.5,  "currency": "USD"},
}


def get_fop_spec(underlying: str) -> dict | None:
    """Return the canonical contract spec for a futures-option underlying,
    or None if the symbol isn't known to this bot yet."""
    return FOP_SPECS.get(underlying.upper())


def ib_qualify_futures_option(
    ib,
    underlying: str,
    expiry: str,                # YYYYMMDD
    strike: float,
    right: str,                 # 'C' or 'P'
    contract_cache: dict,
    exchange: str | None = None,
):
    """Build + qualify a `FuturesOption` IB contract and cache it.

    Unlike equity options which route SMART, FOPs must specify the exact
    exchange (GLOBEX for ES/NQ families, NYMEX for energy + metals).
    If `exchange` is not provided we look it up from FOP_SPECS.

    Returns the qualified `FuturesOption` on success. Raises on failure.
    """
    from ib_async import FuturesOption

    spec = get_fop_spec(underlying)
    if spec is None and exchange is None:
        raise RuntimeError(
            f"No FOP_SPECS entry for {underlying} and no explicit exchange — "
            "add it to broker/ib_contracts.py FOP_SPECS"
        )
    resolved_exchange = exchange or spec["exchange"]
    currency = (spec or {}).get("currency", "USD")
    multiplier = (spec or {}).get("multiplier")

    cache_key = f"FOP:{underlying}:{expiry}:{right}:{strike}:{resolved_exchange}"
    if cache_key in contract_cache:
        return contract_cache[cache_key]

    contract = FuturesOption(
        symbol=underlying,
        lastTradeDateOrContractMonth=expiry,
        strike=strike,
        right=right,
        exchange=resolved_exchange,
        currency=currency,
        multiplier=str(multiplier) if multiplier else "",
    )

    qualified = ib.qualifyContracts(contract)
    if not qualified or not qualified[0] or not getattr(qualified[0], "conId", 0):
        raise RuntimeError(
            f"Could not qualify FOP {underlying} {expiry} {strike}{right} "
            f"on {resolved_exchange}"
        )

    result = qualified[0]
    contract_cache[cache_key] = result
    log.info(
        f"[IB] FOP {underlying} {expiry} {strike}{right} qualified on "
        f"{resolved_exchange} — conId={result.conId}, "
        f"multiplier={multiplier}"
    )
    return result
