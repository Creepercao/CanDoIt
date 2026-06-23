#!/bin/bash
set -e

echo "============================================"
echo "  Multi-Agent Platform - Dev Mode"
echo "============================================"
echo ""

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Install backend deps
echo "[1/3] Installing backend dependencies..."
cd "$SCRIPT_DIR"
pip install -r backend/requirements.txt -q

# Install frontend deps
echo "[2/3] Installing frontend dependencies..."
cd "$SCRIPT_DIR/frontend"
npm install --silent

# Start both
echo "[3/3] Starting servers..."
cd "$SCRIPT_DIR"

# Start backend in background
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload &
BACKEND_PID=$!

# Start frontend
cd "$SCRIPT_DIR/frontend"
npx vite --host &
FRONTEND_PID=$!

echo ""
echo "Backend:  http://localhost:8000"
echo "Frontend: http://localhost:3000"
echo ""
echo "Press Ctrl+C to stop both servers."

trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; exit 0" INT TERM
wait
