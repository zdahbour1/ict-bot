"""
IB Connection Pool — Manages multiple IB connections for parallel execution.

Architecture:
  Connection 0 (clientId base):     Exit Manager (dedicated)
  Connection 1 (clientId base+1):   Scanner Group A
  Connection 2 (clientId base+2):   Scanner Group B

Each connection has its own IB() instance, event loop thread, and work queue.
Contract cache is shared across all connections (thread-safe).
"""
import logging
import threading
import queue

from ib_async import IB

import config

log = logging.getLogger(__name__)


class IBConnection:
    """
    Single IB connection with its own event loop thread and work queue.

    Each IBConnection is self-contained: its own IB() instance, its own
    queue, its own processing thread. No shared mutable state except
    the contract cache (injected, protected by lock).
    """

    # IB informational codes — suppress from error logging
    _IB_INFO_CODES = {2104, 2106, 2107, 2108, 2119, 2158}
    _IB_ACTIONABLE_CODES = {104, 110, 125, 135, 161, 201, 202, 203, 399, 10147}
    _IB_CRITICAL_CODES = {201, 202, 203}

    def __init__(self, client_id: int, label: str = ""):
        self.ib = IB()
        self.client_id = client_id
        self.label = label or f"conn-{client_id}"
        self._queue = queue.Queue()
        self._connected = False
        self._thread = None
        self._stop_event = threading.Event()

        # IB error tracking: order_id → {code, message, reqId, contract}
        self._last_errors = {}
        self._last_errors_lock = threading.Lock()

    def start(self):
        """Start the connection thread: connects to IB AND runs event loop.

        CRITICAL: ib_async ties the asyncio event loop to the thread that
        calls ib.connect(). Both connect() and the event loop (ib.sleep)
        MUST run on the same thread. That's why we do both in _loop().
        """
        self._ready_event = threading.Event()
        self._connect_error = None
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name=f"ib-{self.label}"
        )
        self._thread.start()

        # Wait for connection to establish (or fail)
        if not self._ready_event.wait(timeout=30):
            raise RuntimeError(f"[{self.label}] Connection timed out after 30s")
        if self._connect_error:
            raise self._connect_error
        log.info(f"[{self.label}] Connection thread started and connected")

    def stop(self):
        """Stop the event loop and disconnect."""
        self._stop_event.set()
        self._connected = False
        try:
            self.ib.disconnect()
        except Exception:
            pass

    @property
    def connected(self) -> bool:
        return self._connected and self.ib.isConnected()

    def _loop(self):
        """Connect to IB, then process queue + pump events.

        Both connect and event processing happen on THIS thread so the
        asyncio event loop stays on the same thread throughout.
        """
        # Step 1: Connect on this thread
        try:
            log.info(f"[{self.label}] Connecting to IB at {config.IB_HOST}:{config.IB_PORT} "
                     f"(clientId={self.client_id})...")
            self.ib.connect(
                host=config.IB_HOST,
                port=config.IB_PORT,
                clientId=self.client_id,
                readonly=config.DRY_RUN,
            )
            self.ib.errorEvent += self._on_ib_error
            self._connected = True
            accounts = self.ib.managedAccounts()
            log.info(f"[{self.label}] Connected — clientId={self.client_id}, accounts={accounts}")
        except Exception as e:
            log.error(f"[{self.label}] Connection failed: {e}")
            self._connect_error = e
            self._ready_event.set()
            return

        # Signal that connection is ready
        self._ready_event.set()

        # Step 2: Event loop — process queue items + pump IB events
        while not self._stop_event.is_set():
            try:
                self._process_queue()
                self.ib.sleep(0.1)
            except Exception as e:
                if not self._stop_event.is_set():
                    log.error(f"[{self.label}] Event loop error: {e}")

    def _process_queue(self):
        """Process all pending items in the queue."""
        while not self._queue.empty():
            try:
                func, args, result_event, result_holder = self._queue.get_nowait()
                try:
                    result_holder["value"] = func(*args)
                except Exception as e:
                    result_holder["error"] = e
                finally:
                    result_event.set()
            except queue.Empty:
                break

    def submit(self, func, *args, timeout=60):
        """
        Submit a function to run on this connection's IB thread.
        Blocks the calling thread until the result is ready or timeout.

        This is the ONLY way to call IB API methods safely from other threads.
        """
        if not self._connected:
            raise RuntimeError(f"[{self.label}] Not connected to IB")

        result_event = threading.Event()
        result_holder = {}
        self._queue.put((func, args, result_event, result_holder))

        if not result_event.wait(timeout=timeout):
            raise TimeoutError(f"[{self.label}] IB call timed out after {timeout}s: {func.__name__}")

        if "error" in result_holder:
            raise result_holder["error"]

        return result_holder.get("value")

    # ── IB Error Handler (non-blocking) ──────────────────────
    def _on_ib_error(self, reqId, errorCode, errorString, contract):
        """Called by ib_async on every IB error/warning.
        CRITICAL: runs on IB event loop thread — MUST NOT block."""
        if errorCode in self._IB_INFO_CODES:
            return

        if errorCode in self._IB_ACTIONABLE_CODES:
            with self._last_errors_lock:
                self._last_errors[reqId] = {
                    "code": errorCode,
                    "message": errorString,
                    "reqId": reqId,
                    "contract": str(contract) if contract else None,
                }

        if errorCode in self._IB_CRITICAL_CODES:
            log.error(f"[{self.label} IB ERROR] reqId={reqId} code={errorCode}: {errorString}")
        else:
            log.warning(f"[{self.label} IB] reqId={reqId} code={errorCode}: {errorString}")

    def get_last_error(self, order_id: int) -> dict | None:
        """Retrieve and remove the last captured IB error for an order_id."""
        with self._last_errors_lock:
            return self._last_errors.pop(order_id, None)


class IBConnectionPool:
    """
    Manages multiple IB connections for parallel order execution.

    Connections:
      - exit_conn: dedicated for exit manager (batch pricing, SL updates, positions)
      - scanner_conns[]: shared among scanner threads (order placement, data)

    Contract cache is shared across all connections (thread-safe).
    """

    def __init__(self, num_scanner_connections: int = 2):
        base_id = config.IB_CLIENT_ID

        self.exit_conn = IBConnection(
            client_id=base_id,
            label="exit-mgr",
        )
        self.scanner_conns = [
            IBConnection(
                client_id=base_id + i + 1,
                label=f"scanner-{chr(65 + i)}",  # scanner-A, scanner-B, etc.
            )
            for i in range(num_scanner_connections)
        ]

        # Shared contract cache (thread-safe)
        self._contract_cache = {}
        self._cache_lock = threading.Lock()

    @property
    def contract_cache(self) -> dict:
        return self._contract_cache

    @property
    def cache_lock(self) -> threading.Lock:
        return self._cache_lock

    @property
    def all_connections(self) -> list:
        return [self.exit_conn] + self.scanner_conns

    def get_scanner_connection(self, ticker: str) -> IBConnection:
        """Deterministic mapping: ticker → scanner connection.
        Same ticker always maps to same connection for cache locality."""
        idx = hash(ticker) % len(self.scanner_conns)
        return self.scanner_conns[idx]

    def start_all(self):
        """Start all connections (connect + event loop on same thread each).

        Connections are started sequentially to avoid overwhelming IB.
        Each connection blocks until connected before starting the next.
        """
        for conn in self.all_connections:
            conn.start()  # Blocks until connected
        log.info(f"IBConnectionPool: {len(self.all_connections)} connections active "
                 f"(1 exit + {len(self.scanner_conns)} scanner)")

    def stop_all(self):
        """Stop all connections."""
        for conn in self.all_connections:
            conn.stop()
        log.info("IBConnectionPool: all connections stopped")

    def all_connected(self) -> bool:
        """Check if all connections are alive."""
        return all(conn.connected for conn in self.all_connections)
