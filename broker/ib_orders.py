"""
IB Orders mixin — order placement, brackets, cancellation, order queries.

All methods dispatch onto the IB event loop thread via self._submit_to_ib().
Separated from ib_client.py for readability (ARCH-003 Phase 2).
"""
import logging

from ib_async import MarketOrder, LimitOrder, StopOrder

import config

log = logging.getLogger(__name__)


def _check_not_flex(contract, option_symbol: str):
    """Reject Flex options before order placement. IB rejects these with code 201.
    Flex options have secType='FOP' or tradingClass different from symbol."""
    sec_type = getattr(contract, 'secType', '') or ''
    trading_class = getattr(contract, 'tradingClass', '') or ''
    symbol = getattr(contract, 'symbol', '') or ''

    if sec_type == 'FOP':
        raise RuntimeError(f"Flex option detected for {option_symbol} — "
                           f"secType={sec_type} tradingClass={trading_class}. "
                           f"IB does not allow standard orders on Flex options.")

    log.debug(f"[FLEX CHECK] {option_symbol}: secType={sec_type} "
              f"tradingClass={trading_class} symbol={symbol}")


class IBOrdersMixin:
    """Order placement + management methods. Mixed into IBClient."""

    # ── Single-leg Order Placement ────────────────────────────
    def buy_call(self, option_symbol: str, contracts: int) -> object:
        return self._place_order(option_symbol, contracts, "BUY", "call")

    def buy_put(self, option_symbol: str, contracts: int) -> object:
        return self._place_order(option_symbol, contracts, "BUY", "put")

    def sell_call(self, option_symbol: str, contracts: int) -> object:
        return self._place_order(option_symbol, contracts, "SELL", "call")

    def sell_put(self, option_symbol: str, contracts: int) -> object:
        return self._place_order(option_symbol, contracts, "SELL", "put")

    def _place_order(self, option_symbol: str, contracts: int,
                     action: str, desc: str) -> object:
        if config.DRY_RUN:
            log.info(f"[DRY RUN] IB {action} {desc.upper()}: {contracts}x {option_symbol}")
            return {"dry_run": True, "symbol": option_symbol}
        return self._submit_to_ib(
            self._ib_place_order, option_symbol, contracts, action, desc
        )

    def _ib_place_order(self, option_symbol, contracts, action, desc):
        """Place order with contract validation and fill confirmation."""
        contract = self._occ_to_contract(option_symbol)
        if not contract or not contract.conId:
            raise RuntimeError(f"Contract validation failed for {option_symbol} — order NOT placed")

        _check_not_flex(contract, option_symbol)

        order = MarketOrder(action, contracts)
        if config.IB_ACCOUNT:
            order.account = config.IB_ACCOUNT
        trade = self.ib.placeOrder(contract, order)

        # Wait for fill (up to 5 seconds)
        for _ in range(10):
            self.ib.sleep(0.5)
            if trade.orderStatus.status == "Filled":
                break

        fill_price = trade.orderStatus.avgFillPrice
        status = trade.orderStatus.status

        # Quick check if not filled yet
        if status != "Filled" and status not in ("Cancelled", "Inactive"):
            self.ib.sleep(1)
            if trade.orderStatus.status == "Filled":
                fill_price = trade.orderStatus.avgFillPrice
                status = "Filled"
            elif trade.fills:
                fill_price = trade.fills[0].execution.avgPrice
                status = "Filled"
            else:
                log.warning(f"[IB] Order {trade.order.orderId} not filled after 6s — "
                            f"status: {trade.orderStatus.status} (will track as Submitted)")

        perm_id = trade.order.permId
        con_id = contract.conId
        log.info(f"[IB] {action} {desc.upper()}: {contracts}x {option_symbol} — "
                 f"orderId={trade.order.orderId} permId={perm_id} conId={con_id} "
                 f"status={status} fill=${fill_price:.2f}")

        result = {
            "symbol": option_symbol,
            "contracts": contracts,
            "order_id": trade.order.orderId,
            "perm_id": perm_id,
            "con_id": con_id,
            "status": status,
            "fill_price": fill_price,
        }

        ib_error = self._get_last_error(trade.order.orderId)
        if ib_error:
            result["ib_error"] = ib_error
            log.warning(f"[IB] Order {trade.order.orderId} error: "
                        f"code={ib_error['code']} {ib_error['message']}")

        return result

    # ── Bracket Orders (OCO TP + SL on IB) ────────────────────
    def place_bracket_order(self, option_symbol: str, contracts: int,
                            action: str, tp_price: float, sl_price: float) -> dict:
        """Place a bracket order: parent (market) + TP (limit) + SL (stop)."""
        if config.DRY_RUN:
            log.info(f"[DRY RUN] Bracket {action}: {contracts}x {option_symbol} "
                     f"TP=${tp_price:.2f} SL=${sl_price:.2f}")
            return {"dry_run": True, "symbol": option_symbol}
        return self._submit_to_ib(
            self._ib_place_bracket, option_symbol, contracts, action, tp_price, sl_price
        )

    def _ib_place_bracket(self, option_symbol, contracts, action, tp_price, sl_price):
        """Runs on IB thread. Places bracket order."""
        contract = self._occ_to_contract(option_symbol)
        if not contract or not contract.conId:
            raise RuntimeError(f"Contract validation failed for {option_symbol}")

        _check_not_flex(contract, option_symbol)

        exit_action = "SELL" if action == "BUY" else "BUY"

        parent = MarketOrder(action, contracts)
        parent.orderId = self.ib.client.getReqId()
        parent.transmit = False
        if config.IB_ACCOUNT:
            parent.account = config.IB_ACCOUNT

        tp_order = LimitOrder(exit_action, contracts, tp_price)
        tp_order.orderId = self.ib.client.getReqId()
        tp_order.parentId = parent.orderId
        tp_order.transmit = False
        if config.IB_ACCOUNT:
            tp_order.account = config.IB_ACCOUNT

        sl_order = StopOrder(exit_action, contracts, sl_price)
        sl_order.orderId = self.ib.client.getReqId()
        sl_order.parentId = parent.orderId
        sl_order.transmit = True  # last child triggers all
        if config.IB_ACCOUNT:
            sl_order.account = config.IB_ACCOUNT

        parent_trade = self.ib.placeOrder(contract, parent)
        tp_trade = self.ib.placeOrder(contract, tp_order)
        sl_trade = self.ib.placeOrder(contract, sl_order)

        # Wait for parent fill (up to 5 seconds)
        for _ in range(10):
            self.ib.sleep(0.5)
            if parent_trade.orderStatus.status == "Filled":
                break

        fill_price = parent_trade.orderStatus.avgFillPrice
        status = parent_trade.orderStatus.status

        if status != "Filled" and status not in ("Cancelled", "Inactive"):
            self.ib.sleep(1)
            if parent_trade.orderStatus.status == "Filled":
                fill_price = parent_trade.orderStatus.avgFillPrice
                status = "Filled"

        perm_id = parent_trade.order.permId
        tp_perm_id = tp_trade.order.permId
        sl_perm_id = sl_trade.order.permId
        con_id = contract.conId

        log.info(f"[IB] BRACKET {action}: {contracts}x {option_symbol} — "
                 f"parent={parent.orderId} permId={perm_id} conId={con_id} "
                 f"status={status} fill=${fill_price:.2f} "
                 f"TP={tp_order.orderId}(perm={tp_perm_id}) SL={sl_order.orderId}(perm={sl_perm_id})")

        result = {
            "symbol": option_symbol,
            "contracts": contracts,
            "order_id": parent.orderId,
            "perm_id": perm_id,
            "con_id": con_id,
            "tp_order_id": tp_order.orderId,
            "tp_perm_id": tp_perm_id,
            "sl_order_id": sl_order.orderId,
            "sl_perm_id": sl_perm_id,
            "status": status,
            "fill_price": fill_price,
        }

        ib_error = self._get_last_error(parent.orderId)
        if ib_error:
            result["ib_error"] = ib_error
            log.warning(f"[IB] Bracket parent {parent.orderId} error: "
                        f"code={ib_error['code']} {ib_error['message']}")

        return result

    # ── Bracket SL modification ───────────────────────────────
    def update_bracket_sl(self, sl_order_id: int, new_sl_price: float) -> bool:
        """Update the stop loss leg of a bracket order."""
        if config.DRY_RUN:
            return True
        try:
            return self._submit_to_ib(self._ib_update_bracket_sl, sl_order_id, new_sl_price)
        except Exception as e:
            log.warning(f"Failed to update bracket SL: {e}")
            return False

    def _ib_update_bracket_sl(self, sl_order_id: int, new_sl_price: float) -> bool:
        for trade in self.ib.openTrades():
            if trade.order.orderId == sl_order_id:
                trade.order.auxPrice = new_sl_price
                self.ib.placeOrder(trade.contract, trade.order)
                log.info(f"[IB] Updated bracket SL orderId={sl_order_id} → ${new_sl_price:.2f}")
                return True
        log.warning(f"[IB] SL orderId={sl_order_id} not found in open trades")
        return False

    # ── Cancellation ──────────────────────────────────────────
    def cancel_bracket_children(self, tp_order_id: int, sl_order_id: int):
        """Cancel TP and SL legs by stored order IDs (legacy)."""
        if config.DRY_RUN:
            return
        try:
            self._submit_to_ib(self._ib_cancel_orders, tp_order_id, sl_order_id)
        except Exception as e:
            log.warning(f"Failed to cancel bracket children: {e}")

    def _ib_cancel_orders(self, *order_ids):
        for trade in self.ib.openTrades():
            if trade.order.orderId in order_ids:
                self.ib.cancelOrder(trade.order)
                log.info(f"[IB] Cancelled orderId={trade.order.orderId}")

    def cancel_order_by_id(self, order_id: int):
        """Cancel a specific order by orderId. Thread-safe."""
        try:
            self._submit_to_ib(self._ib_cancel_single_order, order_id, timeout=5)
        except Exception as e:
            log.warning(f"Failed to cancel orderId={order_id}: {e}")

    def _ib_cancel_single_order(self, order_id: int):
        for trade in self.ib.openTrades():
            if trade.order.orderId == order_id:
                self.ib.cancelOrder(trade.order)
                log.info(f"[IB] Cancel sent for orderId={order_id}")
                return
        log.warning(f"[IB] orderId={order_id} not found in openTrades")

    # ── Order Queries ─────────────────────────────────────────
    def find_open_orders_for_contract(self, con_id: int, symbol: str) -> list:
        """Find ALL open orders on IB for a specific contract.
        Searches by conId (primary) and symbol (fallback). Thread-safe."""
        try:
            return self._submit_to_ib(self._ib_find_orders_for_contract, con_id, symbol, timeout=10)
        except Exception as e:
            log.warning(f"Failed to query open orders: {e}")
            return []

    def _ib_find_orders_for_contract(self, con_id: int, symbol: str) -> list:
        symbol_clean = (symbol or "").replace(" ", "")
        results = []
        for trade in self.ib.openTrades():
            matched = False
            if con_id and trade.contract and trade.contract.conId == con_id:
                matched = True
            elif symbol_clean and trade.contract:
                local = (trade.contract.localSymbol or "").replace(" ", "")
                if local == symbol_clean:
                    matched = True

            if matched:
                status = trade.orderStatus.status
                if status not in ("Cancelled", "Inactive", "ApiCancelled"):
                    results.append({
                        "orderId": trade.order.orderId,
                        "permId": trade.order.permId,
                        "action": trade.order.action,
                        "orderType": trade.order.orderType,
                        "totalQty": float(trade.order.totalQuantity),
                        "status": status,
                        "conId": trade.contract.conId if trade.contract else None,
                    })
        return results

    def check_bracket_orders_active(self, trade: dict) -> bool:
        """Check if bracket orders (TP/SL) are still active on IB."""
        tp_id = trade.get("ib_tp_order_id")
        sl_id = trade.get("ib_sl_order_id")
        if not tp_id and not sl_id:
            return False
        try:
            return self._submit_to_ib(self._ib_check_brackets_active, tp_id, sl_id, timeout=5)
        except Exception:
            return False  # If can't check, assume cancelled (safe to proceed)

    def _ib_check_brackets_active(self, tp_id, sl_id) -> bool:
        check_ids = {i for i in [tp_id, sl_id] if i}
        for trade in self.ib.openTrades():
            if trade.order.orderId in check_ids:
                status = trade.orderStatus.status
                if status in ("Submitted", "PreSubmitted", "PendingSubmit"):
                    return True
        return False

    # ── Orphaned Order Cleanup ────────────────────────────────
    def cleanup_orphaned_orders(self) -> int:
        """Cancel all open IB orders that don't match a DB open trade.
        Returns count of orders cancelled. Call on startup."""
        try:
            return self._submit_to_ib(self._ib_cleanup_orphans, timeout=30)
        except Exception as e:
            log.warning(f"Orphaned order cleanup failed: {e}")
            return 0

    def _ib_cleanup_orphans(self) -> int:
        """Runs on IB thread. Finds and cancels unmatched orders."""
        db_order_ids = set()
        try:
            from db.connection import get_session
            from sqlalchemy import text
            session = get_session()
            if session:
                rows = session.execute(
                    text("SELECT ib_order_id FROM trades WHERE status='open' AND ib_order_id IS NOT NULL "
                         "UNION SELECT ib_tp_perm_id FROM trades WHERE status='open' AND ib_tp_perm_id IS NOT NULL "
                         "UNION SELECT ib_sl_perm_id FROM trades WHERE status='open' AND ib_sl_perm_id IS NOT NULL")
                ).fetchall()
                db_order_ids = {int(r[0]) for r in rows if r[0]}
                session.close()
        except Exception as e:
            log.warning(f"Could not query DB for order IDs: {e}")
            return 0

        cancelled = 0
        for trade in self.ib.openTrades():
            order_id = trade.order.orderId
            perm_id = trade.order.permId
            status = trade.orderStatus.status

            if order_id in db_order_ids or perm_id in db_order_ids:
                continue

            if status in ("Cancelled", "Inactive", "ApiCancelled"):
                continue

            symbol = ""
            if trade.contract and trade.contract.localSymbol:
                symbol = trade.contract.localSymbol.strip()
            log.warning(f"[CLEANUP] Cancelling orphaned order: orderId={order_id} "
                        f"permId={perm_id} status={status} symbol={symbol}")
            try:
                self.ib.cancelOrder(trade.order)
                cancelled += 1
            except Exception as e:
                log.warning(f"[CLEANUP] Failed to cancel orderId={order_id}: {e}")

        if cancelled:
            log.info(f"[CLEANUP] Cancelled {cancelled} orphaned order(s)")
        else:
            log.info("[CLEANUP] No orphaned orders found")
        return cancelled
