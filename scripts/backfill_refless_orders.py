"""ENH-045 — Back-fill ``orderRef`` on legacy working IB orders.

Context: orders placed before the orderRef-stamping era have no
TICKER-YYMMDD-NN tag in IB, so the Activity tab shows them with a
blank "Order Ref" column. They're operationally fine — cancellation,
fills, brackets all work — but the lack of provenance makes manual
triage harder.

This script walks every working order on IB, tries to match it back
to an open DB ``trade_legs`` row via ``ib_order_id``, and if it finds
a match AND the order currently has a blank ``orderRef``, issues a
``modifyOrder`` to stamp the trade's ``client_trade_id`` on it.

Dry-run by default. Use ``--apply`` to actually modify orders.

Run from the repo root::

    python scripts/backfill_refless_orders.py           # dry-run
    python scripts/backfill_refless_orders.py --apply   # write refs
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("refless-backfill")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Actually modify orders. Default: dry-run.")
    ap.add_argument("--client-id", type=int, default=98)
    ap.add_argument("--host", default=config.IB_HOST)
    ap.add_argument("--port", type=int, default=config.IB_PORT)
    args = ap.parse_args()

    log.info(f"Connecting to IB @ {args.host}:{args.port} clientId={args.client_id}")
    from ib_async import IB
    ib = IB()
    ib.connect(args.host, args.port, clientId=args.client_id,
                readonly=False, timeout=8)
    try:
        ib.reqAllOpenOrders()
        ib.sleep(1.0)
    except Exception as e:
        log.warning(f"reqAllOpenOrders failed: {e}")

    # Build orderId -> client_trade_id map from DB open legs.
    from sqlalchemy import text
    from db.connection import get_session
    session = get_session()
    if session is None:
        log.error("DB session unavailable")
        ib.disconnect()
        return 1
    try:
        rows = session.execute(text(
            "SELECT l.ib_order_id, l.ib_tp_order_id, l.ib_sl_order_id, "
            "       t.client_trade_id "
            "FROM trades t JOIN trade_legs l ON l.trade_id = t.id "
            "WHERE t.status='open' AND l.leg_status='open' "
            "  AND t.client_trade_id IS NOT NULL"
        )).fetchall()
    finally:
        session.close()
    oid_to_ref: dict[int, str] = {}
    for entry_oid, tp_oid, sl_oid, ref in rows:
        for oid in (entry_oid, tp_oid, sl_oid):
            if oid and ref:
                oid_to_ref[int(oid)] = ref
    log.info(f"DB has {len(oid_to_ref)} orderId → client_trade_id mappings")

    ACTIVE = {"Submitted", "PreSubmitted", "PendingSubmit"}
    candidates = []
    for t in ib.openTrades():
        if t.orderStatus.status not in ACTIVE:
            continue
        current_ref = (t.order.orderRef or "").strip()
        if current_ref:
            continue       # already tagged
        oid = int(t.order.orderId or 0)
        target_ref = oid_to_ref.get(oid)
        if not target_ref:
            continue
        candidates.append((t, target_ref))

    log.info(f"Found {len(candidates)} ref-less orders matchable to "
             f"known client_trade_ids")
    for t, ref in candidates:
        log.info(f"  orderId={t.order.orderId} {t.order.action} "
                 f"{getattr(t.contract, 'localSymbol', '?')} → {ref}")

    if not args.apply:
        log.warning("Dry-run. Re-run with --apply to modify orders.")
        ib.disconnect()
        return 0

    applied = 0
    for t, ref in candidates:
        try:
            t.order.orderRef = ref
            ib.placeOrder(t.contract, t.order)    # modifyOrder via re-place
            log.info(f"  APPLIED orderId={t.order.orderId} ref={ref}")
            applied += 1
        except Exception as e:
            log.error(f"  FAILED orderId={t.order.orderId}: {e}")
    ib.sleep(2.0)
    log.info(f"Done. Applied {applied}/{len(candidates)} ref stamps.")
    ib.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
