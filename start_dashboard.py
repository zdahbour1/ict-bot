"""
Dashboard Launcher — starts everything with one command.

1. Starts Docker Compose (PostgreSQL + API + Frontend + pgAdmin)
2. Starts the Bot Manager sidecar (for UI bot start/stop control)

Usage: python start_dashboard.py
"""
import os
import sys
import subprocess
import time
import signal

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable

sidecar_process = None


def start_docker():
    """Start Docker Compose services."""
    print("[1/2] Starting Docker services (PostgreSQL, API, Frontend, pgAdmin)...")
    result = subprocess.run(
        ["docker", "compose", "up", "-d", "--build"],
        cwd=SCRIPT_DIR,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  ERROR: Docker failed to start:\n{result.stderr}")
        sys.exit(1)
    print("  Docker services started.")


def start_sidecar():
    """Start the bot manager sidecar."""
    global sidecar_process
    print("[2/2] Starting Bot Manager sidecar (port 9000)...")
    env = os.environ.copy()
    env["DATABASE_URL"] = os.getenv(
        "DATABASE_URL",
        "postgresql://ict_bot:ict_bot_dev@localhost:5432/ict_bot"
    )
    sidecar_process = subprocess.Popen(
        [PYTHON, "bot_manager.py"],
        cwd=SCRIPT_DIR,
        env=env,
    )
    time.sleep(2)
    if sidecar_process.poll() is not None:
        print("  ERROR: Sidecar failed to start")
        sys.exit(1)
    print("  Bot Manager sidecar started.")


def print_urls():
    print()
    print("=" * 55)
    print("  ICT Trading Bot Dashboard — Ready!")
    print("=" * 55)
    print()
    print("  Dashboard:    http://localhost")
    print("  API Docs:     http://localhost:8000/docs")
    print("  pgAdmin:      http://localhost:5050")
    print("  Bot Manager:  http://localhost:9000/status")
    print()
    print("  To start the bot: click 'Start Bot' in the dashboard")
    print("  To stop everything: press Ctrl+C here")
    print()
    print("=" * 55)


def shutdown(sig=None, frame=None):
    global sidecar_process
    print("\nShutting down...")
    if sidecar_process and sidecar_process.poll() is None:
        print("  Stopping sidecar...")
        sidecar_process.terminate()
        sidecar_process.wait(timeout=10)
    print("  Stopping Docker services...")
    subprocess.run(["docker", "compose", "down"], cwd=SCRIPT_DIR, capture_output=True)
    print("  Done.")
    sys.exit(0)


def check_already_running():
    """Check if the sidecar is already running."""
    import urllib.request
    try:
        resp = urllib.request.urlopen("http://localhost:9000/status", timeout=3)
        import json
        status = json.loads(resp.read())
        bot_status = status.get("status", "unknown")
        bot_pid = status.get("pid")
        print("ERROR: Bot Manager sidecar is already running!")
        print()
        print(f"  Bot status: {bot_status}")
        if bot_pid:
            print(f"  Bot PID:    {bot_pid}")
        print()
        print("Please run stop_all first to shut down the existing system.")
        sys.exit(1)
    except Exception:
        pass  # Not running — good


if __name__ == "__main__":
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    check_already_running()
    start_docker()
    start_sidecar()
    print_urls()

    # Keep running until Ctrl+C
    try:
        while True:
            # Check sidecar health
            if sidecar_process and sidecar_process.poll() is not None:
                print("WARNING: Sidecar died, restarting...")
                start_sidecar()
            time.sleep(5)
    except KeyboardInterrupt:
        shutdown()
