"""
Interactive Brokers Client — thin facade composing mixin modules.

Architecture: IB event loop runs on a dedicated thread. All IB API calls
are dispatched via a queue and executed on that thread.

Split into focused modules (ARCH-003 Phase 2):
- ib_market_data.py — prices, greeks, VIX, contract validation
- ib_orders.py      — order placement, brackets, cancellation, queries
- ib_positions.py   — position queries, fills, reconciliation support

IBClient inherits from all three mixins + the core IBClientCore below.
Public API is unchanged from the pre-split monolithic ib_client.py.
"""
import logging
import threading
import queue

from ib_async import IB

import config
from broker.ib_market_data import IBMarketDataMixin
from broker.ib_orders import IBOrdersMixin, _check_not_flex  # noqa: F401 (re-export)
from broker.ib_positions import IBPositionsMixin

log = logging.getLogger(__name__)


class IBClientCore:
    """
    Connection lifecycle, error handling, and IB-thread dispatch.

    Two modes:
    1. Pool mode: pass an IBConnection + shared cache (preferred)
    2. Legacy mode: no args — creates its own IB() and queues (backwards compatible)
    """

    _IB_INFO_CODES = {2104, 2106, 2107, 2108, 2119, 2158}
    _IB_ACTIONABLE_CODES = {104, 110, 125, 135, 161, 201, 202, 203, 399, 10147}
    _IB_CRITICAL_CODES = {201, 202, 203}

    def __init__(self, connection=None, contract_cache=None, cache_lock=None):
        if connection is not None:
            # ── Pool mode: use provided IBConnection ──
            self._conn = connection
            self.ib = connection.ib
            self._contract_cache = contract_cache if contract_cache is not None else {}
            self._cache_lock = cache_lock if cache_lock is not None else threading.Lock()
            self._connected = connection.connected
            self._pool_mode = True
        else:
            # ── Legacy mode: standalone IB connection ──
            self._conn = None
            self.ib = IB()
            self._contract_cache = {}
            self._cache_lock = threading.Lock()
            self._connected = False
            self._pool_mode = False
            self._order_queue = queue.Queue()
            self._priority_queue = queue.Queue()
            self._last_errors = {}
            self._last_errors_lock = threading.Lock()

    # ── Connection ────────────────────────────────────────────
    def connect(self):
        """Connect to IB. Only used in legacy mode — pool mode connects via pool."""
        if self._pool_mode:
            raise RuntimeError("IBClient in pool mode — use pool.connect_all() instead")
        log.info(f"Connecting to Interactive Brokers at {config.IB_HOST}:{config.IB_PORT} "
                 f"(clientId={config.IB_CLIENT_ID})...")
        self.ib.connect(
            host=config.IB_HOST,
            port=config.IB_PORT,
            clientId=config.IB_CLIENT_ID,
            readonly=config.DRY_RUN,
        )
        accounts = self.ib.managedAccounts()
        if config.IB_ACCOUNT:
            if config.IB_ACCOUNT not in accounts:
                log.warning(f"Configured account {config.IB_ACCOUNT} not found in {accounts}")
        log.info(f"Connected to IB — accounts: {accounts}")
        self._register_error_handler()
        self._connected = True

    # ── IB Error Event Handling (legacy mode only) ────────────
    def _register_error_handler(self):
        if self._pool_mode:
            return  # Pool connections register their own handlers
        self.ib.errorEvent += self._on_ib_error
        log.info("IB errorEvent handler registered")

    def _on_ib_error(self, reqId, errorCode, errorString, contract):
        """Legacy mode error handler. Non-blocking — log only, no DB writes."""
        if errorCode in self._IB_INFO_CODES:
            return
        if errorCode in self._IB_ACTIONABLE_CODES:
            with self._last_errors_lock:
                self._last_errors[reqId] = {
                    "code": errorCode, "message": errorString,
                    "reqId": reqId, "contract": str(contract) if contract else None,
                }
        if errorCode in self._IB_CRITICAL_CODES:
            log.error(f"[IB ERROR] reqId={reqId} code={errorCode}: {errorString}")
        else:
            log.warning(f"[IB ERROR] reqId={reqId} code={errorCode}: {errorString}")

    def _get_last_error(self, order_id: int) -> dict | None:
        """Retrieve and remove the last captured IB error for an order_id."""
        if self._pool_mode:
            return self._conn.get_last_error(order_id)
        with self._last_errors_lock:
            return self._last_errors.pop(order_id, None)

    # ── IB Thread Dispatch ────────────────────────────────────
    def process_orders(self):
        """Legacy mode only: process queues on the main thread."""
        if self._pool_mode:
            return  # Pool connections have their own event loops
        while not self._priority_queue.empty():
            try:
                func, args, result_event, result_holder = self._priority_queue.get_nowait()
                try:
                    result_holder["value"] = func(*args)
                except Exception as e:
                    result_holder["error"] = e
                finally:
                    result_event.set()
            except queue.Empty:
                break
        if not self._order_queue.empty():
            try:
                func, args, result_event, result_holder = self._order_queue.get_nowait()
                try:
                    result_holder["value"] = func(*args)
                except Exception as e:
                    result_holder["error"] = e
                finally:
                    result_event.set()
            except queue.Empty:
                pass
        self.ib.sleep(0.1)

    def _submit_to_ib(self, func, *args, timeout=60, priority=False):
        """Submit a function to run on the IB event loop thread."""
        if self._pool_mode:
            return self._conn.submit(func, *args, timeout=timeout)
        # Legacy mode: use internal queues
        result_event = threading.Event()
        result_holder = {}
        q = self._priority_queue if priority else self._order_queue
        q.put((func, args, result_event, result_holder))
        if not result_event.wait(timeout=timeout):
            raise TimeoutError(f"IB call timed out after {timeout}s: {func.__name__}")
        if "error" in result_holder:
            raise result_holder["error"]
        return result_holder.get("value")


class IBClient(IBMarketDataMixin, IBOrdersMixin, IBPositionsMixin, IBClientCore):
    """
    Public IB API client facade.

    Composed from mixins — all public methods are unchanged from the
    pre-refactor monolithic ib_client.py. Importers don't need to change.
    """
    pass
