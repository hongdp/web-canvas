#!/bin/bash

# Exit on error
set -e

# Change directory to the workspace root where start.sh is located
cd "$(dirname "$0")"

LOG_FILE="app.log"

show_help() {
  echo "Usage: ./start.sh [options]"
  echo ""
  echo "Options:"
  echo "  --storage-dir, --storage_dir <path>   Set the storage directory"
  echo "  --host                                Expose server to local network (default)"
  echo "  --no-host                             Only bind to localhost"
  echo "  -d, --daemon                          Run in background (persistent after logout)"
  echo "  --stop                                Stop any running backend/frontend servers"
  echo "  --status                              Check if servers are currently running"
  echo "  --logs                                View the server log output"
  echo "  -h, --help                            Show this help menu"
}

# 1. Handle commands that don't start the server
case "$1" in
  --stop)
    echo "Stopping Web Canvas servers..."
    pkill -f "scripts/start-server.js" || true
    pkill -f "scripts/api_server.py" || true
    echo "Servers stopped."
    exit 0
    ;;
  --status)
    PID_JS=$(pgrep -f "scripts/start-server.js" || true)
    PID_PY=$(pgrep -f "scripts/api_server.py" || true)
    if [ -n "$PID_JS" ] || [ -n "$PID_PY" ]; then
      echo "Web Canvas servers are RUNNING:"
      [ -n "$PID_JS" ] && echo "  - Orchestration Server (Node/Vite) PID: $PID_JS"
      [ -n "$PID_PY" ] && echo "  - API Backend (Python) PID: $PID_PY"
    else
      echo "Web Canvas servers are STOPPED."
    fi
    exit 0
    ;;
  --logs)
    if [ -f "$LOG_FILE" ]; then
      tail -n 50 -f "$LOG_FILE"
    else
      echo "No log file found at $LOG_FILE. Start the server with --daemon first."
    fi
    exit 0
    ;;
  -h|--help)
    show_help
    exit 0
    ;;
esac

# 2. Stop any existing running processes to release ports
echo "=================================================="
echo " Stopping existing Web Canvas servers..."
pkill -f "scripts/start-server.js" || true
pkill -f "scripts/api_server.py" || true
sleep 1

# 3. Determine configuration
STORAGE_DIR=""
HOST="--host"
DAEMON=false

# Load default storage directory from .env if it exists
if [ -f .env ]; then
  ENV_DIR=$(grep -E "^VITE_STORAGE_DIR=" .env | cut -d'=' -f2-)
  if [ -n "$ENV_DIR" ]; then
    STORAGE_DIR="$ENV_DIR"
  fi
fi

# Parse options
while [[ $# -gt 0 ]]; do
  case $1 in
    --storage-dir|--storage_dir)
      STORAGE_DIR="$2"
      shift 2
      ;;
    --no-host)
      HOST=""
      shift
      ;;
    --host)
      HOST="--host"
      shift
      ;;
    -d|--daemon)
      DAEMON=true
      shift
      ;;
    *)
      echo "Unknown option: $1"
      show_help
      exit 1
      ;;
  esac
done

if [ -z "$STORAGE_DIR" ]; then
  STORAGE_DIR="storage"
fi

echo "Storage Directory: $STORAGE_DIR"
if [ -n "$HOST" ]; then
  echo "Exposing server to local network: Yes"
else
  echo "Exposing server to local network: No (localhost only)"
fi
echo "=================================================="

if [ "$DAEMON" = true ]; then
  echo "Starting servers in the background (persistent)..."
  if [ -n "$HOST" ]; then
    nohup npm run dev -- --storage-dir "$STORAGE_DIR" --host < /dev/null > "$LOG_FILE" 2>&1 &
  else
    nohup npm run dev -- --storage-dir "$STORAGE_DIR" < /dev/null > "$LOG_FILE" 2>&1 &
  fi
  DAEMON_PID=$!
  disown $DAEMON_PID
  
  sleep 2
  PID_JS=$(pgrep -f "scripts/start-server.js" || true)
  if [ -n "$PID_JS" ]; then
    echo "Servers started successfully in background!"
    echo "Logs are written to: $LOG_FILE"
    echo "You can check status using: ./start.sh --status"
    echo "You can tail logs using: ./start.sh --logs"
    echo "You can stop servers using: ./start.sh --stop"
  else
    echo "Failed to start servers in background. Check $LOG_FILE for details."
  fi
else
  echo "Starting servers in foreground (session dependent)..."
  echo "Press Ctrl+C to stop the servers."
  echo "--------------------------------------------------"
  if [ -n "$HOST" ]; then
    exec npm run dev -- --storage-dir "$STORAGE_DIR" --host
  else
    exec npm run dev -- --storage-dir "$STORAGE_DIR"
  fi
fi
