"""
Bot Manager Sidecar — runs on the host machine alongside IB TWS.
Provides HTTP endpoints for the Docker-hosted dashboard to start/stop the bot.

Usage: python bot_manager.py
Listens on port 9000.
"""
import os
import sys
import signal
import subprocess
import logging
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  bot_manager — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# Bot process state
_bot_process = None
_bot_start_time = None

# Test runner state (pytest subprocess)
_test_process = None

# Backtest runner state (backtest_engine subprocess)
_backtest_process = None

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://ict_bot:ict_bot_dev@localhost:5432/ict_bot")
SIDECAR_PORT = int(os.getenv("SIDECAR_PORT", "9000"))


class BotManagerHandler(BaseHTTPRequestHandler):
    """HTTP handler for bot start/stop/status."""

    def _send_json(self, data: dict, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self._send_json({})

    def do_GET(self):
        if self.path == "/status":
            self._handle_status()
        elif self.path == "/health":
            self._send_json({"status": "ok", "service": "bot_manager"})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path == "/start":
            self._handle_start()
        elif self.path == "/stop":
            self._handle_stop()
        elif self.path == "/close-trade":
            self._handle_close_trade()
        elif self.path == "/run-tests":
            self._handle_run_tests()
        elif self.path == "/run-backtest":
            self._handle_run_backtest()
        else:
            self._send_json({"error": "not found"}, 404)

    def _handle_run_backtest(self):
        """Spawn a backtest subprocess. Body:
            {"name": str?, "strategy": "ict", "tickers": ["QQQ"],
             "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD",
             "config": {...}}
        Returns 202 immediately with the new run_id (or sidecar pid if
        run_id can't be determined before spawn — engine creates it)."""
        global _backtest_process
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(content_length)) if content_length > 0 else {}
        except Exception:
            body = {}

        # Validate required fields
        tickers = body.get("tickers")
        if not tickers or not isinstance(tickers, list):
            self._send_json({"error": "tickers (list) required"}, 400)
            return
        for k in ("start_date", "end_date"):
            if not body.get(k):
                self._send_json({"error": f"{k} required"}, 400)
                return

        # Don't stack runs
        if _backtest_process is not None and _backtest_process.poll() is None:
            self._send_json({
                "error": "a backtest is already running",
                "pid": _backtest_process.pid,
            }, 409)
            return

        runner = os.path.join(BOT_DIR, "run_backtest_engine.py")
        if not os.path.exists(runner):
            self._send_json({"error": f"runner not found: {runner}"}, 500)
            return

        args = [PYTHON, runner, json.dumps(body)]
        env = os.environ.copy()
        env["DATABASE_URL"] = DATABASE_URL

        log_file = os.path.join(BOT_DIR, "backtest.log")
        log.info(f"Spawning backtest: strategy={body.get('strategy')} "
                 f"tickers={tickers} {body.get('start_date')}→{body.get('end_date')}")
        try:
            with open(log_file, "w") as f:
                f.write(f"=== backtest started at {datetime.now().isoformat()} ===\n")
                f.write(f"Request: {json.dumps(body)}\n\n")
                f.flush()
                _backtest_process = subprocess.Popen(
                    args, cwd=BOT_DIR, env=env,
                    stdout=f, stderr=subprocess.STDOUT,
                )
        except Exception as e:
            log.error(f"Failed to spawn backtest: {e}")
            self._send_json({"error": f"spawn failed: {e}"}, 500)
            return

        self._send_json({
            "status": "started",
            "pid": _backtest_process.pid,
            "log_file": "backtest.log",
        }, status=202)

    def _handle_run_tests(self):
        """Spawn pytest as a detached subprocess.

        Body: {"suite": "unit" | "integration" | "concurrency" | "all"}
        Returns 202 immediately — the pytest_db_reporter plugin writes
        test_runs/test_results rows as the run progresses, so the UI
        polls /api/test-runs/summary to see the new row appear.
        """
        global _test_process
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(content_length)) if content_length > 0 else {}
        except Exception:
            body = {}

        suite = body.get("suite", "unit").strip()
        # Allow-list of suites → which pytest args to use
        suites = {
            "unit":        ["tests/unit/", "-m", "not integration and not slow"],
            "concurrency": ["tests/unit/", "-m", "concurrency"],
            "integration": ["tests/integration/", "-m", "integration"],
            "all":         ["tests/"],
        }
        if suite not in suites:
            self._send_json({"error": f"unknown suite '{suite}'"}, 400)
            return

        # Don't stack runs — if one is live, reject
        if _test_process is not None and _test_process.poll() is None:
            self._send_json({
                "error": "a test run is already in progress",
                "pid": _test_process.pid,
            }, 409)
            return

        args = [PYTHON, "-m", "pytest", *suites[suite], "-q", "--tb=short"]
        env = os.environ.copy()
        env["PYTEST_DB_REPORT"] = "1"
        env["PYTEST_SUITE"] = suite
        env["PYTEST_TRIGGERED_BY"] = body.get("triggered_by", "dashboard")
        env["DATABASE_URL"] = DATABASE_URL

        log_file = os.path.join(BOT_DIR, "pytest.log")
        log.info(f"Spawning pytest: suite={suite} args={args[3:]}")
        try:
            with open(log_file, "w") as f:
                f.write(f"=== pytest run started at {datetime.now().isoformat()} ===\n")
                f.write(f"Suite: {suite}\nArgs: {' '.join(args)}\n\n")
                f.flush()
                _test_process = subprocess.Popen(
                    args,
                    cwd=BOT_DIR,
                    env=env,
                    stdout=f,
                    stderr=subprocess.STDOUT,
                )
        except Exception as e:
            log.error(f"Failed to spawn pytest: {e}")
            self._send_json({"error": f"spawn failed: {e}"}, 500)
            return

        self._send_json({
            "status": "started",
            "pid": _test_process.pid,
            "suite": suite,
            "log_file": "pytest.log",
        }, status=202)

    def _handle_close_trade(self):
        """Close a trade on IB. Checks position, cancels brackets, sells if still open."""
        content_length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(content_length)) if content_length > 0 else {}

        symbol = body.get("symbol", "")
        ticker = body.get("ticker", "")
        contracts = body.get("contracts", 0)
        direction = body.get("direction", "LONG")

        log.info(f"Close trade request: {ticker} {symbol} {contracts}x {direction}")

        try:
            from ib_async import IB, Stock, Option, MarketOrder
            # Connect a temporary IB client to check and close
            ib = IB()
            ib.connect(host=os.getenv("IB_HOST", "127.0.0.1"),
                       port=int(os.getenv("IB_PORT", "7497")),
                       clientId=99)  # Use clientId 99 to avoid conflict with bot

            # Check if position exists
            positions = ib.positions()
            position = None
            for p in positions:
                local_sym = (p.contract.localSymbol or "").strip().replace(" ", "")
                if symbol.replace(" ", "") in local_sym and abs(p.position) > 0:
                    position = p
                    break

            if not position:
                # Position already closed — find fill price and time from IB
                exit_price = 0
                exit_time = None
                try:
                    fills = ib.fills()
                    for fill in reversed(fills):
                        local_sym = (fill.contract.localSymbol or "").strip().replace(" ", "")
                        if symbol.replace(" ", "") in local_sym:
                            exit_price = float(fill.execution.price)
                            exit_time = str(fill.execution.time) if fill.execution.time else None
                            log.info(f"Found fill for closed position: ${exit_price:.2f} at {exit_time}")
                            break
                except Exception:
                    pass
                ib.disconnect()
                self._send_json({"status": "already_closed", "position_was_open": False,
                                "exit_price": exit_price, "exit_time": exit_time})
                return

            # Cancel any open orders for this symbol
            for trade in ib.openTrades():
                if trade.contract.conId == position.contract.conId:
                    ib.cancelOrder(trade.order)
                    log.info(f"Cancelled order {trade.order.orderId} for {symbol}")

            ib.sleep(1)

            # Check position again after cancels
            positions = ib.positions()
            still_open = False
            for p in positions:
                if p.contract.conId == position.contract.conId and abs(p.position) > 0:
                    still_open = True
                    break

            exit_price = 0
            if still_open:
                # Close with market order
                qty = int(abs(position.position))
                action = "SELL" if position.position > 0 else "BUY"
                position.contract.exchange = "SMART"
                order = MarketOrder(action, qty)
                if os.getenv("IB_ACCOUNT"):
                    order.account = os.getenv("IB_ACCOUNT")
                trade = ib.placeOrder(position.contract, order)
                for _ in range(20):
                    ib.sleep(0.5)
                    if trade.orderStatus.status == "Filled":
                        break
                exit_price = trade.orderStatus.avgFillPrice
                # Get fill time
                fill_time = None
                if trade.fills:
                    fill_time = str(trade.fills[-1].execution.time)
                log.info(f"Closed {ticker}: {action} {qty}x @ ${exit_price:.2f} at {fill_time}")
            else:
                # Position was closed by bracket before our cancel arrived
                exit_price = position.marketPrice if hasattr(position, 'marketPrice') else 0
                fill_time = None

            ib.disconnect()
            self._send_json({
                "status": "closed",
                "position_was_open": still_open,
                "exit_price": exit_price,
                "exit_time": fill_time,
            })

        except Exception as e:
            log.error(f"Close trade failed: {e}")
            self._send_json({"error": str(e)}, 500)

    def _handle_scan_control(self, action: str):
        global _bot_process
    def _handle_status(self):
        global _bot_process, _bot_start_time
        # Sidecar only knows if process is alive — DB has the real state
        if _bot_process and _bot_process.poll() is None:
            self._send_json({
                "status": "running",
                "scans_active": False,  # DB has the real state
                "pid": _bot_process.pid,
                "started_at": _bot_start_time.isoformat() if _bot_start_time else None,
            })
        else:
            if _bot_process:
                exit_code = _bot_process.returncode
                _bot_process = None
                _bot_start_time = None
                # Bot just died — reconcile the DB so /api/bot/status
                # doesn't keep reporting "running". Best-effort; if DB
                # is unavailable we don't block the response.
                self._heal_db_on_exit(exit_code)
            else:
                exit_code = None
            self._send_json({
                "status": "stopped",
                "pid": None,
                "exit_code": exit_code,
            })

    def _heal_db_on_exit(self, exit_code):
        """Mark bot_state as stopped when the sidecar detects its child
        process has exited (crashed or finished). Called once at the
        transition from 'running' → 'stopped'."""
        try:
            import psycopg2
            # Parse DATABASE_URL → connection params minimally
            conn = psycopg2.connect(DATABASE_URL)
            try:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE bot_state SET status='stopped', pid=NULL, "
                    "ib_connected=FALSE, scans_active=FALSE, "
                    "stopped_at=NOW(), "
                    "last_error = COALESCE(SUBSTRING(last_error FOR 400), '') "
                    "             || ' | auto-healed: bot process exited' "
                    "             || CASE WHEN %s IS NULL THEN '' "
                    "                ELSE ' (exit=' || %s || ')' END "
                    "WHERE status = 'running'",
                    (exit_code, exit_code),
                )
                conn.commit()
                if cur.rowcount > 0:
                    log.warning(
                        f"bot_state auto-healed: process exited "
                        f"(exit_code={exit_code})"
                    )
            finally:
                conn.close()
        except Exception as e:
            log.debug(f"could not auto-heal bot_state on exit: {e}")

    def _handle_start(self):
        global _bot_process, _bot_start_time
        # Check if already running
        if _bot_process and _bot_process.poll() is None:
            self._send_json({"status": "already_running", "pid": _bot_process.pid}, 409)
            return

        log.info("Starting bot...")
        env = os.environ.copy()
        env["DATABASE_URL"] = DATABASE_URL

        # Start bot as subprocess
        log_file = os.path.join(BOT_DIR, "bot_stdout.log")
        _bot_process = subprocess.Popen(
            [PYTHON, "main.py"],
            cwd=BOT_DIR,
            env=env,
            stdout=open(log_file, "a"),
            stderr=subprocess.STDOUT,
        )

        # Wait for bot to connect to IB (takes ~5-7s)
        import time
        time.sleep(8)

        if _bot_process.poll() is not None:
            # Bot died within 3 seconds — read last lines of log for error
            exit_code = _bot_process.returncode
            error_msg = ""
            try:
                with open(log_file, "r") as f:
                    lines = f.readlines()
                    # Get last 5 non-empty lines
                    last_lines = [l.strip() for l in lines[-10:] if l.strip()]
                    error_msg = "\n".join(last_lines[-5:])
            except Exception:
                error_msg = f"Bot crashed with exit code {exit_code}"
            _bot_process = None
            log.error(f"Bot crashed on startup: {error_msg}")
            self._send_json({
                "status": "failed",
                "error": error_msg,
                "exit_code": exit_code,
            }, 500)
            return

        _bot_start_time = datetime.now()
        log.info(f"Bot started — PID: {_bot_process.pid}")

        self._send_json({
            "status": "started",
            "pid": _bot_process.pid,
        })

    def _handle_stop(self):
        global _bot_process, _bot_start_time
        if not _bot_process or _bot_process.poll() is not None:
            self._send_json({"status": "not_running"}, 400)
            return

        pid = _bot_process.pid
        log.info(f"Stopping bot (PID: {pid})...")

        # Write stop file for graceful shutdown (works on Windows)
        stop_file = os.path.join(BOT_DIR, ".bot_stop")
        try:
            with open(stop_file, "w") as f:
                f.write("stop")
            log.info("Stop file written — waiting for bot to shut down gracefully...")

            # Wait up to 15 seconds for graceful exit
            try:
                _bot_process.wait(timeout=15)
                log.info("Bot exited gracefully")
            except subprocess.TimeoutExpired:
                log.warning("Bot didn't stop in 15s — force killing")
                _bot_process.kill()
                _bot_process.wait(timeout=5)
        except Exception as e:
            log.error(f"Error stopping bot: {e}")
            # Fallback: force kill
            try:
                _bot_process.kill()
                _bot_process.wait(timeout=5)
            except Exception:
                pass

        exit_code = _bot_process.returncode
        _bot_process = None
        _bot_start_time = None
        log.info(f"Bot stopped (exit code: {exit_code})")

        self._send_json({
            "status": "stopped",
            "pid": pid,
            "exit_code": exit_code,
        })

    def log_message(self, format, *args):
        """Suppress default HTTP request logging — we use our own logger."""
        log.debug(f"{self.client_address[0]} {args[0]}")


def main():
    print("=" * 50)
    print("  ICT Trading Bot — Manager Sidecar")
    print(f"  Listening on port {SIDECAR_PORT}")
    print(f"  Bot directory: {BOT_DIR}")
    print(f"  Python: {PYTHON}")
    print(f"  DATABASE_URL: {DATABASE_URL.split('@')[-1]}")
    print("=" * 50)
    print()
    print("Endpoints:")
    print(f"  GET  http://localhost:{SIDECAR_PORT}/status  — bot status")
    print(f"  POST http://localhost:{SIDECAR_PORT}/start   — start bot")
    print(f"  POST http://localhost:{SIDECAR_PORT}/stop    — stop bot")
    print(f"  GET  http://localhost:{SIDECAR_PORT}/health  — sidecar health")
    print()

    server = HTTPServer(("0.0.0.0", SIDECAR_PORT), BotManagerHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Sidecar shutting down...")
        # Stop bot if running
        if _bot_process and _bot_process.poll() is None:
            log.info("Stopping bot before exit...")
            _bot_process.terminate()
            _bot_process.wait(timeout=10)
        server.server_close()


if __name__ == "__main__":
    main()
