"""Probe IB for what market-data subscriptions we actually have.

IB's API doesn't expose a subscription list directly — you infer it by
trying to fetch data and checking whether the request succeeds, fails
with an entitlement error (code 10089, 10167, 10168), or returns
empty/delayed data (code 10197 means delayed, not live).

Run:
    python tools/probe_market_data.py

Exits 0 when done; prints a table summarizing each asset class probed.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from ib_async import IB, Stock, Option, Future, FuturesOption


# Subscription-related error codes worth surfacing distinctly.
ENTITLEMENT_CODES = {10089, 10167, 10168, 10197, 354}
DELAYED_CODES = {10167, 10197}


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("probe_market_data")


def _end_utc_str(when: datetime) -> str:
    """IB historical data end format (UTC dash notation, the currently-
    recommended format per IB 2025)."""
    return when.astimezone(timezone.utc).strftime("%Y%m%d-%H:%M:%S")


def _probe_contract(ib: IB, label: str, contract) -> dict:
    """Try to qualify + get historical 1m bar for a contract.

    Returns {ok, qualified, bars, err_codes, err_msgs}.
    """
    result = {
        "label": label,
        "ok": False,
        "qualified": False,
        "bar_count": 0,
        "err_codes": [],
        "err_msgs": [],
    }
    # Catch IB errors raised on this connection
    captured = []
    def _capture(reqId, code, msg, contract_=None):
        captured.append((code, msg))
    ib.errorEvent += _capture
    try:
        # Qualify
        qualified = ib.qualifyContracts(contract)
        if qualified and getattr(qualified[0], "conId", 0):
            result["qualified"] = True
        else:
            return result
        c = qualified[0]

        # Request 1 day of 5m bars ending 1 day ago (safely in the past).
        end_dt = datetime.now(timezone.utc) - timedelta(days=1)
        bars = ib.reqHistoricalData(
            c,
            endDateTime=_end_utc_str(end_dt),
            durationStr="1 D",
            barSizeSetting="5 mins",
            whatToShow="TRADES",
            useRTH=False,
            formatDate=2,
        )
        result["bar_count"] = len(bars or [])
        result["ok"] = result["bar_count"] > 0
    except Exception as e:
        result["err_msgs"].append(f"exception: {type(e).__name__}: {e}")
    finally:
        ib.errorEvent -= _capture

    # Dump captured errors (from IB callbacks) into the result.
    for code, msg in captured:
        result["err_codes"].append(code)
        # Truncate long messages for the report
        result["err_msgs"].append(f"{code}: {msg[:140]}")
    return result


def main() -> int:
    import os
    host = os.getenv("IB_HOST", "127.0.0.1")
    port = int(os.getenv("IB_PORT", "7497"))

    ib = IB()
    log.info(f"Connecting to IB at {host}:{port} (clientId=77, readonly)")
    ib.connect(host=host, port=port, clientId=77, readonly=True)
    accts = ib.managedAccounts()
    log.info(f"Connected. Accounts: {accts}")

    probes = [
        # label, contract factory
        ("Stock SPY (SMART)",
         Stock("SPY", "SMART", "USD")),
        ("Stock AAPL (NASDAQ)",
         Stock("AAPL", "SMART", "USD")),
        ("Equity option SPY 440 C (nearest-term-ish)",
         Option("SPY", "20260515", 440, "C", "SMART", "100", "USD")),
        ("Future MES Jun-2026 (CME)",
         Future("MES", "202606", "CME", currency="USD")),
        ("Future ES Jun-2026 (CME)",
         Future("ES", "202606", "CME", currency="USD")),
        ("Future MNQ Jun-2026 (CME)",
         Future("MNQ", "202606", "CME", currency="USD")),
        ("Future GC Jun-2026 (COMEX)",
         Future("GC", "202606", "COMEX", currency="USD")),
        ("Future CL Jun-2026 (NYMEX)",
         Future("CL", "202606", "NYMEX", currency="USD")),
        # Quarterly options on quarterly futures — most liquid (user tip).
        # Jun-2026 quarterly: 3rd Friday = 20260619.
        ("FOP MES 5400 C Jun-26 quarterly (CME)",
         FuturesOption("MES", "20260619", 5400, "C", "CME",
                        multiplier="5", currency="USD")),
        ("FOP ES 5400 C Jun-26 quarterly (CME)",
         FuturesOption("ES", "20260619", 5400, "C", "CME",
                        multiplier="50", currency="USD")),
    ]

    print("\n" + "=" * 100)
    print(f"{'ASSET CLASS / CONTRACT':<55}  {'QUALIFIED':<10}  {'BARS':<6}  {'STATUS'}")
    print("=" * 100)
    for label, contract in probes:
        log.info(f"--- probing: {label} ---")
        r = _probe_contract(ib, label, contract)
        status = (
            "OK (data flowing)" if r["ok"]
            else "qualified, no bars" if r["qualified"] and r["bar_count"] == 0
            else "contract not found / not entitled"
        )
        if any(c in ENTITLEMENT_CODES for c in r["err_codes"]):
            delayed = any(c in DELAYED_CODES for c in r["err_codes"])
            status = "DELAYED only" if delayed else "NOT ENTITLED (subscription missing)"

        print(f"{label:<55}  {'Y' if r['qualified'] else 'N':<10}  "
              f"{r['bar_count']:<6}  {status}")
        if r["err_msgs"]:
            for m in r["err_msgs"][:3]:
                print(f"    | {m}")
        time.sleep(0.3)
    print("=" * 100)
    print("\nLegend:")
    print("  QUALIFIED=Y  → IB recognizes the contract spec (doesn't mean data flows)")
    print("  BARS > 0     → historical-data subscription is ACTIVE for this asset class")
    print("  NOT ENTITLED → you can see the contract but not the data")
    print("  DELAYED      → realtime not subscribed; IB will only give 15-min-delayed")
    ib.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
