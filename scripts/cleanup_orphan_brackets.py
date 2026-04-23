"""One-shot cleanup for orphan bracket orders on IB.

Scenario this addresses (2026-04-23):
After the DN OCA bug + phantom-ICT adoption bug, IB ended up with
multiple SL bracket orders on the same contract — some legitimate
(attached to an open DN leg) and some orphaned (attached to closed
phantom trades). Dashboard stays noisy with stale brackets in
"Transmit" status until they naturally time out.

Strategy: connect to IB as a fresh clientId (doesn't conflict with
the running bot pool at 1–4), list every working order, compare
against the authoritative set of ``ib_tp_order_id`` +
``ib_sl_order_id`` values on OPEN ``trade_legs`` rows, cancel
anything not referenced.

**Only cancels SELL option orders** (brackets / manual close orders).
Entry-side BUY orders are never cancelled by this script to avoid
ripping a fresh entry mid-fill.

Run from the repo root::

    python scripts/cleanup_orphan_brackets.py           # dry-run
    python scripts/cleanup_orphan_brackets.py --cancel  # actually cancel
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

# Make the project importable when run from scripts/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("orphan-cleanup")


def _tracked_bracket_order_ids() -> set[int]:
    """Set of every ``ib_tp_order_id`` / ``ib_sl_order_id`` on open legs."""
    from sqlalchemy import text
    from db.connection import get_session
    session = get_session()
    if session is None:
        raise RuntimeError("DB session unavailable")
    try:
        rows = session.execute(text(
            """
            SELECT l.ib_tp_order_id, l.ib_sl_order_id
              FROM trades t
              JOIN trade_legs l ON l.trade_id = t.id
             WHERE t.status = 'open'
               AND l.leg_status = 'open'
               AND l.contracts_open > 0
            """
        )).fetchall()
    finally:
        session.close()
    ids: set[int] = set()
    for tp, sl in rows:
        if tp:
            ids.add(int(tp))
        if sl:
            ids.add(int(sl))
    return ids


def _tracked_entry_order_ids() -> set[int]:
    """Set of entry-side ``ib_order_id`` values. We never cancel these
    even if they're working — they're fresh fills."""
    from sqlalchemy import text
    from db.connection import get_session
    session = get_session()
    if session is None:
        return set()
    try:
        rows = session.execute(text(
            "SELECT ib_order_id FROM trade_legs "
            "WHERE ib_order_id IS NOT NULL "
            "  AND entry_time > NOW() - INTERVAL '1 hour'"
        )).fetchall()
    finally:
        session.close()
    return {int(r[0]) for r in rows if r[0]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cancel", action="store_true",
                    help="Actually cancel the orphans. Default: dry-run.")
    ap.add_argument("--aggressive", action="store_true",
                    help="Cancel EVERY working SELL option order, not just "
                         "those unreferenced by open DB legs. Use for a "
                         "full reset when IB is noisy with duplicate brackets.")
    ap.add_argument("--flat-close", action="store_true",
                    help="After cancelling, also buy-to-close every "
                         "remaining option position to flatten the account.")
    ap.add_argument("--client-id", type=int, default=99,
                    help="IB clientId for this session (default 99).")
    ap.add_argument("--host", default=config.IB_HOST)
    ap.add_argument("--port", type=int, default=config.IB_PORT)
    args = ap.parse_args()

    log.info(f"Connecting to IB @ {args.host}:{args.port} clientId={args.client_id}")
    from ib_async import IB
    ib = IB()
    ib.connect(args.host, args.port, clientId=args.client_id, readonly=False,
                timeout=8)
    log.info("Connected.")
    # Fetch orders across ALL clientIds — the bot pool (1-4) placed
    # these brackets; our fresh clientId=99 wouldn't see them via the
    # default openTrades() which only returns this-session orders.
    try:
        ib.reqAllOpenOrders()
        ib.sleep(1.0)
    except Exception as e:
        log.warning(f"reqAllOpenOrders failed: {e}")

    try:
        tracked_brackets = _tracked_bracket_order_ids()
        tracked_entries = _tracked_entry_order_ids()
        log.info(f"DB tracks {len(tracked_brackets)} bracket orderIds + "
                 f"{len(tracked_entries)} recent entry orderIds")

        ACTIVE = {"Submitted", "PreSubmitted", "PendingSubmit"}

        open_trades = ib.openTrades()
        log.info(f"IB shows {len(open_trades)} open trades (all clientIds).")

        orphans: list = []
        safe_skip: list = []
        for t in open_trades:
            order = t.order
            status = t.orderStatus.status
            order_id = order.orderId
            action = order.action
            symbol = getattr(t.contract, "localSymbol", None) or \
                     getattr(t.contract, "symbol", "?")
            sec_type = getattr(t.contract, "secType", "?")
            perm_id = order.permId

            if status not in ACTIVE:
                continue

            # Only ever consider SELL orders on options — those are
            # brackets (STP SL, LMT TP) or manual closes.
            if action != "SELL":
                safe_skip.append((order_id, status, action, symbol, "not-SELL"))
                continue
            if sec_type not in ("OPT", "FOP"):
                safe_skip.append((order_id, status, action, symbol, "not-OPT"))
                continue

            if not args.aggressive:
                if order_id in tracked_brackets:
                    safe_skip.append((order_id, status, action, symbol, "tracked"))
                    continue
                if order_id in tracked_entries:
                    safe_skip.append((order_id, status, action, symbol, "recent-entry"))
                    continue

            orphans.append({
                "order_id": order_id, "perm_id": perm_id, "status": status,
                "action": action, "symbol": symbol, "sec_type": sec_type,
                "order_type": getattr(order, "orderType", "?"),
                "ib_trade": t,
            })

        log.info(f"Found {len(orphans)} orphan bracket orders "
                 f"({len(safe_skip)} legitimate — skipped)")
        for o in orphans:
            log.info(f"  ORPHAN: orderId={o['order_id']} permId={o['perm_id']} "
                     f"{o['order_type']} {o['action']} {o['symbol']} "
                     f"status={o['status']}")

        if not orphans:
            log.info("Nothing to cancel — IB state is clean.")
            return 0

        if not args.cancel:
            log.warning("Dry-run only. Re-run with --cancel to actually "
                        "cancel these orders.")
            return 0

        # Cancel them
        cancelled = 0
        for o in orphans:
            try:
                ib.cancelOrder(o["ib_trade"].order)
                log.info(f"  CANCELLED orderId={o['order_id']} {o['symbol']}")
                cancelled += 1
            except Exception as e:
                log.error(f"  FAILED to cancel orderId={o['order_id']}: {e}")

        # Give IB a moment to process the cancels
        ib.sleep(2.0)
        log.info(f"Done. Cancelled {cancelled}/{len(orphans)} orphan brackets.")

        # ── Optional flat-close pass ─────────────────────────
        if args.flat_close:
            log.info("--flat-close requested. Buying-to-close every open "
                     "option position...")
            ib.sleep(1.0)
            positions = ib.positions()
            closed = 0
            from ib_async import MarketOrder
            for p in positions:
                c = p.contract
                qty = int(p.position or 0)
                if qty == 0:
                    continue
                if getattr(c, "secType", "") not in ("OPT", "FOP"):
                    continue
                # Short position (qty<0) → BUY to close; long (qty>0) → SELL.
                flat_action = "BUY" if qty < 0 else "SELL"
                flat_qty = abs(qty)
                try:
                    order = MarketOrder(flat_action, flat_qty)
                    order.orderRef = "flat-close-orphan-cleanup"
                    ib.placeOrder(c, order)
                    log.info(f"  FLAT {flat_action} {flat_qty}x "
                             f"{c.localSymbol} conId={c.conId}")
                    closed += 1
                except Exception as e:
                    log.error(f"  FLAT failed for {c.localSymbol}: {e}")
            ib.sleep(3.0)
            log.info(f"Flat-close sent {closed} close orders.")

    finally:
        ib.disconnect()
        log.info("Disconnected.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
