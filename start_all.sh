#!/bin/bash
echo "================================================"
echo "  ICT Trading Bot - Starting Dashboard System"
echo "================================================"
echo

cd "$(dirname "$0")"

# Check if sidecar is already running
if curl -s http://localhost:9000/status >/dev/null 2>&1; then
    echo "ERROR: Bot Manager sidecar is already running!"
    echo
    echo "  Status: $(curl -s http://localhost:9000/status)"
    echo
    echo "Please run ./stop_all.sh first to shut down the existing system."
    exit 1
fi

python start_dashboard.py
