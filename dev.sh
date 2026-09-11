#!/bin/bash
# Starts the backend and the Vite dev server together, and stops both on Ctrl-C.
#
# Development deliberately runs two processes: Vite serves the frontend with
# hot reload and proxies /api to the backend. The single-container layout is
# for deployment -- see `docker build` in README.md to run that locally.
set -e

cd "$(dirname "$0")"

export ADMIN_TOKEN="${ADMIN_TOKEN:-local-admin-token}"

if [ ! -d backend/venv ]; then
  echo "-> Creating backend virtualenv..."
  python3 -m venv backend/venv
  ./backend/venv/bin/pip install -q -r backend/requirements.txt
fi

if [ ! -d frontend/node_modules ]; then
  echo "-> Installing frontend dependencies..."
  (cd frontend && npm install)
fi

# Stop both halves when either exits, so Ctrl-C never leaves a stray server
# holding port 8000.
# Each half runs in a subshell, so $! is the subshell's pid and not the server's.
# Killing only that leaves uvicorn and vite orphaned, still holding 8000 and
# 5173 -- which then makes the next run of this script fail on "Address already
# in use". Kill the subshell's children first, then the subshell.
cleanup() {
  trap - EXIT INT TERM
  for PID in "$BACKEND_PID" "$FRONTEND_PID"; do
    [ -n "$PID" ] || continue
    pkill -P "$PID" 2>/dev/null || true
    kill "$PID" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

echo "-> Starting backend on http://127.0.0.1:8000"
(cd backend && ./venv/bin/python main.py) &
BACKEND_PID=$!

echo "-> Starting frontend on http://localhost:5173"
(cd frontend && npm run dev) &
FRONTEND_PID=$!

# Polled rather than `wait -n`, which needs bash 4.3+. macOS ships bash 3.2,
# where `wait -n` fails outright -- the script then exited non-zero and its own
# EXIT trap tore down both servers a second after starting them.
#
# Same semantics: return as soon as either half dies, so the trap can stop the
# other rather than leaving a half-running stack behind.
while kill -0 "$BACKEND_PID" 2>/dev/null && kill -0 "$FRONTEND_PID" 2>/dev/null; do
  sleep 1
done
