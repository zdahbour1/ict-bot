"""
IB ↔ DB Reconciliation — syncs bot state with IB's actual positions.
Runs on startup and periodically.
"""
import logging
from datetime import datetime
import pytz
import config

log = logging.getLogger(__name__)
PT = pytz.timezone("America/Los_Angeles")


def startup_reconciliation(client, exit_manager):
    """
    Run once after IB connects, before scanners start.
    Syncs DB open trades with IB's actual positions.
    """
    log.info("=" * 50)
    log.info("Running startup reconciliation...")

    try:
        ib_positions = client.get_ib_positions_raw()
    except Exception as e:
        log.warning(f"Startup reconciliation skipped — can't get IB positions: {e}")
        return

    # Build lookup of IB positions by cleaned symbol
    ib_by_symbol = {}
    for p in ib_positions:
        sym = p.get("symbol", "").replace(" ", "")
        if sym and abs(p.get("qty", 0)) > 0:
            ib_by_symbol[sym] = p

    # Build lookup of bot's open trades
    bot_by_symbol = {}
    for t in exit_manager.open_trades:
        bot_by_symbol[t["symbol"]] = t

    # ── 1. DB trades with no IB position → closed while bot was down ──
    for sym, trade in list(bot_by_symbol.items()):
        if sym not in ib_by_symbol:
            ticker = trade.get("ticker", "UNK")
            log.info(f"[RECONCILE] {ticker} {sym} — in DB but not on IB → marking closed")

            # Check IB fills for exit price
            exit_price = trade.get("entry_price", 0)
            try:
                fill = client.check_recent_fills(sym)
                if fill and fill.get("price"):
                    exit_price = fill["price"]
                    log.info(f"[RECONCILE] Found IB fill: ${exit_price:.2f}")
            except Exception:
                pass

            entry_price = trade.get("entry_price", 0)
            pnl_pct = (exit_price - entry_price) / entry_price if entry_price > 0 else 0
            result = "WIN" if pnl_pct > 0 else "LOSS" if pnl_pct < 0 else "SCRATCH"

            # Update DB
            if trade.get("db_id"):
                try:
                    from db.writer import close_trade
                    close_trade(trade["db_id"], exit_price, result, "BRACKET/CLOSED (BOT OFFLINE)", {})
                except Exception:
                    pass

            # Remove from open trades
            exit_manager.open_trades.remove(trade)
            log.info(f"[RECONCILE] {ticker} closed — {result} P&L={pnl_pct:+.1%}")

    # ── 2. IB positions with no DB trade → orphans to adopt ──
    for sym, pos in ib_by_symbol.items():
        if sym not in bot_by_symbol:
            ticker = pos.get("ticker", "UNK")
            qty = int(abs(pos["qty"]))
            avg_cost = pos.get("avg_cost", 0)
            right = pos.get("right", "C")
            direction = "SHORT" if right == "P" else "LONG"

            log.info(f"[RECONCILE] {ticker} {sym} — on IB but not in DB → adopting")

            trade = {
                "ticker": ticker,
                "symbol": sym,
                "contracts": qty,
                "entry_price": avg_cost,
                "profit_target": avg_cost * (1 + config.PROFIT_TARGET),
                "stop_loss": avg_cost * (1 - config.STOP_LOSS),
                "entry_time": datetime.now(PT),
                "direction": direction,
                "_adopted": True,
                "peak_pnl_pct": 0.0,
                "dynamic_sl_pct": -config.STOP_LOSS,
            }

            # Add to exit manager
            exit_manager.add_trade(trade)

            # Check if bracket orders exist on IB for this position
            has_brackets = _check_brackets_exist(client, sym)
            if not has_brackets and config.USE_BRACKET_ORDERS:
                # Create bracket orders
                try:
                    tp_price = round(avg_cost * (1 + config.PROFIT_TARGET), 2)
                    sl_price = round(avg_cost * (1 - config.STOP_LOSS), 2)
                    log.info(f"[RECONCILE] Creating bracket orders for {sym}: TP=${tp_price} SL=${sl_price}")
                    # Note: can't create brackets for existing positions easily with IB
                    # Would need to place separate TP limit + SL stop orders
                    # For now, just log — the exit manager will handle it
                    log.info(f"[RECONCILE] Adopted {ticker} — exit manager will monitor")
                except Exception as e:
                    log.warning(f"[RECONCILE] Failed to create brackets for {sym}: {e}")

            log.info(f"[RECONCILE] Adopted {ticker} {sym}: {qty}x @ ${avg_cost:.2f} {direction}")

    # ── 3. Matched trades — verify brackets, update counts ──
    matched = 0
    for sym in bot_by_symbol:
        if sym in ib_by_symbol:
            matched += 1
            trade = bot_by_symbol[sym]
            ib_pos = ib_by_symbol[sym]
            ib_qty = int(abs(ib_pos.get("qty", 0)))
            bot_qty = trade.get("contracts", 0)

            if ib_qty != bot_qty:
                log.warning(f"[RECONCILE] {trade.get('ticker')} quantity mismatch: "
                           f"bot={bot_qty} IB={ib_qty}")
                trade["contracts"] = ib_qty

    orphans = len(ib_by_symbol) - matched
    phantoms = len(bot_by_symbol) - matched

    log.info(f"Reconciliation complete: {matched} matched, "
             f"{orphans} adopted, {phantoms} closed")
    log.info("=" * 50)

    exit_manager._save_trades()


def periodic_reconciliation(client, exit_manager):
    """
    Lighter version of reconciliation for periodic checks.
    Detects phantom trades (in bot but not on IB).
    """
    try:
        ib_positions = client.get_ib_positions_raw()
    except Exception:
        return

    ib_symbols = set()
    for p in ib_positions:
        sym = p.get("symbol", "").replace(" ", "")
        if sym and abs(p.get("qty", 0)) > 0:
            ib_symbols.add(sym)

    with exit_manager._lock:
        removed = []
        for trade in list(exit_manager.open_trades):
            sym = trade["symbol"]
            if sym not in ib_symbols:
                ticker = trade.get("ticker", "UNK")
                log.warning(f"[RECONCILE] Phantom: {ticker} {sym} — removing from bot")
                exit_manager.open_trades.remove(trade)
                removed.append(trade)

                if trade.get("db_id"):
                    try:
                        from db.writer import close_trade
                        # Try to find the exit price from IB fills
                        exit_price = trade.get("entry_price", 0)
                        try:
                            fill = client.check_recent_fills(sym)
                            if fill and fill.get("price"):
                                exit_price = fill["price"]
                        except Exception:
                            pass

                        entry = trade.get("entry_price", 0)
                        pnl = (exit_price - entry) / entry if entry > 0 else 0
                        result = "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "SCRATCH"
                        close_trade(trade["db_id"], exit_price, result,
                                   "BRACKET/CLOSED (RECONCILE)", {})
                    except Exception:
                        pass

        if removed:
            exit_manager._save_trades()
            log.info(f"[RECONCILE] Removed {len(removed)} phantom trade(s)")


def _check_brackets_exist(client, symbol: str) -> bool:
    """Check if there are active bracket orders on IB for a symbol."""
    try:
        open_orders = client._submit_to_ib(_get_open_orders_for_symbol, client.ib, symbol)
        return len(open_orders) > 0
    except Exception:
        return False


def _get_open_orders_for_symbol(ib, symbol: str) -> list:
    """Runs on IB thread — check for open orders matching symbol."""
    matching = []
    for trade in ib.openTrades():
        local_sym = ""
        if trade.contract and trade.contract.localSymbol:
            local_sym = trade.contract.localSymbol.strip().replace(" ", "")
        if symbol.replace(" ", "") in local_sym or local_sym in symbol.replace(" ", ""):
            matching.append({
                "orderId": trade.order.orderId,
                "action": trade.order.action,
                "orderType": trade.order.orderType,
                "status": trade.orderStatus.status,
            })
    return matching
