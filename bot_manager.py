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
        else:
            self._send_json({"error": "not found"}, 404)

    def _handle_status(self):
        global _bot_process, _bot_start_time
        if _bot_process and _bot_process.poll() is None:
            self._send_json({
                "status": "running",
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
        _bot_process = subprocess.Popen(
            [PYTHON, "main.py"],
            cwd=BOT_DIR,
            env=env,
            stdout=open(os.path.join(BOT_DIR, "bot_stdout.log"), "a"),
            stderr=subprocess.STDOUT,
        )
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

        # Send SIGTERM for graceful shutdown
        try:
            _bot_process.terminate()
            try:
                _bot_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                log.warning("Bot didn't stop in 10s — killing")
                _bot_process.kill()
                _bot_process.wait(timeout=5)
        except Exception as e:
            log.error(f"Error stopping bot: {e}")

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
