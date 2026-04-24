"""
IB Orders mixin — order placement, brackets, cancellation, order queries.

All methods dispatch onto the IB event loop thread via self._submit_to_ib().
Separated from ib_client.py for readability (ARCH-003 Phase 2).
"""
import logging

from ib_async import MarketOrder, LimitOrder, StopOrder, Stock

import config

log = logging.getLogger(__name__)


def _compute_combo_net_limit(mixin, leg_contracts, action: str,
                              legs: list) -> float | None:
    """Compute a limit price for a combo order using each leg's current
    quote. Returns None when the auto-limit feature is off or when
    quotes are unavailable (falls back to MarketOrder in the caller).

    For a SPREAD order the IB convention is:
    - ``action="BUY"``  → caller pays up to ``limit_price`` net debit
      (positive value means max debit willing to pay; for a net credit
      spread you'd submit action=SELL instead)
    - ``action="SELL"`` → caller receives at least ``limit_price`` net
      credit

    We compute signed net premium = sum(direction * leg_mid) across
    legs. For iron condors this is typically a net credit (positive
    for the SELLER). We then widen by a configurable slippage buffer
    so the order crosses the spread rather than posting at mid.

    Implementation keeps things simple: if ANY leg's quote fails,
    return None and let the caller fall back to MKT. Better to
    slip than to miss the entry.
    """
    # Read config once; cheap DB call.
    try:
        from db.settings_cache import get_bool, get_float
        auto = get_bool("DN_COMBO_AUTO_LIMIT", default=True)
        slip_bps = get_float("DN_COMBO_LIMIT_SLIP_BPS", default=200.0)
    except Exception:
        auto = True
        slip_bps = 200.0
    if not auto:
        return None
    # Fetch mid for each leg via the existing single-leg quote path.
    midprices = []
    for i, leg, contract in leg_contracts:
        sym = leg.get("symbol")
        if not sym:
            return None
        try:
            px = mixin.get_option_price(sym)
            if not px or px <= 0:
                return None
            midprices.append((leg, float(px)))
        except Exception as e:
            log.warning(f"[COMBO-LIMIT] quote failed for {sym}: {e} — "
                        f"falling back to MKT")
            return None
    # Signed net premium per share-multiplier. For iron condor the
    # standard sign convention:
    #   short leg (SELL) → we receive premium → positive contribution
    #   long leg  (BUY)  → we pay premium → negative contribution
    net = 0.0
    for leg, px in midprices:
        direction = (leg.get("direction") or "LONG").upper()
        sign = +1 if direction == "SHORT" else -1
        net += sign * px
    # ENH-063 (2026-04-24) — IB BAG limit sign convention:
    #   For action=BUY, limit > 0 means "pay up to limit debit"; limit
    #   < 0 means "accept at least -limit credit". For action=SELL the
    #   reverse holds. An iron condor/butterfly is normally a CREDIT
    #   spread (net > 0 with our short=+/long=- sign convention).
    #   The old code sent abs(net)+buf as a BUY limit — positive, i.e.
    #   "I'll pay up to $X debit" — which on a credit combo is
    #   extremely off-fair and triggers IB's "price cap" protection,
    #   leaving the order stuck unfilled. Fix: keep the sign.
    #
    # Signed net → signed buy-side debit:
    #   debit_to_pay = -net
    #     net = +4.42 credit    → debit_to_pay = -4.42 (i.e. receive $4.42)
    #     net = -2.00 debit     → debit_to_pay = +2.00 (i.e. pay $2.00)
    # Widen toward worse-for-us so the combo crosses the spread:
    #   BUY:  increase debit_to_pay by buf
    #   SELL: decrease net (credit received) by buf
    # slip_bps is basis points of |net|; default 200 bps = 2%.
    buf = abs(net) * (slip_bps / 10_000.0)
    if action.upper() == "SELL":
        # SELL the combo: we want to receive at least (net - buf)
        limit = net - buf
    else:
        # BUY the combo: we're willing to pay up to (-net + buf)
        limit = -net + buf
    log.info(f"[COMBO-LIMIT] net_premium=${net:+.2f} action={action} "
             f"slip={slip_bps:.0f}bps → signed limit=${limit:+.2f}")
    return round(limit, 2)


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
    # Every single-leg entry now accepts order_ref so the IB Order Ref
    # column (TWS Activity tab) carries the strategy-TICKER-YYMMDD-NN
    # tag. Without this, pre-2026-04-24 single-leg trades showed blank
    # Order Ref in IB even though we had the ref in our DB.
    def buy_call(self, option_symbol: str, contracts: int,
                  order_ref: str | None = None) -> object:
        return self._place_order(option_symbol, contracts, "BUY", "call",
                                   order_ref=order_ref)

    def buy_put(self, option_symbol: str, contracts: int,
                 order_ref: str | None = None) -> object:
        return self._place_order(option_symbol, contracts, "BUY", "put",
                                   order_ref=order_ref)

    def sell_call(self, option_symbol: str, contracts: int,
                   order_ref: str | None = None) -> object:
        return self._place_order(option_symbol, contracts, "SELL", "call",
                                   order_ref=order_ref)

    def sell_put(self, option_symbol: str, contracts: int,
                  order_ref: str | None = None) -> object:
        return self._place_order(option_symbol, contracts, "SELL", "put",
                                   order_ref=order_ref)

    # Stock close helpers (ENH-036) — used by multi-leg exit flow when a
    # delta-neutral trade uses a stock hedge leg. MKT order, same
    # fill-wait + result-dict contract as _place_order.
    def sell_stock(self, ticker_symbol: str, shares: int,
                    exchange: str = "SMART",
                    order_ref: str | None = None) -> object:
        return self._submit_to_ib(self._ib_place_stock_order,
                                   ticker_symbol, shares, "SELL", exchange,
                                   order_ref)

    def buy_stock(self, ticker_symbol: str, shares: int,
                   exchange: str = "SMART",
                   order_ref: str | None = None) -> object:
        return self._submit_to_ib(self._ib_place_stock_order,
                                   ticker_symbol, shares, "BUY", exchange,
                                   order_ref)

    def _ib_place_stock_order(self, ticker_symbol, shares, action, exchange,
                               order_ref: str | None = None):
        """Runs on IB thread. Places a stock MKT order (for hedge-leg
        entry + close). Minimal — no bracket, no stop — caller already
        manages the position via the options legs."""
        from ib_async import Stock
        contract = Stock(ticker_symbol, exchange, "USD")
        qualified = self.ib.qualifyContracts(contract)
        if not qualified or not qualified[0].conId:
            raise RuntimeError(f"STK qualification failed for {ticker_symbol}")
        contract = qualified[0]
        order = MarketOrder(action, shares)
        if config.IB_ACCOUNT:
            order.account = config.IB_ACCOUNT
        if order_ref:
            order.orderRef = order_ref
        trade = self.ib.placeOrder(contract, order)
        for _ in range(10):
            self.ib.sleep(0.5)
            if trade.orderStatus.status == "Filled":
                break
        fill_price = trade.orderStatus.avgFillPrice
        status = trade.orderStatus.status
        log.info(f"[IB] STK {action}: {shares}x {ticker_symbol} — "
                 f"orderId={trade.order.orderId} permId={trade.order.permId} "
                 f"conId={contract.conId} status={status} fill=${fill_price:.2f}")
        return {
            "symbol": ticker_symbol, "contracts": shares,
            "order_id": trade.order.orderId,
            "perm_id": trade.order.permId, "con_id": contract.conId,
            "status": status, "fill_price": fill_price,
        }

    def _place_order(self, option_symbol: str, contracts: int,
                     action: str, desc: str,
                     order_ref: str | None = None) -> object:
        if config.DRY_RUN:
            log.info(f"[DRY RUN] IB {action} {desc.upper()}: {contracts}x "
                     f"{option_symbol} ref={order_ref}")
            return {"dry_run": True, "symbol": option_symbol}
        return self._submit_to_ib(
            self._ib_place_order, option_symbol, contracts, action, desc,
            order_ref
        )

    def _ib_place_order(self, option_symbol, contracts, action, desc,
                        order_ref: str | None = None):
        """Place order with contract validation and fill confirmation."""
        contract = self._occ_to_contract(option_symbol)
        if not contract or not contract.conId:
            raise RuntimeError(f"Contract validation failed for {option_symbol} — order NOT placed")

        _check_not_flex(contract, option_symbol)

        order = MarketOrder(action, contracts)
        if config.IB_ACCOUNT:
            order.account = config.IB_ACCOUNT
        if order_ref:
            order.orderRef = order_ref
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
            return {"dry_run": True, "symbol": option_symbol, "order_ref": order_ref,
                    "client_id": None}
        return self._submit_to_ib(
            self._ib_place_bracket, option_symbol, contracts, action, tp_price, sl_price, order_ref
        )

    # ── FOP (Futures Options) ─────────────────────────────
    def place_bracket_order_fop(self, spec: dict, contracts: int,
                                 action: str, tp_price: float, sl_price: float,
                                 order_ref: str | None = None) -> dict:
        """Place a bracket order on a FuturesOption contract (ENH-034).

        `spec` must provide: symbol, exchange, currency, multiplier,
        expiry (YYYYMMDD), strike, right ('C'|'P'). Optionally `con_id`
        if already qualified (skips the qualification round-trip).

        Returns same shape as place_bracket_order: symbol / order_id /
        perm_id / con_id / tp_* / sl_* / status / fill_price / order_ref
        / client_id. Bot's insert_trade persists the structured fields
        (sec_type='FOP', expiry, strike, right, multiplier, exchange)
        via the new trade_legs row.
        """
        if config.DRY_RUN:
            log.info(f"[DRY RUN] FOP Bracket {action}: {contracts}x "
                     f"{spec.get('symbol')} {spec.get('expiry')} "
                     f"{spec.get('strike')}{spec.get('right')} "
                     f"TP=${tp_price:.2f} SL=${sl_price:.2f} ref={order_ref}")
            return {"dry_run": True, "symbol": spec.get("symbol"),
                    "order_ref": order_ref, "client_id": None}
        return self._submit_to_ib(
            self._ib_place_bracket_fop, spec, contracts, action,
            tp_price, sl_price, order_ref
        )

    def _ib_place_bracket_fop(self, spec, contracts, action, tp_price,
                               sl_price, order_ref):
        """Runs on IB thread. Builds FuturesOption and submits bracket."""
        from ib_async import FuturesOption
        contract = FuturesOption(
            symbol=spec["symbol"],
            lastTradeDateOrContractMonth=spec["expiry"],
            strike=float(spec["strike"]),
            right=spec["right"],
            exchange=spec["exchange"],
            multiplier=str(int(spec["multiplier"])),
            currency=spec.get("currency", "USD"),
        )
        qualified = self.ib.qualifyContracts(contract)
        if not qualified or not qualified[0] or not qualified[0].conId:
            raise RuntimeError(
                f"FOP qualification failed: {spec['symbol']} {spec['expiry']} "
                f"{spec['strike']}{spec['right']} on {spec['exchange']}"
            )
        contract = qualified[0]

        exit_action = "SELL" if action == "BUY" else "BUY"
        placing_client_id = self.ib.client.clientId

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

        log.info(f"[IB] FOP BRACKET {action}: {contracts}x {spec['symbol']} "
                 f"{spec['expiry']} {spec['strike']}{spec['right']} — "
                 f"parent={parent.orderId} permId={parent_trade.order.permId} "
                 f"conId={contract.conId} status={status} fill=${fill_price:.2f} "
                 f"TP={tp_order.orderId}(perm={tp_trade.order.permId}) @ ${tp_price:.2f} "
                 f"SL={sl_order.orderId}(perm={sl_trade.order.permId}) @ ${sl_price:.2f} "
                 f"ref={order_ref or '—'}")

        # Reuse the "symbol" key to carry something grep-friendly. We also
        # populate the structured FOP fields so insert_trade / trade_legs
        # can persist them as first-class leg data.
        return {
            "symbol": (contract.localSymbol or "").strip() or
                       f"{spec['symbol']}{spec['expiry']}{spec['right']}{int(spec['strike'])}",
            "contracts": contracts,
            "order_id": parent.orderId,
            "perm_id": parent_trade.order.permId,
            "con_id": contract.conId,
            "tp_order_id": tp_order.orderId,
            "tp_perm_id": tp_trade.order.permId,
            "sl_order_id": sl_order.orderId,
            "sl_perm_id": sl_trade.order.permId,
            "status": status,
            "fill_price": fill_price,
            "order_ref": order_ref,
            "client_id": placing_client_id,
            # Structured FOP fields for DB persistence:
            "sec_type": "FOP",
            "underlying": spec["symbol"],
            "expiry": spec["expiry"],
            "strike": float(spec["strike"]),
            "right": spec["right"],
            "multiplier": int(spec["multiplier"]),
            "exchange": spec["exchange"],
            "currency": spec.get("currency", "USD"),
        }

    # ── FOP chain + quote probes (used by strategy.fop_selector) ─
    def fop_chain(self, underlying: str, exchange: str) -> list[dict]:
        """List all listed FOP expiries/strikes/rights for an underlying.
        Returns one dict per contract with keys: expiry, strike, right, con_id.
        Empty list on failure (selector then bails gracefully)."""
        try:
            return self._submit_to_ib(self._ib_fop_chain, underlying, exchange, timeout=30)
        except Exception as e:
            log.warning(f"fop_chain({underlying}) failed: {e}")
            return []

    def _ib_fop_chain(self, underlying: str, exchange: str) -> list[dict]:
        from ib_async import FuturesOption
        spec = FuturesOption(symbol=underlying, exchange=exchange, currency="USD")
        details = self.ib.reqContractDetails(spec) or []
        out = []
        for d in details:
            c = d.contract
            out.append({
                "expiry": getattr(c, "lastTradeDateOrContractMonth", "") or "",
                "strike": float(getattr(c, "strike", 0) or 0),
                "right": getattr(c, "right", "") or "",
                "con_id": getattr(c, "conId", 0),
                "multiplier": getattr(c, "multiplier", ""),
                "local_symbol": getattr(c, "localSymbol", ""),
            })
        return out

    def fop_quote(self, symbol: str, exchange: str, expiry: str,
                   strike: float, right: str, multiplier: int) -> dict:
        """Snapshot market-data probe for a single FOP contract. Returns
        {bid, ask, volume, open_interest, con_id}. Best-effort — on any
        IB hiccup returns zeros so the selector rejects the contract."""
        try:
            return self._submit_to_ib(self._ib_fop_quote, symbol, exchange,
                                       expiry, strike, right, multiplier, timeout=15)
        except Exception as e:
            log.debug(f"fop_quote {symbol} {expiry} {strike}{right}: {e}")
            return {"bid": 0, "ask": 0, "volume": 0, "open_interest": 0}

    def _ib_fop_quote(self, symbol, exchange, expiry, strike, right, multiplier):
        from ib_async import FuturesOption
        contract = FuturesOption(
            symbol=symbol, lastTradeDateOrContractMonth=expiry,
            strike=float(strike), right=right, exchange=exchange,
            multiplier=str(int(multiplier)), currency="USD",
        )
        qualified = self.ib.qualifyContracts(contract)
        if not qualified or not getattr(qualified[0], "conId", 0):
            return {"bid": 0, "ask": 0, "volume": 0, "open_interest": 0}
        c = qualified[0]
        # Snapshot-ish: subscribe, wait briefly, read ticker, cancel.
        ticker = self.ib.reqMktData(c, "100,101", snapshot=False, regulatorySnapshot=False)
        self.ib.sleep(2.0)
        bid = float(ticker.bid or 0)
        ask = float(ticker.ask or 0)
        vol = int(ticker.volume or 0)
        # Open interest comes on genericTickList 101 → callOpenInterest / putOpenInterest
        oi = int(getattr(ticker, "callOpenInterest", 0) or
                 getattr(ticker, "putOpenInterest", 0) or 0)
        try:
            self.ib.cancelMktData(c)
        except Exception:
            pass
        return {"bid": bid, "ask": ask, "volume": vol,
                "open_interest": oi, "con_id": c.conId}

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

        # BOTH orders transmit=True. The previous ``tp.transmit=False``
        # pattern is only valid for parent-child bracket orders where the
        # child's submission is gated on the parent transmitting.
        # place_protection_brackets attaches to an EXISTING position —
        # there's no parent — so tp.transmit=False left the TP stuck
        # in "Transmit" status forever. That failed PASS 4's tp_bad
        # check on the very next cycle → re-restore → another pair
        # → another stuck TP → loop. Caught 2026-04-23 as the 4× pairs
        # of TP/SL brackets showing on every single-leg ICT trade.
        tp_order = LimitOrder("SELL", contracts, tp_price)
        tp_order.orderId = self.ib.client.getReqId()
        tp_order.ocaGroup = oca_group
        tp_order.ocaType = 1
        tp_order.tif = "DAY"
        tp_order.transmit = True
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

        # Phase 5 (multi-strategy v2): stamp the clientId that PLACED the
        # bracket. This lets cancel_order_by_id route the cancel directly
        # to the owning pool slot instead of fanning out blindly.
        # See docs/multi_strategy_architecture_v2.md §7 Phase 5.
        try:
            placing_client_id = self.ib.client.clientId
        except Exception:
            placing_client_id = None

        log.info(f"[IB clientId={placing_client_id}] BRACKET {action}: "
                 f"{contracts}x {option_symbol} — "
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
            "client_id": placing_client_id,
        }

        ib_error = self._get_last_error(parent.orderId)
        if ib_error:
            result["ib_error"] = ib_error
            log.warning(f"[IB] Bracket parent {parent.orderId} error: "
                        f"code={ib_error['code']} {ib_error['message']}")

        return result

    # ── Multi-leg orders (Phase 6) ────────────────────────────
    def place_multi_leg_order(self, legs: list[dict],
                               order_ref: str | None = None) -> dict:
        """Submit N legs as independent orders in one OCA group.

        Each leg dict (from LegSpec.__dict__ or equivalent) carries:
            sec_type:  'OPT' | 'FOP' | 'STK'
            symbol:    OCC for options, ticker for STK
            direction: 'LONG' | 'SHORT'
            contracts: int
            # option-only:
            strike, right, expiry, multiplier, exchange, currency
            leg_role, underlying

        All legs share one ocaGroup so the first exit cancels the rest.
        Every leg's ``orderRef`` is stamped with ``order_ref`` for IB↔DB
        correlation (docs/ib_db_correlation.md).

        Returns a dict:
            {
                "oca_group": "MULTILEG-<n>",
                "order_ref": order_ref,
                "legs": [
                    {"leg_index": 0, "symbol": ..., "order_id": ...,
                     "perm_id": ..., "con_id": ..., "status": ...,
                     "fill_price": ..., "client_id": <pool slot>},
                    ...
                ],
                "all_filled": bool,
                "fills_received": int,
                "ib_client_id": <placing clientId — same for all legs>,
            }

        On partial-fill failure, returns the partial result (caller decides
        whether to unwind). See docs/multi_strategy_architecture_v2.md §7
        Phase 6.
        """
        if config.DRY_RUN:
            log.info(f"[DRY RUN] MULTI-LEG: {len(legs)} legs ref={order_ref}")
            return {
                "dry_run": True,
                "oca_group": "DRY-RUN",
                "order_ref": order_ref,
                "legs": [
                    {"leg_index": i, "symbol": l.get("symbol"),
                     "order_id": None, "perm_id": None, "con_id": None,
                     "status": "DryRun", "fill_price": 0.0, "client_id": None}
                    for i, l in enumerate(legs)
                ],
                "all_filled": False,
                "fills_received": 0,
                "ib_client_id": None,
            }
        return self._submit_to_ib(self._ib_place_multi_leg, legs, order_ref)

    def _build_leg_contract(self, leg: dict):
        """Build an IB Contract for a single leg. Runs on IB thread."""
        sec_type = (leg.get("sec_type") or "OPT").upper()
        if sec_type in ("OPT", "FOP"):
            contract = self._occ_to_contract(leg["symbol"])
            if not contract or not contract.conId:
                raise RuntimeError(
                    f"Contract validation failed for {leg.get('symbol')}")
            _check_not_flex(contract, leg["symbol"])
            return contract
        if sec_type == "STK":
            symbol = leg.get("underlying") or leg["symbol"]
            exchange = leg.get("exchange") or "SMART"
            currency = leg.get("currency") or "USD"
            contract = Stock(symbol, exchange, currency)
            try:
                qualified = self.ib.qualifyContracts(contract)
                if qualified:
                    contract = qualified[0]
            except Exception as e:
                log.debug(f"[MULTILEG] qualifyContracts({symbol}) failed: {e}")
            return contract
        raise RuntimeError(f"Unsupported sec_type={sec_type!r} for multi-leg")

    def _ib_place_multi_leg(self, legs: list[dict],
                             order_ref: str | None = None) -> dict:
        """Runs on IB thread. Places each leg as an independent MarketOrder
        in one OCA group and waits up to 10s for fills."""
        try:
            uniq = self.ib.client.getReqId()
        except Exception:
            uniq = id(legs) & 0xFFFFFF
        oca_group = f"MULTILEG-{uniq}"

        try:
            placing_client_id = self.ib.client.clientId
        except Exception:
            placing_client_id = None

        submitted: list[tuple[int, dict, object, object]] = []
        for i, leg in enumerate(legs):
            direction = (leg.get("direction") or "LONG").upper()
            action = "BUY" if direction == "LONG" else "SELL"
            contracts = int(leg.get("contracts") or 1)

            contract = self._build_leg_contract(leg)

            order = MarketOrder(action, contracts)
            order.orderId = self.ib.client.getReqId()
            # NOTE: do NOT stamp ocaGroup/ocaType on ENTRY legs — the
            # earlier code used ocaType=1 ("cancel on fill") which IB
            # applied the moment the first leg filled, cancelling the
            # other 3 legs of an iron condor mid-placement. Caught
            # 2026-04-23 as the AVGO partial-condor bug (1/4 filled,
            # resulting in a naked short call). OCA is the right tool
            # for EXIT coordination (first TP/SL cancels siblings),
            # not for entry. We keep ``oca_group`` as a correlation
            # label in the result dict so downstream tracking works.
            order.tif = "DAY"
            if config.IB_ACCOUNT:
                order.account = config.IB_ACCOUNT
            if order_ref:
                order.orderRef = order_ref

            ib_trade = self.ib.placeOrder(contract, order)
            submitted.append((i, leg, contract, ib_trade))
            log.info(f"[IB clientId={placing_client_id}] MULTILEG leg={i} "
                     f"{action} {contracts}x {leg.get('symbol')} "
                     f"role={leg.get('leg_role') or '—'} oca={oca_group} "
                     f"orderId={order.orderId} ref={order_ref or '—'}")

        # Wait for fills (up to 10s)
        TERMINAL = {"Filled", "Cancelled", "Inactive", "ApiCancelled"}
        for _ in range(20):
            try:
                self.ib.sleep(0.5)
            except Exception:
                break
            if all(t[3].orderStatus.status in TERMINAL for t in submitted):
                break

        legs_result = []
        fills_received = 0
        all_filled = True
        for i, leg, contract, ib_trade in submitted:
            status = ib_trade.orderStatus.status
            fill_price = ib_trade.orderStatus.avgFillPrice or 0.0
            if status == "Filled":
                fills_received += 1
            else:
                all_filled = False
            legs_result.append({
                "leg_index": i,
                "symbol": leg.get("symbol"),
                "leg_role": leg.get("leg_role"),
                "sec_type": leg.get("sec_type"),
                "direction": leg.get("direction"),
                "contracts": int(leg.get("contracts") or 1),
                "order_id": ib_trade.order.orderId,
                "perm_id": ib_trade.order.permId,
                "con_id": getattr(contract, "conId", None),
                "status": status,
                "fill_price": float(fill_price) if fill_price else 0.0,
                "client_id": placing_client_id,
                "oca_group": oca_group,
                "order_ref": order_ref,
                # Carry instrument metadata forward so insert_multi_leg_trade
                # writes strike/right/expiry/underlying on trade_legs.
                # Dropping these caused NULL strikes, which broke the
                # UI leg-drill-down (ENH-047) on multi-leg trades.
                "strike": leg.get("strike"),
                "right": leg.get("right"),
                "expiry": leg.get("expiry"),
                "multiplier": leg.get("multiplier", 100),
                "underlying": leg.get("underlying"),
                "exchange": leg.get("exchange", "SMART"),
                "currency": leg.get("currency", "USD"),
            })

        if not all_filled and fills_received > 0:
            log.error(f"[IB] MULTILEG partial: {fills_received}/{len(legs)} "
                      f"filled oca={oca_group} ref={order_ref} — "
                      f"caller must decide whether to unwind")

        return {
            "oca_group": oca_group,
            "order_ref": order_ref,
            "legs": legs_result,
            "all_filled": all_filled,
            "fills_received": fills_received,
            "ib_client_id": placing_client_id,
        }

    # ── Multi-leg as IB BAG / combo order (ENH-046) ───────────
    def place_combo_order(self, legs: list[dict],
                           order_ref: str | None = None,
                           action: str = "BUY",
                           limit_price: float | None = None) -> dict:
        """Submit N legs as ONE IB Bag/combo order.

        Unlike ``place_multi_leg_order`` (which submits N independent
        MarketOrders — one per leg), this builds a single ``Bag``
        contract with N ``ComboLeg`` entries. IB fills the combo
        atomically at a net price:
          - LimitOrder at ``limit_price`` if provided (recommended
            for real trading — you name your credit/debit)
          - MarketOrder otherwise (fast, but slippage risk on net)

        Benefits over N-independent-orders:
          - TWS shows ONE order that expands to its legs
          - All-or-nothing fill — no partial-condor residual risk
          - One conId for the combo → one bracket at net P&L instead
            of N per-leg brackets firing at random
          - Cleanly maps to the trades envelope ↔ one IB order model

        ``legs`` uses the same dict shape as ``place_multi_leg_order``.
        Returns a superset of that method's result plus ``combo_order_id``
        and ``net_fill_price`` for the envelope-level bracketing logic.

        Status: ENH-046 pilot. NOT wired to the live trade-entry path
        yet — ``TradeEntryManager`` still uses ``place_multi_leg_order``
        by default. Opt in by flipping a strategy config flag.
        """
        if config.DRY_RUN:
            log.info(f"[DRY RUN] COMBO: {len(legs)} legs action={action} "
                     f"limit={limit_price} ref={order_ref}")
            return {
                "dry_run": True,
                "combo_order_id": None,
                "order_ref": order_ref,
                "net_fill_price": 0.0,
                "legs": [
                    {"leg_index": i, "symbol": l.get("symbol"),
                     "order_id": None, "perm_id": None, "con_id": None,
                     "status": "DryRun", "fill_price": 0.0,
                     "client_id": None, "combo": True}
                    for i, l in enumerate(legs)
                ],
                "all_filled": False,
                "fills_received": 0,
                "ib_client_id": None,
            }
        return self._submit_to_ib(self._ib_place_combo, legs, order_ref,
                                   action, limit_price)

    def _ib_place_combo(self, legs: list[dict],
                        order_ref: str | None,
                        action: str,
                        limit_price: float | None) -> dict:
        """Runs on IB thread. Qualifies every leg's contract, builds a
        Bag contract referencing each leg's conId, and submits one order."""
        from ib_async import Contract, ComboLeg

        if not legs:
            raise RuntimeError("place_combo_order requires >= 1 leg")

        # Qualify each leg to get its conId — a Bag needs conIds, not
        # OCC symbols. Reuse _build_leg_contract which handles OPT/FOP/STK.
        leg_contracts: list[tuple[int, dict, object]] = []
        for i, leg in enumerate(legs):
            c = self._build_leg_contract(leg)
            if not getattr(c, "conId", None):
                raise RuntimeError(
                    f"combo leg {i} {leg.get('symbol')!r} — contract "
                    f"qualification returned no conId"
                )
            leg_contracts.append((i, leg, c))

        # Infer the underlying for the Bag header. For options all legs
        # share the same underlying; for defensive mixed cases, take
        # leg 0's. Fall back to the OCC ticker prefix.
        first_leg = legs[0]
        underlying = (first_leg.get("underlying")
                      or (first_leg.get("symbol") or "")[:3])
        currency = first_leg.get("currency", "USD")
        exchange = first_leg.get("exchange", "SMART")

        bag = Contract()
        bag.symbol = underlying
        bag.secType = "BAG"
        bag.currency = currency
        bag.exchange = exchange
        bag.comboLegs = []
        for i, leg, c in leg_contracts:
            combo_leg = ComboLeg()
            combo_leg.conId = int(c.conId)
            combo_leg.ratio = int(leg.get("contracts") or 1)
            leg_dir = (leg.get("direction") or "LONG").upper()
            combo_leg.action = "BUY" if leg_dir == "LONG" else "SELL"
            combo_leg.exchange = leg.get("exchange") or exchange
            combo_leg.openClose = 0  # 0 = same as contract default
            bag.comboLegs.append(combo_leg)

        qty = int(legs[0].get("contracts") or 1)
        # IB slippage fix (2026-04-23): when caller hasn't specified a
        # limit_price AND the auto-limit mode is on, compute a net
        # mid-price from each leg's quote so the combo fills closer to
        # fair value instead of blindly MKT. Falls through to MKT if
        # any leg quote is unavailable or DN_COMBO_AUTO_LIMIT=false.
        if limit_price is None:
            limit_price = _compute_combo_net_limit(
                self, leg_contracts, action, legs
            )
        if limit_price is not None:
            order = LimitOrder(action, qty, float(limit_price))
        else:
            order = MarketOrder(action, qty)
        order.orderId = self.ib.client.getReqId()
        order.tif = "DAY"
        if config.IB_ACCOUNT:
            order.account = config.IB_ACCOUNT
        if order_ref:
            order.orderRef = order_ref

        try:
            placing_client_id = self.ib.client.clientId
        except Exception:
            placing_client_id = None

        ib_trade = self.ib.placeOrder(bag, order)
        log.info(f"[IB clientId={placing_client_id}] COMBO {action} "
                 f"{qty}x {underlying} ({len(legs)} legs) "
                 f"limit={limit_price} orderId={order.orderId} "
                 f"ref={order_ref or '—'}")

        # Wait up to 10s for fill
        TERMINAL = {"Filled", "Cancelled", "Inactive", "ApiCancelled"}
        for _ in range(20):
            try:
                self.ib.sleep(0.5)
            except Exception:
                break
            if ib_trade.orderStatus.status in TERMINAL:
                break

        net_fill = float(ib_trade.orderStatus.avgFillPrice or 0.0)
        status = ib_trade.orderStatus.status
        filled = (status == "Filled")

        # ── ENH-050 — per-leg fill-price recovery chain ─────────
        # IB doesn't always break out per-leg prices on combo fills.
        # Three-stage fallback so every leg gets a sensible price and
        # a ``price_source`` tag for visibility:
        #   1) ib_trade.fills   → price_source='exec'
        #   2) ib.executions()  → price_source='exec'  (broader view)
        #   3) live quote mid   → price_source='quote'
        #   4) proportional     → price_source='proportional'
        leg_fill_prices: dict[int, tuple[float, str]] = {}
        leg_con_ids = {int(c.conId): (i, leg) for i, leg, c in leg_contracts}

        # Stage 1: primary — ib_trade.fills
        for fill in getattr(ib_trade, "fills", []) or []:
            c_id = getattr(fill.contract, "conId", None)
            if c_id is None:
                continue
            c_id = int(c_id)
            if c_id in leg_con_ids and c_id not in leg_fill_prices:
                px = float(fill.execution.avgPrice or 0.0)
                if px > 0:
                    leg_fill_prices[c_id] = (px, "exec")

        # Stage 2: executions stream — richer than fills when Bag routing
        # produced split-leg executions on different venues.
        if any(cid not in leg_fill_prices for cid in leg_con_ids):
            try:
                for exec_row in (self.ib.executions() or []):
                    try:
                        if exec_row.execution.orderId != ib_trade.order.orderId:
                            continue
                        c_id = int(getattr(exec_row.contract, "conId", 0) or 0)
                        if c_id in leg_con_ids and c_id not in leg_fill_prices:
                            px = float(exec_row.execution.avgPrice or 0.0)
                            if px > 0:
                                leg_fill_prices[c_id] = (px, "exec")
                    except Exception:
                        continue
            except Exception as e:
                log.debug(f"[COMBO] executions() fallback failed: {e}")

        # Stage 3: post-fill mid quote for any leg still missing.
        for c_id, (i, leg) in leg_con_ids.items():
            if c_id in leg_fill_prices:
                continue
            sym = leg.get("symbol")
            if not sym:
                continue
            try:
                mid = self.get_option_price(sym)
                if mid and mid > 0:
                    leg_fill_prices[c_id] = (float(mid), "quote")
            except Exception as e:
                log.debug(f"[COMBO] quote fallback for {sym}: {e}")

        # Stage 4: proportional split of the combo net_fill.
        # Only runs if we know the net and some legs are still missing.
        if abs(net_fill) > 0.0 and any(cid not in leg_fill_prices for cid in leg_con_ids):
            missing = [cid for cid in leg_con_ids if cid not in leg_fill_prices]
            share = abs(net_fill) / len(leg_con_ids)   # equal-weight
            for c_id in missing:
                leg_fill_prices[c_id] = (round(share, 4), "proportional")
            log.warning(f"[COMBO] {len(missing)} leg(s) got proportional "
                        f"fallback price ${share:.4f}")

        legs_result = []
        for i, leg, c in leg_contracts:
            # Unwrap (price, source) tuple or default to 0.0 / None
            _entry = leg_fill_prices.get(int(c.conId))
            if _entry is None:
                per_leg_px = 0.0
                leg_price_source = None
            else:
                per_leg_px, leg_price_source = _entry
            legs_result.append({
                "leg_index": i,
                "symbol": leg.get("symbol"),
                "leg_role": leg.get("leg_role"),
                "sec_type": leg.get("sec_type"),
                "direction": leg.get("direction"),
                "contracts": int(leg.get("contracts") or 1),
                "order_id": ib_trade.order.orderId,   # same on every leg
                "perm_id": ib_trade.order.permId,     # same on every leg
                "con_id": int(c.conId),
                "status": status,
                "fill_price": per_leg_px,
                "client_id": placing_client_id,
                "order_ref": order_ref,
                "combo": True,
                # ENH-050 — tag how we arrived at the fill price so the
                # UI + audit trail can flag non-actual prices.
                "price_source": leg_price_source,
                # Instrument metadata so trade_legs gets strike/right/expiry
                # (see combo-path fix for the multi-leg path above).
                "strike": leg.get("strike"),
                "right": leg.get("right"),
                "expiry": leg.get("expiry"),
                "multiplier": leg.get("multiplier", 100),
                "underlying": leg.get("underlying"),
                "exchange": leg.get("exchange", "SMART"),
                "currency": leg.get("currency", "USD"),
            })

        return {
            "combo_order_id": ib_trade.order.orderId,
            "combo_perm_id": ib_trade.order.permId,
            "order_ref": order_ref,
            "net_fill_price": net_fill,
            "legs": legs_result,
            "all_filled": filled,
            "fills_received": len(legs) if filled else 0,
            "ib_client_id": placing_client_id,
        }

    def place_combo_close_order(self, legs: list[dict],
                                  order_ref: str | None = None,
                                  limit_price: float | None = None) -> dict:
        """Close a previously-opened combo position by submitting the
        REVERSE Bag. Every leg's action is flipped (BUY ↔ SELL) so the
        combined order nets out the position in one IB order.

        Uses the same result dict shape as ``place_combo_order`` so the
        exit path can call either side symmetrically.
        """
        reversed_legs: list[dict] = []
        for leg in legs:
            d = dict(leg)
            dir_ = (d.get("direction") or "LONG").upper()
            d["direction"] = "SHORT" if dir_ == "LONG" else "LONG"
            reversed_legs.append(d)
        # IB accepts the Bag "BUY" convention whether it's a net-credit
        # or net-debit close; the comboLegs' per-leg actions carry the
        # real sign. We keep outer action=BUY for consistency.
        return self.place_combo_order(
            reversed_legs, order_ref=order_ref,
            action="BUY", limit_price=limit_price,
        )

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

    def cancel_order_by_id(self, order_id: int,
                           preferred_client_id: int | None = None):
        """Cancel a specific order by orderId, across every client in the pool.

        IB's ``cancelOrder`` only succeeds on the client that PLACED the
        order — all other clients get Error 10147. Fan-out across the
        pool so whichever client owns the order processes it; others
        harmlessly emit 10147 which we swallow.

        Phase 5 (multi-strategy v2): ``preferred_client_id`` lets callers
        route the cancel directly to the pool slot that originally placed
        the order (stamped on ``trades.ib_client_id`` at entry). If the
        preferred slot is still in the pool, we try it FIRST and skip the
        fan-out on success. If it's not present (reconnect/reinit) or the
        cancel raises, we fall through to the legacy fan-out behavior.
        """
        # Preferred-client fast path: try the owning slot first.
        if preferred_client_id is not None and self._pool is not None:
            preferred_conn = None
            for conn in self._pool.all_connections:
                if getattr(conn, "client_id", None) == preferred_client_id:
                    preferred_conn = conn
                    break
            if preferred_conn is not None:
                try:
                    if preferred_conn is self._conn:
                        self._submit_to_ib(self._ib_cancel_single_order,
                                           order_id, timeout=5)
                    else:
                        preferred_conn.submit(
                            self._ib_cancel_single_order_on_conn,
                            preferred_conn.ib, order_id, timeout=5,
                        )
                    log.debug(f"[IB] Cancel orderId={order_id} routed to "
                              f"owning clientId={preferred_client_id} "
                              f"({preferred_conn.label}) — skipping fan-out")
                    return
                except Exception as e:
                    log.debug(f"[IB] Preferred cancel on clientId="
                              f"{preferred_client_id} failed: {e} — "
                              f"falling back to fan-out")
            else:
                log.debug(f"[IB] preferred_client_id={preferred_client_id} "
                          f"not in pool — falling back to fan-out")

        # Legacy fan-out: own connection + every other pool connection.
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

    def cancel_order_by_perm_id(self, perm_id: int,
                                preferred_client_id: int | None = None):
        """Cancel by IB permId — globally unique across all clients.
        Fans out across the pool; the owning client processes the
        cancel, others harmlessly return 10147.

        Phase 5 (multi-strategy v2): ``preferred_client_id`` — same
        contract as ``cancel_order_by_id``. If the owning pool slot is
        present, we submit the cancel there first and skip the fan-out.
        Falls back to fan-out if the slot is missing or the attempt raises.
        """
        if not perm_id:
            return
        if self._pool is None:
            try:
                self._submit_to_ib(self._ib_cancel_by_perm_id_on, self.ib, perm_id, timeout=5)
            except Exception as e:
                log.warning(f"Failed to cancel permId={perm_id}: {e}")
            return

        # Preferred-client fast path
        if preferred_client_id is not None:
            preferred_conn = None
            for conn in self._pool.all_connections:
                if getattr(conn, "client_id", None) == preferred_client_id:
                    preferred_conn = conn
                    break
            if preferred_conn is not None:
                try:
                    preferred_conn.submit(self._ib_cancel_by_perm_id_on,
                                          preferred_conn.ib, perm_id, timeout=5)
                    log.debug(f"[IB] Cancel permId={perm_id} routed to "
                              f"owning clientId={preferred_client_id} "
                              f"({preferred_conn.label}) — skipping fan-out")
                    return
                except Exception as e:
                    log.debug(f"[IB] Preferred cancel permId={perm_id} on "
                              f"clientId={preferred_client_id} failed: {e} — "
                              f"falling back to fan-out")
            else:
                log.debug(f"[IB] preferred_client_id={preferred_client_id} "
                          f"not in pool — falling back to fan-out")

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
        """Return every working order across all clientIds in the account.

        Pool-aware: fans out across every connection and dedupes by permId
        keeping the most-terminal status. Bug caught 2026-04-22 — this
        method was previously single-connection, so brackets placed by
        scanner-A/B/C (clientIds 2/3/4) were invisible to the exit-mgr
        (clientId 1) caller. That made reconcile PASS 4 declare every
        bracket MISSING and trigger spurious bracket restoration (9
        trades got a duplicate OCA group before the fix landed).
        """
        try:
            own = self._submit_to_ib(self._ib_get_all_working_orders_on_conn,
                                      self.ib, timeout=10)
        except Exception as e:
            log.warning(f"Failed to query all working orders (own): {e}")
            own = []

        if self._pool is None:
            return own

        TERMINAL_RANK = {
            "Cancelled": 4, "ApiCancelled": 4, "Inactive": 4, "Filled": 4,
            "PendingCancel": 3, "Submitted": 2, "PreSubmitted": 1,
            "PendingSubmit": 0,
        }
        merged: dict = {}

        def _key(e):
            pid = e.get("permId") or 0
            if pid:
                return ("p", pid)
            return ("o", e.get("clientId") or 0, e.get("orderId"))

        def _merge(e):
            k = _key(e)
            cur = merged.get(k)
            if cur is None:
                merged[k] = e
                return
            cur_rank = TERMINAL_RANK.get(cur.get("status"), 2)
            new_rank = TERMINAL_RANK.get(e.get("status"), 2)
            if new_rank > cur_rank:
                merged[k] = e

        for e in own:
            _merge(e)
        for conn in self._pool.all_connections:
            if conn is self._conn:
                continue
            try:
                rows = conn.submit(self._ib_get_all_working_orders_on_conn,
                                    conn.ib, timeout=5)
            except Exception as e:
                log.debug(f"get_all_working_orders fan-out on {conn.label}: {e}")
                continue
            for row in rows or []:
                _merge(row)
        return list(merged.values())

    def _ib_get_all_working_orders(self) -> list:
        """Backward-compat shim — on-self connection only."""
        return self._ib_get_all_working_orders_on_conn(self.ib)

    @staticmethod
    def _ib_get_all_working_orders_on_conn(ib) -> list:
        """Runs on a specific connection's IB thread."""
        results = []
        for trade in ib.openTrades():
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
                # ENH-062: PASS 6 / orphan-combo cleanup filters on
                # secType to isolate BAG (combo) parents from single-
                # leg OPT brackets. Must be present on every row.
                "secType":   getattr(contract, "secType", "") if contract else "",
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
