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
        else:
            self._send_json({"error": "not found"}, 404)

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
            else:
                exit_code = None
            self._send_json({
                "status": "stopped",
                "pid": None,
                "exit_code": exit_code,
            })

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
