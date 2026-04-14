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
    CRITICAL: If we can't get IB positions, we ABORT — never close trades
    based on incomplete data.
    """
    log.info("=" * 50)
    log.info("Running startup reconciliation...")

    try:
        ib_positions = client.get_ib_positions_raw()
    except Exception as e:
        log.error(f"Startup reconciliation ABORTED — can't get IB positions: {e}")
        log.error("Will NOT close any DB trades. Retry on next periodic reconciliation.")
        try:
            from db.writer import add_system_log
            add_system_log("reconciliation", "error", f"Aborted: {e}")
        except Exception as e:
            pass
        return

    # Build lookup of IB positions by conId (primary) and symbol (fallback)
    ib_by_con_id = {}
    ib_by_symbol = {}
    for p in ib_positions:
        con_id = p.get("conId")
        sym = p.get("symbol", "").replace(" ", "")
        if abs(p.get("qty", 0)) > 0:
            if con_id:
                ib_by_con_id[con_id] = p
            if sym:
                ib_by_symbol[sym] = p

    # Build lookup of bot's open trades by conId (primary) and symbol (fallback)
    bot_trades = list(exit_manager.open_trades)

    # ── SAFETY CHECK: if IB returns 0 positions but we have DB trades, ABORT ──
    if len(ib_by_con_id) == 0 and len(ib_by_symbol) == 0 and len(bot_trades) > 0:
        log.warning(f"[RECONCILE] SAFETY: IB returned 0 positions but bot has {len(bot_trades)} open trades. "
                    f"ABORTING reconciliation to protect open trades.")
        try:
            from db.writer import add_system_log
            add_system_log("reconciliation", "warn",
                          f"Aborted: IB returned 0 positions but bot has {len(bot_trades)} trades")
        except Exception as e:
            pass
        log.info("Reconciliation complete: ABORTED (safety check)")
        log.info("=" * 50)
        exit_manager._save_trades()
        return

    log.info(f"[RECONCILE] IB: {len(ib_by_con_id)} positions (by conId), "
             f"{len(ib_by_symbol)} (by symbol). Bot: {len(bot_trades)} open trades")

    # ── 1. DB trades with no IB position → closed while bot was down ──
    for trade in list(bot_trades):
        sym = trade["symbol"]
        con_id = trade.get("ib_con_id")
        ticker = trade.get("ticker", "UNK")

        # Match by conId first (exact), then by symbol (fallback)
        matched = False
        if con_id and con_id in ib_by_con_id:
            matched = True
        elif sym in ib_by_symbol:
            matched = True

        if matched:
            continue  # Position exists on IB — will handle in step 3

        # Not found on IB
        if not con_id:
            log.warning(f"[RECONCILE] {ticker} {sym} — no conId stored, cannot verify. Keeping open.")
            continue

        log.info(f"[RECONCILE] {ticker} {sym} (conId={con_id}) — not on IB → marking closed")

        # Check IB fills for exit price
        exit_price = trade.get("entry_price", 0)
        try:
            fill = client.check_recent_fills(sym)
            if fill and fill.get("price"):
                exit_price = fill["price"]
                log.info(f"[RECONCILE] Found IB fill: ${exit_price:.2f}")
        except Exception as e:
            pass

        entry_price = trade.get("entry_price", 0)
        pnl_pct = (exit_price - entry_price) / entry_price if entry_price > 0 else 0
        result = "WIN" if pnl_pct > 0 else "LOSS" if pnl_pct < 0 else "SCRATCH"

        # Update DB
        if trade.get("db_id"):
            try:
                from db.writer import close_trade
                close_trade(trade["db_id"], exit_price, result, "BRACKET/CLOSED (BOT OFFLINE)", {})
            except Exception as e:
                pass

        # Remove from open trades
        exit_manager.open_trades.remove(trade)
        log.info(f"[RECONCILE] {ticker} closed — {result} P&L={pnl_pct:+.1%}")

    # ── 2. IB positions with no DB trade → orphans to adopt ──
    bot_con_ids = {t.get("ib_con_id") for t in exit_manager.open_trades if t.get("ib_con_id")}
    bot_symbols = {t["symbol"] for t in exit_manager.open_trades}

    for pos in ib_positions:
        con_id = pos.get("conId")
        sym = pos.get("symbol", "").replace(" ", "")
        if abs(pos.get("qty", 0)) == 0:
            continue
        # Skip if already tracked (by conId or symbol)
        if con_id in bot_con_ids or sym in bot_symbols:
            continue

        ticker = pos.get("ticker", "UNK")
        qty = int(abs(pos["qty"]))
        avg_cost = pos.get("avg_cost", 0)
        right = pos.get("right", "C")
        direction = "SHORT" if right == "P" else "LONG"

        log.info(f"[RECONCILE] {ticker} {sym} (conId={con_id}) — on IB but not in DB → adopting")

        trade = {
            "ticker": ticker,
            "symbol": sym,
            "contracts": qty,
            "entry_price": avg_cost,
            "profit_target": avg_cost * (1 + config.PROFIT_TARGET),
            "stop_loss": avg_cost * (1 - config.STOP_LOSS),
            "entry_time": datetime.now(PT),
            "direction": direction,
            "ib_con_id": con_id,
            "_adopted": True,
            "peak_pnl_pct": 0.0,
            "dynamic_sl_pct": -config.STOP_LOSS,
        }

        # Add to exit manager
        exit_manager.add_trade(trade)
        log.info(f"[RECONCILE] Adopted {ticker} {sym} (conId={con_id}): {qty}x @ ${avg_cost:.2f} {direction}")

    # ── 3. Matched trades — verify quantities ──
    matched = 0
    for trade in exit_manager.open_trades:
        con_id = trade.get("ib_con_id")
        sym = trade["symbol"]
        ib_pos = ib_by_con_id.get(con_id) if con_id else ib_by_symbol.get(sym)
        if ib_pos:
            matched += 1
            ib_qty = int(abs(ib_pos.get("qty", 0)))
            bot_qty = trade.get("contracts", 0)

            if ib_qty != bot_qty:
                log.warning(f"[RECONCILE] {trade.get('ticker')} quantity mismatch: "
                           f"bot={bot_qty} IB={ib_qty}")
                trade["contracts"] = ib_qty

            # Backfill conId if missing
            if not trade.get("ib_con_id") and ib_pos.get("conId"):
                trade["ib_con_id"] = ib_pos["conId"]
                log.info(f"[RECONCILE] Backfilled conId={ib_pos['conId']} for {trade.get('ticker')}")

    log.info(f"Reconciliation complete: {matched} matched")
    log.info("=" * 50)

    exit_manager._save_trades()


def periodic_reconciliation(client, exit_manager):
    """
    Lighter version of reconciliation for periodic checks.
    Detects phantom trades (in bot but not on IB).
    ABORTS if can't get IB positions — never closes trades on incomplete data.
    """
    try:
        ib_positions = client.get_ib_positions_raw()
    except Exception as e:
        log.debug(f"Periodic reconciliation skipped — IB positions unavailable: {e}")
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
                        except Exception as e:
                            pass

                        entry = trade.get("entry_price", 0)
                        pnl = (exit_price - entry) / entry if entry > 0 else 0
                        result = "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "SCRATCH"
                        close_trade(trade["db_id"], exit_price, result,
                                   "BRACKET/CLOSED (RECONCILE)", {})
                    except Exception as e:
                        pass

        if removed:
            exit_manager._save_trades()
            log.info(f"[RECONCILE] Removed {len(removed)} phantom trade(s)")


def _check_brackets_exist(client, symbol: str) -> bool:
    """Check if there are active bracket orders on IB for a symbol."""
    try:
        open_orders = client._submit_to_ib(_get_open_orders_for_symbol, client.ib, symbol)
        return len(open_orders) > 0
    except Exception as e:
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
