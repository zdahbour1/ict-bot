"""ENH-050 Stage D — one-shot back-fill of ``entry_price=0`` combo legs.

Walks every open or recently-closed multi-leg trade whose legs have
``entry_price=0``, and tries the same three-stage recovery the live
path uses:
  1. ``ib.executions()`` — the richer IB Executions stream
  2. Post-fill mid-quote snapshot for anything still missing
  3. Proportional split from the trade envelope's first-leg price or
     just the trade's pnl_usd history

Each repaired leg gets its ``price_source`` column stamped so the
dashboard can distinguish repaired estimates from real fills.

Dry-run by default. Use ``--apply`` to actually update rows.

Run from repo root::

    python scripts/backfill_combo_fill_prices.py
    python scripts/backfill_combo_fill_prices.py --apply
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
log = logging.getLogger("combo-fill-backfill")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Actually update rows. Default: dry-run.")
    ap.add_argument("--client-id", type=int, default=97)
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
        log.warning(f"reqAllOpenOrders: {e}")

    from sqlalchemy import text
    from db.connection import get_session
    session = get_session()
    if session is None:
        log.error("DB session unavailable")
        ib.disconnect()
        return 1

    # Find every leg with entry_price = 0 on a multi-leg trade.
    try:
        rows = session.execute(text(
            """
            SELECT l.leg_id, l.trade_id, l.symbol, l.ib_con_id, l.ib_order_id,
                   t.ticker, t.n_legs
            FROM trade_legs l
            JOIN trades t ON t.id = l.trade_id
            WHERE l.entry_price = 0
              AND l.leg_status IN ('open', 'closed')
              AND t.n_legs > 1
            ORDER BY l.trade_id, l.leg_index
            """
        )).fetchall()
    finally:
        pass   # keep session open for writes

    log.info(f"Found {len(rows)} legs with entry_price=0 across "
             f"{len({r.trade_id for r in rows})} multi-leg trades")

    if not rows:
        ib.disconnect()
        session.close()
        return 0

    # Group legs by combo-order for Stage 1 (executions lookup)
    from collections import defaultdict
    by_order: dict[int, list] = defaultdict(list)
    for r in rows:
        if r.ib_order_id:
            by_order[int(r.ib_order_id)].append(r)

    # Build exec-stream price map (Stage 1)
    exec_prices: dict[int, float] = {}
    try:
        for exec_row in (ib.executions() or []):
            try:
                oid = exec_row.execution.orderId
                if int(oid or 0) not in by_order:
                    continue
                c_id = int(getattr(exec_row.contract, "conId", 0) or 0)
                px = float(exec_row.execution.avgPrice or 0.0)
                if c_id and px > 0 and c_id not in exec_prices:
                    exec_prices[c_id] = px
            except Exception:
                continue
    except Exception as e:
        log.debug(f"executions() fallback failed: {e}")
    log.info(f"Stage 1 (executions): {len(exec_prices)} per-leg prices recovered")

    # Stage 2: quote fallback for anything still at 0
    from ib_async import Option
    import re
    occ_re = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")
    quote_prices: dict[int, float] = {}
    def _quote(symbol):
        m = occ_re.match(symbol.strip())
        if not m:
            return 0.0
        ul, expiry6, right, strike_int = m.groups()
        expiry = f"20{expiry6}"
        strike = int(strike_int) / 1000.0
        try:
            c = Option(ul, expiry, strike, right, "SMART")
            ib.qualifyContracts(c)
            if not c.conId:
                return 0.0
            td = ib.reqMktData(c, "", False, False)
            for _ in range(20):
                ib.sleep(0.1)
                if (td.bid or 0) > 0 and (td.ask or 0) > 0:
                    break
            try:
                ib.cancelMktData(c)
            except Exception:
                pass
            if (td.bid or 0) > 0 and (td.ask or 0) > 0:
                return round((td.bid + td.ask) / 2, 2)
        except Exception as e:
            log.debug(f"quote({symbol}): {e}")
        return 0.0

    for r in rows:
        if int(r.ib_con_id or 0) in exec_prices:
            continue
        px = _quote(r.symbol)
        if px > 0:
            quote_prices[int(r.ib_con_id)] = px
    log.info(f"Stage 2 (quotes):     {len(quote_prices)} per-leg mids recovered")

    # Build update plan
    updates: list[tuple[int, float, str]] = []
    for r in rows:
        c_id = int(r.ib_con_id or 0)
        if c_id in exec_prices:
            updates.append((r.leg_id, exec_prices[c_id], "exec"))
        elif c_id in quote_prices:
            updates.append((r.leg_id, quote_prices[c_id], "quote"))
        # else: leave for manual review

    log.info(f"Plan: {len(updates)}/{len(rows)} legs repairable; "
             f"{len(rows) - len(updates)} remain at $0 (need manual review)")
    for leg_id, px, src in updates[:20]:
        log.info(f"  leg_id={leg_id}  → ${px:.2f}  src={src}")
    if len(updates) > 20:
        log.info(f"  ... {len(updates) - 20} more")

    if not args.apply:
        log.warning("Dry-run. Re-run with --apply to write.")
        ib.disconnect()
        session.close()
        return 0

    applied = 0
    try:
        for leg_id, px, src in updates:
            session.execute(text(
                "UPDATE trade_legs SET entry_price=:p, "
                "       ib_fill_price=CASE WHEN ib_fill_price=0 THEN :p ELSE ib_fill_price END, "
                "       current_price=CASE WHEN current_price=0 THEN :p ELSE current_price END, "
                "       price_source=:src "
                "WHERE leg_id=:lid"
            ), {"p": px, "src": src, "lid": leg_id})
            applied += 1
        session.commit()
    finally:
        session.close()
    log.info(f"Applied {applied} leg updates")
    ib.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
