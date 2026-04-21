"""
IB Positions mixin — position queries, fills, reconciliation support.

All methods dispatch onto the IB event loop thread via self._submit_to_ib().
Separated from ib_client.py for readability (ARCH-003 Phase 2).
"""
import logging

log = logging.getLogger(__name__)


class IBPositionsMixin:
    """Position + fill queries. Mixed into IBClient."""

    # ── IB Reconciliation ─────────────────────────────────────
    def get_ib_positions_raw(self) -> list:
        """Get raw IB positions for reconciliation. Thread-safe.
        RAISES on failure — caller must handle. Never returns empty on timeout."""
        return self._submit_to_ib(self._ib_get_positions_raw, timeout=45)

    def _ib_get_positions_raw(self) -> list:
        """Returns detailed position info for reconciliation."""
        positions = self.ib.positions()
        result = []
        for p in positions:
            if p.contract.secType == "OPT" and p.position != 0:
                result.append({
                    "symbol": p.contract.localSymbol.strip() if p.contract.localSymbol else "",
                    "conId": p.contract.conId,
                    "ticker": p.contract.symbol,
                    "expiry": p.contract.lastTradeDateOrContractMonth,
                    "strike": p.contract.strike,
                    "right": p.contract.right,
                    "qty": float(p.position),
                    "avg_cost": float(p.avgCost) / 100,  # IB avgCost per share → per contract
                    "market_price": float(p.marketPrice) if hasattr(p, 'marketPrice') else 0,
                })
        return result

    # ── Position Quantity Check (safety guard for sell orders) ──
    def get_position_quantity(self, con_id: int) -> int:
        """Get the current position quantity for a specific conId on IB.
        Returns: positive int for long, negative for short, 0 if no position.
        Thread-safe. Returns 0 on error (safe — prevents selling)."""
        if not con_id:
            return 0
        try:
            return self._submit_to_ib(self._ib_get_position_qty, con_id, timeout=15)
        except Exception as e:
            log.warning(f"Could not check position for conId={con_id}: {e}")
            return 0  # Safe default — don't sell if we can't verify

    def _ib_get_position_qty(self, con_id: int) -> int:
        for p in self.ib.positions():
            if p.contract.conId == con_id:
                return int(p.position)
        return 0

    # ── Check Recent Executions (for timeout hardening) ───────
    def check_recent_fills(self, symbol: str) -> dict | None:
        """Check if a symbol was recently filled on IB. Thread-safe."""
        try:
            return self._submit_to_ib(self._ib_check_fills, symbol)
        except Exception:
            return None

    def _ib_check_fills(self, symbol: str) -> dict | None:
        fills = self.ib.fills()
        for fill in reversed(fills):
            local_sym = fill.contract.localSymbol.strip() if fill.contract.localSymbol else ""
            if symbol in local_sym or (fill.contract.symbol and fill.contract.symbol in symbol):
                return {
                    "symbol": local_sym,
                    "qty": float(fill.execution.shares),
                    "price": float(fill.execution.price),
                    "side": fill.execution.side,
                    "time": str(fill.execution.time),
                    "order_id": fill.execution.orderId,
                    "conId": fill.contract.conId if fill.contract else None,
                }
        return None

    def check_fill_by_conid(self, con_id: int) -> dict | None:
        """Check recent fills for a specific conId. More precise than symbol search."""
        if not con_id:
            return None
        try:
            return self._submit_to_ib(self._ib_check_fill_by_conid, con_id, timeout=10)
        except Exception:
            return None

    def _ib_check_fill_by_conid(self, con_id: int) -> dict | None:
        """Search fills by exact conId match. Returns most recent SELL fill."""
        for fill in reversed(self.ib.fills()):
            if fill.contract and fill.contract.conId == con_id:
                if fill.execution.side == "SLD":
                    return {
                        "symbol": (fill.contract.localSymbol or "").strip(),
                        "qty": float(fill.execution.shares),
                        "price": float(fill.execution.price),
                        "side": fill.execution.side,
                        "time": str(fill.execution.time),
                        "order_id": fill.execution.orderId,
                        "conId": con_id,
                    }
        return None

    # ── Generic Positions ─────────────────────────────────────
    def get_open_positions(self) -> list:
        try:
            return self._submit_to_ib(self._ib_get_positions)
        except Exception as e:
            log.warning(f"Could not fetch IB positions: {e}")
            return []

    def _ib_get_positions(self) -> list:
        positions = self.ib.positions()
        return [
            {
                "symbol": p.contract.localSymbol or str(p.contract),
                "qty": float(p.position),
                "avg_cost": float(p.avgCost),
            }
            for p in positions
            if p.contract.secType == "OPT"
        ]
