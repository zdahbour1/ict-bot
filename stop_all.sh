#!/bin/bash
echo "================================================"
echo "  ICT Trading Bot - Stopping All Services"
echo "================================================"
echo

cd "$(dirname "$0")"

echo "[1/3] Stopping bot via sidecar..."
curl -s -X POST http://localhost:9000/stop 2>/dev/null || true
echo

echo "[2/3] Stopping sidecar and bot processes..."
pkill -f "bot_manager.py" 2>/dev/null || true
pkill -f "python main.py" 2>/dev/null || true
sleep 2

echo "[3/3] Stopping Docker services..."
docker compose down
echo
echo "All services stopped."
