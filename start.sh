#!/bin/bash
# ─────────────────────────────────────────────────
#  Pocket Option AI Bot — start all services
# ─────────────────────────────────────────────────

cd "$(dirname "$0")"

echo "=== Stopping any existing processes ==="
lsof +D "$(pwd)" 2>/dev/null | grep "Python" | awk '{print $2}' | sort -u | xargs kill -9 2>/dev/null
lsof -i :8000 -i :5173 2>/dev/null | grep LISTEN | awk '{print $2}' | xargs kill -9 2>/dev/null
pkill -9 -f "uvicorn.*dashboard" 2>/dev/null
pkill -9 -f "playwright/driver/node" 2>/dev/null
sleep 2

echo "=== Starting Backend (FastAPI :8000) ==="
source venv/bin/activate
nohup uvicorn dashboard.app:app \
    --host 127.0.0.1 \
    --port 8000 \
    > /tmp/backend.log 2>&1 &
BACKEND_PID=$!
echo "Backend PID: $BACKEND_PID"

echo "=== Starting Frontend (Vite :5173) ==="
cd dashboard
nohup npm run dev -- --port 5173 \
    > /tmp/frontend.log 2>&1 &
FRONTEND_PID=$!
cd ..
echo "Frontend PID: $FRONTEND_PID"

# Wait for backend to be ready
sleep 4
STATUS=$(curl -s http://127.0.0.1:8000/status 2>/dev/null)
if [ -z "$STATUS" ]; then
    echo "⚠️  Backend not responding — check /tmp/backend.log"
else
    echo "✅ Backend: $STATUS"
fi

echo ""
echo "=== Starting Bot ==="
source venv/bin/activate
nohup python -u main.py \
    > /tmp/bot.log 2>&1 &
BOT_PID=$!
echo "Bot PID: $BOT_PID"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  All services started"
echo "  Dashboard : http://localhost:5173"
echo "  API       : http://127.0.0.1:8000"
echo ""
echo "  Logs:"
echo "    tail -f /tmp/bot.log"
echo "    tail -f /tmp/backend.log"
echo "    tail -f /tmp/frontend.log"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
