#!/bin/bash
# ─────────────────────────────────────────────────
#  Pocket Option AI Bot — stop all services
# ─────────────────────────────────────────────────

cd "$(dirname "$0")"

echo "=== Stopping all processes ==="

lsof +D "$(pwd)" 2>/dev/null | grep "Python" | awk '{print $2}' | sort -u | xargs kill -9 2>/dev/null
ps aux | grep "playwright/driver/node" | grep -v grep | awk '{print $2}' | xargs kill -9 2>/dev/null
lsof -i :8000 -i :5173 2>/dev/null | grep LISTEN | awk '{print $2}' | xargs kill -9 2>/dev/null
pkill -9 -f "uvicorn" 2>/dev/null
pkill -9 -f "vite" 2>/dev/null
pkill -9 -f "node.*dashboard" 2>/dev/null

sleep 2

# Verify
REMAINING_PORTS=$(lsof -i :8000 -i :5173 2>/dev/null | grep LISTEN)
REMAINING_PY=$(lsof +D "$(pwd)" 2>/dev/null | grep "Python" | awk '{print $2}' | sort -u)

if [ -z "$REMAINING_PORTS" ] && [ -z "$REMAINING_PY" ]; then
    echo "✅ All processes stopped. Ports 8000 and 5173 are free."
else
    [ -n "$REMAINING_PORTS" ] && echo "⚠️  Still on ports: $REMAINING_PORTS"
    [ -n "$REMAINING_PY"    ] && echo "⚠️  Still running:  $REMAINING_PY"
fi
