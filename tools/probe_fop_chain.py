"""Probe IB for what FOP strikes/expiries are actually available on MES
and ES. Uses reqContractDetails with an incomplete spec — IB returns
every matching contract it recognizes. If the list comes back with 0
entries, either the subscription is missing or the underlying futures
don't have listed options on this account.
"""
from __future__ import annotations
import logging
import os

from ib_async import IB, FuturesOption

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(name)s - %(message)s")
log = logging.getLogger("probe_fop_chain")

ib = IB()
ib.connect(host=os.getenv("IB_HOST", "127.0.0.1"),
           port=int(os.getenv("IB_PORT", "7497")),
           clientId=88, readonly=True)

# Incomplete spec: no strike, no right. IB returns the whole chain.
for symbol, exchange in [("MES", "CME"), ("ES", "CME"), ("NQ", "CME"),
                          ("GC", "COMEX"), ("CL", "NYMEX")]:
    print(f"\n=== {symbol} FOP chain (on {exchange}) ===")
    spec = FuturesOption(symbol=symbol, exchange=exchange, currency="USD")
    try:
        details = ib.reqContractDetails(spec)
    except Exception as e:
        print(f"  ERROR: {e}")
        continue
    if not details:
        print(f"  [empty] — either no subscription for {symbol} FOP, "
              f"or no listed options on this exchange")
        continue
    # Summarise unique expiries + strike ranges
    expiries = sorted({d.contract.lastTradeDateOrContractMonth for d in details})
    strikes = sorted({float(d.contract.strike) for d in details if d.contract.strike})
    print(f"  {len(details)} contracts, {len(expiries)} expiries, "
          f"{len(strikes)} strikes")
    print(f"  expiries (first 8): {expiries[:8]}")
    if strikes:
        print(f"  strike range: {min(strikes):.1f} .. {max(strikes):.1f}")

ib.disconnect()
