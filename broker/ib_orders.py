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
                            action: str, tp_price: float, sl_price: float,
                            order_ref: str | None = None) -> dict:
        """Place a bracket order: parent (market) + TP (limit) + SL (stop).

        ``order_ref`` is stamped on all three orders' ``orderRef``
        field so the parent + TP + SL share one correlation tag
        visible in IB / TWS and in our DB (``trades.client_trade_id``).
        See docs/ib_db_correlation.md.
        """
        if config.DRY_RUN:
            log.info(f"[DRY RUN] Bracket {action}: {contracts}x {option_symbol} "
                     f"TP=${tp_price:.2f} SL=${sl_price:.2f} ref={order_ref}")
            return {"dry_run": True, "symbol": option_symbol, "order_ref": order_ref}
        return self._submit_to_ib(
            self._ib_place_bracket, option_symbol, contracts, action, tp_price, sl_price, order_ref
        )

    def place_protection_brackets(self, option_symbol: str, contracts: int,
                                   tp_price: float, sl_price: float,
                                   order_ref: str | None = None) -> dict:
        """Attach TP + SL protection to an EXISTING long position.

        Unlike ``place_bracket_order`` (which opens a new trade with a
        parent BUY + two SELL children), this places only the two SELL
        children — a LMT take-profit and a STP stop-loss — bound
        together in an OCA group so one cancels the other.

        Used by the reconcile-driven bracket restoration path: when we
        detect a trade that became ``unprotected_position`` (brackets
        cancelled but position still held), we call this to put fresh
        protection back on without re-buying.
        """
        if config.DRY_RUN:
            log.info(f"[DRY RUN] Restore brackets: {contracts}x {option_symbol} "
                     f"TP=${tp_price:.2f} SL=${sl_price:.2f} ref={order_ref}")
            return {"dry_run": True, "symbol": option_symbol, "order_ref": order_ref}
        return self._submit_to_ib(
            self._ib_place_protection_brackets, option_symbol, contracts,
            tp_price, sl_price, order_ref
        )

    def _ib_place_protection_brackets(self, option_symbol, contracts,
                                       tp_price, sl_price,
                                       order_ref: str | None = None):
        """Runs on IB thread. Places SELL LMT + SELL STP in an OCA group."""
        contract = self._occ_to_contract(option_symbol)
        if not contract or not contract.conId:
            raise RuntimeError(f"Contract validation failed for {option_symbol}")
        _check_not_flex(contract, option_symbol)

        # Unique OCA group so the two orders cancel each other but
        # don't interact with any other bracket.
        oca_group = f"RESTORE-{self.ib.client.getReqId()}"

        tp_order = LimitOrder("SELL", contracts, tp_price)
        tp_order.orderId = self.ib.client.getReqId()
        tp_order.ocaGroup = oca_group
        tp_order.ocaType = 1
        tp_order.tif = "DAY"
        tp_order.transmit = False
        if config.IB_ACCOUNT:
            tp_order.account = config.IB_ACCOUNT
        if order_ref:
            tp_order.orderRef = order_ref

        sl_order = StopOrder("SELL", contracts, sl_price)
        sl_order.orderId = self.ib.client.getReqId()
        sl_order.ocaGroup = oca_group
        sl_order.ocaType = 1
        sl_order.tif = "DAY"
        sl_order.transmit = True
        if config.IB_ACCOUNT:
            sl_order.account = config.IB_ACCOUNT
        if order_ref:
            sl_order.orderRef = order_ref

        tp_trade = self.ib.placeOrder(contract, tp_order)
        sl_trade = self.ib.placeOrder(contract, sl_order)

        for _ in range(6):
            self.ib.sleep(0.5)
            if (tp_trade.orderStatus.status in ("Submitted", "PreSubmitted")
                and sl_trade.orderStatus.status in ("Submitted", "PreSubmitted")):
                break

        log.warning(
            f"[IB] RESTORE BRACKETS: {contracts}x {option_symbol} "
            f"TP={tp_order.orderId}(perm={tp_trade.order.permId}) @ ${tp_price:.2f} "
            f"status={tp_trade.orderStatus.status} "
            f"SL={sl_order.orderId}(perm={sl_trade.order.permId}) @ ${sl_price:.2f} "
            f"status={sl_trade.orderStatus.status} "
            f"oca={oca_group}"
        )

        return {
            "symbol": option_symbol,
            "contracts": contracts,
            "tp_order_id": tp_order.orderId,
            "tp_perm_id":  tp_trade.order.permId,
            "tp_price":    tp_price,
            "sl_order_id": sl_order.orderId,
            "sl_perm_id":  sl_trade.order.permId,
            "sl_price":    sl_price,
            "oca_group":   oca_group,
            "tp_status":   tp_trade.orderStatus.status,
            "sl_status":   sl_trade.orderStatus.status,
        }

    def _ib_place_bracket(self, option_symbol, contracts, action, tp_price, sl_price,
                           order_ref: str | None = None):
        """Runs on IB thread. Places bracket order. Stamps orderRef on
        all three legs for IB↔DB correlation (docs/ib_db_correlation.md)."""
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
        if order_ref:
            parent.orderRef = order_ref

        tp_order = LimitOrder(exit_action, contracts, tp_price)
        tp_order.orderId = self.ib.client.getReqId()
        tp_order.parentId = parent.orderId
        tp_order.transmit = False
        if config.IB_ACCOUNT:
            tp_order.account = config.IB_ACCOUNT
        if order_ref:
            tp_order.orderRef = order_ref

        sl_order = StopOrder(exit_action, contracts, sl_price)
        sl_order.orderId = self.ib.client.getReqId()
        sl_order.parentId = parent.orderId
        sl_order.transmit = True
        if config.IB_ACCOUNT:
            sl_order.account = config.IB_ACCOUNT
        if order_ref:
            sl_order.orderRef = order_ref

        parent_trade = self.ib.placeOrder(contract, parent)
        tp_trade = self.ib.placeOrder(contract, tp_order)
        sl_trade = self.ib.placeOrder(contract, sl_order)

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
                 f"TP={tp_order.orderId}(perm={tp_perm_id}) @ ${tp_price:.2f} "
                 f"SL={sl_order.orderId}(perm={sl_perm_id}) @ ${sl_price:.2f} "
                 f"ref={order_ref or '—'}")

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
            "order_ref": order_ref,
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
        """Cancel a specific order by orderId, across every client in the pool.

        IB's ``cancelOrder`` only succeeds on the client that PLACED the
        order — all other clients get Error 10147. Fan-out across the
        pool so whichever client owns the order processes it; others
        harmlessly emit 10147 which we swallow.
        """
        try:
            self._submit_to_ib(self._ib_cancel_single_order, order_id, timeout=5)
        except Exception as e:
            log.warning(f"Failed to cancel orderId={order_id} on own client: {e}")

        if self._pool is not None:
            for conn in self._pool.all_connections:
                if conn is self._conn:
                    continue
                try:
                    conn.submit(self._ib_cancel_single_order_on_conn,
                                conn.ib, order_id, timeout=5)
                except Exception as e:
                    log.debug(f"[IB pool fan-out] cancel orderId={order_id} "
                              f"on {conn.label}: {e}")

    def _ib_cancel_single_order(self, order_id: int):
        """Runs on THIS connection's IB thread. Cancel one order."""
        self._ib_cancel_single_order_on_conn(self.ib, order_id)

    @staticmethod
    def _ib_cancel_single_order_on_conn(ib, order_id: int):
        """Runs on a specific connection's IB thread. Cancel one order.
        Used for cross-client fan-out: same code, different connection."""
        for trade in ib.openTrades():
            if trade.order.orderId == order_id:
                ib.cancelOrder(trade.order)
                log.info(f"[IB clientId={trade.order.clientId}] "
                         f"Cancel sent for orderId={order_id}")
                return
        log.debug(f"[IB] orderId={order_id} not in this connection's openTrades")

    def cancel_order_by_perm_id(self, perm_id: int):
        """Cancel by IB permId — globally unique across all clients.
        Fans out across the pool; the owning client processes the
        cancel, others harmlessly return 10147."""
        if not perm_id:
            return
        if self._pool is None:
            try:
                self._submit_to_ib(self._ib_cancel_by_perm_id_on, self.ib, perm_id, timeout=5)
            except Exception as e:
                log.warning(f"Failed to cancel permId={perm_id}: {e}")
            return
        for conn in self._pool.all_connections:
            try:
                conn.submit(self._ib_cancel_by_perm_id_on, conn.ib, perm_id, timeout=5)
            except Exception as e:
                log.debug(f"[IB pool fan-out] cancel permId={perm_id} on {conn.label}: {e}")

    @staticmethod
    def _ib_cancel_by_perm_id_on(ib, perm_id: int):
        for trade in ib.openTrades():
            if trade.order.permId == perm_id:
                ib.cancelOrder(trade.order)
                log.info(f"[IB clientId={trade.order.clientId}] "
                         f"Cancel sent for permId={perm_id} orderId={trade.order.orderId}")
                return

    # ── Order Queries ─────────────────────────────────────────
    def refresh_all_open_orders(self) -> int:
        """Refresh local ``openTrades()`` cache with orders from EVERY
        clientId in the IB account via reqAllOpenOrders."""
        try:
            return self._submit_to_ib(self._ib_refresh_all_open_orders, timeout=10)
        except Exception as e:
            log.warning(f"refresh_all_open_orders failed: {e}")
            return 0

    def _ib_refresh_all_open_orders(self) -> int:
        try:
            self.ib.reqAllOpenOrders()
        except Exception as e:
            log.warning(f"[IB] reqAllOpenOrders failed: {e}")
            return len(self.ib.openTrades())
        try:
            self.ib.sleep(0.25)
        except Exception:
            pass
        return len(self.ib.openTrades())

    def find_open_orders_for_contract(self, con_id: int, symbol: str) -> list:
        """Find ALL open orders on IB for a specific contract.

        Pool-aware: queries EVERY connection in the pool and dedupes by
        permId, preferring the most-terminal status. Fixes the stale-
        cache bug (SPY 2026-04-21) where an order cancelled via one
        client leaves the old "Submitted" in another client's cache.
        See docs/close_flow_fixes_2026_04_21.md §2.
        """
        try:
            own = self._submit_to_ib(
                self._ib_find_orders_for_contract_on_conn,
                self.ib, con_id, symbol, True,  # include_terminal=True
                timeout=10,
            )
        except Exception as e:
            log.warning(f"Failed to query open orders: {e}")
            own = []

        TERMINAL_SET = {"Cancelled", "ApiCancelled", "Inactive", "Filled"}
        if self._pool is None:
            return [e for e in own if e.get("status") not in TERMINAL_SET]

        TERMINAL_RANK = {
            "Cancelled": 4, "ApiCancelled": 4, "Inactive": 4, "Filled": 4,
            "PendingCancel": 3, "Submitted": 2, "PreSubmitted": 1,
            "PendingSubmit": 0,
        }
        merged: dict = {}

        def _key(entry):
            pid = entry.get("permId") or 0
            if pid:
                return ("p", pid)
            return ("o", entry.get("clientId") or 0, entry.get("orderId"))

        def _merge(entry):
            k = _key(entry)
            cur = merged.get(k)
            if cur is None:
                merged[k] = entry
                return
            cur_rank = TERMINAL_RANK.get(cur.get("status"), 2)
            new_rank = TERMINAL_RANK.get(entry.get("status"), 2)
            if new_rank > cur_rank:
                merged[k] = entry

        for e in own:
            _merge(e)
        for conn in self._pool.all_connections:
            if conn is self._conn:
                continue
            try:
                rows = conn.submit(
                    self._ib_find_orders_for_contract_on_conn,
                    conn.ib, con_id, symbol, True,
                    timeout=5,
                )
            except Exception as e:
                log.debug(f"find_open_orders fan-out on {conn.label}: {e}")
                continue
            for row in rows or []:
                _merge(row)
        return [e for e in merged.values()
                if e.get("status") not in TERMINAL_SET]

    def get_all_working_orders(self) -> list:
        """Return every working order across all clientIds in the account."""
        try:
            return self._submit_to_ib(self._ib_get_all_working_orders, timeout=10)
        except Exception as e:
            log.warning(f"Failed to query all working orders: {e}")
            return []

    def _ib_get_all_working_orders(self) -> list:
        results = []
        for trade in self.ib.openTrades():
            status = trade.orderStatus.status
            if status in ("Cancelled", "ApiCancelled", "Inactive", "Filled"):
                continue
            order = trade.order
            contract = trade.contract
            results.append({
                "orderId":   order.orderId,
                "permId":    order.permId,
                "action":    order.action,
                "orderType": order.orderType,
                "totalQty":  float(order.totalQuantity),
                "status":    status,
                "conId":     contract.conId if contract else None,
                "parentId":  getattr(order, "parentId", 0) or 0,
                "lmtPrice":  float(getattr(order, "lmtPrice", 0) or 0),
                "auxPrice":  float(getattr(order, "auxPrice", 0) or 0),
                "symbol":    "".join((getattr(contract, "localSymbol", "") or "").split()) if contract else "",
                "clientId":  getattr(order, "clientId", 0) or 0,
                "orderRef":  getattr(order, "orderRef", "") or "",
            })
        return results

    def _ib_find_orders_for_contract(self, con_id: int, symbol: str) -> list:
        """Runs on IB thread. Returns OPEN orders matching this contract
        from THIS connection's view only. Public callers should use
        ``find_open_orders_for_contract`` which fans out pool-wide."""
        return self._ib_find_orders_for_contract_on_conn(
            self.ib, con_id, symbol, include_terminal=False
        )

    @staticmethod
    def _ib_find_orders_for_contract_on_conn(ib, con_id: int, symbol: str,
                                              include_terminal: bool = True) -> list:
        """Same as _ib_find_orders_for_contract against an explicit ib
        instance. Used by the pool fan-out in find_open_orders_for_contract."""
        symbol_clean = (symbol or "").replace(" ", "")
        results = []
        TERMINAL_EXCLUDE = {"Cancelled", "Inactive", "ApiCancelled"}
        for trade in ib.openTrades():
            matched = False
            if con_id and trade.contract and trade.contract.conId == con_id:
                matched = True
            elif symbol_clean and trade.contract:
                local = (trade.contract.localSymbol or "").replace(" ", "")
                if local == symbol_clean:
                    matched = True
            if not matched:
                continue
            status = trade.orderStatus.status
            if not include_terminal and status in TERMINAL_EXCLUDE:
                continue
            results.append({
                "orderId":   trade.order.orderId,
                "permId":    trade.order.permId,
                "action":    trade.order.action,
                "orderType": trade.order.orderType,
                "totalQty":  float(trade.order.totalQuantity),
                "status":    status,
                "conId":     trade.contract.conId if trade.contract else None,
                "parentId":  getattr(trade.order, "parentId", 0) or 0,
                "lmtPrice":  float(getattr(trade.order, "lmtPrice", 0) or 0),
                "auxPrice":  float(getattr(trade.order, "auxPrice", 0) or 0),
                "orderRef":  getattr(trade.order, "orderRef", "") or "",
                "clientId":  getattr(trade.order, "clientId", 0) or 0,
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
                # Multi-strategy v2: IB order ids moved from trades to trade_legs.
                # Join so we pick up order ids for every leg of every open trade.
                rows = session.execute(text(
                    "SELECT l.ib_order_id   FROM trade_legs l JOIN trades t ON t.id = l.trade_id "
                    "  WHERE t.status='open' AND l.ib_order_id   IS NOT NULL "
                    "UNION "
                    "SELECT l.ib_tp_perm_id FROM trade_legs l JOIN trades t ON t.id = l.trade_id "
                    "  WHERE t.status='open' AND l.ib_tp_perm_id IS NOT NULL "
                    "UNION "
                    "SELECT l.ib_sl_perm_id FROM trade_legs l JOIN trades t ON t.id = l.trade_id "
                    "  WHERE t.status='open' AND l.ib_sl_perm_id IS NOT NULL"
                )).fetchall()
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
